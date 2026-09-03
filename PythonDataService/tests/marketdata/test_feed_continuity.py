"""Fail-closed continuity inside IbkrMarketDataFeed.stream_bars (spec §4.2, §4.5, §8)."""

from __future__ import annotations

from contextlib import aclosing
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock

import pytest

from app.broker.ibkr.bars import (
    IBKRBarInterrupted,
    IBKRBarStreamError,
    IBKRBarSubscriptionStalled,
)
from app.broker.ibkr.client import NotConnectedError
from app.marketdata import ibkr_feed as feed_module
from app.marketdata.feed import (
    ContinuityEventRef,
    ContinuityPolicy,
    FeedContinuityEvent,
    MarketDataFeedError,
    SubstitutionGrant,
    SubstitutionRefusal,
)
from app.marketdata.ibkr_feed import IbkrMarketDataFeed

_MINUTE0 = 1_788_375_600_000  # 2026-09-02 15:00:00 ET
_TF = 900_000


def _ibkr_bar(start_ms: int, *, contribution_count: int = 12, spans_interruption: bool = False, phase: str = "RTH"):
    return SimpleNamespace(
        symbol="SPY", start_ms=start_ms, end_ms=start_ms + 60_000,
        open=Decimal("1"), high=Decimal("1"), low=Decimal("1"), close=Decimal("1"), volume=12,
        fetched_at_ms=start_ms + 60_000, source="ibkr", provenance="ibkr_realtime", venue="ARCA",
        session_phase=phase, use_rth=True, contribution_count=contribution_count,
        spans_interruption=spans_interruption,
    )


def _raw_5s(source_ms: int, close: str = "1") -> SimpleNamespace:
    """One raw IBKR 5-second bar, in the shape ``MinuteAssembler.feed`` consumes.

    Scripting these instead of pre-built minutes makes the feed's own assembler
    do the real work: the open minute, its contribution count, its generation
    set and ``flush_if_complete`` are then facts, not fixtures.
    """
    return SimpleNamespace(
        time=datetime.fromtimestamp(source_ms / 1000, tz=UTC),
        open=Decimal(close), high=Decimal(close), low=Decimal(close), close=Decimal(close),
        volume=1,
    )


def _rth_minute_raw(minute_start_ms: int, seconds: range | tuple[int, ...]) -> list[SimpleNamespace]:
    return [_raw_5s(minute_start_ms + second * 1_000) for second in seconds]


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[FeedContinuityEvent] = []
        self.fail = False

    async def __call__(self, event: FeedContinuityEvent) -> ContinuityEventRef:
        if self.fail:
            raise OSError("journal unwritable")
        self.events.append(event)
        return ContinuityEventRef(run_id="run-1", evidence_seq=len(self.events))


def _next_trigger(last_end: int) -> int:
    # Fake decision clock: triggers at k * 15 min + 60 s; smallest one strictly after last_end.
    candidate = (last_end // _TF) * _TF + 60_000
    return candidate if candidate > last_end else candidate + _TF


def _policy(sink: _RecordingSink, *, grant=None) -> ContinuityPolicy:
    return ContinuityPolicy(
        decision_session="rth",
        next_trigger_ms=_next_trigger,
        substitution_grant=grant or (lambda s, e: SubstitutionRefusal(reason="SUBSTITUTION_NOT_AUTHORIZED")),
        record_event=sink,
    )


def _client(*, generation: int = 1) -> MagicMock:
    client = MagicMock()
    client.is_connected.return_value = True
    client.connection_lost = False
    client.connection_generation = generation
    client.settings = SimpleNamespace(feed_continuity_enabled=True)
    return client


class _Source:
    """Scripted stream_minute_bars: each call yields its scripted items; an exception item is raised.

    Three item shapes, matching what the real stream can produce:

    * an exception — raised, ending that call;
    * a raw 5-second bar (has ``time``) — folded into the caller's shared
      ``assembler`` under this call's connection generation, exactly as
      ``stream_minute_bars`` does, and the minute it closes (if any) is yielded;
    * a pre-built minute (has ``start_ms``) — yielded as is, for the cases where
      the assembler's internals are not what is under test.
    """

    def __init__(self, *calls: list, same_generation: bool = False) -> None:
        self.calls = list(calls)
        self.assemblers: list = []
        self.invocations = 0
        #: Calls whose generator has finished or been closed.
        self.closed = 0
        #: A 1100 -> 1102 soft restore keeps the socket and its generation, so
        #: every call folds under generation 1 instead of a fresh one.
        self.same_generation = same_generation

    def __call__(self, _client, _symbol, *, use_rth=True, on_source_bar=None, assembler=None, **_kw):
        self.assemblers.append(assembler)
        script = self.calls[self.invocations] if self.invocations < len(self.calls) else []
        self.invocations += 1
        generation = 1 if self.same_generation else self.invocations

        async def _gen():
            try:
                for item in script:
                    if isinstance(item, BaseException):
                        raise item
                    if hasattr(item, "time"):
                        emitted = assembler.feed(
                            item,
                            symbol=_symbol,
                            generation=generation,
                            venue="ARCA",
                            use_rth=use_rth,
                        )
                        if on_source_bar is not None and assembler.last_source_ms is not None:
                            on_source_bar(assembler.last_source_ms)
                        if emitted is not None:
                            yield emitted
                        continue
                    if on_source_bar is not None:
                        on_source_bar(item.start_ms)
                    yield item
            finally:
                self.closed += 1

        return _gen()


@pytest.fixture(autouse=True)
def _no_monitor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.marketdata.ibkr_continuity.get_monitor", lambda: None)
    monkeypatch.setattr("app.marketdata.ibkr_continuity.now_ms_utc", lambda: _MINUTE0 + 45_000)
    monkeypatch.setattr("app.marketdata.ibkr_continuity.session_state_at_ms",
                        lambda now_ms: SimpleNamespace(phase="RTH" if now_ms >= _MINUTE0 - 3_600_000 else "PRE"))


async def _collect(feed: IbkrMarketDataFeed, policy: ContinuityPolicy, n: int) -> list:
    out = []
    async for bar in feed.stream_bars("SPY", continuity=policy):
        out.append(bar)
        if len(out) == n:
            break
    return out


async def test_count_complete_interruption_resumes_on_the_same_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _RecordingSink()
    source = _Source(
        [_ibkr_bar(_MINUTE0 - 60_000), IBKRBarInterrupted("socket", cause="socket_down")],
        [_ibkr_bar(_MINUTE0, contribution_count=12, spans_interruption=True), _ibkr_bar(_MINUTE0 + 60_000)],
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    feed = IbkrMarketDataFeed(_client())

    bars = await _collect(feed, _policy(sink), 3)

    assert [b.provenance for b in bars] == ["realtime", "realtime_across_reconnect", "realtime"]
    assert [e.kind for e in sink.events] == ["interruption", "recovered"]
    assert sink.events[0].cause == "socket_down" and sink.events[0].last_delivered_end_ms == _MINUTE0
    assert bars[1].continuity_event_ref == "run-1:2"
    assert source.assemblers[0] is source.assemblers[1]  # the same assembler survived


async def test_interruption_before_any_delivered_bar_is_fatal_as_today(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _RecordingSink()
    source = _Source([IBKRBarInterrupted("x", cause="socket_down")], [_ibkr_bar(_MINUTE0)])
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    feed = IbkrMarketDataFeed(_client())
    with pytest.raises(MarketDataFeedError) as excinfo:
        await _collect(feed, _policy(sink), 1)
    assert excinfo.value.reason is None
    assert sink.events == []


async def test_a_complete_open_minute_is_flushed_and_delivered_before_the_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing was delivered yet, but the assembler holds a whole minute.

    The interruption adopts the open minute as the continuity anchor instead of
    failing, and the minute is flushed and delivered *between* the interruption
    and the recovery — the evidence a consumer replays must show it that way.
    """
    sink = _RecordingSink()
    source = _Source(
        [*_rth_minute_raw(_MINUTE0, range(0, 60, 5)), IBKRBarInterrupted("x", cause="socket_down")],
        [_ibkr_bar(_MINUTE0 + 60_000)],
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    feed = IbkrMarketDataFeed(_client())

    observed: list[tuple[int, str, tuple[str, ...]]] = []
    async for bar in feed.stream_bars("SPY", continuity=_policy(sink)):
        observed.append((bar.start_ms, bar.provenance, tuple(e.kind for e in sink.events)))
        if len(observed) == 2:
            break

    # The bar after the recovery is the minute the resubscribed line landed
    # in, so it carries the recovery that explains it.
    assert observed == [
        (_MINUTE0, "realtime", ("interruption",)),
        (_MINUTE0 + 60_000, "realtime_across_reconnect", ("interruption", "recovered")),
    ]
    # The flushed minute is a delivered bar, so the deadline the run promises
    # is derived from where it left the watermark, not from where the
    # interruption found it (ruling P6).
    assert sink.events[0].last_delivered_end_ms == _MINUTE0 + 60_000
    assert sink.events[0].deadline_ms == _MINUTE0 + 980_000


async def test_a_minute_stitched_by_the_real_assembler_crosses_the_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nine 5-second bars on one socket, three on the next, one delivered minute.

    Also the positive edge of ruling P9: the interruption *touched* this minute
    (it was open and short when delivery stopped), and the touched rule still
    delivers it, because the reconnect proved it complete by count.
    """
    sink = _RecordingSink()
    source = _Source(
        [*_rth_minute_raw(_MINUTE0, range(0, 45, 5)), IBKRBarInterrupted("x", cause="socket_down")],
        [*_rth_minute_raw(_MINUTE0, range(45, 60, 5)), _raw_5s(_MINUTE0 + 60_000)],
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    feed = IbkrMarketDataFeed(_client())

    bars = await _collect(feed, _policy(sink), 1)

    assert source.assemblers[0] is source.assemblers[1]
    assert [b.start_ms for b in bars] == [_MINUTE0]
    assert bars[0].provenance == "realtime_across_reconnect"
    assert bars[0].continuity_event_ref == "run-1:2"
    assert bars[0].volume == 12  # every contribution from both sockets survived
    assert [e.kind for e in sink.events] == ["interruption", "recovered"]


async def test_an_interruption_outliving_the_open_minute_refuses_the_short_minute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ruling P9: a 9/12 minute is never delivered just because one socket built it.

    The reconnect's first source bar lands in the *next* minute, so 15:00 emits
    with one generation's contributions and ``spans_interruption=False``.
    Nothing on the bar says it was cut short; only the loop saw it open when
    delivery stopped, so only the loop can refuse it.
    """
    sink = _RecordingSink()
    source = _Source(
        [
            _ibkr_bar(_MINUTE0 - 60_000),
            *_rth_minute_raw(_MINUTE0, range(0, 45, 5)),  # 9 of 12
            IBKRBarInterrupted("x", cause="socket_down"),
        ],
        [*_rth_minute_raw(_MINUTE0 + 60_000, range(0, 15, 5))],
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    feed = IbkrMarketDataFeed(_client())

    delivered: list = []
    with pytest.raises(MarketDataFeedError) as excinfo:
        async for bar in feed.stream_bars("SPY", continuity=_policy(sink)):
            delivered.append(bar)

    assert excinfo.value.reason == "SUBSTITUTION_NOT_AUTHORIZED"
    assert [b.start_ms for b in delivered] == [_MINUTE0 - 60_000]  # the 9/12 minute never shipped
    assert [e.kind for e in sink.events] == ["interruption", "recovered", "refused"]
    assert sink.events[-1].window_start_ms == _MINUTE0
    assert sink.events[-1].window_end_ms == _MINUTE0 + 60_000
    assert sink.events[-1].contribution_count == 9


async def test_a_stall_outliving_the_open_minute_refuses_the_short_minute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stall path reaches the same refusal — it is the same interruption choreography."""
    sink = _RecordingSink()
    source = _Source(
        [
            _ibkr_bar(_MINUTE0 - 60_000),
            *_rth_minute_raw(_MINUTE0, range(0, 45, 5)),
            IBKRBarSubscriptionStalled("stalled"),
        ],
        [*_rth_minute_raw(_MINUTE0 + 60_000, range(0, 15, 5))],
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    feed = IbkrMarketDataFeed(_client())

    with pytest.raises(MarketDataFeedError) as excinfo:
        await _collect(feed, _policy(sink), 2)

    assert excinfo.value.reason == "SUBSTITUTION_NOT_AUTHORIZED"
    assert sink.events[0].cause == "stall"
    assert sink.events[-1].kind == "refused" and sink.events[-1].contribution_count == 9


async def test_an_ordinary_gap_with_no_interruption_is_delivered_without_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ruling P11: a gap the live stream simply had (no interruption) stays non-fatal.

    The port promises ordinary bar gaps are silent (spec §6). Scanning for
    wholly-missed minutes on *every* emitted bar would turn a quiet two-minute
    RTH stretch into a run-ending refusal.
    """
    sink = _RecordingSink()
    source = _Source([_ibkr_bar(_MINUTE0), _ibkr_bar(_MINUTE0 + 180_000)])
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    feed = IbkrMarketDataFeed(_client())

    bars = await _collect(feed, _policy(sink), 2)

    assert [b.start_ms for b in bars] == [_MINUTE0, _MINUTE0 + 180_000]
    assert sink.events == []


async def test_contiguous_missed_minutes_are_offered_as_one_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ruling P11: one episode, one window, one event — not one per minute."""
    sink = _RecordingSink()
    source = _Source(
        [_ibkr_bar(_MINUTE0 - 60_000), IBKRBarInterrupted("x", cause="socket_down")],
        [_ibkr_bar(_MINUTE0 + 180_000)],  # 15:00, 15:01 and 15:02 never assembled
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    feed = IbkrMarketDataFeed(_client())

    with pytest.raises(MarketDataFeedError) as excinfo:
        await _collect(feed, _policy(sink), 2)

    assert excinfo.value.reason == "SUBSTITUTION_NOT_AUTHORIZED"
    refusals = [e for e in sink.events if e.kind == "refused"]
    assert len(refusals) == 1
    assert refusals[0].window_start_ms == _MINUTE0
    assert refusals[0].window_end_ms - refusals[0].window_start_ms == 180_000
    assert refusals[0].contribution_count is None  # nothing was ever assembled for it


async def test_a_missed_window_outside_the_session_is_one_gap_and_the_run_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = _RecordingSink()
    pre = _MINUTE0 - 8 * 3_600_000  # 07:00 ET
    source = _Source(
        [_ibkr_bar(pre - 60_000, phase="PRE"), IBKRBarInterrupted("x", cause="socket_down")],
        [_ibkr_bar(pre + 180_000, phase="PRE")],
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    monkeypatch.setattr("app.marketdata.ibkr_continuity.now_ms_utc", lambda: pre + 30_000)
    feed = IbkrMarketDataFeed(_client())

    bars = await _collect(feed, _policy(sink), 2)

    assert [b.start_ms for b in bars] == [pre - 60_000, pre + 180_000]
    gaps = [e for e in sink.events if e.kind == "gap"]
    assert len(gaps) == 1
    assert (gaps[0].window_start_ms, gaps[0].window_end_ms) == (pre, pre + 180_000)


async def test_an_omitted_gap_does_not_spend_the_scan_the_next_bar_still_needs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interruption straddling the session open must not swallow RTH minutes.

    The run streams with ``use_rth=False``, so an RTH-sealed binding's loop sees
    pre-market minutes too. When the first bar after a recovery is a touched
    pre-market minute it resolves as a non-fatal ``gap`` and is never delivered
    — the interruption is still open. If that omitted bar spent the
    missed-window scan, the RTH minutes behind the *next* bar would vanish with
    no ``refused`` event and no journal entry at all, and the run would carry on
    deciding as though nothing had been lost.
    """
    sink = _RecordingSink()
    open_ms = _MINUTE0 - 3_600_000  # the fake session boundary this module uses
    source = _Source(
        [_ibkr_bar(open_ms - 120_000, phase="PRE"), IBKRBarInterrupted("x", cause="socket_down")],
        [
            # Touched, short, and outside the decision session: omitted as a gap.
            _ibkr_bar(open_ms - 60_000, phase="PRE", contribution_count=9, spans_interruption=True),
            # Three RTH minutes later — 09:30, 09:31 and 09:32 never assembled.
            _ibkr_bar(open_ms + 180_000),
        ],
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    # Inside the deadline the pre-market bar anchors (open_ms + 80_000).
    monkeypatch.setattr("app.marketdata.ibkr_continuity.now_ms_utc", lambda: open_ms + 30_000)
    feed = IbkrMarketDataFeed(_client())

    delivered: list = []
    with pytest.raises(MarketDataFeedError) as excinfo:
        async for bar in feed.stream_bars("SPY", continuity=_policy(sink)):
            delivered.append(bar)

    assert excinfo.value.reason == "SUBSTITUTION_NOT_AUTHORIZED"
    assert [b.start_ms for b in delivered] == [open_ms - 120_000]
    assert [e.kind for e in sink.events] == ["interruption", "recovered", "gap", "refused"]
    assert (sink.events[2].window_start_ms, sink.events[2].window_end_ms) == (
        open_ms - 60_000,
        open_ms,
    )
    refused = sink.events[3]
    assert (refused.window_start_ms, refused.window_end_ms) == (open_ms, open_ms + 180_000)
    assert refused.contribution_count is None


async def test_a_missed_window_straddling_the_session_open_is_split_at_the_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside or outside is decided per window, so a straddle is two facts, not one."""
    sink = _RecordingSink()
    open_ms = _MINUTE0 - 3_600_000  # the fake session boundary this module's fixture uses
    source = _Source(
        [_ibkr_bar(open_ms - 120_000, phase="PRE"), IBKRBarInterrupted("x", cause="socket_down")],
        [_ibkr_bar(open_ms + 120_000)],
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    # Inside the deadline the pre-market bar anchors (open_ms + 80_000).
    monkeypatch.setattr("app.marketdata.ibkr_continuity.now_ms_utc", lambda: open_ms + 30_000)
    feed = IbkrMarketDataFeed(_client())

    with pytest.raises(MarketDataFeedError) as excinfo:
        await _collect(feed, _policy(sink), 2)

    assert excinfo.value.reason == "SUBSTITUTION_NOT_AUTHORIZED"
    assert [e.kind for e in sink.events] == ["interruption", "recovered", "gap", "refused"]
    assert (sink.events[2].window_start_ms, sink.events[2].window_end_ms) == (open_ms - 60_000, open_ms)
    assert (sink.events[3].window_start_ms, sink.events[3].window_end_ms) == (open_ms, open_ms + 120_000)


async def test_a_flushed_trigger_bar_earns_the_deadline_the_interruption_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reconnect still inside its window must not be refused (ruling P6).

    The flushed minute closes exactly on the fake clock's 15:01 trigger, so the
    consumer's next decision bar — and the deadline protecting it — moved a
    whole interval out. A deadline anchored on the pre-flush watermark would
    expire at 15:01:20 and kill a socket that returned with minutes to spare.
    """
    assert _next_trigger(_MINUTE0 + 60_000 - 1) == _MINUTE0 + 60_000  # it is a trigger instant
    sink = _RecordingSink()
    source = _Source(
        [*_rth_minute_raw(_MINUTE0, range(0, 60, 5)), IBKRBarInterrupted("x", cause="socket_down")],
        [_ibkr_bar(_MINUTE0 + 60_000)],
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    client = _client()
    client.is_connected.return_value = False
    # Past the pre-flush deadline (15:01:20); far inside the post-flush one.
    monkeypatch.setattr("app.marketdata.ibkr_continuity.now_ms_utc", lambda: _MINUTE0 + 80_001)

    async def _reconnect(_seconds: float) -> None:
        client.is_connected.return_value = True

    monkeypatch.setattr("app.marketdata.ibkr_continuity.asyncio.sleep", _reconnect)
    feed = IbkrMarketDataFeed(client)

    bars = await _collect(feed, _policy(sink), 2)

    assert [b.start_ms for b in bars] == [_MINUTE0, _MINUTE0 + 60_000]
    assert [e.kind for e in sink.events] == ["interruption", "recovered"]
    assert sink.events[0].deadline_ms == _MINUTE0 + 980_000


async def test_the_wait_refuses_exactly_at_the_deadline_the_interruption_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other edge: the wait never outlives the deadline the journal promised."""
    sink = _RecordingSink()
    source = _Source(
        [*_rth_minute_raw(_MINUTE0, range(0, 60, 5)), IBKRBarInterrupted("x", cause="socket_down")],
        [],
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    client = _client()
    client.is_connected.return_value = False
    monkeypatch.setattr("app.marketdata.ibkr_continuity.now_ms_utc", lambda: _MINUTE0 + 980_001)

    async def _never_recovers(_seconds: float) -> None:
        raise AssertionError("the wait outlived the deadline the interruption event recorded")

    monkeypatch.setattr("app.marketdata.ibkr_continuity.asyncio.sleep", _never_recovers)
    feed = IbkrMarketDataFeed(client)

    with pytest.raises(MarketDataFeedError) as excinfo:
        await _collect(feed, _policy(sink), 2)

    assert excinfo.value.reason == "DECISION_BAR_MISSED"
    assert [e.kind for e in sink.events] == ["interruption", "refused"]
    assert sink.events[0].deadline_ms == sink.events[-1].deadline_ms == _MINUTE0 + 980_000
    assert sink.events[-1].last_delivered_end_ms == _MINUTE0 + 60_000


async def test_incomplete_minute_inside_rth_is_refused_with_the_grant_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _RecordingSink()
    source = _Source(
        [_ibkr_bar(_MINUTE0 - 60_000), IBKRBarInterrupted("x", cause="socket_down")],
        [_ibkr_bar(_MINUTE0, contribution_count=11, spans_interruption=True)],
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    feed = IbkrMarketDataFeed(_client())
    with pytest.raises(MarketDataFeedError) as excinfo:
        await _collect(feed, _policy(sink), 2)
    assert excinfo.value.reason == "SUBSTITUTION_NOT_AUTHORIZED"
    assert [e.kind for e in sink.events] == ["interruption", "recovered", "refused"]
    assert sink.events[-1].window_start_ms == _MINUTE0 and sink.events[-1].reason == "SUBSTITUTION_NOT_AUTHORIZED"


async def test_a_grant_is_never_honored_in_this_slice(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _RecordingSink()
    source = _Source(
        [_ibkr_bar(_MINUTE0 - 60_000), IBKRBarInterrupted("x", cause="socket_down")],
        [_ibkr_bar(_MINUTE0, contribution_count=11, spans_interruption=True)],
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    feed = IbkrMarketDataFeed(_client())

    def _grant(s: int, e: int) -> SubstitutionGrant:
        return SubstitutionGrant(authorization_id="a", window_start_ms=s, window_end_ms=e)

    with pytest.raises(MarketDataFeedError) as excinfo:
        await _collect(feed, _policy(sink, grant=_grant), 2)
    assert excinfo.value.reason == "SUBSTITUTION_PATH_UNAVAILABLE"


async def test_unresolvable_minute_outside_rth_is_a_gap_and_the_run_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _RecordingSink()
    pre = _MINUTE0 - 8 * 3_600_000  # 07:00 ET
    source = _Source(
        [_ibkr_bar(pre - 60_000, phase="PRE"), IBKRBarInterrupted("x", cause="socket_down")],
        [_ibkr_bar(pre, contribution_count=3, spans_interruption=True, phase="PRE"), _ibkr_bar(pre + 60_000, phase="PRE")],
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    monkeypatch.setattr("app.marketdata.ibkr_continuity.now_ms_utc", lambda: pre + 30_000)
    feed = IbkrMarketDataFeed(_client())
    bars = await _collect(feed, _policy(sink), 2)
    assert [b.start_ms for b in bars] == [pre - 60_000, pre + 60_000]
    assert [e.kind for e in sink.events] == ["interruption", "recovered", "gap"]
    assert sink.events[-1].window_start_ms == pre


async def test_a_touched_pre_market_minute_holding_every_print_is_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Twelve contributions is every print a minute can hold, in any session phase.

    The count test is undefined outside RTH only for a *short* minute, where
    sparse bars are normal; a stitched pre-market minute holding all twelve is
    complete and is delivered, not omitted (implementation review, round 7).
    """
    sink = _RecordingSink()
    pre = _MINUTE0 - 8 * 3_600_000  # 07:00 ET
    source = _Source(
        [_ibkr_bar(pre - 60_000, phase="PRE"), IBKRBarInterrupted("x", cause="socket_down")],
        [_ibkr_bar(pre, contribution_count=12, spans_interruption=True, phase="PRE")],
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    monkeypatch.setattr("app.marketdata.ibkr_continuity.now_ms_utc", lambda: pre + 30_000)
    feed = IbkrMarketDataFeed(_client())

    bars = await _collect(feed, _policy(sink), 2)

    assert [(b.start_ms, b.provenance) for b in bars] == [
        (pre - 60_000, "realtime"),
        (pre, "realtime_across_reconnect"),
    ]
    assert [e.kind for e in sink.events] == ["interruption", "recovered"]


async def test_wholly_missed_minutes_are_resolved_before_the_next_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _RecordingSink()
    source = _Source(
        [_ibkr_bar(_MINUTE0 - 60_000), IBKRBarInterrupted("x", cause="socket_down")],
        [_ibkr_bar(_MINUTE0 + 120_000)],  # 15:00 and 15:01 never assembled at all
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    feed = IbkrMarketDataFeed(_client())
    with pytest.raises(MarketDataFeedError) as excinfo:
        await _collect(feed, _policy(sink), 2)
    assert excinfo.value.reason == "SUBSTITUTION_NOT_AUTHORIZED"
    assert sink.events[-1].kind == "refused" and sink.events[-1].window_start_ms == _MINUTE0


async def test_deadline_passing_during_the_wait_is_decision_bar_missed(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _RecordingSink()
    source = _Source([_ibkr_bar(_MINUTE0 - 60_000), IBKRBarInterrupted("x", cause="socket_down")], [])
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    client = _client()
    client.is_connected.return_value = False
    monkeypatch.setattr("app.marketdata.ibkr_continuity.now_ms_utc", lambda: _MINUTE0 + 60_000 + 20_001)
    feed = IbkrMarketDataFeed(client)
    with pytest.raises(MarketDataFeedError) as excinfo:
        await _collect(feed, _policy(sink), 2)
    assert excinfo.value.reason == "DECISION_BAR_MISSED"
    assert sink.events[-1].kind == "refused" and sink.events[-1].reason == "DECISION_BAR_MISSED"


def _restore_after(client: MagicMock, attribute: str):
    async def _sleep(_seconds: float) -> None:
        setattr(client, attribute, False)

    return _sleep


async def test_soft_loss_1100_waits_for_restore_then_resumes(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _RecordingSink()
    source = _Source(
        [_ibkr_bar(_MINUTE0 - 60_000), IBKRBarInterrupted("1100", cause="soft_loss_1100")],
        [_ibkr_bar(_MINUTE0, contribution_count=12, spans_interruption=True)],
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    client = _client()
    client.connection_lost = True
    monkeypatch.setattr("app.marketdata.ibkr_continuity.asyncio.sleep", _restore_after(client, "connection_lost"))
    feed = IbkrMarketDataFeed(client)
    bars = await _collect(feed, _policy(sink), 2)
    assert bars[1].provenance == "realtime_across_reconnect"
    assert sink.events[0].cause == "soft_loss_1100"


async def test_stall_enters_the_same_choreography(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _RecordingSink()
    source = _Source(
        [_ibkr_bar(_MINUTE0 - 60_000), IBKRBarSubscriptionStalled("stalled")],
        [_ibkr_bar(_MINUTE0, contribution_count=12, spans_interruption=True)],
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    feed = IbkrMarketDataFeed(_client())
    bars = await _collect(feed, _policy(sink), 2)
    assert bars[1].provenance == "realtime_across_reconnect"
    assert sink.events[0].cause == "stall"


async def test_sink_failure_is_fatal_and_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _RecordingSink()
    sink.fail = True
    source = _Source([_ibkr_bar(_MINUTE0 - 60_000), IBKRBarInterrupted("x", cause="socket_down")], [])
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    feed = IbkrMarketDataFeed(_client())
    with pytest.raises(MarketDataFeedError) as excinfo:
        await _collect(feed, _policy(sink), 2)
    assert excinfo.value.reason == "CONTINUITY_EVIDENCE_UNWRITABLE"


async def test_sink_failure_withholds_the_bar_the_flush_had_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No delivered bar may precede the evidence explaining it (spec rule 9).

    The flush now runs before the ``interruption`` event is written, so the bar
    it produces is held until that write succeeds. When it cannot, the run dies
    having delivered nothing.
    """
    sink = _RecordingSink()
    sink.fail = True
    source = _Source(
        [*_rth_minute_raw(_MINUTE0, range(0, 60, 5)), IBKRBarInterrupted("x", cause="socket_down")],
        [_ibkr_bar(_MINUTE0 + 60_000)],
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    feed = IbkrMarketDataFeed(_client())

    delivered: list = []
    with pytest.raises(MarketDataFeedError) as excinfo:
        async for bar in feed.stream_bars("SPY", continuity=_policy(sink)):
            delivered.append(bar)

    assert excinfo.value.reason == "CONTINUITY_EVIDENCE_UNWRITABLE"
    assert delivered == []


async def test_not_connected_on_reentry_is_a_second_episode_under_the_same_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resubscribe can race the reconnect (implementation review, finding 3).

    The generation the first recovery named died before it delivered anything.
    The journal must show that -- a second ``interruption``/``recovered`` pair,
    from generation 2 to 3 -- under the deadline the first interruption
    anchored, because no bar was delivered in between; and the bar that finally
    arrives is explained by the recovery that actually delivered it.
    """
    sink = _RecordingSink()
    source = _Source(
        [_ibkr_bar(_MINUTE0 - 60_000), IBKRBarInterrupted("x", cause="socket_down")],
        [NotConnectedError("IBKR client is not connected")],
        [_ibkr_bar(_MINUTE0, contribution_count=12, spans_interruption=True)],
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    client = _client()
    type(client).connection_generation = PropertyMock(side_effect=[1, 2, 3])
    feed = IbkrMarketDataFeed(client)

    bars = await _collect(feed, _policy(sink), 2)

    assert source.invocations == 3
    assert bars[1].provenance == "realtime_across_reconnect"
    assert [(e.kind, e.generation_from, e.generation_to) for e in sink.events] == [
        ("interruption", 1, None),
        ("recovered", 1, 2),
        ("interruption", 2, None),
        ("recovered", 2, 3),
    ]
    assert sink.events[2].cause == "socket_down"
    assert {e.deadline_ms for e in sink.events if e.kind == "interruption"} == {_MINUTE0 + 80_000}
    assert bars[1].continuity_event_ref == ContinuityEventRef(run_id="run-1", evidence_seq=4).ref()


async def test_not_connected_before_any_delivered_bar_is_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _RecordingSink()
    source = _Source([NotConnectedError("IBKR client is not connected")])
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    feed = IbkrMarketDataFeed(_client())
    with pytest.raises(MarketDataFeedError) as excinfo:
        await _collect(feed, _policy(sink), 1)
    assert excinfo.value.reason is None
    assert sink.events == []


async def test_an_unrelated_bar_stream_error_stays_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a survivable interruption is recovered; an invariant violation still kills the stream."""
    sink = _RecordingSink()
    source = _Source(
        [_ibkr_bar(_MINUTE0 - 60_000), IBKRBarStreamError("Non-monotonic IBKR 5-second bar timestamp")],
        [_ibkr_bar(_MINUTE0)],
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    feed = IbkrMarketDataFeed(_client())
    with pytest.raises(MarketDataFeedError) as excinfo:
        await _collect(feed, _policy(sink), 2)
    assert excinfo.value.reason is None
    assert source.invocations == 1
    assert sink.events == []


async def test_a_recovering_monitor_holds_delivery_until_it_reports_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = _RecordingSink()
    source = _Source(
        [_ibkr_bar(_MINUTE0 - 60_000), IBKRBarInterrupted("x", cause="socket_down")],
        [_ibkr_bar(_MINUTE0, contribution_count=12, spans_interruption=True)],
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    monitor = SimpleNamespace(recovery_state="RECONNECTING")
    monkeypatch.setattr("app.marketdata.ibkr_continuity.get_monitor", lambda: monitor)

    async def _heal(_seconds: float) -> None:
        monitor.recovery_state = "HEALTHY"

    monkeypatch.setattr("app.marketdata.ibkr_continuity.asyncio.sleep", _heal)
    feed = IbkrMarketDataFeed(_client())

    bars = await _collect(feed, _policy(sink), 2)

    assert monitor.recovery_state == "HEALTHY"
    assert bars[1].provenance == "realtime_across_reconnect"


async def test_the_landing_minute_after_a_complete_flush_must_prove_itself_by_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reconnect landing at :10 loses two prints (implementation review, finding 1).

    The open minute was complete and flushed, so nothing records it as touched;
    the landing minute holds one generation's contributions, so
    ``spans_interruption`` reads false. Only the loop can see where the
    resubscribed line landed (spec §4.2 rule 4) -- without that fact a 10/12
    minute was delivered as ``realtime`` inside the decision session.
    """
    sink = _RecordingSink()
    source = _Source(
        [*_rth_minute_raw(_MINUTE0, range(0, 60, 5)), IBKRBarInterrupted("x", cause="socket_down")],
        [*_rth_minute_raw(_MINUTE0 + 60_000, range(10, 60, 5)), _raw_5s(_MINUTE0 + 120_000)],
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    feed = IbkrMarketDataFeed(_client())

    with pytest.raises(MarketDataFeedError) as excinfo:
        await _collect(feed, _policy(sink), 2)

    assert excinfo.value.reason == "SUBSTITUTION_NOT_AUTHORIZED"
    assert [e.kind for e in sink.events] == ["interruption", "recovered", "refused"]
    refused = sink.events[-1]
    assert (refused.window_start_ms, refused.window_end_ms, refused.contribution_count) == (
        _MINUTE0 + 60_000,
        _MINUTE0 + 120_000,
        10,
    )


async def test_a_landing_minute_that_holds_every_print_is_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sibling edge: landing on the :00 print leaves nothing to prove short.

    It is still the minute the run resumed on, so it is delivered as
    ``realtime_across_reconnect`` carrying the recovery -- the evidence a
    replay needs to see where the stream picked up, and the tag
    ``admit_on_delivery`` keys its lateness check on.
    """
    sink = _RecordingSink()
    source = _Source(
        [*_rth_minute_raw(_MINUTE0, range(0, 60, 5)), IBKRBarInterrupted("x", cause="socket_down")],
        [*_rth_minute_raw(_MINUTE0 + 60_000, range(0, 60, 5)), _raw_5s(_MINUTE0 + 120_000)],
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    feed = IbkrMarketDataFeed(_client())

    bars = await _collect(feed, _policy(sink), 2)

    assert [(b.start_ms, b.provenance, b.continuity_event_ref) for b in bars] == [
        (_MINUTE0, "realtime", None),
        (_MINUTE0 + 60_000, "realtime_across_reconnect", ContinuityEventRef(run_id="run-1", evidence_seq=2).ref()),
    ]
    assert [e.kind for e in sink.events] == ["interruption", "recovered"]


async def test_a_socket_already_healthy_past_the_deadline_is_still_a_missed_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deadline is the consumer's, not the socket's (implementation review, finding 2).

    A stall takes 60 s to detect, and the process stall behind #1921 can delay
    that detection further; by the time the loop looks, the line may be healthy
    again with the decision bar already gone. A wait that checked the deadline
    only while the socket was unhealthy let that reconnect through.
    """
    sink = _RecordingSink()
    source = _Source(
        [_ibkr_bar(_MINUTE0 - 60_000), IBKRBarSubscriptionStalled("stalled")],
        [_ibkr_bar(_MINUTE0, contribution_count=12)],
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    monkeypatch.setattr("app.marketdata.ibkr_continuity.now_ms_utc", lambda: _MINUTE0 + 80_000)

    async def _never_polls(_seconds: float) -> None:
        raise AssertionError("a healthy socket past its deadline must be refused, not polled")

    monkeypatch.setattr("app.marketdata.ibkr_continuity.asyncio.sleep", _never_polls)
    feed = IbkrMarketDataFeed(_client())  # connected, no soft loss: healthy at first glance

    with pytest.raises(MarketDataFeedError) as excinfo:
        await _collect(feed, _policy(sink), 2)

    assert excinfo.value.reason == "DECISION_BAR_MISSED"
    assert [e.kind for e in sink.events] == ["interruption", "refused"]
    assert sink.events[0].deadline_ms == sink.events[-1].deadline_ms == _MINUTE0 + 80_000


async def test_a_minute_cut_by_a_same_generation_restore_is_delivered_across_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 1100 -> 1102 soft restore keeps the socket generation (GitHub review, Codex).

    Every print of the open minute then comes over generation 1, so the
    assembler's ``spans_interruption`` reads false -- but the loop saw the
    minute cut open. The port must say so: provenance
    ``realtime_across_reconnect`` and the recovery that explains it, or
    ``admit_on_delivery`` would wave the bar through as an ordinary
    ``realtime`` minute.
    """
    sink = _RecordingSink()
    source = _Source(
        [
            _ibkr_bar(_MINUTE0 - 60_000),
            *_rth_minute_raw(_MINUTE0, range(0, 30, 5)),
            IBKRBarInterrupted("x", cause="soft_loss_1100"),
        ],
        [*_rth_minute_raw(_MINUTE0, range(30, 60, 5)), _raw_5s(_MINUTE0 + 60_000)],
        same_generation=True,
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    feed = IbkrMarketDataFeed(_client())

    bars = await _collect(feed, _policy(sink), 2)

    assert bars[1].start_ms == _MINUTE0
    assert bars[1].provenance == "realtime_across_reconnect"
    assert bars[1].continuity_event_ref == ContinuityEventRef(run_id="run-1", evidence_seq=2).ref()
    assert [(e.kind, e.generation_from, e.generation_to) for e in sink.events] == [
        ("interruption", 1, None),
        ("recovered", 1, 1),
    ]


async def test_a_short_minute_and_the_minutes_missed_behind_it_are_one_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One fact per episode (P11), even when the episode starts mid-minute.

    The assembler emits the cut-short minute only when the first post-recovery
    print lands in a later minute -- here three minutes on. The two wholly
    missed minutes in between are the same outage, so the refusal names the
    whole window; a coalesced window carries no single count (P12).
    """
    sink = _RecordingSink()
    source = _Source(
        [
            _ibkr_bar(_MINUTE0 - 60_000),
            *_rth_minute_raw(_MINUTE0, range(0, 30, 5)),
            IBKRBarInterrupted("x", cause="socket_down"),
        ],
        [_raw_5s(_MINUTE0 + 180_000)],
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    feed = IbkrMarketDataFeed(_client())

    with pytest.raises(MarketDataFeedError) as excinfo:
        await _collect(feed, _policy(sink), 2)

    assert excinfo.value.reason == "SUBSTITUTION_NOT_AUTHORIZED"
    assert [e.kind for e in sink.events] == ["interruption", "recovered", "refused"]
    refused = sink.events[-1]
    assert (refused.window_start_ms, refused.window_end_ms, refused.contribution_count) == (
        _MINUTE0,
        _MINUTE0 + 180_000,
        None,
    )


async def test_a_short_minute_and_the_minutes_missed_behind_it_are_one_gap_outside_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same episode outside the decision session is one recorded gap, then life goes on."""
    sink = _RecordingSink()
    pre = _MINUTE0 - 8 * 3_600_000  # 07:00 ET
    source = _Source(
        [
            _ibkr_bar(pre - 60_000, phase="PRE"),
            *_rth_minute_raw(pre, range(0, 30, 5)),
            IBKRBarInterrupted("x", cause="socket_down"),
        ],
        [*_rth_minute_raw(pre + 180_000, range(0, 60, 5)), _raw_5s(pre + 240_000)],
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    monkeypatch.setattr("app.marketdata.ibkr_continuity.now_ms_utc", lambda: pre + 30_000)
    feed = IbkrMarketDataFeed(_client())

    bars = await _collect(feed, _policy(sink), 2)

    assert [(b.start_ms, b.provenance) for b in bars] == [
        (pre - 60_000, "realtime"),
        (pre + 180_000, "realtime_across_reconnect"),
    ]
    assert [e.kind for e in sink.events] == ["interruption", "recovered", "gap"]
    gap = sink.events[-1]
    assert (gap.window_start_ms, gap.window_end_ms, gap.contribution_count) == (
        pre,
        pre + 180_000,
        None,
    )


@pytest.mark.parametrize("with_policy", [False, True])
async def test_closing_the_consumer_stream_closes_the_broker_stream_at_once(
    monkeypatch: pytest.MonkeyPatch, with_policy: bool
) -> None:
    """A consumer that stops early releases the line now, not when the GC gets to it.

    ``stream_bars`` delegates through two generators; each is closed with
    ``aclosing`` so the outer ``aclose()`` reaches ``stream_minute_bars`` --
    and the lease release in its ``finally`` -- before it returns.
    """
    sink = _RecordingSink()
    source = _Source([_ibkr_bar(_MINUTE0 - 60_000), _ibkr_bar(_MINUTE0)])
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    feed = IbkrMarketDataFeed(_client())
    policy = _policy(sink) if with_policy else None

    async with aclosing(feed.stream_bars("SPY", continuity=policy)) as stream:
        async for _bar in stream:
            break

    assert source.closed == 1


async def test_kill_switch_restores_todays_fail_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _RecordingSink()
    source = _Source([_ibkr_bar(_MINUTE0 - 60_000), IBKRBarInterrupted("x", cause="socket_down")])
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    client = _client()
    client.settings = SimpleNamespace(feed_continuity_enabled=False)
    feed = IbkrMarketDataFeed(client)
    with pytest.raises(MarketDataFeedError) as excinfo:
        await _collect(feed, _policy(sink), 2)
    assert excinfo.value.reason is None
    assert sink.events == []
