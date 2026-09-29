"""Shared simulated cash/risk is custody-owned, exact and isolated (#2546)."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.broker.alpaca.clerk.account_authority import shadow_evidence_account_id_for_strategy
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeGate
from app.broker.alpaca.clerk.money import display_cents, dollars
from app.broker.alpaca.clerk.sealed_ledger import canonical_sha256
from app.broker.alpaca.clerk.sqlite.account_risk import AccountRiskPolicy, append_risk_policy
from app.broker.alpaca.clerk.sqlite.budget_authority import commit_budget_authority_cutover
from app.broker.alpaca.clerk.sqlite.budget_commands import submit_budgeted_deploy
from app.broker.alpaca.clerk.sqlite.budget_projection import BudgetUnavailable
from app.broker.alpaca.clerk.sqlite.day_pnl import observed_day_pnl
from app.broker.alpaca.clerk.sqlite.enter import EnterSubmission, accept_enter
from app.broker.alpaca.clerk.sqlite.live_envelope_sync import LiveEnvelopeSync
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.risk_admission import require_current_risk_admission
from app.broker.alpaca.clerk.sqlite.simulated_account import (
    SimulatedAccountProjection,
    SimulationBaseline,
    SimulationEvidenceUnavailable,
    private_dry_run_cash,
)
from app.broker.contract.models import BrokerOrderLeg
from app.lean_sidecar.trading_calendar import previous_completed_session_close_ms
from app.marketdata.feed import MarketDataBar
from app.services.broker_v2_panel.budget_deploy import _money_view
from app.services.source_bar_ledger import SourceBarLedger
from app.utils.session_anchors import MAX_TIMESTAMP_MS
from tests.broker.alpaca.clerk.sqlite.conftest import DAY_PNL_SID, NOON, _append_day_pnl_slice, _TestClock
from tests.broker.alpaca.clerk.sqlite.test_account_risk_policy import _hold
from tests.broker.alpaca.clerk.sqlite.test_budget_commands import TERMS
from tests.broker.alpaca.clerk.sqlite.test_live_envelope_sync import _Read

type ShadowContext = tuple[ClerkSqliteRepository, SimulatedAccountProjection, _TestClock]


@pytest.mark.parametrize("field", ["session_start_ms", "observed_at_ms", "mark_cutoff_ms"])
def test_simulation_baseline_rejects_out_of_domain_timestamps(field: str) -> None:
    values = dict(session_start_ms=NOON, observed_at_ms=NOON, mark_cutoff_ms=NOON,
                  initial_capital_usd="1000", equity_usd="1000")
    with pytest.raises(ValidationError, match=field):
        SimulationBaseline(**{**values, field: MAX_TIMESTAMP_MS + 1})


@pytest.fixture
def shadow(tmp_path: Path) -> Iterator[ShadowContext]:
    clock = _TestClock(NOON)
    repo = ClerkSqliteRepository.initialize(account_id="shadow:LIVE", artifacts_root=tmp_path, clock=clock)
    append_risk_policy(repo, policy=AccountRiskPolicy(1, .1, 100, "profile", 1, "owner", NOON), expected_revision=0)
    projection = SimulatedAccountProjection(repo=repo, artifacts_root=tmp_path)
    projection.observe(reference_cash=1000, observed_at_ms=clock(), now_ms=clock())
    try:
        yield repo, projection, clock
    finally:
        repo.close()


def test_establish_session_baseline_persists_once_and_is_idempotent(tmp_path: Path) -> None:
    """#2550: shadow activation calls this narrow entry point instead of
    discarding observe()'s AccountObservation return value for its baseline
    side effect. A second call for the same session must not append a
    second SIMULATION_SESSION_BASELINE transition."""
    clock = _TestClock(NOON)
    repo = ClerkSqliteRepository.initialize(account_id="shadow:LIVE", artifacts_root=tmp_path, clock=clock)
    try:
        projection = SimulatedAccountProjection(repo=repo, artifacts_root=tmp_path)
        first = projection.establish_session_baseline(reference_cash=1000, now_ms=clock())
        assert first.initial_capital_usd == Decimal(1000)
        assert sum(row["transition_kind"] == "SIMULATION_SESSION_BASELINE" for row in repo.custody_transitions()) == 1
        second = projection.establish_session_baseline(reference_cash=1000, now_ms=clock())
        assert second == first
        assert sum(row["transition_kind"] == "SIMULATION_SESSION_BASELINE" for row in repo.custody_transitions()) == 1
    finally:
        repo.close()


def _deploy(repo: ClerkSqliteRepository, projection: SimulatedAccountProjection, *, sid: str = DAY_PNL_SID, cents: int = 100_000, reference: int | Decimal = 1000) -> LiveEnvelopeGate:
    if repo.budget_authority_version() < 2:
        commit_budget_authority_cutover(repo, actor="owner", reviewed_token="fresh-simulation", stop_receipt="no-old-runs")
    repo.register_strategy_instance(strategy_instance_id=sid, symbol="SPY", config_hash=f"seal-{sid}", exit_terms=TERMS)
    policy = repo.account_risk_policy()
    observation = projection.observe(reference_cash=reference, observed_at_ms=repo.clock(), now_ms=repo.clock())
    gate = LiveEnvelopeGate(values=None, custody_is_simulated=True)
    gate.publish(replace(observation, risk_revision=None if policy is None else policy.revision))
    submit_budgeted_deploy(repo, strategy_instance_id=sid, lifecycle_run_id=f"run-{sid}", world="synthetic" if repo.account_id.startswith("sim:") else "shadow",
        committed_cents=cents, configuration_hash=f"seal-{sid}", exit_terms_hash=canonical_sha256(TERMS.model_dump(mode="json")),
        risk_revision=0 if policy is None else policy.revision, actor="owner", envelope=gate, minimum_position_cost=Decimal("100.01"))
    return gate


def _enter(repo: ClerkSqliteRepository, gate: LiveEnvelopeGate, quantity: int = 2) -> EnterSubmission:
    return accept_enter(repo, account_id=repo.account_id, strategy_instance_id=DAY_PNL_SID, decision_id="one-buy",
        lifecycle_run_id=f"run-{DAY_PNL_SID}", leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=quantity),
        reference_price=100, envelope=gate)


def _fill(repo: ClerkSqliteRepository, accepted: EnterSubmission, *, key: str, side: str, quantity: float, price: float) -> None:
    _append_day_pnl_slice(repo, accepted, execution_id=key, side=side, quantity=quantity, price=price, occurred_at_ms=repo.clock())


def _mark(root: Path, account: str, *, at_ms: int, price: int | Decimal, provider: str = "ibkr") -> None:
    bars = SourceBarLedger(artifacts_root=root, account_id=account)
    try:
        bars.append(MarketDataBar(feed_id=provider, symbol="SPY", start_ms=at_ms-60_000, end_ms=at_ms,
            open=Decimal(price), high=Decimal(price), low=Decimal(price), close=Decimal(price), volume=100,
            fetched_at_ms=at_ms, session_phase="RTH"), run_id="marks")
    finally:
        bars.close()


def test_shadow_cash_and_baseline_are_own_economics_not_reference_changes(shadow: ShadowContext, tmp_path: Path) -> None:
    repo, projection, clock = shadow
    gate = _deploy(repo, projection)
    accepted = _enter(repo, gate)
    _fill(repo, accepted, key="buy", side="BUY", quantity=2, price=100)
    _fill(repo, accepted, key="sell", side="SELL", quantity=1, price=120)
    evidence = shadow_evidence_account_id_for_strategy(DAY_PNL_SID)
    _mark(tmp_path, evidence, at_ms=clock(), price=130)
    current = projection.observe(reference_cash=1200, observed_at_ms=clock(), now_ms=clock())
    assert current.cash_available_usd == Decimal("1120")
    assert current.last_equity_usd == 1000  # reference deposit never resets risk
    assert current.equity_usd == Decimal("1049.97")
    assert observed_day_pnl(observation=current, now_ms=clock()).total_usd == pytest.approx(49.97, abs=1e-9, rel=0)
    assert current.unrealized_pl_usd == pytest.approx(30, abs=1e-9, rel=0)
    assert current.fills_seen_before_ms == clock() + 1
    assert repo.reserved_cash_decimal(seen_before_ms=current.fills_seen_before_ms) == 0
    assert current.modelled_fees_seen_before_ms == clock() + 1
    close = previous_completed_session_close_ms(NOON + 86_400_000)
    _mark(tmp_path, evidence, at_ms=close, price=130)
    clock.advance(86_400_000)
    repo.revive_execution_lease()
    _mark(tmp_path, evidence, at_ms=clock(), price=131)
    tomorrow = projection.observe(reference_cash=1500, observed_at_ms=clock(), now_ms=clock())
    # SEC ceil(120*.0000206)=.01; TAF ceil(1*.000195)=.01; CAT ceil(3*.000003)=.01.
    assert tomorrow.cash_available_usd == Decimal("1419.97")
    assert tomorrow.last_equity_usd == pytest.approx(1049.97, abs=1e-9, rel=0)
    assert tomorrow.equity_usd == Decimal("1050.97")
    assert observed_day_pnl(observation=tomorrow, now_ms=clock()).total_usd == pytest.approx(1, abs=1e-9, rel=0)


@pytest.mark.parametrize("entry_at_close", [False, True])
def test_prior_close_baseline_uses_matching_fill_mark_and_fee_cutoffs(
    shadow: ShadowContext, tmp_path: Path, entry_at_close: bool,
) -> None:
    repo, projection, clock = shadow
    accepted = _enter(repo, _deploy(repo, projection), quantity=1)
    close = previous_completed_session_close_ms(NOON + 86_400_000)
    clock.advance(close - clock() + (0 if entry_at_close else 1))
    repo.revive_execution_lease()
    _fill(repo, accepted, key="boundary-buy", side="BUY", quantity=1, price=100)
    evidence = shadow_evidence_account_id_for_strategy(DAY_PNL_SID)
    if entry_at_close:
        _mark(tmp_path, evidence, at_ms=close, price=110)
    # The after-close case intentionally has no prior-close price: that lot
    # did not exist at the baseline instant and must not require a fake mark.
    clock.advance(NOON + 86_400_000 - clock())
    repo.revive_execution_lease()
    _mark(tmp_path, evidence, at_ms=clock(), price=120)
    next_day = projection.observe(reference_cash=1000, observed_at_ms=clock(), now_ms=clock())
    assert next_day.cash_available_usd == Decimal("899.99")
    assert next_day.equity_usd == Decimal("1019.99")
    expected_baseline = 1009.99 if entry_at_close else 1000
    expected_pnl = 10 if entry_at_close else 19.99
    assert next_day.last_equity_usd == pytest.approx(expected_baseline, abs=1e-9, rel=0)
    assert observed_day_pnl(observation=next_day, now_ms=clock()).total_usd == pytest.approx(expected_pnl, abs=1e-9, rel=0)


def test_after_close_sale_keeps_realized_change_and_fee_out_of_baseline(
    shadow: ShadowContext, tmp_path: Path,
) -> None:
    repo, projection, clock = shadow
    accepted = _enter(repo, _deploy(repo, projection), quantity=1)
    _fill(repo, accepted, key="before-close-buy", side="BUY", quantity=1, price=100)
    evidence = shadow_evidence_account_id_for_strategy(DAY_PNL_SID)
    close = previous_completed_session_close_ms(NOON + 86_400_000)
    _mark(tmp_path, evidence, at_ms=close, price=110)
    clock.advance(close + 1 - clock())
    repo.revive_execution_lease()
    _fill(repo, accepted, key="after-close-sale", side="SELL", quantity=1, price=120)
    clock.advance(NOON + 86_400_000 - clock())
    repo.revive_execution_lease()
    next_day = projection.observe(reference_cash=1000, observed_at_ms=clock(), now_ms=clock())
    # Close: open gain $10 less the buy's $.01 CAT accrual. Next day: realized
    # gain $20 less $.03 total fees. Cash and equity each include fees once.
    assert next_day.cash_available_usd == Decimal("1019.97")
    assert next_day.last_equity_usd == pytest.approx(1009.99, abs=1e-9, rel=0)
    assert next_day.equity_usd == Decimal("1019.97")
    assert observed_day_pnl(observation=next_day, now_ms=clock()).total_usd == pytest.approx(9.98, abs=1e-9, rel=0)


@pytest.mark.parametrize("world", ["dry_run", "shadow"])
def test_simulated_equity_and_open_pnl_are_exact_fifo_at_a_whole_cent_boundary(tmp_path: Path, world: str) -> None:
    """#2556: equity and open P&L read FIFO's exact fields, never its float views.

    0.048360857 shares bought at $100 and marked at $100.3101682007 gain
    exactly $0.0149999999999999999 -- 18 significant digits, more than a
    binary float keeps. Its float view is 0.015, which half-even rounds to
    2 cents; equity built from that view ($1000.01 starting cash + 0.015 -
    the buy's $0.01 modelled fee) is 1000.015, which rounds to $1000.02.
    The exact values show 1 cent and $1000.01 -- on the money view Deploy
    and Home draw (equity and the open P&L note), not only in the retained
    baseline. The oracle is an independent ``Fraction``.
    """
    clock = _TestClock(NOON)
    cash = Decimal("1000.01")
    if world == "dry_run":
        repo = ClerkSqliteRepository.initialize(account_id=f"sim:{DAY_PNL_SID}", artifacts_root=tmp_path, clock=clock)
        projection = SimulatedAccountProjection(repo=repo, artifacts_root=tmp_path, initial_cash=cash)
        evidence, provider = repo.account_id, "fixture"
    else:
        repo = ClerkSqliteRepository.initialize(account_id="shadow:LIVE", artifacts_root=tmp_path, clock=clock)
        append_risk_policy(repo, policy=AccountRiskPolicy(1, .1, 100, "profile", 1, "owner", NOON), expected_revision=0)
        projection = SimulatedAccountProjection(repo=repo, artifacts_root=tmp_path)
        projection.observe(reference_cash=cash, observed_at_ms=clock(), now_ms=clock())
        evidence, provider = shadow_evidence_account_id_for_strategy(DAY_PNL_SID), "ibkr"
    try:
        accepted = _enter(repo, _deploy(repo, projection, cents=100_001, reference=cash), quantity=1)
        _fill(repo, accepted, key="boundary-buy", side="BUY", quantity=0.048360857, price=100)
        mark = Decimal("100.3101682007")
        _mark(tmp_path, evidence, at_ms=previous_completed_session_close_ms(NOON + 86_400_000), price=mark, provider=provider)
        clock.advance(86_400_000)
        repo.revive_execution_lease()
        _mark(tmp_path, evidence, at_ms=clock(), price=mark, provider=provider)
        observation = projection.observe(reference_cash=cash, observed_at_ms=clock(), now_ms=clock())
        retained = projection.establish_session_baseline(reference_cash=cash, now_ms=clock())
        with repo.write_fence():
            shown = _money_view(repo, observation, world="synthetic" if world == "dry_run" else "shadow", account_id="LIVE")
    finally:
        repo.close()
    f = Fraction
    open_pnl = f("0.048360857") * (f("100.3101682007") - 100)
    assert open_pnl == f("0.0149999999999999999")
    equity = f("1000.01") + open_pnl - f("0.01")
    # What the owner reads: the money view's equity and open P&L note.
    assert (shown.equity_usd, shown.open_pnl_usd) == ("1000.01", "0.01")
    assert shown.today_pnl_usd == "0.00"  # marked at the same price at the prior close
    assert f(observation.equity_usd) == equity
    assert f(observation.unrealized_pl_usd) == open_pnl
    # The retained baseline behind today's P&L is exact too.
    assert f(retained.equity_usd) == equity
    assert dollars(display_cents(retained.equity_usd)) == "1000.01"


def test_simulated_open_pnl_values_the_retained_close_itself(tmp_path: Path) -> None:
    """#2556: the mark is the retained bar close, never a float of it.

    The close $100.014999999999999999999 (24 significant digits, retained
    exactly) values one share bought at $100 at exactly 1.4999...9 cents,
    shown as $0.01. Hopped through a float, the close becomes 100.015 and
    the same share shows $0.02.
    """
    clock = _TestClock(NOON)
    repo = ClerkSqliteRepository.initialize(account_id=f"sim:{DAY_PNL_SID}", artifacts_root=tmp_path, clock=clock)
    projection = SimulatedAccountProjection(repo=repo, artifacts_root=tmp_path, initial_cash=Decimal(1000))
    try:
        accepted = _enter(repo, _deploy(repo, projection), quantity=1)
        _fill(repo, accepted, key="one-share", side="BUY", quantity=1, price=100)
        close = Decimal("100.014999999999999999999")
        _mark(tmp_path, repo.account_id, at_ms=clock(), price=close, provider="fixture")
        observation = projection.observe(reference_cash=1000, observed_at_ms=clock(), now_ms=clock())
    finally:
        repo.close()
    assert observation.unrealized_pl_usd == close - 100
    assert dollars(display_cents(observation.unrealized_pl_usd)) == "0.01"


def test_future_simulated_fill_cannot_enter_a_current_valuation(shadow: ShadowContext) -> None:
    repo, projection, clock = shadow
    accepted = _enter(repo, _deploy(repo, projection))
    _append_day_pnl_slice(repo, accepted, execution_id="future", side="BUY", quantity=2,
                         price=100, occurred_at_ms=clock() + 1)
    with pytest.raises(SimulationEvidenceUnavailable, match="ahead"):
        projection.observe(reference_cash=1000, observed_at_ms=clock(), now_ms=clock())


async def test_real_unrealized_is_never_shadow_profit_or_loss(shadow: ShadowContext, tmp_path: Path) -> None:
    repo, projection, clock = shadow
    accepted = _enter(repo, _deploy(repo, projection))
    _fill(repo, accepted, key="buy", side="BUY", quantity=2, price=100)
    _mark(tmp_path, shadow_evidence_account_id_for_strategy(DAY_PNL_SID), at_ms=clock(), price=40)
    read = _Read(cash=1000, unrealized=9999, last_equity=2_000_000)
    sync = LiveEnvelopeSync(repo=repo, read=read, envelope=LiveEnvelopeGate(values=None, custody_is_simulated=True), simulation=projection)
    try:
        assert await sync.tick() == "hold_raised"
        assert _hold(repo) is not None
        reading = await sync.observe()
        assert reading.observation.unrealized_pl_usd == pytest.approx(-120, abs=1e-9, rel=0)
        assert reading.observation.last_equity_usd == 1000
        assert reading.breached
    finally:
        await sync.stop()


def test_two_shadow_deployments_share_one_pool(shadow: ShadowContext) -> None:
    repo, projection, _ = shadow
    _deploy(repo, projection, sid="one", cents=60_000)
    with pytest.raises(BudgetUnavailable, match="unreserved"):
        _deploy(repo, projection, sid="two", cents=60_000)
    assert repo.deployment_budget("two") is None


def test_private_starting_cash_is_exact_and_independent(tmp_path: Path) -> None:
    for sid, cash in (("dry-one", Decimal("1000.01")), ("dry-two", Decimal("2000.02"))):
        repo = ClerkSqliteRepository.initialize(account_id=f"sim:{sid}", artifacts_root=tmp_path, clock=_TestClock(NOON))
        try:
            projection = SimulatedAccountProjection(repo=repo, artifacts_root=tmp_path, initial_cash=cash)
            observation = projection.observe(reference_cash=9_000_000, observed_at_ms=NOON, now_ms=NOON)
            assert observation.cash_available_usd == cash
            assert not any(row["transition_kind"] == "SIMULATION_SESSION_BASELINE" for row in repo.custody_transitions())
            gate = _deploy(repo, projection, sid=sid, cents=int(cash*100))
            assert require_current_risk_admission(repo, envelope=gate, now_ms=NOON).cash_available_usd == cash
            # Durable consent, not a changed process-local seed, wins on reopen.
            restarted = SimulatedAccountProjection(repo=repo, artifacts_root=tmp_path, initial_cash=Decimal(1))
            assert restarted.observe(reference_cash=0, observed_at_ms=NOON, now_ms=NOON).cash_available_usd == cash
        finally:
            repo.close()


def test_missing_wrong_provider_stale_marks_and_gap_baseline_refuse(shadow: ShadowContext, tmp_path: Path) -> None:
    repo, projection, clock = shadow
    accepted = _enter(repo, _deploy(repo, projection))
    _fill(repo, accepted, key="buy", side="BUY", quantity=2, price=100)
    with pytest.raises(SimulationEvidenceUnavailable):
        projection.observe(reference_cash=1000, observed_at_ms=clock(), now_ms=clock())
    evidence = shadow_evidence_account_id_for_strategy(DAY_PNL_SID)
    _mark(tmp_path, evidence, at_ms=clock(), price=100, provider="fixture")
    with pytest.raises(SimulationEvidenceUnavailable):
        projection.observe(reference_cash=1000, observed_at_ms=clock(), now_ms=clock())
    _mark(tmp_path, evidence, at_ms=clock(), price=100)
    clock.advance(81_000)
    with pytest.raises(SimulationEvidenceUnavailable):
        projection.observe(reference_cash=1000, observed_at_ms=clock(), now_ms=clock())
    clock.advance(86_400_000)
    repo.revive_execution_lease()
    _mark(tmp_path, evidence, at_ms=clock(), price=100)
    with pytest.raises(SimulationEvidenceUnavailable):
        projection.observe(reference_cash=1000, observed_at_ms=clock(), now_ms=clock())


def test_retained_baseline_survives_rebuild_and_real_cash_change(tmp_path: Path) -> None:
    clock = _TestClock(NOON)
    repo = ClerkSqliteRepository.initialize(account_id="shadow:LIVE", artifacts_root=tmp_path, clock=clock)
    projection = SimulatedAccountProjection(repo=repo, artifacts_root=tmp_path)
    expected = projection.observe(reference_cash=1000, observed_at_ms=clock(), now_ms=clock())
    path = repo.db_path
    repo.close()
    path.rename(path.with_suffix(".prior"))
    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(account_id="shadow:LIVE", artifacts_root=tmp_path, clock=clock)
    try:
        fresh = SimulatedAccountProjection(repo=rebuilt, artifacts_root=tmp_path).observe(reference_cash=2000, observed_at_ms=clock(), now_ms=clock())
        assert fresh.last_equity_usd == expected.last_equity_usd == 1000
        assert fresh.cash_available_usd == Decimal(2000)
    finally:
        rebuilt.close()


def test_private_budget_proves_initial_baseline_if_crash_preceded_first_projection(tmp_path: Path) -> None:
    clock = _TestClock(NOON)
    repo = ClerkSqliteRepository.initialize(account_id=f"sim:{DAY_PNL_SID}", artifacts_root=tmp_path, clock=clock)
    try:
        before_commit = SimulatedAccountProjection(repo=repo, artifacts_root=tmp_path, initial_cash=Decimal("1000.01"))
        gate = _deploy(repo, before_commit, cents=100_001)
        accepted = _enter(repo, gate)
        _fill(repo, accepted, key="filled-before-restart", side="BUY", quantity=2, price=100)
        _mark(tmp_path, repo.account_id, at_ms=clock(), price=101, provider="fixture")
        assert not any(row["transition_kind"] == "SIMULATION_SESSION_BASELINE" for row in repo.custody_transitions())
        restored = SimulatedAccountProjection(repo=repo, artifacts_root=tmp_path)
        observation = restored.observe(reference_cash=0, observed_at_ms=clock(), now_ms=clock())
        assert observation.cash_available_usd == Decimal("800.01")
        assert observation.last_equity_usd == pytest.approx(1000.01, abs=1e-9, rel=0)
        assert observation.unrealized_pl_usd == pytest.approx(2, abs=1e-9, rel=0)
        assert sum(row["transition_kind"] == "SIMULATION_SESSION_BASELINE" for row in repo.custody_transitions()) == 1
    finally:
        repo.close()


def test_a_fresh_observation_cannot_outlive_its_mark_or_session(shadow: ShadowContext, tmp_path: Path) -> None:
    from app.broker.alpaca.clerk.et_day import et_day_window_ms
    from app.broker.alpaca.clerk.sqlite.uncertainty import AdmissionBlockedError

    repo, projection, clock = shadow
    accepted = _enter(repo, _deploy(repo, projection))
    _fill(repo, accepted, key="buy", side="BUY", quantity=2, price=100)
    _mark(tmp_path, shadow_evidence_account_id_for_strategy(DAY_PNL_SID), at_ms=clock(), price=100)
    clock.advance(70_000)
    repo.revive_execution_lease()
    observation = replace(projection.observe(reference_cash=1000, observed_at_ms=clock(), now_ms=clock()), risk_revision=1)
    gate = LiveEnvelopeGate(values=None, custody_is_simulated=True)
    gate.publish(observation)
    clock.advance(11_000)
    assert gate.fresh_observation(clock()) is not None  # cash itself is only 11s old
    with pytest.raises(AdmissionBlockedError, match="current market prices"):
        require_current_risk_admission(repo, envelope=gate, now_ms=clock())
    next_midnight = et_day_window_ms(clock())[1]
    gate.publish(replace(observation, observed_at_ms=next_midnight-1, simulation_marks_valid_until_ms=next_midnight+60_000))
    with pytest.raises(AdmissionBlockedError, match="session baseline"):
        require_current_risk_admission(repo, envelope=gate, now_ms=next_midnight)


async def test_synthetic_runtime_projects_transient_consent_before_the_budget_commit(tmp_path: Path) -> None:
    from app.broker.alpaca.clerk.active_authority import (
        activate_synthetic_clerk_authority,
        select_synthetic_clerk_runtime,
    )
    from app.broker.alpaca.clerk.synthetic_broker import SyntheticBroker

    clock = _TestClock(NOON)
    account_id = "sim:first-deploy"
    await activate_synthetic_clerk_authority(account_id=account_id, artifacts_root=tmp_path, clock=clock)
    broker = SyntheticBroker(account_id=account_id, clock=clock)
    runtime = await select_synthetic_clerk_runtime(account_id=account_id, read=broker, trade=broker,
        artifacts_root=tmp_path, simulation_initial_cash=Decimal("500.01"),
        repository_opener=lambda account, root: ClerkSqliteRepository.open(account_id=account, artifacts_root=root, clock=clock))
    try:
        assert runtime.authority_kind == "synthetic", runtime.startup_failure
        assert runtime.envelope_sync is not None
        observation = runtime.envelope_sync.envelope.fresh_observation(clock())
        assert observation is not None and observation.cash_available_usd == Decimal("500.01")
        assert runtime.clerk.live_envelope is runtime.envelope_sync.envelope
        assert (await runtime.envelope_sync.observe()).daily_loss_exempt
        assert runtime.sqlite_repository.account_risk_policy() is None
    finally:
        await runtime.close()


def test_private_dry_run_cash_is_the_observed_cash_without_needing_a_price(tmp_path: Path) -> None:
    """Hurdle H27: a Dry Run's own cash is the same formula ``observe``
    publishes, and unlike ``observe`` it needs no mark for its open position."""
    clock = _TestClock(NOON)
    repo = ClerkSqliteRepository.initialize(account_id=f"sim:{DAY_PNL_SID}", artifacts_root=tmp_path, clock=clock)
    try:
        projection = SimulatedAccountProjection(repo=repo, artifacts_root=tmp_path, initial_cash=Decimal(1000))
        gate = _deploy(repo, projection, cents=100_000)
        accepted = _enter(repo, gate)
        _fill(repo, accepted, key="buy", side="BUY", quantity=2, price=100)
        _fill(repo, accepted, key="sell", side="SELL", quantity=1, price=120)
        with pytest.raises(SimulationEvidenceUnavailable, match="Fresh simulated price evidence"):
            projection.observe(reference_cash=0, observed_at_ms=clock(), now_ms=clock())
        unpriced = private_dry_run_cash(repo, now_ms=clock())
        _mark(tmp_path, repo.account_id, at_ms=clock(), price=130, provider="fixture")
        priced = projection.observe(reference_cash=0, observed_at_ms=clock(), now_ms=clock())
    finally:
        repo.close()
    # 1000 - 200 + 120; today's modelled fees settle at the session boundary.
    assert unpriced == priced.cash_available_usd == Decimal(920)


def test_private_dry_run_cash_is_only_for_a_private_dry_run(shadow: ShadowContext) -> None:
    repo, _, clock = shadow
    with pytest.raises(ValueError, match="private Dry Run"):
        private_dry_run_cash(repo, now_ms=clock())
