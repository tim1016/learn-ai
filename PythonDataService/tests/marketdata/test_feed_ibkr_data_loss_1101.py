"""IBKR 1101 ("connectivity restored, data lost") is an ADR 0053 interruption (#2393).

After 1101 IBKR has dropped every market-data subscription on its side, so a
``reqRealTimeBars`` list requested before it never receives another print.
Everything between the IBKR transport and the continuity sink is production
code: the real ``IbkrClient`` error handler, ``AutoReconnectMonitor``,
``IbkrMarketDataFeed``, ``ContinuityLoop``, the shared-subscription registry,
its pacer and the liveness gate. Only the ``ib_async`` transport and the
wall/monotonic clocks are faked. The monotonic clock never advances, so the
60 s stall timer can never be what notices the dead line.
"""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.broker.contract.capabilities import ExtendedHoursWindow
from app.broker.ibkr import bars as bars_mod
from app.broker.ibkr.auto_reconnect_monitor import AutoReconnectMonitor
from app.broker.ibkr.bars import IBKRBarInterrupted, IBKRBarStreamError, stream_raw_5s_bars
from app.broker.ibkr.client import IbkrClient
from app.marketdata.feed import (
    ContinuityEventRef,
    ContinuityPolicy,
    FeedContinuityEvent,
    SubstitutionRefusal,
)
from app.marketdata.ibkr_feed import IbkrMarketDataFeed
from app.services.decision_session import RunDecisionSession
from tests._helpers.ibkr_feed_adversarial import AcceleratedFeedClock

_ET = ZoneInfo("America/New_York")
_WINDOW = ExtendedHoursWindow(open_minute_et=4 * 60, close_minute_et=20 * 60)
_TF = 900_000


def _et_ms(hour: int, minute: int) -> int:
    return int(datetime(2026, 9, 22, hour, minute, tzinfo=_ET).timestamp() * 1000)  # a Tuesday


_PRE_MINUTE = _et_ms(7, 29)


def _raw_5s(source_ms: int) -> SimpleNamespace:
    price = Decimal("1")
    return SimpleNamespace(
        time=datetime.fromtimestamp(source_ms / 1000, tz=UTC),
        open=price, high=price, low=price, close=price, volume=1,
    )


class _Line(list):
    """One ``reqRealTimeBars`` list; ``dead`` once IBKR dropped it server-side."""

    dead = False


class _Transport:
    """``ib_async.IB`` stand-in that keeps the one distinction 1101 turns on.

    After 1101 a list requested before it is never appended to again; only a
    list returned by a ``reqRealTimeBars`` issued after it receives prints.
    """

    def __init__(self) -> None:
        self.lines: list[_Line] = []
        self.cancelled: list[_Line] = []

    def isConnected(self) -> bool:
        return True

    async def qualifyContractsAsync(self, contract: Any) -> list[Any]:
        contract.conId = 756_733
        return [contract]

    async def reqCurrentTimeAsync(self) -> datetime:
        return datetime.now(UTC)

    def reqRealTimeBars(self, contract: Any, bar_size: int, what_to_show: str, *, useRTH: bool) -> _Line:
        line = _Line()
        self.lines.append(line)
        return line

    def cancelRealTimeBars(self, bars: _Line) -> None:
        self.cancelled.append(bars)

    def realtimeBars(self) -> list[_Line]:
        return [line for line in self.lines if all(line is not gone for gone in self.cancelled)]

    def drop_every_subscription(self) -> None:
        """What 1101 means on IBKR's side: every open line is gone for good."""
        for line in self.lines:
            line.dead = True

    def print_bar(self, source_ms: int) -> int:
        """Deliver one 5 s bar to every live line; return how many lines got it."""
        fed = 0
        for line in self.realtimeBars():
            if not line.dead:
                line.append(_raw_5s(source_ms))
                fed += 1
        return fed

    def print_minute(self, minute_start_ms: int) -> None:
        for second in range(0, 60, 5):
            self.print_bar(minute_start_ms + second * 1_000)


def _client(transport: _Transport) -> IbkrClient:
    client = IbkrClient()
    client._ib = transport  # type: ignore[assignment]
    client._connection_generation = 1  # as one successful connect() leaves it
    client._desired_connected = True
    return client


def _data_lost(client: IbkrClient, transport: _Transport) -> None:
    """1100 then 1101 through the real errorEvent handler, back to back."""
    client._on_ib_error(-1, 1100, "Connectivity between IB and TWS has been lost.", None)
    client._on_ib_error(-1, 1101, "Connectivity between IB and TWS has been restored- data lost.", None)
    transport.drop_every_subscription()


class _ChartResubscribe:
    """Production's only recovery callback restarts chart streams, never bot leases."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> None:
        self.calls += 1


def _monitor(client: IbkrClient, callback: _ChartResubscribe) -> AutoReconnectMonitor:
    return AutoReconnectMonitor(
        client,
        recovery_callbacks=[callback],
        probe_interval_s=3600,
        subscription_recovery_interval_s=0.0,
    )


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[FeedContinuityEvent] = []

    async def __call__(self, event: FeedContinuityEvent) -> ContinuityEventRef:
        self.events.append(event)
        return ContinuityEventRef(run_id="run-1", evidence_seq=len(self.events))


def _next_trigger(last_end: int) -> int:
    candidate = (last_end // _TF) * _TF + 60_000
    return candidate if candidate > last_end else candidate + _TF


def _extended_policy(sink: _RecordingSink) -> ContinuityPolicy:
    return ContinuityPolicy(
        session=RunDecisionSession(kind="extended", window=_WINDOW),
        next_trigger_ms=_next_trigger,
        substitution_grant=lambda s, e: SubstitutionRefusal(reason="SUBSTITUTION_NOT_AUTHORIZED"),
        record_event=sink,
    )


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bars_mod, "_REALTIME_BAR_SUBSCRIPTIONS", bars_mod._RealtimeBarSubscriptionRegistry())


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> AcceleratedFeedClock:
    clock = AcceleratedFeedClock(wall_ms=_PRE_MINUTE + 60_000)
    for target in (
        "app.broker.ibkr.bars.now_ms_utc",
        "app.broker.ibkr.minute_assembler.now_ms_utc",
        "app.marketdata.ibkr_feed.now_ms_utc",
        "app.marketdata.ibkr_continuity.now_ms_utc",
    ):
        monkeypatch.setattr(target, clock.now_ms)
    # Only ``bars``' own view of the monotonic clock: patching ``time.monotonic``
    # itself would also freeze the event loop's clock, and with it every sleep.
    monkeypatch.setattr(bars_mod, "time", SimpleNamespace(monotonic=clock.monotonic))
    return clock


async def _until(predicate, *, timeout_s: float = 2.0) -> bool:
    for _ in range(int(timeout_s / 0.02)):
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


@pytest.mark.asyncio
async def test_1101_in_pre_market_resubscribes_a_fresh_line_under_continuity(
    clock: AcceleratedFeedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An extended run's line dropped by 1101 in PRE is replaced, not left silent.

    Before #2393 the liveness gate never read 1101, the monitor's chart-only
    callbacks cleared ``subscriptions_stale`` and reported HEALTHY, and the
    bot's lease kept reading a list IBKR no longer fed -- blind until the RTH
    open plus 60 s, then dead with ``DECISION_BAR_MISSED``.
    """
    transport = _Transport()
    client = _client(transport)
    chart_resubscribe = _ChartResubscribe()
    monitor = _monitor(client, chart_resubscribe)
    monkeypatch.setattr("app.marketdata.ibkr_continuity.get_monitor", lambda: monitor)
    sink = _RecordingSink()
    feed = IbkrMarketDataFeed(client)

    async with aclosing(feed.stream_bars("SPY", use_rth=False, continuity=_extended_policy(sink))) as bars:
        first = asyncio.ensure_future(anext(bars))
        assert await _until(lambda: len(transport.lines) == 1)
        transport.print_minute(_PRE_MINUTE)
        delivered = await asyncio.wait_for(first, timeout=2)
        pending = asyncio.ensure_future(anext(bars))
        try:
            await asyncio.sleep(0.05)
            _data_lost(client, transport)
            await monitor._tick()
            assert chart_resubscribe.calls == 1
            assert monitor.recovery_state == "HEALTHY"
            # The monitor's chart callbacks are not the bot's line: 1101 stays
            # visible until a bar actually arrives on a line opened after it.
            assert client.subscriptions_stale is True

            assert await _until(lambda: len(transport.lines) == 2), "1101 never forced a fresh reqRealTimeBars"
            transport.print_minute(_PRE_MINUTE + 60_000)
            landed = await asyncio.wait_for(pending, timeout=2)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    assert delivered.start_ms == _PRE_MINUTE
    assert landed.start_ms == _PRE_MINUTE + 60_000
    assert [(event.kind, event.cause) for event in sink.events] == [
        ("interruption", "data_lost_1101"),
        ("recovered", None),
    ]
    # Same socket: 1101 fences the line, not the connection.
    assert (sink.events[1].generation_from, sink.events[1].generation_to) == (1, 1)
    assert client.subscriptions_stale is False


@pytest.mark.asyncio
async def test_reacquire_after_1101_never_multiplexes_onto_the_dead_line(
    clock: AcceleratedFeedClock,
) -> None:
    """A peer suspended across 1100 -> 1101 cannot hand its dead list to a re-acquirer."""
    transport = _Transport()
    client = _client(transport)
    monitor = _monitor(client, _ChartResubscribe())
    held = stream_raw_5s_bars(client, "SPY", use_rth=False)
    leaving = stream_raw_5s_bars(client, "SPY", use_rth=False)
    first_held = asyncio.ensure_future(anext(held))
    first_leaving = asyncio.ensure_future(anext(leaving))
    assert await _until(lambda: len(transport.lines) == 1)
    transport.print_bar(_PRE_MINUTE)
    await asyncio.wait_for(first_held, 1)  # ``held`` is now suspended at its yield
    await asyncio.wait_for(first_leaving, 1)
    next_leaving = asyncio.ensure_future(anext(leaving))
    await asyncio.sleep(0.05)
    client._on_ib_error(-1, 1100, "lost", None)
    with pytest.raises(IBKRBarInterrupted):
        await asyncio.wait_for(next_leaving, 1)
    await leaving.aclose()
    client._on_ib_error(-1, 1101, "restored- data lost", None)
    transport.drop_every_subscription()
    await monitor._tick()

    rejoined = stream_raw_5s_bars(client, "SPY", use_rth=False)
    next_rejoined = asyncio.ensure_future(anext(rejoined))
    try:
        assert await _until(lambda: len(transport.lines) == 2), "the re-acquire multiplexed onto the dead list"
        transport.print_bar(_PRE_MINUTE + 5_000)
        got = await asyncio.wait_for(next_rejoined, 1)
        assert got.start_ms == _PRE_MINUTE + 5_000
        with pytest.raises(IBKRBarInterrupted) as interrupted:
            await asyncio.wait_for(anext(held), 1)
        assert interrupted.value.cause == "data_lost_1101"
    finally:
        next_rejoined.cancel()
        await asyncio.gather(next_rejoined, return_exceptions=True)
        await held.aclose()
        await rejoined.aclose()
    assert transport.lines[0] in transport.cancelled  # the dead line was released, on its own socket


@pytest.mark.asyncio
async def test_monitor_recovery_after_1101_with_no_line_open_clears_stale(
    clock: AcceleratedFeedClock,
) -> None:
    """With no real-time-bar line lost there is no bar owed: recovery clears the flag."""
    transport = _Transport()
    client = _client(transport)
    callback = _ChartResubscribe()
    monitor = _monitor(client, callback)
    _data_lost(client, transport)
    assert client.subscriptions_stale is True

    await monitor._tick()

    assert callback.calls == 1
    assert client.subscriptions_stale is False


@pytest.mark.asyncio
async def test_monitor_runs_chart_recovery_once_per_1101_while_the_bar_is_owed(
    clock: AcceleratedFeedClock,
) -> None:
    """A stale flag held for a sparse PRE line must not re-run ``resubscribe_all`` every tick.

    Each chart restart can issue a fresh ``reqRealTimeBars``; repeating it
    while the fresh line waits for its first extended-hours print would spend
    the shared 60-per-600 s budget on nothing.
    """
    transport = _Transport()
    client = _client(transport)
    callback = _ChartResubscribe()
    monitor = _monitor(client, callback)
    stream = stream_raw_5s_bars(client, "SPY", use_rth=False)
    first = asyncio.ensure_future(anext(stream))
    try:
        assert await _until(lambda: len(transport.lines) == 1)
        _data_lost(client, transport)
        for _ in range(3):
            await monitor._tick()
        assert callback.calls == 1
        assert client.subscriptions_stale is True
        _data_lost(client, transport)  # a second, distinct 1101 is recovered again
        await monitor._tick()
        assert callback.calls == 2
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await stream.aclose()


@pytest.mark.asyncio
async def test_1101_storm_cannot_spend_the_shared_request_budget(
    clock: AcceleratedFeedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-requests of lines 1101 dropped are capped; past the cap the stream fails closed.

    The refusal is fatal (not an ``IBKRBarInterrupted``) and issues no request,
    so a flapping link ends the run instead of exhausting the 60-per-600 s
    allowance every other line on the username shares.
    """
    pacer = bars_mod._RealtimeBarRequestPacer(max_lost_line_requests=2)
    monkeypatch.setattr(
        bars_mod, "_REALTIME_BAR_SUBSCRIPTIONS", bars_mod._RealtimeBarSubscriptionRegistry(pacer)
    )
    transport = _Transport()
    client = _client(transport)

    async def _open_then_lose_the_line() -> None:
        stream = stream_raw_5s_bars(client, "SPY", use_rth=False)
        pending = asyncio.ensure_future(anext(stream))
        try:
            lines_before = len(transport.lines)
            assert await _until(lambda: len(transport.lines) == lines_before + 1)
            _data_lost(client, transport)
            with pytest.raises(IBKRBarInterrupted) as interrupted:
                await asyncio.wait_for(pending, 1)
            assert interrupted.value.cause == "data_lost_1101"
        finally:
            await stream.aclose()

    for _ in range(3):  # the first line, then two replacements
        await _open_then_lose_the_line()

    refused = stream_raw_5s_bars(client, "SPY", use_rth=False)
    with pytest.raises(IBKRBarStreamError) as storm:
        await asyncio.wait_for(anext(refused), 1)
    await refused.aclose()
    assert not isinstance(storm.value, IBKRBarInterrupted)
    assert len(transport.lines) == 3
