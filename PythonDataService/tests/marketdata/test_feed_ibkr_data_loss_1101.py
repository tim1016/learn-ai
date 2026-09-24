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
from eventkit import Event
from ib_async import Ticker

from app.broker.alpaca.clerk.sqlite.qualification_polygon_replay import _PolygonReplayClient
from app.broker.contract.capabilities import ExtendedHoursWindow
from app.broker.ibkr import bars as bars_mod
from app.broker.ibkr.auto_reconnect_monitor import AutoReconnectMonitor
from app.broker.ibkr.bars import (
    IBKRBarInterrupted,
    RealtimeBarClient,
    realtime_bar_lines_unreplaced,
    stream_raw_5s_bars,
)
from app.broker.ibkr.client import IbkrClient
from app.broker.ibkr.health import build_broker_health
from app.broker.ibkr.market_liveness import IbkrMarketStatusSource
from app.marketdata.feed import (
    ContinuityEventRef,
    ContinuityPolicy,
    FeedContinuityEvent,
    MarketDataFeedError,
    SubstitutionRefusal,
)
from app.marketdata.ibkr_feed import IbkrMarketDataFeed
from app.services.decision_session import RunDecisionSession
from tests._helpers.ibkr_feed_adversarial import AcceleratedFeedClock, _AdversarialIbkrClient

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
        self._con_ids: dict[str, int] = {}
        # The reqMktData side the quote/status source owns (ADR 0067).
        self.errorEvent = Event()
        self.wrapper = SimpleNamespace(_market_status_callbacks_installed=True, ticker2ReqId={"mktData": {}})
        self.quote_requests = 0
        self.client = None  # no server version to report in ``IbkrClient.health()``

    def isConnected(self) -> bool:
        return True

    async def qualifyContractsAsync(self, contract: Any) -> list[Any]:
        contract.conId = self._con_ids.setdefault(contract.symbol, 756_733 + len(self._con_ids))
        return [contract]

    def reqMktData(self, contract: Any, *_args: Any) -> Ticker:
        self.quote_requests += 1
        return Ticker(contract=contract)

    def cancelMktData(self, contract: Any) -> None:
        return None

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

    Before #2393 the liveness gate never read 1101, so the bot's lease kept
    reading a list IBKR no longer fed -- blind until the RTH open plus 60 s,
    then dead with ``DECISION_BAR_MISSED``. The monitor's chart-only callbacks
    still clear ``subscriptions_stale``: the bar line is fenced by its epoch,
    not by the quote source's health.
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
            assert client.connection_state == "connected"

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
    assert realtime_bar_lines_unreplaced(client) is False


@pytest.mark.asyncio
async def test_1101_re_request_paced_past_the_deadline_is_refused_at_the_deadline(
    clock: AcceleratedFeedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spent 60-per-600 s budget cannot hold a run past its decision deadline.

    After ``recovered`` the fresh line waits on the shared pacer, which can
    sleep for most of ten minutes. Before the #2397 review nothing enforced the
    consumer's deadline there: the run sat blocked long past it with no
    ``refused`` event. The wait is bounded by the deadline the interruption
    recorded, and the run ends with ADR 0053's ``DECISION_BAR_MISSED``.
    """
    never = asyncio.Event()

    async def _pacing_sleep(_seconds: float) -> None:
        await never.wait()  # a real pacing wait: nothing frees the slot in time

    pacer = bars_mod._RealtimeBarRequestPacer(max_requests=1, clock=clock.monotonic, sleep=_pacing_sleep)
    monkeypatch.setattr(bars_mod, "_REALTIME_BAR_SUBSCRIPTIONS", bars_mod._RealtimeBarSubscriptionRegistry(pacer))
    transport = _Transport()
    client = _client(transport)
    monitor = _monitor(client, _ChartResubscribe())
    monkeypatch.setattr("app.marketdata.ibkr_continuity.get_monitor", lambda: monitor)
    sink = _RecordingSink()
    feed = IbkrMarketDataFeed(client)

    async with aclosing(feed.stream_bars("SPY", use_rth=False, continuity=_extended_policy(sink))) as bars:
        first = asyncio.ensure_future(anext(bars))
        assert await _until(lambda: len(transport.lines) == 1)
        transport.print_minute(_PRE_MINUTE)
        await asyncio.wait_for(first, timeout=2)
        pending = asyncio.ensure_future(anext(bars))
        try:
            await asyncio.sleep(0.05)
            _data_lost(client, transport)
            await monitor._tick()
            with pytest.raises(MarketDataFeedError) as refused:
                await asyncio.wait_for(pending, timeout=2)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    assert refused.value.reason == "DECISION_BAR_MISSED"
    assert [(event.kind, event.reason) for event in sink.events] == [
        ("interruption", None),
        ("recovered", None),
        ("refused", "DECISION_BAR_MISSED"),
    ]
    assert sink.events[2].deadline_ms == sink.events[0].deadline_ms
    assert len(transport.lines) == 1  # no request was spent past the deadline


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
async def test_monitor_runs_chart_recovery_once_per_1101(
    clock: AcceleratedFeedClock,
) -> None:
    """Chart recovery runs once per 1101; a bot lease still held never re-runs it.

    Each chart restart can issue a fresh ``reqRealTimeBars``; repeating it
    while a bot's old lease waits to be interrupted would spend the shared
    60-per-600 s budget on nothing.
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
        assert client.subscriptions_stale is False
        _data_lost(client, transport)  # a second, distinct 1101 is recovered again
        await monitor._tick()
        assert callback.calls == 2
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await stream.aclose()


@pytest.mark.asyncio
async def test_a_1101_landing_during_recovery_keeps_the_feed_stale(
    clock: AcceleratedFeedClock,
) -> None:
    """A 1101 that lands while the callbacks run drops what they just resubscribed.

    Before the #2397 review the run still cleared ``subscriptions_stale`` and
    reported HEALTHY for the older epoch, so health and the quote source saw a
    recovered feed until the next recovery interval. The later loss stays
    stale until its own recovery runs.
    """
    transport = _Transport()
    client = _client(transport)

    class _FlappingResubscribe(_ChartResubscribe):
        async def __call__(self) -> None:
            await super().__call__()
            if self.calls == 1:
                _data_lost(client, transport)

    callback = _FlappingResubscribe()
    monitor = AutoReconnectMonitor(
        client,
        recovery_callbacks=[callback],
        probe_interval_s=3600,
        subscription_recovery_interval_s=10.0,  # the production default
        now_ms=clock.now_ms,
    )
    _data_lost(client, transport)

    await monitor._tick()
    clock.advance_ms(5_000)
    await monitor._tick()  # inside the recovery interval: nothing re-runs yet

    assert callback.calls == 1
    assert client.subscriptions_stale is True
    assert monitor.recovery_state == "RESTORING"
    assert build_broker_health(client, monitor).connection_state != "connected"

    clock.advance_ms(5_000)
    await monitor._tick()

    assert callback.calls == 2
    assert client.subscriptions_stale is False
    assert monitor.recovery_state == "HEALTHY"


@pytest.mark.asyncio
async def test_1101_burst_within_the_pacer_budget_does_not_kill_runs(
    clock: AcceleratedFeedClock,
) -> None:
    """A flapping link re-requests every line each 1101; inside the 60-per-600 s pacer none dies.

    Eight symbols through four 1101s is 32 re-requests plus 8 first requests,
    within the shared budget. #2393's first cut capped re-requests at 30 and
    threw a bare error after ``recovered``, killing every run here.
    """
    transport = _Transport()
    client = _client(transport)
    symbols = ("SPY", "QQQ", "IWM", "DIA", "AAPL", "MSFT", "NVDA", "TSLA")
    streams = [stream_raw_5s_bars(client, symbol, use_rth=False) for symbol in symbols]
    pending = [asyncio.ensure_future(anext(stream)) for stream in streams]
    try:
        for flap in range(4):
            expected = len(symbols) * (flap + 1)
            assert await _until(lambda expected=expected: len(transport.lines) == expected)
            _data_lost(client, transport)
            for stream, waiting in zip(streams, pending, strict=True):
                with pytest.raises(IBKRBarInterrupted) as interrupted:
                    await asyncio.wait_for(waiting, 1)
                assert interrupted.value.cause == "data_lost_1101"
                await stream.aclose()
            streams = [stream_raw_5s_bars(client, symbol, use_rth=False) for symbol in symbols]
            pending = [asyncio.ensure_future(anext(stream)) for stream in streams]

        assert await _until(lambda: len(transport.lines) == len(symbols) * 5)
        assert transport.print_bar(_PRE_MINUTE) == len(symbols)
        landed = await asyncio.wait_for(asyncio.gather(*pending), 1)
    finally:
        for waiting in pending:
            waiting.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for stream in streams:
            await stream.aclose()
    assert [bar.start_ms for bar in landed] == [_PRE_MINUTE] * len(symbols)


@pytest.mark.asyncio
async def test_1101_in_pre_recovers_the_quote_source_while_the_rth_lease_is_interrupted(
    clock: AcceleratedFeedClock,
) -> None:
    """An RTH-only lease cannot print until 09:30; the quote source must not wait for it.

    #2393's first cut held ``subscriptions_stale`` until a replacement bar
    line delivered, so the reqMktData status/quote source refused and
    published RECOVERING until the open -- blocking the top-of-book that
    PRE/POST safe-flatten and the extended-hours EXIT re-drive price off.
    """
    transport = _Transport()
    client = _client(transport)
    monitor = _monitor(client, _ChartResubscribe())
    source = IbkrMarketStatusSource(symbols=lambda: ("SPY",), client=lambda: client, clock=clock.now_ms)
    rth_only = stream_raw_5s_bars(client, "SPY", use_rth=True)
    waiting = asyncio.ensure_future(anext(rth_only))
    try:
        assert await _until(lambda: len(transport.lines) == 1)
        assert (await source()).connected is True
        assert await _until(lambda: transport.quote_requests == 1)

        _data_lost(client, transport)
        assert realtime_bar_lines_unreplaced(client) is True
        assert (await source()).connected is False  # 1101 dropped the quote line too
        await monitor._tick()

        assert build_broker_health(client, monitor).connection_state == "connected"
        await source()
        assert await _until(lambda: transport.quote_requests == 2), "the quote line was never requested again"
        assert (await source()).connected is True

        with pytest.raises(IBKRBarInterrupted) as interrupted:
            await asyncio.wait_for(waiting, 1)
        assert interrupted.value.cause == "data_lost_1101"
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        await rth_only.aclose()
        await source.close()
    assert realtime_bar_lines_unreplaced(client) is False


@pytest.mark.asyncio
async def test_1101_with_no_replacement_ever_requested_leaves_nothing_stale(
    clock: AcceleratedFeedClock,
) -> None:
    """A consumer that never re-acquires after 1101 cannot leave health stale forever."""
    transport = _Transport()
    client = _client(transport)
    monitor = _monitor(client, _ChartResubscribe())
    stream = stream_raw_5s_bars(client, "SPY", use_rth=False)
    waiting = asyncio.ensure_future(anext(stream))
    assert await _until(lambda: len(transport.lines) == 1)
    _data_lost(client, transport)
    await monitor._tick()
    assert build_broker_health(client, monitor).realtime_bar_lines_unreplaced is True

    with pytest.raises(IBKRBarInterrupted):
        await asyncio.wait_for(waiting, 1)
    await stream.aclose()  # the run ends here: no fresh line is ever requested

    health = build_broker_health(client, monitor)
    assert (health.connection_state, health.recovery_state) == ("connected", "HEALTHY")
    assert health.subscriptions_stale is False
    assert health.realtime_bar_lines_unreplaced is False
    assert len(transport.lines) == 1


def test_every_bar_client_satisfies_the_realtime_bar_protocol() -> None:
    """The registry and gate read exactly these members; no client may default one silently."""
    assert isinstance(_client(_Transport()), RealtimeBarClient)
    assert isinstance(_PolygonReplayClient([]), RealtimeBarClient)
    assert isinstance(_AdversarialIbkrClient(SimpleNamespace()), RealtimeBarClient)  # type: ignore[arg-type]
