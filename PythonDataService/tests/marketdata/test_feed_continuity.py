"""Fail-closed continuity inside IbkrMarketDataFeed.stream_bars (spec §4.2, §4.5, §8)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

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

    def __init__(self, *calls: list) -> None:
        self.calls = list(calls)
        self.assemblers: list = []
        self.invocations = 0

    def __call__(self, _client, _symbol, *, use_rth=True, on_source_bar=None, assembler=None, **_kw):
        self.assemblers.append(assembler)
        script = self.calls[self.invocations] if self.invocations < len(self.calls) else []
        self.invocations += 1
        generation = self.invocations

        async def _gen():
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

    assert observed == [
        (_MINUTE0, "realtime", ("interruption",)),
        (_MINUTE0 + 60_000, "realtime", ("interruption", "recovered")),
    ]
    # The anchor is the open minute's start, and the deadline the run promises
    # is derived from it once, before the flush moves the watermark on.
    assert sink.events[0].last_delivered_end_ms == _MINUTE0
    assert sink.events[0].deadline_ms == _MINUTE0 + 80_000


async def test_a_minute_stitched_by_the_real_assembler_crosses_the_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nine 5-second bars on one socket, three on the next, one delivered minute."""
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


async def test_the_wait_enforces_the_deadline_the_interruption_event_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flush that crosses a decision trigger must not silently extend the wait.

    The flushed minute moves ``last_delivered_end_ms`` past the fake clock's
    15:01 trigger, so a deadline re-derived inside the wait would sit a whole
    decision interval later than the one the ``interruption`` event promised.
    """
    sink = _RecordingSink()
    source = _Source(
        [*_rth_minute_raw(_MINUTE0, range(0, 60, 5)), IBKRBarInterrupted("x", cause="socket_down")],
        [],
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    client = _client()
    client.is_connected.return_value = False
    monkeypatch.setattr("app.marketdata.ibkr_continuity.now_ms_utc", lambda: _MINUTE0 + 80_001)

    async def _never_recovers(_seconds: float) -> None:
        raise AssertionError("the wait outlived the deadline the interruption event recorded")

    monkeypatch.setattr("app.marketdata.ibkr_continuity.asyncio.sleep", _never_recovers)
    feed = IbkrMarketDataFeed(client)

    with pytest.raises(MarketDataFeedError) as excinfo:
        await _collect(feed, _policy(sink), 2)

    assert excinfo.value.reason == "DECISION_BAR_MISSED"
    assert [e.kind for e in sink.events] == ["interruption", "refused"]
    assert sink.events[0].deadline_ms == sink.events[-1].deadline_ms == _MINUTE0 + 80_000
    # The flush did move the watermark; the deadline just did not follow it.
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


async def test_not_connected_on_reentry_keeps_waiting_rather_than_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resubscribe can race the reconnect; that is still the interruption, not a new fault."""
    sink = _RecordingSink()
    source = _Source(
        [_ibkr_bar(_MINUTE0 - 60_000), IBKRBarInterrupted("x", cause="socket_down")],
        [NotConnectedError("IBKR client is not connected")],
        [_ibkr_bar(_MINUTE0, contribution_count=12, spans_interruption=True)],
    )
    monkeypatch.setattr(feed_module, "stream_minute_bars", source)
    feed = IbkrMarketDataFeed(_client())

    bars = await _collect(feed, _policy(sink), 2)

    assert source.invocations == 3
    assert bars[1].provenance == "realtime_across_reconnect"
    assert [e.kind for e in sink.events] == ["interruption", "recovered"]


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
