"""The Clerk shapes every program leg from the run's session and its decision bar."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import app.broker.alpaca.clerk.sqlite.runtime as clerk_runtime
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeGate
from app.broker.alpaca.clerk.models import ChannelHealth, EffectOperationState, EffectPurpose
from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade, StrategyRegistrationConflictError
from app.broker.alpaca.clerk.stream_health import STREAM_HEALTH_REASON_CODE, StreamHealthGate
from app.broker.alpaca.marketable_limit import ExtendedHoursAllowances
from app.broker.contract.capabilities import ExtendedHoursWindow
from app.broker.contract.models import OrderType, TimeInForce
from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.schemas.exit_terms import ExitTermsInput
from app.schemas.market_liveness import MarketClockLivenessEvidence, MarketLivenessFact
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.market_liveness import compose_market_liveness
from app.services.source_bar_ledger import RetainedSourceBar
from app.utils.timestamps import Clock, now_ms_utc, ny_datetime, to_ms_utc
from tests._helpers.bot_runner.market import clock_read_before_the_close
from tests.broker.alpaca.clerk.live_envelope_fixtures import TEST_ENVELOPE_VALUES
from tests.broker.alpaca.clerk.sqlite.conftest import (
    _FakeReadPort,
    _FakeTradePort,
    remove_budget_schema_for_legacy_fixture,
)
from tests.broker.alpaca.clerk.sqlite.test_exit import _make_entry

# #2596: these ENTERs run on the default clock; keep it inside a session.
pytestmark = pytest.mark.usefixtures("wall_clock_in_session")

ACCOUNT_ID = "PA-TEST"
SID = "spy-bot"
RUN_ID = "run-1"

_ET = ZoneInfo("America/New_York")
_DAY = date(2026, 9, 2)
_WINDOW = ExtendedHoursWindow(open_minute_et=4 * 60, close_minute_et=20 * 60)
_ALLOWANCES = ExtendedHoursAllowances(entry_bps=Decimal("10"), exit_bps=Decimal("20"))
_EXTENDED_POLICY = ProgramLegPolicy(window=_WINDOW, allowances=_ALLOWANCES)


def _bar(
    hour: int, minute: int, *, phase: str, close: str = "100.00", day: date = _DAY
) -> RetainedSourceBar:
    end = to_ms_utc(datetime(day.year, day.month, day.day, hour, minute, tzinfo=_ET))
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


def _binding(*, use_rth: bool, account_id: str = ACCOUNT_ID) -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id=SID,
        exit_terms=ExitTermsInput(exit_allowance_bps=20, band_multiple=2, spread_cap_bps=50).seal(),
        strategy_key="deployment_validation",
        broker="alpaca",
        symbol="SPY",
        use_rth=use_rth,
        mode="trade",
        quantity=1,
        carryover_policy="FORBID",
        sealed_account_id=account_id,
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
    live_envelope: LiveEnvelopeGate | None = None,
    send_delay_ms: int = 0,
    order_transitions: list[dict] | None = None,
    clock: Clock | None = None,
    account_id: str = ACCOUNT_ID,
    authority_kind: str = "sqlite",
) -> tuple[_FakeTradePort, EffectOperationState, str]:
    """Drive one ENTER through the facade and report the port and the receipt.

    ``send_delay_ms`` puts the Clerk's clock that long after the decision bar's
    close, as ``_exit``'s does; ``clock`` replaces that clock outright.
    ``order_transitions``, when given, collects the custody transitions of the
    ENTER's order before the repository closes. ``account_id`` and
    ``authority_kind`` name the authority (a Dry Run's is ``sim:``/synthetic).
    """
    decision_clock = _decision_clock(retained_source_bar)
    repo = ClerkSqliteRepository.initialize(
        account_id=account_id,
        artifacts_root=tmp_path,
        clock=clock if clock is not None else lambda: decision_clock() + send_delay_ms,
    )
    trade = _FakeTradePort()
    facade = SqliteAlpacaClerkFacade(
        repo=repo,
        read=_FakeReadPort(),
        trade=trade,
        authority_kind=authority_kind,
        account_mode="paper",
        program_leg_policy=policy,
        stream_health=stream_health,
        live_envelope=live_envelope,
    )
    binding = _binding(use_rth=use_rth, account_id=account_id)
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
        if order_transitions is not None:
            for order_ref in receipt.child_order_refs:
                order_transitions.extend(repo.transitions_for_order(order_ref))
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
    send_delay_ms: int = 0,
    decided_at_ms: int | None = None,
) -> tuple[_FakeTradePort, EffectOperationState, str]:
    """Drive one EXIT through the facade, after a filled ENTER, and report the port and the receipt.

    ``send_delay_ms`` puts the Clerk's clock that long after the decision bar's
    close: a live EXIT reaches the Clerk seconds after its bar closes, never at
    the closing instant itself. ``decided_at_ms`` pins the decision instant
    when there is no bar to read it from.
    """
    decision_clock = (
        _decision_clock(retained_source_bar) if decided_at_ms is None else (lambda: decided_at_ms)
    )
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=lambda: decision_clock() + send_delay_ms,
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


def test_the_one_published_leg_policy_carries_the_envelope_allowances_for_both_sides(
    tmp_path: Path,
) -> None:
    """Entries are priced from the envelope, and admission reads the policy that prices.

    The owner's decision names entries *and* exits. The entry price cannot be
    driven end-to-end the way the exit is — a live ENTER with no fresh
    observation is refused ``LIVE_ENVELOPE_UNOBSERVED`` before it is priced
    — so what is pinned here is the accessor both
    sides share. Start and Resume admission receive it with the held
    custody snapshot; ``_execute_effect`` prices from it (which
    the extended-exit tests above prove end-to-end). One accessor is what stops
    admission answering this policy's questions off a different object.
    """
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    armed = replace(TEST_ENVELOPE_VALUES, xh_entry_bps=30.0, xh_exit_bps=40.0)
    facade = SqliteAlpacaClerkFacade(
        repo=repo,
        read=_FakeReadPort(),
        trade=_FakeTradePort(),
        account_mode="paper",
        # The composed policy's 10 / 20 bps is the pre-envelope answer, and the
        # one a wrong wiring would give.
        program_leg_policy=_EXTENDED_POLICY,
        live_envelope=LiveEnvelopeGate(values=armed, custody_is_simulated=False),
    )
    try:
        policy = facade.program_leg_policy
    finally:
        repo.close()

    assert policy.allowances == ExtendedHoursAllowances(
        entry_bps=Decimal("30"), exit_bps=None
    )
    assert policy.window == _EXTENDED_POLICY.window


async def test_an_extended_exit_is_priced_from_the_bots_sealed_terms_not_the_account_envelope(
    tmp_path: Path,
) -> None:
    """The bot's deployed 20 bps outranks the account envelope's 500."""
    envelope = LiveEnvelopeGate(
        values=replace(TEST_ENVELOPE_VALUES, xh_entry_bps=500.0, xh_exit_bps=500.0),
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
    assert leg.limit_price == pytest.approx(99.80)  # registered 20 bps outranks account envelope


async def test_an_extended_exit_prices_from_the_configured_envelope(
    tmp_path: Path,
) -> None:
    """An EXIT is never refused or delayed for want of an arming.

    A position the operator is closing prices from the configured values;
    no arming record seals the envelope any more (#2629).
    """
    envelope = LiveEnvelopeGate(
        # As in production, the environment feeds both the configured envelope
        # and the policy's allowances, so the two agree at 20 bps on the exit.
        values=replace(TEST_ENVELOPE_VALUES, xh_entry_bps=10.0, xh_exit_bps=20.0),
        custody_is_simulated=False,
    )

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


async def test_a_regular_hours_exit_decided_inside_the_session_keeps_the_market_day_leg(
    tmp_path: Path,
) -> None:
    """Exits sent during regular hours are unchanged (#2440)."""
    trade, state, explanation = await _exit(
        tmp_path,
        use_rth=True,
        policy=_EXTENDED_POLICY,
        retained_source_bar=_bar(15, 59, phase="RTH"),
        send_delay_ms=2_000,
    )

    assert state is not EffectOperationState.REJECTED, explanation
    (leg,) = trade.submitted_legs
    assert (leg.order_type, leg.time_in_force, leg.limit_price, leg.extended_hours) == (
        OrderType.MARKET,
        TimeInForce.DAY,
        None,
        False,
    )


@pytest.mark.parametrize(
    ("account_id", "authority_kind", "prices_from_the_live_market"),
    [("PA-TEST", "sqlite", True), ("sim:spy-bot", "synthetic", False)],
)
def test_a_late_exit_is_repriced_from_the_live_market_only_off_a_synthetic_authority(
    tmp_path: Path, account_id: str, authority_kind: str, prices_from_the_live_market: bool
) -> None:
    """#2440: one pricing seam per authority, named by every path that drives an EXIT.

    The runner, restart recovery, the sweep and its watchdog, operator
    Reconcile Now and the operator's flatten all read ``recovery_pricing``.
    A synthetic authority fills against retained bars, never the live market
    (PR #2230 review), so it re-prices nothing — whichever of them drives
    such an EXIT, it folds for the operator instead of being priced off a
    market it does not execute in.
    """
    repo = ClerkSqliteRepository.initialize(account_id=account_id, artifacts_root=tmp_path)
    repo.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="test", exit_terms=ExitTermsInput(exit_allowance_bps=20, band_multiple=2, spread_cap_bps=50).seal())
    facade = SqliteAlpacaClerkFacade(
        repo=repo,
        read=_FakeReadPort(),
        trade=_FakeTradePort(),
        authority_kind=authority_kind,
        account_mode="paper",
        program_leg_policy=_EXTENDED_POLICY,
    )
    try:
        pricing = facade.recovery_pricing
        assert (pricing is not UNPRICEABLE_RECOVERY) is prices_from_the_live_market
        if prices_from_the_live_market:
            assert pricing.policy_for(SID) == _EXTENDED_POLICY
    finally:
        repo.close()


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
    """A regular-hours ENTER is the market DAY leg whatever the declared window."""
    trade, _state, _explanation = await _enter(
        tmp_path,
        use_rth=True,
        policy=_EXTENDED_POLICY,
        retained_source_bar=_bar(10, 0, phase="RTH"),
    )

    (leg,) = trade.submitted_legs
    assert (leg.order_type, leg.time_in_force, leg.extended_hours) == (OrderType.MARKET, TimeInForce.DAY, False)


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


@pytest.mark.parametrize("change", ["disconnect", "generation"])
async def test_market_loss_between_admission_and_contact_refuses_the_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    calls = 0

    def liveness(symbol: str, at: int) -> MarketLivenessFact:
        nonlocal calls
        calls += 1
        clock = MarketClockLivenessEvidence(state="OPEN", source="test.clock", observed_at_ms=at)
        from app.schemas.market_liveness import SymbolMarketDataEvidence

        data = SymbolMarketDataEvidence(
            symbol=symbol, generation=str(calls) if change == "generation" else "same",
            state="READY", observed_at_ms=at, valid_until_ms=at + 5_000,
            reason_code="MARKET_DATA_READY", reason="Current test evidence.",
        )
        return compose_market_liveness(
            symbol, now_ms=at, market_clock=clock,
            connected=calls == 1 or change == "generation", connection_changed_at_ms=at,
            symbol_status=None, market_data=data, require_market_data=True,
        )

    monkeypatch.setattr(clerk_runtime, "market_liveness_fact", liveness)
    trade, state, _ = await _enter(
        tmp_path, use_rth=True, policy=_EXTENDED_POLICY,
        retained_source_bar=_bar(10, 30, phase="RTH"),
        stream_health=_stream_health(market_data_healthy=True),
    )

    assert calls == 2
    assert trade.submitted_legs == []
    assert state is EffectOperationState.REJECTED


# The canonical calendar closes this day early; the tests below never name the hour.
_EARLY_CLOSE_DAY = date(2026, 11, 27)
# A live ENTER reaches the Clerk a fraction of a second after its bar closes.
_ENTER_SEND_DELAY_MS = 600
_CLOSED_AFTER_CLOCK_READ = (
    "The regular session has closed. The broker clock was last read before the close, "
    "so it no longer shows the market open."
)


def _last_bar(day: date) -> RetainedSourceBar:
    """The session's last bar: it closes at the canonical calendar's regular close."""
    close = ny_datetime(session_close_ms_utc(day))
    return _bar(close.hour, close.minute, phase="RTH", day=day)


@pytest.mark.parametrize("day", [_DAY, _EARLY_CLOSE_DAY], ids=["regular-close", "early-close"])
async def test_a_regular_hours_enter_reaching_the_clerk_after_the_close_is_rejected_before_broker_contact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, day: date,
) -> None:
    """#2596: the recheck reads the close the last OPEN clock answer named.

    The answer was read just before the close and is still fresh, so the OPEN
    flag alone admitted this market DAY ENTER for Alpaca to queue until the
    next open.
    """
    monkeypatch.setattr(
        clerk_runtime, "market_liveness_fact", clock_read_before_the_close(session_close_ms_utc(day))
    )

    trade, state, explanation = await _enter(
        tmp_path,
        use_rth=True,
        policy=_EXTENDED_POLICY,
        retained_source_bar=_last_bar(day),
        send_delay_ms=_ENTER_SEND_DELAY_MS,
    )

    assert trade.submitted_legs == []
    assert state is EffectOperationState.REJECTED
    assert explanation == f"MARKET_LIVENESS_BLOCKED: {_CLOSED_AFTER_CLOCK_READ}"


async def test_an_enter_accepted_before_the_close_is_refused_when_it_reaches_the_broker_after_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2596: the check just before broker contact reads the same close.

    The recheck reads the clock a second before the close and admits the
    ENTER; by the time the order would reach the broker the close has passed.
    """
    close_ms = session_close_ms_utc(_DAY)
    clock = clock_read_before_the_close(close_ms)
    read_at = (close_ms - 1_000, close_ms + 100)
    reads = 0

    def liveness(symbol: str, _at: int) -> MarketLivenessFact:
        nonlocal reads
        reads += 1
        return clock(symbol, read_at[reads - 1])

    monkeypatch.setattr(clerk_runtime, "market_liveness_fact", liveness)
    transitions: list[dict] = []

    trade, state, _explanation = await _enter(
        tmp_path,
        use_rth=True,
        policy=_EXTENDED_POLICY,
        retained_source_bar=_bar(15, 59, phase="RTH"),
        order_transitions=transitions,
    )

    assert reads == 2
    assert trade.submitted_legs == []
    assert state is EffectOperationState.REJECTED
    refused = transitions[-1]
    assert (refused["transition_kind"], refused["summary_code"]) == (
        "ENTER_SUBMISSION_REFUSED",
        "MARKET_LIVENESS_BLOCKED",
    )
    assert json.loads(refused["facts_json"])["why"] == f"MARKET_CLOSED: {_CLOSED_AFTER_CLOCK_READ}"


async def test_an_extended_enter_on_the_last_regular_bar_still_goes_out_as_an_after_hours_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2596 leaves an extended run alone: past the close the clock reads closed,

    and, exactly as for any closed clock, the declared window proves after-hours
    and the live feed proves it printing -- so the ENTER is the priced limit the
    decision bar's close shapes, never a market order.
    """
    monkeypatch.setattr(
        clerk_runtime, "market_liveness_fact", clock_read_before_the_close(session_close_ms_utc(_DAY))
    )

    trade, state, _explanation = await _enter(
        tmp_path,
        use_rth=False,
        policy=_EXTENDED_POLICY,
        retained_source_bar=_last_bar(_DAY),
        stream_health=_stream_health(market_data_healthy=True),
        send_delay_ms=_ENTER_SEND_DELAY_MS,
    )

    assert state is not EffectOperationState.REJECTED
    (leg,) = trade.submitted_legs
    assert (leg.order_type, leg.time_in_force, leg.extended_hours) == (OrderType.LIMIT, TimeInForce.DAY, True)


_MARKET_ENTRY_AFTER_THE_CLOSE = (
    "The regular session has closed, or closes within seconds. A market order sent now "
    "could reach the broker after the close and wait there until the next open."
)


@pytest.mark.parametrize("day", [_DAY, _EARLY_CLOSE_DAY], ids=["regular-close", "early-close"])
async def test_a_last_bar_market_enter_on_a_clerk_clock_running_behind_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, day: date,
) -> None:
    """#2596: the Clerk holds a market ENTER to the rule a reducing market leg obeys.

    The last bar's decision lands after the close, but this Clerk's clock
    trails real time and reads it 1.2 s before the close. There the broker's
    last answer is fresh and has not reached the close it named, so the
    liveness recheck admits the ENTER. The order could still reach Alpaca
    after the close, which would hold it for the next open: the canonical
    calendar, judged a few seconds ahead, refuses it.
    """
    close_ms = session_close_ms_utc(day)
    monkeypatch.setattr(clerk_runtime, "market_liveness_fact", clock_read_before_the_close(close_ms))

    trade, state, explanation = await _enter(
        tmp_path,
        use_rth=True,
        policy=_EXTENDED_POLICY,
        retained_source_bar=_last_bar(day),
        clock=lambda: close_ms - 1_200,
    )

    assert trade.submitted_legs == []
    assert state is EffectOperationState.REJECTED
    assert explanation == f"MARKET_CLOSED: {_MARKET_ENTRY_AFTER_THE_CLOSE}"


async def test_a_dry_runs_synthetic_authority_refuses_a_market_enter_that_would_reach_the_broker_after_the_close(
    tmp_path: Path,
) -> None:
    """#2596 on the ``sim:`` Clerk: its own calendar, not a broker clock, refuses the late ENTER.

    Neither runner sends a closing-bar decision any more (#2607), so this is
    the backstop behind that screen for the sandbox: a market ENTER the Clerk
    reads 0.6 s after the close could only reach the broker after it.
    """
    close_ms = session_close_ms_utc(_DAY)

    trade, state, explanation = await _enter(
        tmp_path,
        use_rth=True,
        policy=_EXTENDED_POLICY,
        retained_source_bar=_last_bar(_DAY),
        clock=lambda: close_ms + 600,
        account_id="sim:spy-bot",
        authority_kind="synthetic",
    )

    assert trade.submitted_legs == []
    assert state is EffectOperationState.REJECTED
    assert explanation == f"MARKET_CLOSED: {_MARKET_ENTRY_AFTER_THE_CLOSE}"


async def test_a_market_enter_checked_before_broker_contact_a_tenth_of_a_second_before_the_close_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2596: the check just before broker contact asks the calendar again.

    The Clerk accepts the ENTER ten seconds before the close; it reaches the
    point of broker contact at 15:59:59.9, where the broker's answer still
    reads open and the order could not reach Alpaca before the close.
    """
    close_ms = session_close_ms_utc(_DAY)
    monkeypatch.setattr(clerk_runtime, "market_liveness_fact", clock_read_before_the_close(close_ms))
    now_ms = close_ms - 10_000
    accept_enter = clerk_runtime.accept_enter

    def accepted_then_delayed(*args: object, **kwargs: object) -> object:
        nonlocal now_ms
        accepted = accept_enter(*args, **kwargs)
        now_ms = close_ms - 100
        return accepted

    monkeypatch.setattr(clerk_runtime, "accept_enter", accepted_then_delayed)
    transitions: list[dict] = []

    trade, state, _explanation = await _enter(
        tmp_path,
        use_rth=True,
        policy=_EXTENDED_POLICY,
        retained_source_bar=_bar(15, 59, phase="RTH"),
        order_transitions=transitions,
        clock=lambda: now_ms,
    )

    assert trade.submitted_legs == []
    assert state is EffectOperationState.REJECTED
    refused = transitions[-1]
    # #2637: filed under the calendar's refusal, not a liveness block that never happened.
    assert (refused["transition_kind"], refused["summary_code"]) == ("ENTER_SUBMISSION_REFUSED", "MARKET_CLOSED")
    assert json.loads(refused["facts_json"])["why"] == _MARKET_ENTRY_AFTER_THE_CLOSE


async def test_a_market_enter_at_the_open_on_a_clerk_clock_running_behind_names_the_lag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2637: an extended-hours run decides on the bar that closes at the open.

    That bar closed inside the regular session, so its ENTER is a market leg.
    The Clerk's clock trails real time and still reads 0.3 s before the open,
    where a market order may not go out. Missing the entry is safe; the
    receipt says the Clerk's clock caused it, not that the session closed.
    """
    monkeypatch.setattr(clerk_runtime, "market_liveness_fact", _closed_clock)
    at_the_open = _bar(9, 30, phase="PRE")

    trade, state, explanation = await _enter(
        tmp_path,
        use_rth=False,
        policy=_EXTENDED_POLICY,
        retained_source_bar=at_the_open,
        stream_health=_stream_health(market_data_healthy=True),
        clock=lambda: at_the_open.end_ms - 300,
    )

    assert trade.submitted_legs == []
    assert state is EffectOperationState.REJECTED
    assert explanation == (
        "MARKET_CLOSED: The Clerk's clock is running at least 300 ms behind: it still reads before "
        "the regular open, though the bar this entry was decided on closed at or after it. A market "
        "order goes out only inside the regular session, so this entry was missed."
    )


async def test_a_market_enter_with_time_to_reach_the_broker_before_the_close_still_goes_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Six seconds before the close the order lands inside the session: nothing changes."""
    close_ms = session_close_ms_utc(_DAY)
    monkeypatch.setattr(clerk_runtime, "market_liveness_fact", clock_read_before_the_close(close_ms))

    trade, state, _explanation = await _enter(
        tmp_path,
        use_rth=True,
        policy=_EXTENDED_POLICY,
        retained_source_bar=_bar(15, 59, phase="RTH"),
        clock=lambda: close_ms - 6_000,
    )

    assert state is not EffectOperationState.REJECTED
    (leg,) = trade.submitted_legs
    assert (leg.order_type, leg.time_in_force, leg.extended_hours) == (OrderType.MARKET, TimeInForce.DAY, False)


async def test_explicit_terms_survive_a_crash_after_registration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.schemas.exit_terms import ExitTermsInput
    from app.services.bot_carryover import configuration_hash

    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=lambda: _bar(10, 0, phase='RTH').end_ms)
    terms = ExitTermsInput(exit_allowance_bps=37, band_multiple=3, spread_cap_bps=70).seal()
    binding = _binding(use_rth=True).model_copy(update={'exit_terms': terms})
    assert configuration_hash(binding) == configuration_hash(_binding(use_rth=True))
    facade = SqliteAlpacaClerkFacade(repo=repo, read=_FakeReadPort(), trade=_FakeTradePort(), account_mode='paper', program_leg_policy=_EXTENDED_POLICY)
    register = repo.register_strategy_instance

    def crash_after_commit(**kwargs: object) -> None:
        register(**kwargs)
        raise RuntimeError('simulated crash after registration commit')

    monkeypatch.setattr(repo, 'register_strategy_instance', crash_after_commit)
    with pytest.raises(RuntimeError, match='simulated crash'):
        await facade.register_strategy_run(binding)
    repo.close()
    reopened = ClerkSqliteRepository.open(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=lambda: _bar(10, 0, phase='RTH').end_ms)
    try:
        restarted = SqliteAlpacaClerkFacade(repo=reopened, read=_FakeReadPort(), trade=_FakeTradePort(), account_mode='paper', program_leg_policy=_EXTENDED_POLICY)
        await restarted.register_strategy_run(binding)
        assert reopened.exit_terms(SID) == terms
        changed = binding.model_copy(update={'run_id': 'run-next', 'exit_terms': terms.model_copy(update={'exit_allowance_bps': 99})})
        with pytest.raises(StrategyRegistrationConflictError, match='custody-sealed exit terms'):
            await restarted.register_strategy_run(changed)
    finally:
        reopened.close()


async def test_each_bots_flatten_ticket_uses_its_own_terms(tmp_path: Path) -> None:
    from app.broker.alpaca.clerk.sqlite.projection_models import SafeFlattenPlan, SafeFlattenPlanLeg
    from app.schemas.exit_terms import ExitTermsInput
    from app.schemas.market_liveness import TopOfBookQuote

    now = _bar(8, 0, phase='PRE').end_ms
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=lambda: now)
    facade = SqliteAlpacaClerkFacade(repo=repo, read=_FakeReadPort(), trade=_FakeTradePort(), account_mode='paper',
        program_leg_policy=_EXTENDED_POLICY, quote_source=lambda symbol, stamp: TopOfBookQuote(symbol=symbol, bid=100, ask=100.05, source='ibkr', observed_at_ms=stamp))
    try:
        for sid, allowance in [('one', 10), ('two', 40)]:
            binding = _binding(use_rth=True).model_copy(update={'strategy_instance_id': sid, 'run_id': sid,
                'exit_terms': ExitTermsInput(exit_allowance_bps=allowance, band_multiple=3, spread_cap_bps=70).seal()})
            await facade.register_strategy_run(binding)
            plan = SafeFlattenPlan(version_token='test', account_id=ACCOUNT_ID, authority_generation=1,
                db_identity_token='test', control_revision=0, scope='strategy', strategy_instance_id=sid,
                reconciliation_id='test', prepared_at_ms=now, expires_at_ms=now + 1000,
                legs=(SafeFlattenPlanLeg(sid, 'SPY', 'sell', 1, now),))
            priced = facade.price_safe_flatten(plan)
            assert priced.suggested_limit_price == Decimal('99.90' if sid == 'one' else '99.60')
            assert priced.band_cap_bps == allowance * 3
    finally:
        repo.close()


async def test_unset_backfill_stays_unset_after_restart_and_allows_regular_market_flatten(tmp_path: Path) -> None:
    from app.broker.alpaca.clerk.program_leg import LegRefusal
    from app.broker.alpaca.clerk.recovery_reduction import RegularSessionReduction
    from app.broker.alpaca.clerk.sqlite.projection_models import SafeFlattenPlan, SafeFlattenPlanLeg
    from app.schemas.market_liveness import TopOfBookQuote

    now = _bar(8, 0, phase="PRE").end_ms
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=lambda: now)
    facade = SqliteAlpacaClerkFacade(repo=repo, read=_FakeReadPort(), trade=_FakeTradePort(),
        account_mode="paper", program_leg_policy=replace(_EXTENDED_POLICY, allowances=None))
    repo.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="legacy")
    facade.upgrade_legacy_exit_terms()
    assert repo.exit_terms(SID).provenance == "backfilled"
    repo.close()
    reopened = ClerkSqliteRepository.open(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=lambda: now)
    try:
        restarted = SqliteAlpacaClerkFacade(repo=reopened, read=_FakeReadPort(), trade=_FakeTradePort(),
            account_mode="paper", program_leg_policy=_EXTENDED_POLICY,
            quote_source=lambda symbol, stamp: TopOfBookQuote(symbol=symbol, bid=100, ask=100.05,
                source="ibkr", observed_at_ms=stamp))
        plan = SafeFlattenPlan(version_token="test", account_id=ACCOUNT_ID, authority_generation=1,
            db_identity_token="test", control_revision=0, scope="strategy", strategy_instance_id=SID,
            reconciliation_id="test", prepared_at_ms=now, expires_at_ms=now + 1000,
            legs=(SafeFlattenPlanLeg(SID, "SPY", "sell", 1, now),))
        held = restarted.price_safe_flatten(plan)
        assert isinstance(held, LegRefusal)
        assert held.reason_code == "EXTENDED_HOURS_ALLOWANCE_UNSET"
        now = _bar(10, 0, phase="RTH").end_ms
        assert isinstance(restarted.price_safe_flatten(plan), RegularSessionReduction)
    finally:
        reopened.close()


async def test_new_start_without_explicit_exit_terms_is_refused(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    try:
        facade = SqliteAlpacaClerkFacade(repo=repo, read=_FakeReadPort(), trade=_FakeTradePort(),
            account_mode="paper", program_leg_policy=_EXTENDED_POLICY)
        with pytest.raises(StrategyRegistrationConflictError, match="Explicit exit terms"):
            await facade.register_strategy_run(_binding(use_rth=True).model_copy(update={"exit_terms": None}))
        assert repo.strategy_instance(SID) is None
    finally:
        repo.close()


def test_legacy_upgrade_prices_each_bot_from_its_own_arming_once(tmp_path: Path, monkeypatch) -> None:
    """The one-time exit-terms upgrade still reads historical arming (#2629 keeps it)."""
    from tests._helpers.historical_arming import HistoricalArmingLedger as LiveArmingLedger
    from tests.broker.alpaca.clerk.test_live_arming import _armed

    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    try:
        repo.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="legacy")
        for sid in ("another-bot", "never-armed"):
            repo.register_strategy_instance(strategy_instance_id=sid, symbol="SPY", config_hash="legacy")
        ledger = LiveArmingLedger(tmp_path, live_account_id=ACCOUNT_ID)
        ledger.append(_armed(instance=SID, account=ACCOUNT_ID, envelope=replace(TEST_ENVELOPE_VALUES, xh_exit_bps=40)))
        ledger.append(_armed(instance="another-bot", account=ACCOUNT_ID, envelope=replace(TEST_ENVELOPE_VALUES, xh_exit_bps=70)))
        configured = replace(TEST_ENVELOPE_VALUES, xh_exit_bps=20)
        gate = LiveEnvelopeGate(values=configured, custody_is_simulated=False)
        facade = SqliteAlpacaClerkFacade(repo=repo, read=_FakeReadPort(), trade=_FakeTradePort(),
            account_mode="live", live_envelope=gate, program_leg_policy=_EXTENDED_POLICY)
        assert repo.exit_terms(SID) is None
        facade.upgrade_legacy_exit_terms(ledger)
        terms = repo.exit_terms(SID)
        assert terms.provenance == "backfilled"
        assert terms.exit_allowance_bps == 40
        assert repo.exit_terms("another-bot").exit_allowance_bps == 70
        assert repo.exit_terms("never-armed").exit_allowance_bps == 20
        first_sequence = len(repo.custody_transitions())
        monkeypatch.setenv("ALPACA_LIVE_XH_EXIT_BAND_MULTIPLE", "9")
        facade.upgrade_legacy_exit_terms(ledger)
        assert repo.exit_terms(SID) == terms
        assert len(repo.custody_transitions()) == first_sequence
        assert facade.exit_policy_for_instance(SID).allowances.exit_bps == Decimal(40)
    finally:
        repo.close()


@pytest.mark.parametrize("bot_allowance,account_allowance,next_hour", [(20, None, 4), (None, 20, 9)])
def test_recovery_price_and_schedule_share_bot_terms(tmp_path: Path, bot_allowance, account_allowance, next_hour) -> None:
    from app.broker.alpaca.clerk.recovery_reduction import next_redrive_at_ms
    from app.schemas.exit_terms import ExitTerms

    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    try:
        repo.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="legacy",
            exit_terms=ExitTerms(exit_allowance_bps=bot_allowance, band_multiple=2, spread_cap_bps=50, provenance="backfilled"))
        policy = replace(_EXTENDED_POLICY, allowances=None if account_allowance is None else _ALLOWANCES)
        facade = SqliteAlpacaClerkFacade(repo=repo, read=_FakeReadPort(), trade=_FakeTradePort(),
            account_mode="paper", program_leg_policy=policy)
        pricing = facade.recovery_pricing
        next_open = next_redrive_at_ms(not_before_ms=_bar(0, 0, phase="CLOSED").end_ms, policy=pricing.policy_for(SID))
        assert next_open == _bar(next_hour, 0 if next_hour == 4 else 30, phase="PRE" if next_hour == 4 else "RTH").end_ms
        assert pricing.read("SPY", next_open, strategy_instance_id=SID).policy.allowances.exit_bps == (
            None if bot_allowance is None else Decimal(bot_allowance))
    finally:
        repo.close()


def test_v17_terms_upgrade_and_mirror_rebuild_preserve_the_registration_seal(tmp_path: Path) -> None:
    import sqlite3

    terms = ExitTermsInput(exit_allowance_bps=37, band_multiple=3, spread_cap_bps=70).seal()
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    repo.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="legacy", exit_terms=terms)
    journal = repo.custody_transitions()
    path = repo.db_path
    repo.close()
    with sqlite3.connect(path) as conn:
        remove_budget_schema_for_legacy_fixture(conn)
        conn.execute("DROP TABLE strategy_exit_terms")
        conn.execute("UPDATE control_meta SET schema_version=17")
    migrated = ClerkSqliteRepository.open(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    assert migrated.exit_terms(SID) == terms
    assert migrated.custody_transitions() == journal
    migrated.close()
    path.rename(path.with_suffix(".before-rebuild"))
    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    try:
        assert rebuilt.exit_terms(SID) == terms
        assert rebuilt.custody_transitions() == journal
    finally:
        rebuilt.close()
