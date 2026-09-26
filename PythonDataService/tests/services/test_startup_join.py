"""A run's warmup meets its live stream without a hole (#2410).

Every test starts the run partway through the minute ``_J``: the stream
omits that minute as short and records a ``stream_joined`` gap, exactly as
``ContinuityLoop.resolve_emitted`` does, then delivers whole minutes from
``_J + 1 min``. Warmup must end exactly there, with ``_J`` filled from
history, and the held live bars must follow it once, in order.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import date
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import pytest

import app.services.startup_join as startup_join
from app.broker.contract.capabilities import ExtendedHoursWindow
from app.marketdata.feed import (
    RESUME_HOLE_AFTER_HOURS,
    WARMUP_HISTORY_UNAVAILABLE,
    ContinuityEventRef,
    ContinuityPolicy,
    FeedContinuityEvent,
    MarketDataBar,
    MarketDataFeedError,
    SubstitutionRefusal,
    WarmupMinutesMissing,
)
from app.services.bot_trade_strategy import _RetainedSourceBarFeed
from app.services.decision_session import RunDecisionSession
from app.services.session_authority import et_minute_of_day_ms
from app.services.source_bar_ledger import SourceBarLedger
from app.services.startup_join import LiveStartBuffer, StartupDeadline, StreamSeam

_MIN = 60_000
_DAY = date(2026, 9, 24)
_J = et_minute_of_day_ms(_DAY, 13 * 60)  # the minute the stream joined partway through
_LIVE_FROM = _J + _MIN
_RTH = RunDecisionSession(kind="rth", window=None)
_EXTENDED = RunDecisionSession(
    kind="extended", window=ExtendedHoursWindow(open_minute_et=4 * 60, close_minute_et=20 * 60)
)


def _bar(start_ms: int, *, provenance: str = "realtime", phase: str = "RTH", close: str = "500") -> MarketDataBar:
    return MarketDataBar(
        symbol="SPY",
        start_ms=start_ms,
        end_ms=start_ms + _MIN,
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=100,
        fetched_at_ms=start_ms + _MIN,
        feed_id="ibkr",
        session_phase=phase,
        provenance=provenance,
    )


def _history(first_ms: int, last_ms: int, *, phase: str = "RTH") -> list[MarketDataBar]:
    """History minutes opening in ``[first_ms, last_ms]``."""
    return [_bar(start, provenance="history", phase=phase) for start in range(first_ms, last_ms + 1, _MIN)]


class _JoiningFeed:
    """IBKR joined partway through ``joined_ms``: records that gap, then streams live.

    ``history`` answers each warmup fetch in turn (the last answer repeats), so
    a test can make the joined minute appear only on a later attempt. History
    serves only minutes closed by the feed's own clock: the run starts 30 s
    into the joined minute, and the clock advances as the stream reports that
    minute closed and delivers each later one. A run that fetched history
    before its stream joined -- the pre-#2410 order -- never sees that minute.
    """

    feed_id = "ibkr"

    def __init__(
        self,
        *,
        live: Sequence[MarketDataBar],
        history: Sequence[Sequence[MarketDataBar]],
        joined_ms: int | None = _J,
        live_gate: asyncio.Event | None = None,
    ) -> None:
        self._live = list(live)
        self._history = [list(answer) for answer in history]
        self._joined_ms = joined_ms
        self._live_gate = live_gate
        self.fetches = 0
        self.live_yielded = 0
        self.clock_ms = (_J if joined_ms is None else joined_ms) + 30_000

    async def stream_bars(
        self, symbol: str, *, use_rth: bool = True, continuity: ContinuityPolicy | None = None
    ) -> AsyncIterator[MarketDataBar]:
        del use_rth
        if self._joined_ms is not None and continuity is not None:
            self.clock_ms = self._joined_ms + _MIN + 5_000
            await continuity.record_event(
                FeedContinuityEvent(
                    kind="gap",
                    cause="stream_joined",
                    feed_id=self.feed_id,
                    symbol=symbol,
                    observed_at_ms=self._joined_ms + _MIN,
                    window_start_ms=self._joined_ms,
                    window_end_ms=self._joined_ms + _MIN,
                    contribution_count=6,
                )
            )
        for bar in self._live:
            if self._live_gate is not None:
                await self._live_gate.wait()
            self.live_yielded += 1
            self.clock_ms = max(self.clock_ms, bar.end_ms)
            yield bar
        await asyncio.Event().wait()

    async def recent_closed_bars(
        self, symbol: str, *, use_rth: bool = True, lookback_days: int = 5
    ) -> list[MarketDataBar]:
        del symbol, use_rth, lookback_days
        answer = self._history[min(self.fetches, len(self._history) - 1)]
        self.fetches += 1
        return [bar for bar in answer if bar.end_ms <= self.clock_ms]


def _policy(ledger: SourceBarLedger, session: RunDecisionSession = _RTH) -> ContinuityPolicy:
    async def _sink(event: FeedContinuityEvent) -> ContinuityEventRef:
        return ledger.append_event(event, run_id="run-1")

    return ContinuityPolicy(
        session=session,
        next_trigger_ms=lambda last: (last // _MIN + 1) * _MIN,
        substitution_grant=lambda _s, _e: SubstitutionRefusal(reason="SUBSTITUTION_NOT_AUTHORIZED"),
        record_event=_sink,
    )


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[SourceBarLedger]:
    store = SourceBarLedger(artifacts_root=tmp_path, account_id="paper:startup")
    yield store
    store.close(checkpoint=False)


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(startup_join, "_RETRY_MS", 10)


def _run_feed(
    feed: _JoiningFeed, ledger: SourceBarLedger, session: RunDecisionSession = _RTH
) -> _RetainedSourceBarFeed:
    return _RetainedSourceBarFeed(feed, ledger, run_id="run-1", session=session, continuity=_policy(ledger, session))


async def _first_live(run_feed: _RetainedSourceBarFeed, count: int) -> list[MarketDataBar]:
    delivered: list[MarketDataBar] = []
    async for bar in run_feed.stream_bars("SPY", use_rth=True):
        delivered.append(bar)
        if len(delivered) == count:
            break
    return delivered


# ── the seam ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_run_started_mid_minute_warms_through_the_joined_minute_and_meets_its_stream(
    ledger: SourceBarLedger,
) -> None:
    """#2410 regression: the joined minute used to be omitted, leaving a hole after warmup."""
    feed = _JoiningFeed(live=[_bar(_LIVE_FROM), _bar(_LIVE_FROM + _MIN)], history=[_history(_J - 30 * _MIN, _J)])
    run_feed = _run_feed(feed, ledger)

    warmup = await run_feed.recent_closed_bars("SPY", use_rth=True)
    live = await _first_live(run_feed, 2)

    series = [bar.start_ms for bar in (*warmup, *live)]
    assert all(later - earlier == _MIN for earlier, later in pairwise(series))
    assert warmup[-1].start_ms == _J and warmup[-1].provenance == "history"
    assert live[0].start_ms == _LIVE_FROM
    record = ledger.startup_join(run_id="run-1")
    assert record is not None
    assert (record.live_from_ms, record.joined_minute_start_ms) == (_LIVE_FROM, _J)
    assert record.history_joined_at_ms is not None and record.ready_at_ms is not None


@pytest.mark.asyncio
async def test_a_minute_both_history_and_the_stream_hold_is_consumed_exactly_once(
    ledger: SourceBarLedger,
) -> None:
    """History fetched after the next boundary also returns the stream's first minute."""
    late_history = _history(_J - 30 * _MIN, _LIVE_FROM)  # reaches past the seam
    feed = _JoiningFeed(live=[_bar(_LIVE_FROM)], history=[late_history])
    run_feed = _run_feed(feed, ledger)

    warmup = await run_feed.recent_closed_bars("SPY", use_rth=True)
    live = await _first_live(run_feed, 1)

    assert warmup[-1].end_ms == _LIVE_FROM
    rows = ledger.bars(provider="ibkr", symbol="SPY")
    assert [row.start_ms for row in rows].count(_LIVE_FROM) == 1
    assert next(row for row in rows if row.start_ms == _LIVE_FROM).provenance == "realtime"
    assert [bar.start_ms for bar in live] == [_LIVE_FROM]


@pytest.mark.asyncio
async def test_the_seam_is_the_first_delivered_minute_when_the_stream_joined_whole(
    ledger: SourceBarLedger,
) -> None:
    """No gap recorded: the stream's first minute was complete, so warmup ends where it opens."""
    feed = _JoiningFeed(live=[_bar(_J)], history=[_history(_J - 30 * _MIN, _J)], joined_ms=None)
    run_feed = _run_feed(feed, ledger)

    warmup = await run_feed.recent_closed_bars("SPY", use_rth=True)
    live = await _first_live(run_feed, 1)

    assert warmup[-1].end_ms == _J
    assert [bar.start_ms for bar in live] == [_J]


@pytest.mark.asyncio
async def test_minutes_the_stream_delivers_during_a_slow_repair_follow_warmup_in_order(
    ledger: SourceBarLedger,
) -> None:
    """History has not published the joined minute on the first two asks; the stream keeps delivering."""
    without_joined = _history(_J - 30 * _MIN, _J - _MIN)
    feed = _JoiningFeed(
        live=[_bar(_LIVE_FROM + i * _MIN) for i in range(3)],
        history=[without_joined, without_joined, _history(_J - 30 * _MIN, _J)],
    )
    run_feed = _run_feed(feed, ledger)

    warmup = await run_feed.recent_closed_bars("SPY", use_rth=True)
    held_before_release = [row.start_ms for row in ledger.bars(provider="ibkr", symbol="SPY")]
    live = await _first_live(run_feed, 3)

    assert feed.fetches == 3
    assert warmup[-1].start_ms == _J
    # Nothing live was retained before warmup history was.
    assert _LIVE_FROM not in held_before_release
    assert [bar.start_ms for bar in live] == [_LIVE_FROM, _LIVE_FROM + _MIN, _LIVE_FROM + 2 * _MIN]
    rows = ledger.bars(provider="ibkr", symbol="SPY")
    assert [row.start_ms for row in rows] == sorted(row.start_ms for row in rows)


@pytest.mark.asyncio
async def test_the_bars_the_stream_delivers_after_release_are_retained_before_the_next_is_asked_for(
    ledger: SourceBarLedger,
) -> None:
    """The ledger's one causal order: a bar is journaled before the feed produces the next one."""
    gate = asyncio.Event()
    feed = _JoiningFeed(live=[_bar(_LIVE_FROM), _bar(_LIVE_FROM + _MIN)], history=[_history(_J - 5 * _MIN, _J)])
    feed._live_gate = gate
    run_feed = _run_feed(feed, ledger)

    await run_feed.recent_closed_bars("SPY", use_rth=True)
    gate.set()
    await _first_live(run_feed, 2)

    live_rows = [row for row in ledger.bars(provider="ibkr", symbol="SPY") if row.provenance == "realtime"]
    assert [row.start_ms for row in live_rows] == [_LIVE_FROM, _LIVE_FROM + _MIN]


@pytest.mark.asyncio
async def test_after_release_the_stream_is_read_only_as_fast_as_the_run_consumes_it(
    ledger: SourceBarLedger,
) -> None:
    """One bar in flight, as before: the feed must not believe a bar was consumed
    while the strategy has not decided on it (the fleet runner waits on exactly that)."""
    feed = _JoiningFeed(live=[_bar(_LIVE_FROM + i * _MIN) for i in range(5)], history=[_history(_J - 5 * _MIN, _J)])
    run_feed = _run_feed(feed, ledger)

    await run_feed.recent_closed_bars("SPY", use_rth=True)
    stream = run_feed.stream_bars("SPY", use_rth=True)
    first = await anext(stream)
    for _ in range(5):
        await asyncio.sleep(0)
    yielded_while_deciding = feed.live_yielded
    second = await anext(stream)
    await stream.aclose()

    assert (first.start_ms, second.start_ms) == (_LIVE_FROM, _LIVE_FROM + _MIN)
    assert yielded_while_deciding <= feed.live_yielded
    # Everything read before release was held; after it, the run pulls one at a time.
    assert feed.live_yielded <= yielded_while_deciding + 1


# ── the deadline ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_joined_minute_history_never_returns_is_refused_with_the_missing_interval(
    ledger: SourceBarLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.config.settings.STARTUP_JOIN_BUDGET_MS", 1_000)
    feed = _JoiningFeed(live=[_bar(_LIVE_FROM)], history=[_history(_J - 30 * _MIN, _J - _MIN)])
    run_feed = _run_feed(feed, ledger)

    with pytest.raises(MarketDataFeedError) as refused:
        await run_feed.recent_closed_bars("SPY", use_rth=True)

    assert refused.value.reason == WARMUP_HISTORY_UNAVAILABLE
    assert feed.fetches > 1  # retried inside the budget
    record = ledger.startup_join(run_id="run-1")
    assert record is not None
    assert (record.reason_code, record.missing_start_ms, record.missing_end_ms) == (
        WARMUP_HISTORY_UNAVAILABLE,
        _J,
        _LIVE_FROM,
    )
    # A refused run retained none of its held live bars.
    assert all(row.provenance != "realtime" for row in ledger.bars(provider="ibkr", symbol="SPY"))


@pytest.mark.asyncio
async def test_a_budget_that_ends_between_attempts_keeps_the_last_answers_interval(
    ledger: SourceBarLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2486: a retry sleep that wakes past the deadline made ``wait_for``
    time out before its attempt could run, and the synthetic timeout refusal
    named no interval -- so the run's record lost the missing interval the
    last answering attempt had just reported (the 1-in-20 flake in the test
    above). The clock is injected here: every sleep advances it past what the
    budget counted, deterministically, and the recorded refusal must still be
    the last answer's."""
    monkeypatch.setattr("app.config.settings.STARTUP_JOIN_BUDGET_MS", 1_000)
    clock = {"now": 1_000_000}
    monkeypatch.setattr(startup_join, "now_ms_utc", lambda: clock["now"])

    async def _sleep_that_overshoots(_seconds: float) -> None:
        clock["now"] += 200  # the event loop wakes 20x past a 10 ms retry delay

    monkeypatch.setattr(startup_join.asyncio, "sleep", _sleep_that_overshoots)

    feed = _JoiningFeed(live=[_bar(_LIVE_FROM)], history=[_history(_J - 30 * _MIN, _J - _MIN)])
    run_feed = _run_feed(feed, ledger)

    with pytest.raises(MarketDataFeedError) as refused:
        await run_feed.recent_closed_bars("SPY", use_rth=True)

    assert isinstance(refused.value, WarmupMinutesMissing)
    assert refused.value.missing_start_ms == _J
    record = ledger.startup_join(run_id="run-1")
    assert record is not None
    assert (record.reason_code, record.missing_start_ms, record.missing_end_ms) == (
        WARMUP_HISTORY_UNAVAILABLE,
        _J,
        _LIVE_FROM,
    )


@pytest.mark.asyncio
async def test_the_deadline_is_fixed_once_and_retries_never_extend_it(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": 1_000_000}
    monkeypatch.setattr(startup_join, "now_ms_utc", lambda: clock["now"])
    deadline = StartupDeadline(not_before_ms=0, deadline_ms=1_000_100)
    attempts = 0

    async def _still_missing() -> None:
        nonlocal attempts
        attempts += 1
        clock["now"] += 40  # each ask costs time the budget already counted
        raise MarketDataFeedError("not yet", reason=WARMUP_HISTORY_UNAVAILABLE)

    with pytest.raises(MarketDataFeedError, match="not yet"):
        await deadline.run(_still_missing, symbol="SPY")

    # Asks end at 1_000_040, 1_000_080 and 1_000_120; the third ends past the
    # deadline, so its refusal is the run's -- a slow ask is not given more time.
    assert attempts == 3
    assert deadline.deadline_ms == 1_000_100


@pytest.mark.asyncio
async def test_an_attempt_still_in_flight_at_the_deadline_is_cancelled_not_waited_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = startup_join.now_ms_utc()
    deadline = StartupDeadline(not_before_ms=0, deadline_ms=now + 50)

    async def _hangs() -> None:
        await asyncio.Event().wait()

    with pytest.raises(MarketDataFeedError) as refused:
        await deadline.run(_hangs, symbol="SPY")

    assert refused.value.reason == WARMUP_HISTORY_UNAVAILABLE
    assert "deadline" in str(refused.value)


@pytest.mark.asyncio
async def test_a_known_unsupported_repair_is_refused_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    deadline = StartupDeadline(not_before_ms=0, deadline_ms=startup_join.now_ms_utc() + 60_000)
    attempts = 0

    async def _after_hours() -> None:
        nonlocal attempts
        attempts += 1
        raise MarketDataFeedError("after hours", reason=RESUME_HOLE_AFTER_HOURS)

    with pytest.raises(MarketDataFeedError):
        await deadline.run(_after_hours, symbol="SPY")

    assert attempts == 1


def test_the_budget_runs_from_when_the_seam_is_known_and_history_waits_to_settle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.config.settings.STARTUP_JOIN_BUDGET_MS", 180_000)
    monkeypatch.setattr("app.config.settings.STARTUP_JOIN_SETTLE_MS", 5_000)

    deadline = StartupDeadline.for_seam(
        StreamSeam(live_from_ms=_LIVE_FROM, joined_minute_start_ms=_J), known_at_ms=_LIVE_FROM + 4_000
    )

    assert (deadline.not_before_ms, deadline.deadline_ms) == (_LIVE_FROM + 5_000, _LIVE_FROM + 184_000)


# ── extended hours ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_extended_run_joining_after_hours_fills_the_joined_minute_from_history(
    ledger: SourceBarLedger,
) -> None:
    """Owner decision Q3: the startup join minute is filled, extended hours included."""
    joined = et_minute_of_day_ms(_DAY, 17 * 60)
    feed = _JoiningFeed(
        live=[_bar(joined + _MIN, phase="POST")],
        history=[_history(joined - 10 * _MIN, joined, phase="POST")],
        joined_ms=joined,
    )
    run_feed = _run_feed(feed, ledger, _EXTENDED)

    warmup = await run_feed.recent_closed_bars("SPY", use_rth=False)

    assert warmup[-1].start_ms == joined


@pytest.mark.asyncio
async def test_a_resumed_extended_run_with_an_after_hours_hole_is_still_refused_at_once(
    ledger: SourceBarLedger,
) -> None:
    """The startup exception covers the joined minute only, never the hole the bot sat through (ADR 0053 §10)."""
    ledger.append(_bar(et_minute_of_day_ms(_DAY, 15 * 60)), run_id="run-0")
    joined = et_minute_of_day_ms(_DAY, 17 * 60)
    feed = _JoiningFeed(
        live=[_bar(joined + _MIN, phase="POST")],
        history=[_history(et_minute_of_day_ms(_DAY, 15 * 60), joined, phase="POST")],
        joined_ms=joined,
    )
    run_feed = _run_feed(feed, ledger, _EXTENDED)

    with pytest.raises(MarketDataFeedError) as refused:
        await run_feed.recent_closed_bars("SPY", use_rth=False)

    assert refused.value.reason == RESUME_HOLE_AFTER_HOURS
    assert feed.fetches == 0
    record = ledger.startup_join(run_id="run-1")
    assert record is not None and record.reason_code == RESUME_HOLE_AFTER_HOURS


# ── the buffer ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_feed_that_dies_during_preparation_fails_the_run_with_its_own_error() -> None:
    async def _dies() -> AsyncIterator[MarketDataBar]:
        raise MarketDataFeedError("socket gone", reason="FEED_DEATH_TEST")
        yield _bar(_J)  # pragma: no cover - makes this an async generator

    retained: list[MarketDataBar] = []

    async def _retain(bar: MarketDataBar, _at: int | None) -> None:
        retained.append(bar)

    buffer = LiveStartBuffer(_dies(), symbol="SPY", retain=_retain)
    buffer.start()

    with pytest.raises(MarketDataFeedError, match="socket gone"):
        await buffer.seam()
    assert retained == []


@pytest.mark.asyncio
async def test_leaving_the_run_feed_closes_a_stream_warmup_opened_but_the_run_never_read(
    ledger: SourceBarLedger,
) -> None:
    """A run that fails between warmup and its first live bar must not leave the subscription open."""
    closed = asyncio.Event()

    class _ClosingFeed(_JoiningFeed):
        async def stream_bars(self, symbol, *, use_rth=True, continuity=None):  # type: ignore[no-untyped-def]
            try:
                async for bar in super().stream_bars(symbol, use_rth=use_rth, continuity=continuity):
                    yield bar
            finally:
                closed.set()

    feed = _ClosingFeed(live=[_bar(_LIVE_FROM)], history=[_history(_J - 5 * _MIN, _J)])

    with pytest.raises(RuntimeError, match="replay failed"):
        async with _run_feed(feed, ledger) as run_feed:
            await run_feed.recent_closed_bars("SPY", use_rth=True)
            raise RuntimeError("replay failed")

    await asyncio.wait_for(closed.wait(), timeout=1)


@pytest.mark.asyncio
async def test_the_run_reads_its_live_stream_once_and_only_for_its_symbol(ledger: SourceBarLedger) -> None:
    feed = _JoiningFeed(live=[_bar(_LIVE_FROM)], history=[_history(_J - 5 * _MIN, _J)])

    async with _run_feed(feed, ledger) as run_feed:
        await run_feed.recent_closed_bars("SPY", use_rth=True)
        with pytest.raises(ValueError, match="may not stream QQQ"):
            await anext(run_feed.stream_bars("QQQ", use_rth=True))
        await _first_live(run_feed, 1)
        with pytest.raises(RuntimeError, match="released once"):
            await anext(run_feed.stream_bars("SPY", use_rth=True))


# ── the record ───────────────────────────────────────────────────────────────


def test_each_startup_outcome_is_stamped_once_and_a_reentered_run_keeps_the_first(
    ledger: SourceBarLedger,
) -> None:
    ledger.record_startup_opened(run_id="run-1", at_ms=_J)
    ledger.record_startup_opened(run_id="run-1", at_ms=_J + 5)
    ledger.record_startup_seam(run_id="run-1", live_from_ms=_LIVE_FROM, joined_minute_start_ms=_J, deadline_ms=_LIVE_FROM + 180_000)
    ledger.record_startup_seam(run_id="run-1", live_from_ms=_LIVE_FROM + _MIN, joined_minute_start_ms=None, deadline_ms=1)
    ledger.mark_startup_history_joined(run_id="run-1", at_ms=_LIVE_FROM + 6_000)
    ledger.mark_startup_ready(run_id="run-1", at_ms=_LIVE_FROM + 7_000)
    ledger.mark_startup_ready(run_id="run-1", at_ms=_LIVE_FROM + 9_000)

    record = ledger.startup_join(run_id="run-1")

    assert record is not None
    assert (record.opened_at_ms, record.live_from_ms, record.deadline_ms) == (_J, _LIVE_FROM, _LIVE_FROM + 180_000)
    assert (record.ready_at_ms, record.refused_at_ms) == (_LIVE_FROM + 7_000, None)


def test_an_out_of_order_startup_stamp_fails_loudly_and_persists_nothing(ledger: SourceBarLedger) -> None:
    ledger.record_startup_opened(run_id="run-1", at_ms=_J)
    ledger.record_startup_seam(run_id="run-1", live_from_ms=_LIVE_FROM, joined_minute_start_ms=_J, deadline_ms=_LIVE_FROM + 1)

    with pytest.raises(sqlite3.IntegrityError, match="history_joined_at_ms"):
        ledger.mark_startup_ready(run_id="run-1", at_ms=_LIVE_FROM + 7_000)

    record = ledger.startup_join(run_id="run-1")
    assert record is not None and record.ready_at_ms is None


@pytest.mark.parametrize(
    "fields",
    [
        {"live_from_ms": 2},
        {"history_joined_at_ms": 2},
        {"live_from_ms": 2, "deadline_ms": 3, "refused_at_ms": 4},
        {"live_from_ms": 2, "deadline_ms": 3, "ready_at_ms": 4},
        {"live_from_ms": 2, "deadline_ms": 3, "missing_start_ms": 1, "missing_end_ms": 2},
        {"live_from_ms": 253_402_300_800_000, "deadline_ms": 3},
    ],
)
def test_contradictory_startup_evidence_is_unrepresentable(fields: dict[str, object]) -> None:
    from pydantic import ValidationError

    from app.services.source_bar_ledger import RetainedStartupJoin

    with pytest.raises(ValidationError):
        RetainedStartupJoin.model_validate({"run_id": "r", "opened_at_ms": 1, **fields})


# ── review fixes ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_recovered_bar_held_through_preparation_is_admitted_on_its_delivery_not_killed_at_release(
    ledger: SourceBarLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review P1: release happens up to the budget later; judging delivery then refused the run."""
    import app.services.feed_continuity_policy as fcp

    recovered = _bar(_LIVE_FROM).model_copy(update={"provenance": "realtime_across_reconnect"})
    feed = _JoiningFeed(live=[recovered], history=[_history(_J - 5 * _MIN, _J)])
    monkeypatch.setattr(startup_join, "now_ms_utc", lambda: recovered.end_ms + 1_000)  # delivered on time
    monkeypatch.setattr(fcp, "now_ms_utc", lambda: recovered.end_ms + 90_000)  # released 90 s later
    run_feed = _run_feed(feed, ledger)

    await run_feed.recent_closed_bars("SPY", use_rth=True)
    live = await _first_live(run_feed, 1)

    assert [bar.start_ms for bar in live] == [_LIVE_FROM]
    assert all(event.kind != "refused" for event in ledger.events(run_id="run-1"))


@pytest.mark.asyncio
async def test_a_pause_pressed_while_the_run_prepared_governs_the_bars_it_held(
    ledger: SourceBarLedger,
) -> None:
    """Review P1: a held bar's mode was captured when the pump pulled it, before the pause."""
    from app.engine.strategy.signal_program import EvaluationMode
    from app.services.bot_runtime import PauseAwareFeed

    gate = asyncio.Event()
    gate.set()  # running when the stream delivers the bar
    source = _JoiningFeed(live=[_bar(_LIVE_FROM)], history=[_history(_J - 5 * _MIN, _J)])
    run_feed = _run_feed(PauseAwareFeed(source, gate), ledger)  # type: ignore[arg-type]

    await run_feed.recent_closed_bars("SPY", use_rth=True)
    gate.clear()  # the operator pauses before the run takes the held bar
    (bar,) = await _first_live(run_feed, 1)

    assert run_feed.evaluation_mode_for(bar) is EvaluationMode.OBSERVE_ONLY


@pytest.mark.asyncio
async def test_a_stream_that_dies_after_the_seam_ends_preparation_with_its_own_error(
    ledger: SourceBarLedger,
) -> None:
    """Review P2: the deadline kept asking history for a dead stream, then called it a history refusal."""

    class _DiesAfterJoining(_JoiningFeed):
        async def stream_bars(self, symbol, *, use_rth=True, continuity=None):  # type: ignore[no-untyped-def]
            await continuity.record_event(
                FeedContinuityEvent(
                    kind="gap",
                    cause="stream_joined",
                    feed_id=self.feed_id,
                    symbol=symbol,
                    observed_at_ms=_LIVE_FROM,
                    window_start_ms=_J,
                    window_end_ms=_LIVE_FROM,
                )
            )
            raise MarketDataFeedError("socket gone", reason="SOCKET_GONE_TEST")
            yield _bar(_LIVE_FROM)  # pragma: no cover - makes this an async generator

    feed = _DiesAfterJoining(live=[], history=[_history(_J - 5 * _MIN, _J - _MIN)])  # joined minute never arrives
    run_feed = _run_feed(feed, ledger)

    with pytest.raises(MarketDataFeedError, match="socket gone"):
        await run_feed.recent_closed_bars("SPY", use_rth=True)

    assert feed.fetches <= 1
    record = ledger.startup_join(run_id="run-1")
    assert record is not None and record.refused_at_ms is None and record.ready_at_ms is None


@pytest.mark.asyncio
async def test_closing_the_stream_does_not_swallow_the_callers_own_cancellation() -> None:
    """Review P2: a Stop landing while the stream shuts down must still stop the run."""
    shutting_down = asyncio.Event()

    async def _slow_to_close() -> AsyncIterator[MarketDataBar]:
        try:
            yield _bar(_J)
            await asyncio.Event().wait()
        finally:
            shutting_down.set()
            await asyncio.sleep(0.2)

    async def _retain(_bar: MarketDataBar, _at: int | None) -> None:
        return None

    buffer = LiveStartBuffer(_slow_to_close(), symbol="SPY", retain=_retain)
    buffer.start()
    await buffer.seam()

    closing = asyncio.create_task(buffer.aclose())
    await shutting_down.wait()
    closing.cancel()

    with pytest.raises(asyncio.CancelledError):
        await closing


@pytest.mark.asyncio
async def test_an_earlier_refusal_is_not_terminal_evidence_when_a_later_attempt_hangs(
    ledger: SourceBarLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2486 review: only the between-attempts overshoot re-raises the last
    answering refusal. Once a later attempt *has* started, its outcome is the
    run's even when it never answers — the earlier attempt's interval is
    stale evidence by then, so the timeout refusal, naming no interval, is
    the honest record."""
    monkeypatch.setattr("app.config.settings.STARTUP_JOIN_BUDGET_MS", 1_000)
    clock = {"now": 1_000_000}
    monkeypatch.setattr(startup_join, "now_ms_utc", lambda: clock["now"])

    async def _sleep_just_inside_the_budget(seconds: float) -> None:
        clock["now"] += 990  # wake at 1_000_990: a second attempt may start

    monkeypatch.setattr(startup_join.asyncio, "sleep", _sleep_just_inside_the_budget)

    async def _no_settle(instant_ms: int) -> None:
        return None  # the settle wait is not the budget's retry sleep

    monkeypatch.setattr(startup_join, "_sleep_until", _no_settle)

    feed = _JoiningFeed(live=[_bar(_LIVE_FROM)], history=[_history(_J - 30 * _MIN, _J - _MIN)])
    original_recent = feed.recent_closed_bars

    async def _hangs_on_the_second_ask(symbol: str, **kwargs: object) -> list[MarketDataBar]:
        if feed.fetches >= 1:
            await asyncio.Event().wait()  # in flight past the deadline
        return await original_recent(symbol, **kwargs)  # type: ignore[arg-type]

    feed.recent_closed_bars = _hangs_on_the_second_ask  # type: ignore[method-assign]
    run_feed = _run_feed(feed, ledger)

    with pytest.raises(MarketDataFeedError) as refused:
        await run_feed.recent_closed_bars("SPY", use_rth=True)

    assert not isinstance(refused.value, WarmupMinutesMissing)
    assert refused.value.reason == WARMUP_HISTORY_UNAVAILABLE
    assert "still being fetched" in str(refused.value)
    record = ledger.startup_join(run_id="run-1")
    assert record is not None
    assert record.missing_start_ms is None and record.missing_end_ms is None
