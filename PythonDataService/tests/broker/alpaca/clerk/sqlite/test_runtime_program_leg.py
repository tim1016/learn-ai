"""The Clerk shapes every program leg from the run's session and its decision bar."""

from __future__ import annotations

import logging
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
    live_envelope: LiveEnvelopeGate | None = None,
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
        live_envelope=live_envelope,
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


def test_the_one_published_leg_policy_carries_the_sealed_allowances_for_both_sides(
    tmp_path: Path,
) -> None:
    """Entries are sealed like exits, and admission reads the policy that prices.

    The owner's decision names entries *and* exits. The entry price cannot be
    driven end-to-end the way the exit is — a live ENTER whose environment
    differs from its seal is refused ``LIVE_ENVELOPE_DISAGREEMENT`` before it
    is priced, and one with no fresh observation is refused
    ``LIVE_ENVELOPE_UNOBSERVED`` — so what is pinned here is the accessor both
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
        live_envelope=LiveEnvelopeGate(values=armed, sealed=armed, custody_is_simulated=False),
    )
    try:
        policy = facade.program_leg_policy
    finally:
        repo.close()

    assert policy.allowances == ExtendedHoursAllowances(
        entry_bps=Decimal("30"), exit_bps=Decimal("40")
    )
    assert policy.window == _EXTENDED_POLICY.window


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


_EARLY_CLOSE_DAY = date(2026, 11, 27)  # the day after Thanksgiving: NYSE closes at 13:00


@pytest.mark.parametrize(
    ("bar", "label"),
    [
        (_bar(16, 0, phase="RTH"), "the 15:59 bar, decided at the 16:00 close"),
        (
            _bar(13, 0, phase="RTH", day=_EARLY_CLOSE_DAY),
            "the 12:59 bar of an early-close day, decided at its 13:00 close",
        ),
    ],
)
async def test_a_regular_hours_exit_decided_on_the_last_bar_goes_out_as_an_after_hours_limit(
    tmp_path: Path, bar: RetainedSourceBar, label: str
) -> None:
    """#2440 (owner decision #2431): the day's last EXIT is never queued for the next open.

    A regular-hours run's last bar closes *at* the regular close, so its EXIT
    reaches the Clerk a few seconds after the session has ended. A market DAY
    leg sent then is queued by Alpaca for the next regular open. The leg is
    instead the extended-hours shape an extended run would send at that
    instant: a DAY limit flagged for extended hours, the decision bar's close
    less the exit allowance. The close is the canonical calendar's, so an
    early-close day's 13:00 is treated exactly like 16:00.
    """
    trade, state, explanation = await _exit(
        tmp_path,
        use_rth=True,
        policy=_EXTENDED_POLICY,
        retained_source_bar=bar,
        send_delay_ms=2_000,
    )

    assert state is not EffectOperationState.REJECTED, explanation
    (leg,) = trade.submitted_legs
    assert (leg.order_type, leg.time_in_force, leg.limit_price, leg.extended_hours, leg.side.value) == (
        OrderType.LIMIT,
        TimeInForce.DAY,
        99.80,  # floor_tick(100.00 × (1 − 20 / 10⁴))
        True,
        "sell",
    ), label


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
    ("decided_at", "warned"),
    [
        pytest.param((15, 30), False, id="mid-session-the-market-leg-goes-out-as-decided"),
        pytest.param((16, 0), True, id="after-the-close-an-after-hours-price-is-needed"),
    ],
)
async def test_a_regular_hours_exit_warns_it_is_unpriced_only_when_a_price_is_needed(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    decided_at: tuple[int, int],
    warned: bool,
) -> None:
    """#2440 review: ``regular_hours_exit_unpriced`` means an after-hours price was needed and missing.

    With no retained decision bar the EXIT can never be shaped as an
    after-hours limit, so it always carries ``unpriced``. Mid-session that is
    no warning — the market leg goes out as decided — and the warning used to
    fire on every such EXIT anyway. It fires only when the market leg cannot
    be sent at the send instant.
    """
    decided_at_ms = to_ms_utc(datetime(_DAY.year, _DAY.month, _DAY.day, *decided_at, tzinfo=_ET))

    with caplog.at_level(logging.WARNING):
        await _exit(
            tmp_path,
            use_rth=True,
            policy=_EXTENDED_POLICY,
            retained_source_bar=None,
            decided_at_ms=decided_at_ms,
            send_delay_ms=2_000,
        )

    unpriced = [r for r in caplog.records if getattr(r, "action", None) == "regular_hours_exit_unpriced"]
    assert [r.reason_code for r in unpriced] == (["EXTENDED_ANCHOR_UNAVAILABLE"] if warned else [])


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
            assert pricing.policy_source() is _EXTENDED_POLICY
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
