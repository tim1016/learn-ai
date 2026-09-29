"""Risk, fees and execution coverage use one current custody snapshot (#2543)."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.clerk.live_envelope import LIVE_ENVELOPE_UNOBSERVED, AccountObservation, LiveEnvelopeGate
from app.broker.alpaca.clerk.sqlite.day_pnl import (
    DayPnl,
    day_pnl_window_start_ms,
    observed_day_pnl,
    risk_evidence_ready,
)
from app.broker.alpaca.clerk.sqlite.enter import accept_enter
from app.broker.alpaca.clerk.sqlite.fee_evidence import FEE_EVIDENCE_MAX_AGE_MS
from app.broker.alpaca.clerk.sqlite.live_envelope_sync import LiveEnvelopeSync
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.risk_admission import current_risk_readiness, require_current_risk_admission
from app.broker.alpaca.clerk.sqlite.uncertainty import AdmissionBlockedError
from app.broker.contract.models import BrokerOrderLeg
from tests.broker.alpaca.clerk.sqlite.conftest import (
    DAY_PNL_RUN_ID,
    DAY_PNL_SID,
    NOON,
    _accept_day_pnl_enter,
    _append_day_pnl_slice,
    _broker_order_fixture,
    _TestClock,
    complete_fee_evidence,
)
from tests.broker.alpaca.clerk.sqlite.test_account_risk_policy import _hold, _policy
from tests.broker.alpaca.clerk.sqlite.test_budget_commands import _deploy, _new_budget_repo
from tests.broker.alpaca.clerk.sqlite.test_fee_evidence import _activity
from tests.broker.alpaca.clerk.sqlite.test_live_envelope_sync import _Read


def _observation(now_ms: int = NOON) -> AccountObservation:
    return AccountObservation(observed_at_ms=now_ms, broker_cash_usd=100_000, cash_available_usd=100_000,
        equity_usd=100_000, last_equity_usd=100_000, position_count=0,
        risk_cash_flow_evidence_complete=True, risk_cash_flow_window_start_ms=0,
        risk_equity_window_start_ms=day_pnl_window_start_ms(now_ms))


def _pnl(repo: ClerkSqliteRepository, *, retained_start_ms: int | None = None) -> DayPnl:
    return observed_day_pnl(observation=_observation(repo.clock()), now_ms=repo.clock(), retained_start_ms=retained_start_ms)


def test_missing_and_stale_fee_evidence_never_become_zero(tmp_path: Path) -> None:
    clock = _TestClock(NOON)
    repo = ClerkSqliteRepository.initialize(account_id="PA-unknown-fees", artifacts_root=tmp_path, clock=clock)
    try:
        unknown = _pnl(repo)
        assert unknown.known and unknown.total_usd == 0
        assert not risk_evidence_ready(repo, now_ms=clock())
        complete_fee_evidence(repo)
        assert risk_evidence_ready(repo, now_ms=clock())
        clock.advance(FEE_EVIDENCE_MAX_AGE_MS + 1)
        assert not risk_evidence_ready(repo, now_ms=clock())
    finally:
        repo.close()


async def test_equity_including_a_fee_is_not_debited_by_its_provision_again(day_pnl_repo: ClerkSqliteRepository) -> None:
    repo = day_pnl_repo
    complete_fee_evidence(repo)
    accepted = _accept_day_pnl_enter(repo, decision_id="fee-provision-loss")
    _append_day_pnl_slice(repo, accepted, execution_id="large-buy", side="BUY", quantity=100_000, price=1, occurred_at_ms=NOON)
    sync = LiveEnvelopeSync(repo=repo, read=_Read(equity=99_899.90), envelope=LiveEnvelopeGate(values=None, custody_is_simulated=False))
    try:
        await sync.observe()
        sync.apply_risk_policy(_policy(), expected_revision=0)
        assert _hold(repo) is not None
        reading = await sync.observe()
        # Independent fixture: CAT 100000 * .000003 = .30; gross loss 99.80 + .30.
        fees = repo.fee_attribution(now_ms=NOON)
        assert fees.known and {share.state for share in fees.shares} == {"estimated"}
        assert reading.day_pnl.total_usd == pytest.approx(-100.10, abs=1e-9, rel=0)
        assert reading.breached
    finally:
        await sync.stop()


async def test_new_unknown_fee_evidence_blocks_enter_after_a_healthy_tick(tmp_path: Path) -> None:
    """A budgeted account (#2553: only one admits an ENTER) whose healthy reading an unknown fee withdraws."""
    repo = _new_budget_repo(tmp_path)
    _deploy(repo)
    sync = LiveEnvelopeSync(repo=repo, read=_Read(), envelope=LiveEnvelopeGate(values=None, custody_is_simulated=False))
    try:
        await sync.observe()
        assert current_risk_readiness(repo, envelope=sync.envelope, now_ms=repo.clock()).allowed
        undated = _activity("unresolved-fee", "FEE", NOON, -.05).model_copy(update={"occurred_at_ms": None})
        complete_fee_evidence(repo, (undated,))
        before = len(repo.custody_transitions())
        with pytest.raises(AdmissionBlockedError) as refused:
            accept_enter(repo, account_id=repo.account_id, strategy_instance_id="a",
                decision_id="after-unknown-fee", lifecycle_run_id="run-a",
                leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1), reference_price=100, envelope=sync.envelope)
        assert refused.value.decision.reason_code == LIVE_ENVELOPE_UNOBSERVED
        assert sync.envelope.latest_observation() is None
        assert len(repo.custody_transitions()) == before
        assert _hold(repo) is None
    finally:
        await sync.stop()
        repo.close()


@pytest.mark.parametrize("stale_unrealized", [0, 200])
async def test_new_fill_loss_after_a_healthy_tick_is_fenced_and_durable(day_pnl_repo: ClerkSqliteRepository, stale_unrealized: float) -> None:
    repo = day_pnl_repo
    complete_fee_evidence(repo)
    accepted = _accept_day_pnl_enter(repo, decision_id="later-loss")
    read = _Read(unrealized=stale_unrealized)
    sync = LiveEnvelopeSync(repo=repo, read=read, envelope=LiveEnvelopeGate(values=None, custody_is_simulated=False))
    try:
        await sync.observe()
        sync.apply_risk_policy(_policy(), expected_revision=0)
        for identity, side, price in (("buy", "BUY", 100), ("sell", "SELL", 80)):
            _append_day_pnl_slice(repo, accepted, execution_id=identity, side=side, quantity=10, price=price, occurred_at_ms=NOON, fee=0, fee_fidelity="reported")
        with pytest.raises(AdmissionBlockedError) as refused:
            require_current_risk_admission(repo, envelope=sync.envelope, now_ms=NOON)
        assert refused.value.decision.reason_code == LIVE_ENVELOPE_UNOBSERVED
        assert _hold(repo) is None
        read.equity = 99_800
        assert await sync.tick() == "hold_raised"
        assert _hold(repo) is not None
        assert repo.position(DAY_PNL_SID, "SPY") == 0
    finally:
        await sync.stop()


def test_incomplete_execution_population_blocks_risk_with_reported_zero_fees(day_pnl_repo: ClerkSqliteRepository) -> None:
    repo = day_pnl_repo
    complete_fee_evidence(repo)
    accepted = _accept_day_pnl_enter(repo, decision_id="missing-exact-executions")
    fold_order_evidence(repo, effect_operation_id=accepted.effect_operation_id,
        order=_broker_order_fixture(accepted.order_ref, status="filled", filled_quantity=10, quantity=10, filled_avg_price=100))
    pnl = _pnl(repo)
    assert pnl.known and pnl.total_usd == 0  # broker equity remains the account fact
    assert not risk_evidence_ready(repo, now_ms=repo.clock())


def test_late_observed_fee_replaces_provision_without_double_debiting_account_equity(
    day_pnl_repo: ClerkSqliteRepository, day_pnl_clock: _TestClock,
) -> None:
    from dataclasses import replace

    repo = day_pnl_repo
    complete_fee_evidence(repo)
    accepted = _accept_day_pnl_enter(repo, decision_id="fee-settlement")
    _append_day_pnl_slice(repo, accepted, execution_id="fee-buy", side="BUY", quantity=10, price=100, occurred_at_ms=NOON)
    before = repo.fee_attribution(now_ms=NOON)
    assert float(sum(share.amount for share in before.shares)) == pytest.approx(.01, abs=1e-9, rel=0)
    day_pnl_clock.advance(86_400_000)
    repo.revive_execution_lease()
    complete_fee_evidence(repo, (_activity("settled", "FEE", NOON, -.10),))
    after = repo.fee_attribution(now_ms=repo.clock())
    assert after.known and float(sum(share.amount for share in after.shares)) == pytest.approx(.10, abs=1e-9, rel=0)
    observation = replace(_observation(repo.clock()), equity_usd=99_999.90)
    retained = observed_day_pnl(observation=observation, now_ms=repo.clock(), retained_start_ms=day_pnl_window_start_ms(NOON), retained_equity_usd=100_000)
    assert retained.known and retained.total_usd == pytest.approx(-.10, abs=1e-9, rel=0)


def test_fee_at_midnight_stays_in_canonical_fee_evidence(tmp_path: Path) -> None:
    from app.broker.alpaca.clerk.et_day import et_day_window_ms
    from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
    from tests.broker.alpaca.clerk.sqlite.conftest import DAY_PNL_ACCOUNT_ID

    midnight, _ = et_day_window_ms(NOON)
    clock = _TestClock(midnight)
    repo = ClerkSqliteRepository.initialize(account_id=DAY_PNL_ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    try:
        repo.register_strategy_instance(strategy_instance_id=DAY_PNL_SID, symbol="SPY", config_hash="risk-midnight")
        submit_start_run(repo, account_id=repo.account_id, strategy_instance_id=DAY_PNL_SID, lifecycle_run_id=DAY_PNL_RUN_ID, clock=clock)
        complete_fee_evidence(repo)
        accepted = _accept_day_pnl_enter(repo, decision_id="midnight-fee")
        _append_day_pnl_slice(repo, accepted, execution_id="midnight-buy", side="BUY", quantity=10, price=100, occurred_at_ms=midnight)
        fees = repo.fee_attribution(now_ms=clock())
        assert fees.known and float(sum(share.amount for share in fees.shares)) == pytest.approx(.01, abs=1e-9, rel=0)
    finally:
        repo.close()


def test_overflowed_total_is_unknown(day_pnl_repo: ClerkSqliteRepository) -> None:
    from dataclasses import replace

    complete_fee_evidence(day_pnl_repo)
    overflowed = replace(_pnl(day_pnl_repo), current_equity_usd=1.7e308, prior_close_equity_usd=-1.7e308)
    assert not overflowed.known


def test_read_only_risk_judgement_never_withdraws_or_raises_a_hold(day_pnl_repo: ClerkSqliteRepository) -> None:
    from dataclasses import replace

    from app.broker.alpaca.clerk.sqlite.account_risk import append_risk_policy

    complete_fee_evidence(day_pnl_repo)
    append_risk_policy(day_pnl_repo, policy=_policy(), expected_revision=0)
    gate = LiveEnvelopeGate(values=None, custody_is_simulated=False)
    observation = replace(_observation(), risk_revision=1, equity_usd=99_899)
    gate.publish(observation)
    before = day_pnl_repo.custody_transitions()
    for _ in range(2):
        decision = current_risk_readiness(day_pnl_repo, envelope=gate, now_ms=NOON)
        assert not decision.allowed and decision.breach_cause is not None
    assert day_pnl_repo.custody_transitions() == before
    assert gate.latest_observation() is observation
    assert _hold(day_pnl_repo) is None
    with pytest.raises(AdmissionBlockedError):
        require_current_risk_admission(day_pnl_repo, envelope=gate, now_ms=NOON)
    assert _hold(day_pnl_repo) is not None
    assert gate.latest_observation() is None


def test_real_equity_snapshot_cannot_cross_a_prior_close_horizon(day_pnl_repo: ClerkSqliteRepository) -> None:
    from dataclasses import replace

    observation = _observation()
    next_day = NOON + 86_400_000
    assert observed_day_pnl(observation=observation, now_ms=NOON).known
    assert not observed_day_pnl(observation=observation, now_ms=next_day).known
    fresh = replace(observation, risk_equity_window_start_ms=day_pnl_window_start_ms(next_day))
    assert observed_day_pnl(observation=fresh, now_ms=next_day).known
