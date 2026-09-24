"""#2355: the LIVE chart's own IBKR bar line reaches the panel.

The bot panel's LIVE chart runs its own ``reqRealTimeBars`` line
(``use_rth=True``, ``LiveBarAggregator``), separate from the bot's line
(``use_rth=False``, ``IbkrMarketDataFeed``). When only the chart's line
stalls or is refused, the chart must say so, and the market headline must
stop saying "Market data live".

The end-to-end cases run real code: the bot feed, the aggregator, the
``bars.py`` registry and stall watchdog, ``_build_live_chart_from_fills`` and
``build_market_pulse``. Faked: the IBKR transport, the clocks and the
liveness evidence (probe from #2337).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from app.broker.ibkr import bars as bars_mod
from app.marketdata.ibkr_feed import IbkrMarketDataFeed
from app.schemas.broker_v2_panel import ChartLiveResponse, MarketPulseView
from app.schemas.market_liveness import (
    MarketClockLivenessEvidence,
    SymbolTradingStatusEvidence,
)
from app.services import live_bar_aggregator as lba_mod
from app.services.broker_v2_panel import panel_chart_data_source
from app.services.broker_v2_panel.chart_projection_service import chart_feed_view
from app.services.broker_v2_panel.market_pulse import (
    build_market_pulse,
    qualify_pulse_for_chart_feed,
)
from app.services.broker_v2_panel.panel_chart_data_source import (
    _build_live_chart_from_fills,
)
from app.services.live_bar_aggregator import LiveBarAggregator
from app.services.live_chart_window import ChartFeedStatus, resolve_chart_window
from app.services.market_liveness import compose_market_liveness
from tests._helpers.ibkr_feed_adversarial import AcceleratedFeedClock

_START = datetime(2026, 5, 4, 14, 35, tzinfo=UTC)  # Monday 10:35 ET, RTH
_START_MS = int(_START.timestamp() * 1000)
_SATURDAY_MS = int(datetime(2026, 5, 2, 15, 0, tzinfo=UTC).timestamp() * 1000)


class _Transport:
    """IBKR-shaped: one list per ``reqRealTimeBars``; ticks append to flowing lines."""

    def __init__(self, *, chart_flows: Any) -> None:
        self.lines: list[tuple[bool, list[SimpleNamespace]]] = []
        self.chart_flows = chart_flows

    async def qualifyContractsAsync(self, contract: Any) -> list[Any]:
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


async def _run_hub_cycles(
    monkeypatch: pytest.MonkeyPatch,
    *,
    chart_flows_until_step: int | None,
    max_active: int,
    steps: int,
) -> list[tuple[ChartLiveResponse, MarketPulseView]]:
    """One 5 s panel-hub cycle per step: build the chart, then the headline."""
    clock = AcceleratedFeedClock(wall_ms=_START_MS)
    for target in (
        "app.marketdata.ibkr_feed.now_ms_utc",
        "app.broker.ibkr.bars.now_ms_utc",
        "app.broker.ibkr.minute_assembler.now_ms_utc",
    ):
        monkeypatch.setattr(target, clock.now_ms)
    monkeypatch.setattr(bars_mod, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(
        bars_mod, "_REALTIME_BAR_SUBSCRIPTIONS", bars_mod._RealtimeBarSubscriptionRegistry()
    )
    step = {"i": 0}
    transport = _Transport(
        chart_flows=lambda: chart_flows_until_step is None or step["i"] < chart_flows_until_step
    )
    client = _Client(transport, max_active=max_active)

    feed = IbkrMarketDataFeed(client)
    aggregator = LiveBarAggregator(persistence=None)
    monkeypatch.setattr(aggregator, "_resolve_client", lambda: client)
    monkeypatch.setattr(lba_mod, "LIVE_BAR_AGGREGATOR", aggregator)

    async def bot() -> None:
        async for _bar in feed.stream_bars("SPY", use_rth=False):
            pass

    bot_task = asyncio.create_task(bot())
    cycles: list[tuple[ChartLiveResponse, MarketPulseView]] = []
    try:
        for i in range(steps):
            step["i"] = i
            now = clock.now_ms()
            chart = await asyncio.wait_for(
                _build_live_chart_from_fills("sid-2355", "SPY", [], resolution="1m", now_ms=now),
                5,
            )
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


async def test_chart_only_stall_reaches_the_chart_and_the_headline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The chart's line goes silent after its first candle; the bot's line stays healthy.
    cycles = await _run_hub_cycles(
        monkeypatch, chart_flows_until_step=14, max_active=100, steps=32
    )

    chart, pulse = cycles[-1]
    assert pulse.feed_state == "LIVE"
    assert pulse.headline == "Market data live"  # the bot's own feed is live
    assert chart.feed.state == "ERRORED"
    assert chart.feed.attention_required is True
    assert chart.feed.last_error is not None
    assert "IBKRBarSubscriptionStalled" in chart.feed.last_error
    assert chart.feed.last_bar_at_ms == chart.bars[-1].start_ms

    qualified = qualify_pulse_for_chart_feed(pulse, chart.feed)
    assert qualified.headline != "Market data live"
    assert qualified.attention_required is True
    assert qualified.feed_state == "LIVE"  # admission's typed fact is unchanged

    # While both lines flowed, the chart said so and the headline stood.
    healthy_chart, healthy_pulse = cycles[13]
    assert healthy_chart.feed.state == "LIVE"
    assert qualify_pulse_for_chart_feed(healthy_pulse, healthy_chart.feed) is healthy_pulse


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
    assert qualify_pulse_for_chart_feed(pulse, chart.feed).attention_required is True


class _StatusAggregator:
    _persistence = None

    def __init__(self, status: str, last_bar_ms: int | None, last_error: str | None = None) -> None:
        self._status = (status, last_error, last_bar_ms)

    def snapshot(self, symbol: str) -> list:
        return []

    def snapshot_5s(self, symbol: str) -> list:
        return []

    def status(self, symbol: str) -> tuple[str, str | None, int | None]:
        return self._status

    def status_5s(self, symbol: str) -> tuple[str, str | None, int | None]:
        return self._status


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
    ("status", "last_bar_age_ms", "expected"),
    [
        ("streaming", 60_000, "LIVE"),
        ("streaming", 240_000, "STALLED"),
        ("errored", 60_000, "ERRORED"),
        ("resubscribing", 60_000, "RECOVERING"),
        ("subscribing", None, "STARTING"),
        ("idle", None, "STARTING"),
    ],
)
async def test_resolve_chart_window_classifies_the_chart_line(
    status: str, last_bar_age_ms: int | None, expected: str
) -> None:
    last_bar_ms = None if last_bar_age_ms is None else _START_MS - last_bar_age_ms
    feed = await _feed_for(_StatusAggregator(status, last_bar_ms))
    assert feed.state == expected
    assert feed.last_bar_ms == last_bar_ms


async def test_resolve_chart_window_expects_no_live_bar_outside_rth() -> None:
    feed = await _feed_for(_StatusAggregator("errored", None, "boom"), now_ms=_SATURDAY_MS)
    assert feed.state == "NOT_EXPECTED"


@pytest.mark.parametrize(
    ("state", "attention"),
    [
        ("LIVE", False),
        ("NOT_EXPECTED", False),
        ("STARTING", False),
        ("STALLED", True),
        ("ERRORED", True),
        ("RECOVERING", True),
    ],
)
def test_chart_feed_view_flags_every_non_delivering_line(state: str, attention: bool) -> None:
    view = chart_feed_view(ChartFeedStatus(state=state))  # type: ignore[arg-type]
    assert view.state == state
    assert view.attention_required is attention
    assert view.headline
    assert view.explanation


def _pulse(**overrides: object) -> MarketPulseView:
    base: dict[str, object] = {
        "session": "OPEN",
        "market_state": "TRADABLE",
        "market_liveness_reason": "Market open.",
        "market_liveness_observed_at_ms": _START_MS,
        "halted_symbol": None,
        "feed_state": "LIVE",
        "latest_bar_at_ms": _START_MS - 60_000,
        "age_ms": 60_000,
        "source": "ibkr",
        "expected_cadence_ms": 60_000,
        "headline": "Market data live",
        "explanation": "The feed is connected and delivering data within its expected cadence.",
        "next_step": None,
        "attention_required": False,
        "observed_at_ms": _START_MS,
    }
    base.update(overrides)
    return MarketPulseView.model_validate(base)


class _PanelStandIn(BaseModel):
    """The two ``BotPanelView`` fields the snapshot assembly reads."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    market_pulse: MarketPulseView


def test_qualify_pulse_keeps_a_stronger_headline() -> None:
    stale = _pulse(feed_state="STALE", headline="Market data stale", attention_required=True)
    down = chart_feed_view(ChartFeedStatus(state="ERRORED", last_error="boom"))
    assert qualify_pulse_for_chart_feed(stale, down) is stale


async def test_live_snapshot_headline_does_not_vouch_for_a_down_chart_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    panel = _PanelStandIn(symbol="SPY", market_pulse=_pulse())

    async def panel_with_fills(*_args: object, **_kwargs: object):
        return panel, [], ()

    async def chart_from_fills(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(feed=chart_feed_view(ChartFeedStatus(state="STALLED")))

    monkeypatch.setattr(panel_chart_data_source, "get_panel_with_chart_fills", panel_with_fills)
    monkeypatch.setattr(panel_chart_data_source, "_build_live_chart_from_fills", chart_from_fills)

    snapshot_panel, _chart = await panel_chart_data_source.get_live_snapshot_parts(
        "alpaca", "paper-account", "sid-2355", resolution="1m"
    )

    assert snapshot_panel.market_pulse.attention_required is True
    assert snapshot_panel.market_pulse.headline == "Bot market data live; chart feed not live"
    assert snapshot_panel.market_pulse.feed_state == "LIVE"
