"""The Clerk shapes every program leg from the run's session and its decision bar."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import app.broker.alpaca.clerk.sqlite.runtime as clerk_runtime
from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    active_program_leg_policy,
    set_active_clerk_runtime,
)
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeGate
from app.broker.alpaca.clerk.models import ChannelHealth, EffectOperationState, EffectPurpose
from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.stream_health import STREAM_HEALTH_REASON_CODE, StreamHealthGate
from app.broker.alpaca.marketable_limit import ExtendedHoursAllowances
from app.broker.contract.capabilities import ExtendedHoursWindow
from app.broker.contract.models import OrderType, TimeInForce
from app.schemas.market_liveness import MarketClockLivenessEvidence, MarketLivenessFact
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.market_liveness import compose_market_liveness
from app.services.source_bar_ledger import RetainedSourceBar
from app.utils.timestamps import Clock, now_ms_utc, to_ms_utc
from tests.broker.alpaca.clerk.live_envelope_fixtures import TEST_ENVELOPE_VALUES
from tests.broker.alpaca.clerk.sqlite.conftest import _FakeReadPort, _FakeTradePort
from tests.broker.alpaca.clerk.sqlite.test_exit import _make_entry

ACCOUNT_ID = "PA-TEST"
SID = "spy-bot"
RUN_ID = "run-1"

_ET = ZoneInfo("America/New_York")
_DAY = date(2026, 9, 2)
_WINDOW = ExtendedHoursWindow(open_minute_et=4 * 60, close_minute_et=20 * 60)
_ALLOWANCES = ExtendedHoursAllowances(entry_bps=Decimal("10"), exit_bps=Decimal("20"))
_EXTENDED_POLICY = ProgramLegPolicy(window=_WINDOW, allowances=_ALLOWANCES)


def _bar(hour: int, minute: int, *, phase: str, close: str = "100.00") -> RetainedSourceBar:
    end = to_ms_utc(datetime(_DAY.year, _DAY.month, _DAY.day, hour, minute, tzinfo=_ET))
    return RetainedSourceBar(
        seq=1,
        account_id=ACCOUNT_ID,
        provider="ibkr",
        symbol="SPY",
        bar_identity=f"ibkr:SPY:{end - 60_000}:{end}",
        bar_ref="bar-1",
        start_ms=end - 60_000,
        end_ms=end,
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=1,
        fetched_at_ms=end,
        session_phase=phase,
    )


def _decision_clock(bar: RetainedSourceBar | None) -> Clock:
    """Pin the Clerk's clock to the decision instant — the bar's close.

    The runtime proves an extended phase at the repository's clock, so the
    wall-clock default admits an extended ENTER only while the host clock
    itself sits inside an extended session of a trading day.
    """
    if bar is None:
        return now_ms_utc
    end_ms = bar.end_ms
    return lambda: end_ms


def _binding(*, use_rth: bool) -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id=SID,
        strategy_key="deployment_validation",
        broker="alpaca",
        symbol="SPY",
        use_rth=use_rth,
        mode="trade",
        quantity=1,
        carryover_policy="FORBID",
        sealed_account_id=ACCOUNT_ID,
        action_plan=alpaca_v1_action_plan("SPY"),
        run_id=RUN_ID,
        created_at_ms=1,
    )


def _stream_health(*, market_data_healthy: bool) -> StreamHealthGate:
    """A gate whose two channels report exactly this health."""

    def _channel(stream: str, healthy: bool) -> ChannelHealth:
        return ChannelHealth(
            stream=stream,
            healthy=healthy,
            connected=healthy,
            reason="" if healthy else "test",
            observed_at_ms=1_700_000_000_000,
        )

    return StreamHealthGate(
        market_data=lambda: _channel("market_data", market_data_healthy),
        execution=lambda: _channel("execution", True),
        market_data_for_symbol=lambda _symbol: _channel("market_data", market_data_healthy),
    )


async def _enter(
    tmp_path: Path,
    *,
    use_rth: bool,
    policy: ProgramLegPolicy,
    retained_source_bar: RetainedSourceBar | None,
    stream_health: StreamHealthGate | None = None,
) -> tuple[_FakeTradePort, EffectOperationState, str]:
    """Drive one ENTER through the facade and report the port and the receipt."""
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=_decision_clock(retained_source_bar),
    )
    trade = _FakeTradePort()
    facade = SqliteAlpacaClerkFacade(
        repo=repo,
        read=_FakeReadPort(),
        trade=trade,
        account_mode="paper",
        program_leg_policy=policy,
        stream_health=stream_health,
    )
    binding = _binding(use_rth=use_rth)
    await facade.register_strategy_run(binding)
    try:
        receipt = await facade.execute_for_instance(
            strategy_instance_id=SID,
            run_id=RUN_ID,
            decision_id="decision-1",
            purpose=EffectPurpose.ENTER,
            action_plan=binding.action_plan,
            quantity=binding.quantity,
            use_rth=use_rth,
            retained_source_bar=retained_source_bar,
        )
    finally:
        repo.close()
    return trade, receipt.state, receipt.explanation


async def _exit(
    tmp_path: Path,
    *,
    use_rth: bool,
    policy: ProgramLegPolicy,
    retained_source_bar: RetainedSourceBar | None,
    live_envelope: LiveEnvelopeGate | None = None,
) -> tuple[_FakeTradePort, EffectOperationState, str]:
    """Drive one EXIT through the facade, after a filled ENTER, and report the port and the receipt."""
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=_decision_clock(retained_source_bar),
    )
    trade = _FakeTradePort()
    facade = SqliteAlpacaClerkFacade(
        repo=repo,
        read=_FakeReadPort(),
        trade=trade,
        account_mode="paper",
        program_leg_policy=policy,
        live_envelope=live_envelope,
    )
    binding = _binding(use_rth=use_rth)
    await facade.register_strategy_run(binding)
    # The filled ENTER is seeded directly in the repo (as test_exit_reducing_shape.py
    # does), not through this facade's own trade port: only the EXIT's reducing
    # submission below should land in `trade.submitted_legs`.
    await _make_entry(repo, quantity=10, filled_quantity=10, status="filled")
    try:
        receipt = await facade.execute_for_instance(
            strategy_instance_id=SID,
            run_id=RUN_ID,
            decision_id="decision-exit-1",
            purpose=EffectPurpose.EXIT,
            action_plan=binding.action_plan,
            quantity=binding.quantity,
            use_rth=use_rth,
            retained_source_bar=retained_source_bar,
        )
    finally:
        repo.close()
    return trade, receipt.state, receipt.explanation


async def test_an_extended_exit_decision_submits_a_marketable_day_limit_at_the_exit_allowance(
    tmp_path: Path,
) -> None:
    """A filled ENTER plus an extended EXIT decision shapes the reducing leg (review finding 3a)."""
    trade, state, _explanation = await _exit(
        tmp_path,
        use_rth=False,
        policy=_EXTENDED_POLICY,
        retained_source_bar=_bar(18, 30, phase="POST"),
    )

    assert state is not EffectOperationState.REJECTED
    (leg,) = trade.submitted_legs
    assert (leg.order_type, leg.time_in_force, leg.limit_price, leg.extended_hours, leg.side.value) == (
        OrderType.LIMIT,
        TimeInForce.DAY,
        99.80,
        True,
        "sell",
    )


async def test_an_extended_exit_is_priced_from_the_sealed_allowance_not_a_staged_edit(
    tmp_path: Path,
) -> None:
    """ADR 0059 D3: an edited allowance reaches an exit at the re-arm, never before.

    The environment here carries a staged 500 bps that nobody armed, and the
    ledger seals 40. Pricing from the environment would let an operator move a
    real-money anchor by editing a file — and an exit is never envelope-gated,
    so nothing else would catch it. The policy's own configured 20 bps is the
    pre-envelope answer, so a wrong wiring cannot hide behind a plausible price.
    """
    envelope = LiveEnvelopeGate(
        values=replace(TEST_ENVELOPE_VALUES, xh_entry_bps=500.0, xh_exit_bps=500.0),
        sealed=replace(TEST_ENVELOPE_VALUES, xh_entry_bps=30.0, xh_exit_bps=40.0),
        custody_is_simulated=False,
    )

    trade, state, _explanation = await _exit(
        tmp_path,
        use_rth=False,
        policy=_EXTENDED_POLICY,
        retained_source_bar=_bar(18, 30, phase="POST"),
        live_envelope=envelope,
    )

    assert state is not EffectOperationState.REJECTED
    (leg,) = trade.submitted_legs
    assert leg.limit_price == pytest.approx(99.60)  # 100.00 − the sealed 40 bps


async def test_an_extended_exit_still_prices_when_no_arming_record_seals_the_account(
    tmp_path: Path,
) -> None:
    """An EXIT is never refused or delayed for want of a seal.

    A position the operator is closing must not be stranded because no ceremony
    has armed this account yet, or because this observation could not read the
    ledger. Both leave ``sealed`` at ``None``, and the exit prices from the
    configured values exactly as it did before the envelope existed. (The loss
    judgement deliberately does *not* take this fallback — see
    ``LiveEnvelopeSync._seal_unreadable``.)
    """
    envelope = LiveEnvelopeGate(
        # As in production, the environment feeds both the configured envelope
        # and the policy's allowances, so the two agree at 20 bps on the exit.
        values=replace(TEST_ENVELOPE_VALUES, xh_entry_bps=10.0, xh_exit_bps=20.0),
        custody_is_simulated=False,
    )
    assert envelope.sealed is None

    trade, state, explanation = await _exit(
        tmp_path,
        use_rth=False,
        policy=_EXTENDED_POLICY,
        retained_source_bar=_bar(18, 30, phase="POST"),
        live_envelope=envelope,
    )

    assert state is not EffectOperationState.REJECTED, explanation
    (leg,) = trade.submitted_legs
    assert leg.limit_price == pytest.approx(99.80)  # 100.00 − the configured 20 bps


async def test_an_exit_decision_at_session_close_is_rejected_before_broker_contact(
    tmp_path: Path,
) -> None:
    """No session accepts a program leg at 20:00 ET; the EXIT refuses, never submits (review finding 3b)."""
    trade, state, explanation = await _exit(
        tmp_path,
        use_rth=False,
        policy=_EXTENDED_POLICY,
        retained_source_bar=_bar(20, 0, phase="CLOSED"),
    )

    assert state is EffectOperationState.REJECTED
    assert explanation.startswith("SESSION_CLOSED_AT_DECISION:")
    assert trade.submitted_legs == []


async def test_an_extended_decision_outside_the_regular_session_submits_a_marketable_day_limit(
    tmp_path: Path,
) -> None:
    trade, state, _explanation = await _enter(
        tmp_path,
        use_rth=False,
        policy=_EXTENDED_POLICY,
        retained_source_bar=_bar(18, 30, phase="POST"),
    )

    assert state is not EffectOperationState.REJECTED
    (leg,) = trade.submitted_legs
    assert (leg.order_type, leg.time_in_force, leg.limit_price, leg.extended_hours) == (
        OrderType.LIMIT,
        TimeInForce.DAY,
        100.10,
        True,
    )


async def test_an_extended_decision_inside_the_regular_session_submits_a_market_day_leg(
    tmp_path: Path,
) -> None:
    trade, _state, _explanation = await _enter(
        tmp_path,
        use_rth=False,
        policy=_EXTENDED_POLICY,
        retained_source_bar=_bar(10, 0, phase="RTH"),
    )

    (leg,) = trade.submitted_legs
    assert (leg.order_type, leg.time_in_force, leg.limit_price, leg.extended_hours) == (
        OrderType.MARKET,
        TimeInForce.DAY,
        None,
        False,
    )


async def test_an_extended_decision_without_a_retained_bar_is_rejected_before_broker_contact(
    tmp_path: Path,
) -> None:
    trade, state, explanation = await _enter(
        tmp_path,
        use_rth=False,
        policy=_EXTENDED_POLICY,
        retained_source_bar=None,
    )

    assert state is EffectOperationState.REJECTED
    assert explanation.startswith("EXTENDED_ANCHOR_UNAVAILABLE:")
    assert trade.submitted_legs == []


async def test_a_regular_hours_decision_keeps_the_market_day_leg(tmp_path: Path) -> None:
    """The RTH filter never yields an 18:30 bar in production; the runtime does not re-check."""
    trade, _state, _explanation = await _enter(
        tmp_path,
        use_rth=True,
        policy=_EXTENDED_POLICY,
        retained_source_bar=_bar(18, 30, phase="POST"),
    )

    (leg,) = trade.submitted_legs
    assert leg.order_type is OrderType.MARKET
    assert leg.extended_hours is False


async def test_a_regular_only_authority_refuses_an_extended_decision(tmp_path: Path) -> None:
    trade, state, explanation = await _enter(
        tmp_path,
        use_rth=False,
        policy=ProgramLegPolicy.regular_only(),
        retained_source_bar=_bar(18, 30, phase="POST"),
    )

    assert state is EffectOperationState.REJECTED
    assert explanation.startswith("EXTENDED_HOURS_UNSUPPORTED:")
    assert trade.submitted_legs == []


@pytest.mark.parametrize(
    ("policy", "expected_window"),
    [(_EXTENDED_POLICY, _WINDOW), (ProgramLegPolicy.regular_only(), None)],
)
def test_the_facade_publishes_its_leg_policy(
    tmp_path: Path,
    policy: ProgramLegPolicy,
    expected_window: ExtendedHoursWindow | None,
) -> None:
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    facade = SqliteAlpacaClerkFacade(
        repo=repo,
        read=_FakeReadPort(),
        trade=_FakeTradePort(),
        account_mode="paper",
        program_leg_policy=policy,
    )
    try:
        assert facade.program_leg_policy is policy
        assert facade.program_leg_policy.window == expected_window
    finally:
        repo.close()


def test_a_facade_built_without_a_policy_is_regular_only(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    facade = SqliteAlpacaClerkFacade(
        repo=repo, read=_FakeReadPort(), trade=_FakeTradePort(), account_mode="paper"
    )
    try:
        assert facade.program_leg_policy == ProgramLegPolicy.regular_only()
        assert facade.program_leg_policy.window is None
    finally:
        repo.close()


def test_the_process_accessor_reads_the_active_authoritys_policy(tmp_path: Path) -> None:
    assert active_program_leg_policy() == ProgramLegPolicy.regular_only()
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    facade = SqliteAlpacaClerkFacade(
        repo=repo,
        read=_FakeReadPort(),
        trade=_FakeTradePort(),
        account_mode="paper",
        program_leg_policy=_EXTENDED_POLICY,
    )
    set_active_clerk_runtime(ActiveClerkRuntime(authority_kind="sqlite", clerk=facade))
    try:
        assert active_program_leg_policy() is _EXTENDED_POLICY
    finally:
        set_active_clerk_runtime(None)
        repo.close()


def _closed_clock(symbol: str, observed_at_ms: int) -> MarketLivenessFact:
    """What Alpaca's RTH-only clock reports through every extended session."""
    return compose_market_liveness(
        symbol,
        now_ms=observed_at_ms,
        market_clock=MarketClockLivenessEvidence(
            state="CLOSED",
            source="test.clock",
            observed_at_ms=observed_at_ms,
            vendor_timestamp_ms=observed_at_ms,
        ),
        connected=True,
        connection_changed_at_ms=observed_at_ms,
        symbol_status=None,
    )


async def test_a_closed_clock_admits_an_extended_enter_when_the_feed_is_printing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The declared window resolves POST and the market-data channel is live."""
    monkeypatch.setattr(clerk_runtime, "market_liveness_fact", _closed_clock)

    trade, state, _explanation = await _enter(
        tmp_path,
        use_rth=False,
        policy=_EXTENDED_POLICY,
        retained_source_bar=_bar(18, 30, phase="POST"),
        stream_health=_stream_health(market_data_healthy=True),
    )

    assert state is not EffectOperationState.REJECTED
    (leg,) = trade.submitted_legs
    assert (leg.order_type, leg.extended_hours) == (OrderType.LIMIT, True)


async def test_a_closed_clock_refuses_an_extended_enter_with_no_live_feed_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recheck's own half of the #1671 pair, at the submission boundary.

    The declared window still resolves POST, so before the bar-freshness
    conjunct this ENTER was admitted on the schedule alone — which an
    unscheduled PRE/POST closure reads exactly like. With no stream-health
    gate installed there is no live evidence at all, so nothing proves the
    venue is printing and the Clerk refuses before broker contact.
    """
    monkeypatch.setattr(clerk_runtime, "market_liveness_fact", _closed_clock)

    trade, state, explanation = await _enter(
        tmp_path,
        use_rth=False,
        policy=_EXTENDED_POLICY,
        retained_source_bar=_bar(18, 30, phase="POST"),
        stream_health=None,
    )

    assert state is EffectOperationState.REJECTED
    assert explanation.startswith("MARKET_LIVENESS_BLOCKED:")
    assert trade.submitted_legs == []


async def test_a_closed_clock_refuses_an_extended_enter_on_an_unhealthy_market_data_channel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale or disconnected feed is refused by the stream-health gate first.

    Pinned so the two refusals stay distinguishable: this one names the
    broken channel, the one above names the missing liveness proof, and both
    stop the same ENTER before broker contact.
    """
    monkeypatch.setattr(clerk_runtime, "market_liveness_fact", _closed_clock)

    trade, state, explanation = await _enter(
        tmp_path,
        use_rth=False,
        policy=_EXTENDED_POLICY,
        retained_source_bar=_bar(18, 30, phase="POST"),
        stream_health=_stream_health(market_data_healthy=False),
    )

    assert state is EffectOperationState.REJECTED
    assert explanation.startswith(f"{STREAM_HEALTH_REASON_CODE}:")
    assert trade.submitted_legs == []
