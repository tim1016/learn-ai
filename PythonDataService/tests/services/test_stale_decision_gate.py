"""#2345 / #2303: no decision taken after its delivery allowance reaches the Clerk.

``admit_on_delivery`` judged lateness only for recovered bars; a ``realtime``
bar was waved through as "never late by construction". It is not: the IBKR
minute assembler held a complete minute until the next minute's first print,
so an extended run's final minute was delivered at 04:00 the next morning
(G-2), and a regular-hours minute whose line went quiet was delivered 45 s
late (G-1). Both were decided live.

The fix has two legs, both pinned here:

* the assembler emits a minute as soon as it is complete by count, so the
  final minute of an extended session is decided at the session's close;
* the runner judges, whatever the provenance, every decision whose bar closed
  more than the delivery allowance before the wall clock
  (``feed_continuity_policy.late_decision``): a late ENTER is refused with a
  ``DECISION_LATE`` receipt; a late EXIT is exempt, as from the liveness gate,
  and reaches the Clerk carrying its lateness.

The IBKR prints are synthetic 5-second bars (the shape ``reqRealTimeBars``
delivers); whether IBKR really prints nothing between the extended close and
the next pre-market open is a vendor assumption these tests do not prove.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.services.bot_trade_strategy as bot_trade_strategy
import app.services.feed_continuity_policy as fcp
from app.broker.alpaca.broker import ALPACA_EXTENDED_HOURS_WINDOW
from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.sqlite.models import DecisionReceiptResource
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.ibkr.bar_models import IbkrMinuteBar
from app.broker.ibkr.minute_assembler import RTH_CONTRIBUTIONS_PER_MINUTE, MinuteAssembler
from app.lean_sidecar.trading_calendar import next_trading_day, session_open_ms_utc
from app.marketdata.feed import (
    DELIVERY_ALLOWANCE_MS,
    ContinuityEventRef,
    ContinuityPolicy,
    FeedContinuityEvent,
    MarketDataBar,
    SubstitutionRefusal,
)
from app.marketdata.ibkr_feed import IbkrMarketDataFeed
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.decision_session import RunDecisionSession
from app.services.session_authority import declared_session_bounds
from tests._helpers.bot_runner.custody import _SID, _T0
from tests._helpers.bot_runner.doubles import _FakeClerk, _FakeFeed
from tests._helpers.bot_runner.market import (
    patch_fresh_live_market_liveness,
    patch_wall_clock_to_the_fed_bar,
)
from tests.services.bot_runner._support import _green_bar, _red_bar

_EXTENDED = RunDecisionSession(kind="extended", window=ALPACA_EXTENDED_HOURS_WINDOW)
_RTH = RunDecisionSession(kind="rth", window=None)
_EXTENDED_DAY = date(2023, 11, 15)
_RTH_DAY = date(2024, 1, 2)


@pytest.fixture(autouse=True)
def _fresh_live_market_liveness(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_fresh_live_market_liveness(monkeypatch)


def _print(source_ms: int, price: str) -> SimpleNamespace:
    """One synthetic IBKR 5-second TRADES bar."""
    value = Decimal(price)
    return SimpleNamespace(
        time=datetime.fromtimestamp(source_ms / 1000, tz=UTC),
        open=value,
        high=value,
        low=value,
        close=value,
        volume=10,
    )


def _feed(assembler: MinuteAssembler, source_ms: int, price: str = "450.00") -> IbkrMinuteBar | None:
    return assembler.feed(_print(source_ms, price), symbol="SPY", generation=1, venue="SMART", use_rth=False)


def _policy(session: RunDecisionSession) -> ContinuityPolicy:
    async def _sink(event: FeedContinuityEvent) -> ContinuityEventRef:  # never reached by these tests
        raise AssertionError(f"unexpected continuity event {event}")

    return ContinuityPolicy(
        session=session,
        next_trigger_ms=lambda last: (last // 60_000 + 1) * 60_000,
        substitution_grant=lambda _s, _e: SubstitutionRefusal(reason="SUBSTITUTION_NOT_AUTHORIZED"),
        record_event=_sink,
    )


def _pin_wall_clock(monkeypatch: pytest.MonkeyPatch, now_ms: int) -> None:
    monkeypatch.setattr(fcp, "now_ms_utc", lambda: now_ms)


# --- the rule ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("policy", "allowance_ms"),
    [(None, DELIVERY_ALLOWANCE_MS), (_policy(_RTH), _policy(_RTH).delivery_allowance_ms)],
)
def test_late_decision_is_judged_against_the_one_allowance(
    monkeypatch: pytest.MonkeyPatch, policy: ContinuityPolicy | None, allowance_ms: int
) -> None:
    close_ms = session_open_ms_utc(_RTH_DAY) + 60_000

    _pin_wall_clock(monkeypatch, close_ms + allowance_ms)
    assert fcp.late_decision(policy, close_ms) is None

    _pin_wall_clock(monkeypatch, close_ms + allowance_ms + 1)
    late = fcp.late_decision(policy, close_ms)
    assert late is not None
    assert (late.lateness_ms, late.allowance_ms) == (allowance_ms + 1, allowance_ms)


# --- G-2: the extended session's final minute ------------------------------


def test_the_extended_sessions_final_minute_is_emitted_at_the_close_not_the_next_morning() -> None:
    close_ms = _EXTENDED.close_ms(_EXTENDED_DAY)
    final_minute_ms = close_ms - 60_000
    assembler = MinuteAssembler()

    emitted = [_feed(assembler, final_minute_ms + i * 5_000) for i in range(RTH_CONTRIBUTIONS_PER_MINUTE)]

    # Before the fix every call returned None and the minute stayed held until
    # the next trading day's first pre-market print.
    assert emitted[:-1] == [None] * (RTH_CONTRIBUTIONS_PER_MINUTE - 1)
    assert emitted[-1] is not None
    bar = IbkrMarketDataFeed._translate(emitted[-1])
    assert (bar.start_ms, bar.end_ms, bar.provenance) == (final_minute_ms, close_ms, "realtime")
    assert _EXTENDED.includes(bar)
    assert assembler.open_minute_start_ms is None


def test_an_incomplete_final_minute_held_overnight_is_a_late_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A final minute short of its twelve prints still waits for the next print.

    That print is the next session's pre-market open, hours later. The minute
    arrives labelled ``realtime`` -- nothing about one live connection makes
    it timely -- and the decision rule sees it as the stale bar it is.
    """
    close_ms = _EXTENDED.close_ms(_EXTENDED_DAY)
    final_minute_ms = close_ms - 60_000
    next_open_ms = _extended_open_ms(next_trading_day(_EXTENDED_DAY))
    assembler = MinuteAssembler()
    for i in range(RTH_CONTRIBUTIONS_PER_MINUTE - 1):
        assert _feed(assembler, final_minute_ms + i * 5_000) is None

    held = _feed(assembler, next_open_ms, "452.00")

    assert held is not None
    bar = IbkrMarketDataFeed._translate(held)
    assert (bar.end_ms, bar.provenance) == (close_ms, "realtime")
    _pin_wall_clock(monkeypatch, next_open_ms)
    late = fcp.late_decision(_policy(_EXTENDED), bar.end_ms)
    assert late is not None and late.lateness_ms == next_open_ms - close_ms


def _extended_open_ms(session_date: date) -> int:
    """The declared extended window's open on ``session_date``."""
    bounds = declared_session_bounds(session_date, ALPACA_EXTENDED_HOURS_WINDOW)
    assert bounds is not None
    return bounds.open_ms


# --- the runner's gate (G-1, and every other late bar) ----------------------


def _rth_binding() -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id=_SID,
        strategy_key="deployment_validation",
        broker="alpaca",
        symbol="SPY",
        use_rth=True,
        mode="trade",
        quantity=1,
        carryover_policy="FORBID",
        action_plan=alpaca_v1_action_plan("SPY"),
        run_id="run-current",
        created_at_ms=_T0,
    )


def _quiet_line_minute(minute_start_ms: int) -> MarketDataBar:
    """G-1: three prints, then a quiet line; the minute emits when the line resumes."""
    assembler = MinuteAssembler()
    _feed(assembler, minute_start_ms, "450.00")
    _feed(assembler, minute_start_ms + 5_000, "450.50")
    _feed(assembler, minute_start_ms + 10_000, "451.00")
    resumed = _feed(assembler, minute_start_ms + 105_000, "451.00")  # 45 s into the next minute
    assert resumed is not None and resumed.contribution_count == 3
    bar = IbkrMarketDataFeed._translate(resumed)
    assert bar.provenance == "realtime" and bar.close > bar.open  # a green, "live" minute
    return bar


_DECISION_MINUTE_MS = session_open_ms_utc(_RTH_DAY) + 60 * 60_000  # an in-window RTH minute


async def _run_rth_enter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, lateness_ms: int
) -> tuple[_FakeClerk, list[DecisionReceiptResource], int]:
    """Drive the real ``run_trade_bot`` over a two-green-bar ENTER decided ``lateness_ms`` after its close."""
    bars = [_green_bar(_DECISION_MINUTE_MS), _quiet_line_minute(_DECISION_MINUTE_MS)]
    decision_close_ms = bars[-1].end_ms
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path / "clerk")
    repo.register_strategy_instance(strategy_instance_id=_SID, symbol="SPY", config_hash="config-1")
    clerk = _FakeClerk(repository=repo)
    clerk.authority_kind = "sqlite"
    clerk.account_id = "PA-TEST"
    _pin_wall_clock(monkeypatch, decision_close_ms + lateness_ms)
    set_alpaca_clerk(clerk)
    try:
        await bot_trade_strategy.run_trade_bot(_rth_binding(), _FakeFeed(bars, mode="finite"))
        receipts = repo.decision_receipt_tail(strategy_instance_id=_SID, limit=50)
    finally:
        set_alpaca_clerk(None)
        repo.close()
    return clerk, receipts, decision_close_ms


@pytest.mark.asyncio
async def test_a_realtime_decision_delivered_45s_late_never_reaches_the_clerk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G-1: the quiet-line minute is decided 45 s after its close, past the 20 s allowance."""
    clerk, receipts, close_ms = await _run_rth_enter(tmp_path, monkeypatch, lateness_ms=45_000)

    assert clerk.calls == [], "a stale DECIDE reached clerk.execute_for_instance"
    refused = receipts[-1]
    assert refused.outcome == "blocked"
    facts = json.loads(refused.facts_json)
    assert facts["reason_code"] == "DECISION_LATE"
    assert facts["decision_bar_close_ms"] == close_ms
    assert facts["retention_class"] == "protected_refusal"


@pytest.mark.asyncio
async def test_a_decision_inside_its_allowance_still_reaches_the_clerk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clerk, receipts, _close_ms = await _run_rth_enter(
        tmp_path, monkeypatch, lateness_ms=DELIVERY_ALLOWANCE_MS
    )

    assert [call["purpose"] for call in clerk.calls] == ["ENTER"]
    assert all(json.loads(r.facts_json)["reason_code"] != "DECISION_LATE" for r in receipts)


@pytest.mark.asyncio
async def test_a_late_exit_still_reaches_the_clerk_and_carries_its_lateness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A late EXIT is risk reduction: it is exempt, like the liveness gate's (#1671 AC3).

    The ENTER is decided on time; the EXIT's bar is decided 45 s after its
    close. It still reaches ``execute_for_instance``, with the lateness on its
    decision evidence and a ``bot_decision_late`` log naming the exemption.
    """
    enter_ms = _DECISION_MINUTE_MS
    bars = [
        _green_bar(enter_ms - 60_000),
        _green_bar(enter_ms),  # ENTER
        _red_bar(enter_ms + 60_000),
        _red_bar(enter_ms + 120_000),
        _green_bar(enter_ms + 180_000),  # EXIT, three bars after entry
    ]
    exit_close_ms = bars[-1].end_ms
    patch_wall_clock_to_the_fed_bar(monkeypatch)
    fed_bar_clock = fcp.now_ms_utc
    monkeypatch.setattr(
        fcp, "now_ms_utc", lambda: fed_bar_clock() + (45_000 if fed_bar_clock() == exit_close_ms else 0)
    )
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path / "clerk")
    repo.register_strategy_instance(strategy_instance_id=_SID, symbol="SPY", config_hash="config-1")
    clerk = _FakeClerk(repository=repo)
    clerk.authority_kind = "sqlite"
    clerk.account_id = "PA-TEST"
    set_alpaca_clerk(clerk)
    try:
        with caplog.at_level("WARNING", logger="app.services.bot_trade_strategy"):
            await bot_trade_strategy.run_trade_bot(_rth_binding(), _FakeFeed(bars, mode="finite"))
    finally:
        set_alpaca_clerk(None)
        repo.close()

    assert [call["purpose"] for call in clerk.calls] == ["ENTER", "EXIT"]
    assert clerk.calls[0]["decision_evidence"].decision_lateness_ms is None
    assert clerk.calls[1]["decision_evidence"].decision_lateness_ms == 45_000
    (late_log,) = [r for r in caplog.records if getattr(r, "action", None) == "bot_decision_late"]
    assert (late_log.exempt, late_log.intent, late_log.lateness_ms) == ("exit", "EXIT", 45_000)


@pytest.mark.asyncio
async def test_an_entry_that_fell_due_while_the_run_prepared_is_refused_not_caught_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2410: bars the stream delivered during startup preparation are held, then decided.

    The run's warmup opens the stream first and holds its bars until warmup is
    done. A bucket those held bars complete is judged on its own close like
    any other: past the allowance, the ENTER is refused as ``DECISION_LATE``
    and no order reaches the Clerk -- a catch-up entry is never submitted.
    """
    from app.broker.alpaca.clerk.account_authority import paper_evidence_account_id_for_strategy
    from app.services.source_bar_ledger import SourceBarLedger

    bars = [_green_bar(_DECISION_MINUTE_MS), _quiet_line_minute(_DECISION_MINUTE_MS)]
    decision_close_ms = bars[-1].end_ms
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path / "clerk")
    repo.register_strategy_instance(strategy_instance_id=_SID, symbol="SPY", config_hash="config-1")
    clerk = _FakeClerk(repository=repo)
    clerk.authority_kind = "sqlite"
    clerk.account_id = "PA-TEST"
    ledger = SourceBarLedger(
        artifacts_root=tmp_path / "bars", account_id=paper_evidence_account_id_for_strategy(_SID)
    )
    _pin_wall_clock(monkeypatch, decision_close_ms + 45_000)
    set_alpaca_clerk(clerk)
    try:
        await bot_trade_strategy.run_trade_bot(
            _rth_binding(), _FakeFeed(bars, mode="finite"), source_bars=ledger
        )
        receipts = repo.decision_receipt_tail(strategy_instance_id=_SID, limit=50)
        startup = ledger.startup_join(run_id="run-current")
    finally:
        set_alpaca_clerk(None)
        repo.close()
        ledger.close(checkpoint=False)

    assert startup is not None and startup.ready_at_ms is not None  # the bars really were held
    assert clerk.calls == [], "a decision that fell due during preparation reached the Clerk"
    facts = json.loads(receipts[-1].facts_json)
    assert (receipts[-1].outcome, facts["reason_code"]) == ("blocked", "DECISION_LATE")
