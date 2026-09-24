"""#2355: the LIVE chart's own IBKR bar line reaches the panel.

The bot panel's LIVE chart runs its own ``reqRealTimeBars`` line
(``use_rth=True``, ``LiveBarAggregator``), separate from the bot's line
(``use_rth=False``, ``IbkrMarketDataFeed``). When only the chart's line
stalls or is refused, the chart must say so; the market headline speaks for
the bot's own feed only ("Bot market data live").

The end-to-end cases run real code: the bot feed, the aggregator, the
``bars.py`` registry and stall watchdog, ``_build_live_chart_from_fills`` and
``build_market_pulse``. Faked: the IBKR transport, the clocks and the
liveness evidence (probe from #2337).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.broker.ibkr import bars as bars_mod
from app.broker.ibkr.bar_models import IbkrMinuteBar
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.marketdata.ibkr_feed import IbkrMarketDataFeed
from app.schemas.broker_v2_panel import ChartLiveResponse, MarketPulseView
from app.schemas.market_liveness import (
    MarketClockLivenessEvidence,
    SymbolTradingStatusEvidence,
)
from app.services import live_bar_aggregator as lba_mod
from app.services.broker_v2_panel import panel_chart_data_source
from app.services.broker_v2_panel.chart_projection_service import chart_feed_view
from app.services.broker_v2_panel.market_pulse import build_market_pulse
from app.services.broker_v2_panel.panel_chart_data_source import (
    _build_live_chart_from_fills,
)
from app.services.live_bar_aggregator import LiveBarAggregator, LiveLineStatus
from app.services.live_chart_window import ChartFeedStatus, resolve_chart_window
from app.services.market_liveness import compose_market_liveness
from tests._helpers.ibkr_feed_adversarial import AcceleratedFeedClock

_START = datetime(2026, 5, 4, 14, 35, tzinfo=UTC)  # Monday 10:35 ET, RTH
_START_MS = int(_START.timestamp() * 1000)
_SATURDAY_MS = int(datetime(2026, 5, 2, 15, 0, tzinfo=UTC).timestamp() * 1000)
_OPEN_MS = session_open_ms_utc(date(2026, 5, 4))  # 09:30 ET, from the canonical calendar
_OVERNIGHT_MS = _OPEN_MS - 6 * 60 * 60_000  # 03:30 ET: the Gateway's nightly restart window
_FRIDAY_LAST_BAR_MS = int(datetime(2026, 5, 1, 19, 59, tzinfo=UTC).timestamp() * 1000)
# The 1-minute chart's first-bar budget: the IBKR stall timeout (60 s) plus the
# 1-minute freshness budget (180 s).
_FIRST_BAR_BUDGET_MS = 60_000 + 180_000


class _Transport:
    """IBKR-shaped: one list per ``reqRealTimeBars``; ticks append to flowing lines."""

    def __init__(self, *, chart_flows: Any, qualify_hangs: bool = False) -> None:
        self.lines: list[tuple[bool, list[SimpleNamespace]]] = []
        self.chart_flows = chart_flows
        self.qualify_hangs = qualify_hangs

    async def qualifyContractsAsync(self, contract: Any) -> list[Any]:
        if self.qualify_hangs:
            await asyncio.Event().wait()  # IBKR never answers (contracts.py awaits unbounded)
        contract.conId = 756733
        contract.primaryExchange = "ARCA"
        return [contract]

    def reqRealTimeBars(self, contract: Any, bar_size: int, what_to_show: str, *, useRTH: bool) -> list:
        lst: list[SimpleNamespace] = []
        self.lines.append((useRTH, lst))
        return lst

    def cancelRealTimeBars(self, bars: Any) -> None:
        return None

    def tick(self, ts: datetime) -> None:
        for use_rth, lst in self.lines:
            if use_rth and not self.chart_flows():
                continue
            lst.append(
                SimpleNamespace(
                    time=ts,
                    open=Decimal("500"),
                    high=Decimal("501"),
                    low=Decimal("499"),
                    close=Decimal("500.5"),
                    volume=100,
                )
            )


class _Client:
    def __init__(self, transport: _Transport, *, max_active: int) -> None:
        self.ib = transport
        self.connection_lost = False
        self.connection_generation = 1
        self.connected_account = "DU000"
        self.settings = SimpleNamespace(
            realtime_bar_max_active=max_active, feed_continuity_enabled=True
        )

    def require_connected(self) -> None:
        return None

    def is_connected(self) -> bool:
        return True


def _liveness(now_ms: int):
    return compose_market_liveness(
        "SPY",
        now_ms=now_ms,
        market_clock=MarketClockLivenessEvidence(
            state="OPEN", source="test.clock", observed_at_ms=now_ms, vendor_timestamp_ms=now_ms
        ),
        connected=True,
        connection_changed_at_ms=now_ms,
        symbol_status=SymbolTradingStatusEvidence(
            symbol="SPY",
            state="TRADABLE",
            source="test.status",
            observed_at_ms=now_ms,
            source_timestamp_ms=now_ms,
        ),
    )


def _install_clock(monkeypatch: pytest.MonkeyPatch, clock: AcceleratedFeedClock) -> None:
    for target in (
        "app.marketdata.ibkr_feed.now_ms_utc",
        "app.broker.ibkr.bars.now_ms_utc",
        "app.broker.ibkr.minute_assembler.now_ms_utc",
        "app.services.live_bar_aggregator.now_ms_utc",
    ):
        monkeypatch.setattr(target, clock.now_ms)
    monkeypatch.setattr(bars_mod, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(
        bars_mod, "_REALTIME_BAR_SUBSCRIPTIONS", bars_mod._RealtimeBarSubscriptionRegistry()
    )


def _install_aggregator(monkeypatch: pytest.MonkeyPatch, client: object) -> LiveBarAggregator:
    aggregator = LiveBarAggregator(persistence=None)
    monkeypatch.setattr(aggregator, "_resolve_client", lambda: client)
    monkeypatch.setattr(lba_mod, "LIVE_BAR_AGGREGATOR", aggregator)
    return aggregator


async def _live_chart(now_ms: int) -> ChartLiveResponse:
    return await asyncio.wait_for(
        _build_live_chart_from_fills("sid-2355", "SPY", [], resolution="1m", now_ms=now_ms), 5
    )


async def _run_hub_cycles(
    monkeypatch: pytest.MonkeyPatch,
    *,
    chart_flows_until_step: int | None,
    max_active: int,
    steps: int,
) -> list[tuple[ChartLiveResponse, MarketPulseView]]:
    """One 5 s panel-hub cycle per step: build the chart, then the headline."""
    clock = AcceleratedFeedClock(wall_ms=_START_MS)
    _install_clock(monkeypatch, clock)
    step = {"i": 0}
    transport = _Transport(
        chart_flows=lambda: chart_flows_until_step is None or step["i"] < chart_flows_until_step
    )
    client = _Client(transport, max_active=max_active)

    feed = IbkrMarketDataFeed(client)
    aggregator = _install_aggregator(monkeypatch, client)

    async def bot() -> None:
        async for _bar in feed.stream_bars("SPY", use_rth=False):
            pass

    bot_task = asyncio.create_task(bot())
    cycles: list[tuple[ChartLiveResponse, MarketPulseView]] = []
    try:
        for i in range(steps):
            step["i"] = i
            now = clock.now_ms()
            chart = await _live_chart(now)
            pulse = build_market_pulse(
                feed,
                now_ms=now,
                symbol="SPY",
                use_rth=False,
                bot_running=True,
                liveness=_liveness(now),
            )
            cycles.append((chart, pulse))
            await asyncio.sleep(0.02)
            transport.tick(datetime.fromtimestamp(now / 1000, tz=UTC))
            for _ in range(4):
                await asyncio.sleep(0.03)
            clock.advance_ms(5_000)
    finally:
        bot_task.cancel()
        await asyncio.gather(bot_task, return_exceptions=True)
        await aggregator.shutdown()
    return cycles


async def test_chart_only_stall_reaches_the_chart_while_the_headline_speaks_for_the_bot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The chart's line goes silent after its first candle; the bot's line stays healthy.
    cycles = await _run_hub_cycles(
        monkeypatch, chart_flows_until_step=14, max_active=100, steps=32
    )

    chart, pulse = cycles[-1]
    assert pulse.feed_state == "LIVE"
    # The headline is scoped to the bot's own line, so it never vouches for the chart.
    assert pulse.headline == "Bot market data live"
    assert pulse.attention_required is False
    assert chart.feed.state == "ERRORED"
    assert chart.feed.show_notice is True
    assert chart.feed.attention_required is True
    assert chart.feed.last_error is not None
    assert "IBKRBarSubscriptionStalled" in chart.feed.last_error
    assert chart.feed.last_bar_at_ms == chart.bars[-1].start_ms

    # While both lines flowed, the chart said so quietly.
    healthy_chart, _healthy_pulse = cycles[13]
    assert healthy_chart.feed.state == "LIVE"
    assert healthy_chart.feed.show_notice is False


async def test_chart_line_refused_by_the_line_cap_is_errored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The bot holds the only allowed line, so every chart acquire is refused.
    cycles = await _run_hub_cycles(
        monkeypatch, chart_flows_until_step=None, max_active=1, steps=14
    )

    chart, pulse = cycles[-1]
    assert chart.bars == []
    assert pulse.feed_state == "LIVE"
    assert chart.feed.state == "ERRORED"
    assert chart.feed.last_error is not None
    assert "active-line cap" in chart.feed.last_error


async def test_chart_line_that_never_delivers_after_subscribe_is_errored_within_65s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The chart's request is sent but IBKR never delivers a bar on it.
    cycles = await _run_hub_cycles(
        monkeypatch, chart_flows_until_step=0, max_active=100, steps=14
    )

    first_chart, _ = cycles[0]
    assert first_chart.feed.state == "STARTING"
    assert first_chart.feed.attention_required is False
    chart_at_65s, _ = cycles[13]
    assert chart_at_65s.feed.state == "ERRORED"
    assert chart_at_65s.feed.attention_required is True


async def test_chart_line_whose_contract_never_qualifies_is_stalled_after_its_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``qualify_underlying`` awaits without a bound, so the stall watchdog never
    starts; the chart bounds ``STARTING`` itself (#2355 review, minor 3)."""
    clock = AcceleratedFeedClock(wall_ms=_START_MS)
    _install_clock(monkeypatch, clock)
    transport = _Transport(chart_flows=lambda: True, qualify_hangs=True)
    aggregator = _install_aggregator(monkeypatch, _Client(transport, max_active=100))
    try:
        subscribed = await _live_chart(clock.now_ms())
        await asyncio.sleep(0.05)  # the pump reaches the hanging qualification
        assert subscribed.feed.state == "STARTING"

        clock.advance_ms(_FIRST_BAR_BUDGET_MS)
        at_budget = await _live_chart(clock.now_ms())
        clock.advance_ms(5_000)
        past_budget = await _live_chart(clock.now_ms())
        assert aggregator.status("SPY").status == "subscribing"
    finally:
        await aggregator.shutdown()

    assert at_budget.feed.state == "STARTING"
    assert past_budget.feed.state == "STALLED"
    assert past_budget.feed.attention_required is True


def _minute_bar(start_ms: int, *, window_ms: int = 60_000) -> IbkrMinuteBar:
    return IbkrMinuteBar(
        symbol="SPY",
        start_ms=start_ms,
        end_ms=start_ms + window_ms,
        open=Decimal("500"),
        high=Decimal("501"),
        low=Decimal("499"),
        close=Decimal("500.5"),
        volume=100,
        fetched_at_ms=start_ms + window_ms,
    )


class _ChartLine:
    """A scripted chart stream behind the real aggregator pump."""

    def __init__(self) -> None:
        self.mode = "silent"
        self.delivered = asyncio.Event()

    async def stream(self, _client: object, _symbol: str, **_kw: object) -> AsyncIterator[IbkrMinuteBar]:
        mode = self.mode
        if mode == "fail":
            raise RuntimeError("IBKR Gateway restarting")
        if mode == "friday_bar":
            yield _minute_bar(_FRIDAY_LAST_BAR_MS)
        elif mode == "partial_bar":
            yield _minute_bar(_START_MS - 30_000, window_ms=30_000)
        self.delivered.set()
        await asyncio.Event().wait()


async def _wait_for_status(aggregator: LiveBarAggregator, status: str) -> None:
    for _ in range(50):
        if aggregator.status("SPY").status == status:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"line never reached {status!r}: {aggregator.status('SPY')}")


async def _leave_overnight(aggregator: LiveBarAggregator, line: _ChartLine, leftover: str) -> None:
    """Drive the real aggregator into ``leftover`` before the session opens."""
    if leftover == "streaming":
        line.mode = "friday_bar"
        await aggregator.ensure_subscribed("SPY")
    elif leftover == "errored":
        line.mode = "fail"
        await aggregator.ensure_subscribed("SPY")
    else:
        await aggregator.ensure_subscribed("SPY")
        await asyncio.sleep(0.02)
        await aggregator.resubscribe_all()
    await _wait_for_status(aggregator, leftover)
    line.mode = "silent"  # at the open the line is up but has no bar yet


@pytest.mark.parametrize("leftover", ["streaming", "errored", "resubscribing"])
async def test_a_status_left_from_overnight_reads_starting_at_the_open(
    monkeypatch: pytest.MonkeyPatch, leftover: str
) -> None:
    """#2355 review, major 1: no false alarm at open + 30 s."""
    clock = AcceleratedFeedClock(wall_ms=_OVERNIGHT_MS)
    _install_clock(monkeypatch, clock)
    line = _ChartLine()
    monkeypatch.setattr(lba_mod, "stream_minute_bars", line.stream)
    aggregator = _install_aggregator(monkeypatch, object())
    try:
        await _leave_overnight(aggregator, line, leftover)

        clock.advance_ms(_OPEN_MS + 30_000 - clock.now_ms())
        at_open = await _live_chart(clock.now_ms())
        # The leftover status is still what the aggregator reports.
        assert aggregator.status("SPY").status == leftover

        clock.advance_ms(_FIRST_BAR_BUDGET_MS)
        past_budget = await _live_chart(clock.now_ms())
    finally:
        await aggregator.shutdown()

    assert at_open.feed.state == "STARTING"
    assert at_open.feed.attention_required is False
    # A line that still has not drawn a bar of the session is not left STARTING.
    assert past_budget.feed.state == "STALLED"
    assert past_budget.feed.attention_required is True


async def test_a_partial_first_bar_reads_starting_not_stalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2355 review, major 1: a dropped partial bar is not a stalled line."""
    clock = AcceleratedFeedClock(wall_ms=_START_MS)
    _install_clock(monkeypatch, clock)
    line = _ChartLine()
    line.mode = "partial_bar"
    monkeypatch.setattr(lba_mod, "stream_minute_bars", line.stream)
    aggregator = _install_aggregator(monkeypatch, object())
    try:
        await _live_chart(clock.now_ms())
        await asyncio.wait_for(line.delivered.wait(), 1)

        clock.advance_ms(30_000)
        chart = await _live_chart(clock.now_ms())
    finally:
        await aggregator.shutdown()

    assert chart.bars == []
    assert chart.feed.state == "STARTING"
    assert chart.feed.attention_required is False


class _StatusAggregator:
    _persistence = None

    def __init__(self, line: LiveLineStatus) -> None:
        self._line = line

    def snapshot(self, symbol: str) -> list:
        return []

    def snapshot_5s(self, symbol: str) -> list:
        return []

    def status(self, symbol: str) -> LiveLineStatus:
        return self._line

    def status_5s(self, symbol: str) -> LiveLineStatus:
        return self._line


async def _feed_for(aggregator: _StatusAggregator, *, now_ms: int = _START_MS) -> ChartFeedStatus:
    result = await resolve_chart_window(
        symbol="SPY",
        timeframe="1m",
        from_ms=now_ms - 60 * 60_000,
        to_ms=now_ms,
        now_ms=now_ms,
        polygon_api_key="",
        live_aggregator=aggregator,
        polygon_overlay_enabled=False,
    )
    return result.feed


@pytest.mark.parametrize(
    ("status", "changed_age_ms", "last_bar_age_ms", "expected"),
    [
        ("streaming", 600_000, 60_000, "LIVE"),
        ("streaming", 600_000, 240_000, "STALLED"),
        ("errored", 60_000, 60_000, "ERRORED"),
        ("resubscribing", 60_000, 60_000, "RECOVERING"),
        ("subscribing", 30_000, None, "STARTING"),
        ("subscribing", 300_000, None, "STALLED"),
        # A replayed bar after a restart: waiting since the later of bar and subscribe.
        ("subscribing", 200_000, 30_000, "STARTING"),
        ("idle", None, None, "STALLED"),
    ],
)
async def test_resolve_chart_window_classifies_the_chart_line(
    status: str, changed_age_ms: int | None, last_bar_age_ms: int | None, expected: str
) -> None:
    last_bar_ms = None if last_bar_age_ms is None else _START_MS - last_bar_age_ms
    changed_at_ms = None if changed_age_ms is None else _START_MS - changed_age_ms
    feed = await _feed_for(
        _StatusAggregator(
            LiveLineStatus(
                status=status,  # type: ignore[arg-type]
                last_bar_ms=last_bar_ms,
                status_changed_at_ms=changed_at_ms,
            )
        )
    )
    assert feed.state == expected
    assert feed.last_bar_ms == last_bar_ms


async def test_resolve_chart_window_expects_no_live_bar_outside_rth() -> None:
    line = LiveLineStatus(status="errored", last_error="boom", status_changed_at_ms=_SATURDAY_MS)
    feed = await _feed_for(_StatusAggregator(line), now_ms=_SATURDAY_MS)
    assert feed.state == "NOT_EXPECTED"


@pytest.mark.parametrize(
    ("state", "show_notice", "attention"),
    [
        ("LIVE", False, False),
        ("NOT_EXPECTED", False, False),
        ("STARTING", True, False),
        ("STALLED", True, True),
        ("ERRORED", True, True),
        ("RECOVERING", True, True),
    ],
)
def test_chart_feed_view_flags_every_non_delivering_line(
    state: str, show_notice: bool, attention: bool
) -> None:
    view = chart_feed_view(ChartFeedStatus(state=state))  # type: ignore[arg-type]
    assert view.state == state
    assert view.show_notice is show_notice
    assert view.attention_required is attention
    assert view.headline
    assert view.explanation


async def test_live_snapshot_serves_the_same_headline_as_the_panel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The snapshot no longer rewrites the headline, so it and ``/panel`` agree."""
    panel = SimpleNamespace(symbol="SPY", market_pulse=object())

    async def panel_with_fills(*_args: object, **_kwargs: object):
        return panel, [], ()

    async def chart_from_fills(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(feed=chart_feed_view(ChartFeedStatus(state="STALLED")))

    monkeypatch.setattr(panel_chart_data_source, "get_panel_with_chart_fills", panel_with_fills)
    monkeypatch.setattr(panel_chart_data_source, "_build_live_chart_from_fills", chart_from_fills)

    snapshot_panel, _chart = await panel_chart_data_source.get_live_snapshot_parts(
        "alpaca", "paper-account", "sid-2355", resolution="1m"
    )

    assert snapshot_panel is panel
