"""Risk, fees and execution coverage use one current custody snapshot (#2543)."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.clerk.live_envelope import LIVE_ENVELOPE_UNOBSERVED, AccountObservation, LiveEnvelopeGate
from app.broker.alpaca.clerk.sqlite.day_pnl import DayPnl, day_pnl_at
from app.broker.alpaca.clerk.sqlite.economic_projection import SqliteEconomicProjectionReader
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
from tests.broker.alpaca.clerk.sqlite.test_fee_evidence import _activity
from tests.broker.alpaca.clerk.sqlite.test_live_envelope_sync import _Read


def _observation(now_ms: int = NOON) -> AccountObservation:
    return AccountObservation(now_ms, 100_000, 100_000, 100_000, 0, 0)


def _pnl(repo: ClerkSqliteRepository, *, retained_start_ms: int | None = None) -> DayPnl:
    reader = SqliteEconomicProjectionReader.from_repository(repo)
    try:
        return day_pnl_at(reader, repo, observation=_observation(repo.clock()), now_ms=repo.clock(), retained_start_ms=retained_start_ms)
    finally:
        reader.close()


def test_missing_and_stale_fee_evidence_never_become_zero(tmp_path: Path) -> None:
    clock = _TestClock(NOON)
    repo = ClerkSqliteRepository.initialize(account_id="PA-unknown-fees", artifacts_root=tmp_path, clock=clock)
    try:
        unknown = _pnl(repo)
        assert not unknown.known and unknown.fee_usd is None and unknown.total_usd is None
        complete_fee_evidence(repo)
        assert _pnl(repo).total_usd == 0
        clock.advance(FEE_EVIDENCE_MAX_AGE_MS + 1)
        assert not _pnl(repo).known and _pnl(repo).total_usd is None
    finally:
        repo.close()


async def test_estimated_fee_is_part_of_apply_loss_threshold(day_pnl_repo: ClerkSqliteRepository) -> None:
    repo = day_pnl_repo
    complete_fee_evidence(repo)
    accepted = _accept_day_pnl_enter(repo, decision_id="fee-provision-loss")
    _append_day_pnl_slice(repo, accepted, execution_id="large-buy", side="BUY", quantity=100_000, price=1, occurred_at_ms=NOON)
    sync = LiveEnvelopeSync(repo=repo, read=_Read(unrealized=-99.8), envelope=LiveEnvelopeGate(values=None, custody_is_simulated=False))
    try:
        await sync.observe()
        sync.apply_risk_policy(_policy(), expected_revision=0)
        assert _hold(repo) is not None
        reading = await sync.observe()
        # Independent fixture: CAT 100000 * .000003 = .30; gross loss 99.80 + .30.
        assert reading.day_pnl.fee_fidelity == "estimated"
        assert reading.day_pnl.total_usd == pytest.approx(-100.10, abs=1e-9, rel=0)
        assert reading.breached
    finally:
        await sync.stop()


async def test_new_unknown_fee_evidence_blocks_enter_after_a_healthy_tick(day_pnl_repo: ClerkSqliteRepository) -> None:
    repo = day_pnl_repo
    complete_fee_evidence(repo)
    sync = LiveEnvelopeSync(repo=repo, read=_Read(), envelope=LiveEnvelopeGate(values=None, custody_is_simulated=False))
    try:
        await sync.observe()
        sync.apply_risk_policy(_policy(), expected_revision=0)
        assert sync.envelope.latest_observation() is not None
        undated = _activity("unresolved-fee", "FEE", NOON, -.05).model_copy(update={"occurred_at_ms": None})
        complete_fee_evidence(repo, (undated,))
        before = len(repo.custody_transitions())
        with pytest.raises(AdmissionBlockedError) as refused:
            accept_enter(repo, account_id=repo.account_id, strategy_instance_id=DAY_PNL_SID,
                decision_id="after-unknown-fee", lifecycle_run_id=DAY_PNL_RUN_ID,
                leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1), reference_price=100, envelope=sync.envelope)
        assert refused.value.decision.reason_code == LIVE_ENVELOPE_UNOBSERVED
        assert sync.envelope.latest_observation() is None
        assert len(repo.custody_transitions()) == before
        assert _hold(repo) is None
    finally:
        await sync.stop()


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
        read.unrealized = 0
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
    assert pnl.execution_coverage == "incomplete"
    assert not pnl.known and pnl.total_usd is None


def test_late_observed_fee_replaces_provision_in_retained_hold_window(
    day_pnl_repo: ClerkSqliteRepository, day_pnl_clock: _TestClock, seeded_round_trip_without_fees: None,
) -> None:
    repo = day_pnl_repo
    complete_fee_evidence(repo)
    start = _pnl(repo).day_start_ms
    assert _pnl(repo).fee_usd == pytest.approx(.05, abs=1e-9, rel=0)
    day_pnl_clock.advance(86_400_000)
    repo.revive_execution_lease()
    complete_fee_evidence(repo, (_activity("settled", "FEE", NOON, -.10),))
    retained = _pnl(repo, retained_start_ms=start)
    assert retained.known and retained.fee_fidelity == "observed"
    assert retained.fee_usd == pytest.approx(.10, abs=1e-9, rel=0)
    assert retained.total_usd == pytest.approx(99.90, abs=1e-9, rel=0)
    assert _pnl(repo).total_usd == 0


def test_fee_at_midnight_is_included_at_the_inclusive_risk_boundary(tmp_path: Path) -> None:
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
        assert _pnl(repo).fee_usd == pytest.approx(.01, abs=1e-9, rel=0)
        assert _pnl(repo).total_usd == pytest.approx(-.01, abs=1e-9, rel=0)
    finally:
        repo.close()


def test_overflowed_total_is_unknown(day_pnl_repo: ClerkSqliteRepository) -> None:
    from dataclasses import replace

    complete_fee_evidence(day_pnl_repo)
    overflowed = replace(_pnl(day_pnl_repo), realized_usd=1.7e308, unrealized_usd=1.7e308)
    assert not overflowed.known and overflowed.total_usd is None


def test_read_only_risk_judgement_never_withdraws_or_raises_a_hold(day_pnl_repo: ClerkSqliteRepository) -> None:
    from dataclasses import replace

    from app.broker.alpaca.clerk.sqlite.account_risk import append_risk_policy

    complete_fee_evidence(day_pnl_repo)
    append_risk_policy(day_pnl_repo, policy=_policy(), expected_revision=0)
    gate = LiveEnvelopeGate(values=None, custody_is_simulated=False)
    observation = replace(_observation(), risk_revision=1, unrealized_pl_usd=-101)
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
