"""Tests for the chart projections (S1, spec §8).

Covers the bounded HISTORY preset → aggregation ladder + size bound, the LIVE
pane source-tagging + fill markers, and a regression that the 7-day live
resolver cap is not widened.
"""

from __future__ import annotations

import pytest

from app.data_lake.polygon_fetcher import PolygonBar
from app.schemas.broker_bots import BotStatusView
from app.services.broker_v2_panel import panel_data_source
from app.services.broker_v2_panel.chart_projection_service import (
    _MAX_HISTORY_BARS,
    ChartPresetError,
    build_history_chart,
    build_live_chart,
    coerce_history_preset,
    live_window,
)
from app.services.live_chart_window import MAX_CHART_RANGE_MS, ChartWindowResult
from tests.broker.v2panel.fixtures import SID, fill_entry

_NOW = 1_700_000_000_000


def test_seven_day_live_resolver_cap_unchanged() -> None:
    """Regression: the existing 7-day cap is not widened by the history contract."""
    assert MAX_CHART_RANGE_MS == 7 * 86_400_000


@pytest.mark.parametrize(
    ("preset", "aggregation"),
    [
        ("1D", "1m"),
        ("5D", "5m"),
        ("1M", "30m"),
        ("3M", "1h"),
        ("1Y", "1d"),
        ("All", "1d"),
    ],
)
async def test_history_preset_maps_to_fixed_aggregation(preset, aggregation) -> None:
    async def _source(symbol, start, end, multiplier, timespan):
        return []

    result = await build_history_chart(
        preset,  # type: ignore[arg-type]
        [],
        strategy_instance_id=SID,
        symbol="SPY",
        bar_source=_source,
        now_ms=_NOW,
    )
    assert result.aggregation == aggregation
    assert result.preset == preset


def test_unknown_preset_is_rejected() -> None:
    with pytest.raises(ChartPresetError):
        coerce_history_preset("7D")


async def test_history_bars_are_polygon_tagged_and_bounded() -> None:
    # Return more than the size bound; the response must truncate to the newest.
    # Space bars 5s apart inside the 1D window so every fixture bar falls in the
    # lookback and the size bound — not windowing — is what truncates them.
    bars = [
        PolygonBar(
            t_ms=_NOW - (i + 1) * 5_000,
            open=1.0,
            high=2.0,
            low=0.5,
            close=1.5,
            volume=10,
            vwap=1.2,
            n=3,
        )
        for i in range(_MAX_HISTORY_BARS + 50)
    ]

    async def _source(symbol, start, end, multiplier, timespan):
        return bars

    result = await build_history_chart(
        "1D",
        [],
        strategy_instance_id=SID,
        symbol="SPY",
        bar_source=_source,
        now_ms=_NOW,
    )
    assert result.truncated is True
    assert len(result.bars) == _MAX_HISTORY_BARS
    assert all(bar.source == "polygon" for bar in result.bars)
    # Bars are sorted ascending by start_ms after truncation.
    starts = [bar.start_ms for bar in result.bars]
    assert starts == sorted(starts)


async def test_history_fill_markers_within_window() -> None:
    entries = [
        fill_entry(sid=SID, intent="i1", ts_ms=_NOW - 30_000, qty=100, price=500.0),
        # Outside the 1D lookback → excluded.
        fill_entry(sid=SID, intent="i2", ts_ms=_NOW - 5 * 86_400_000, qty=50, price=400.0),
    ]

    async def _source(symbol, start, end, multiplier, timespan):
        return []

    result = await build_history_chart(
        "1D",
        entries,
        strategy_instance_id=SID,
        symbol="SPY",
        bar_source=_source,
        now_ms=_NOW,
    )
    assert len(result.fill_markers) == 1
    assert result.fill_markers[0].side == "buy"
    assert result.fill_markers[0].price == 500.0


def test_live_chart_tags_source_and_markers() -> None:
    from decimal import Decimal

    from app.broker.ibkr.models import IbkrMinuteBar

    bar = IbkrMinuteBar(
        symbol="SPY",
        start_ms=_NOW - 60_000,
        end_ms=_NOW,
        open=Decimal("500"),
        high=Decimal("501"),
        low=Decimal("499"),
        close=Decimal("500.5"),
        volume=1000,
        fetched_at_ms=_NOW,
        source="ibkr",
    )
    chart_window = ChartWindowResult(
        bars=[bar], timeframe="1m", resolution="1m", is_streaming=True
    )
    entries = [fill_entry(sid=SID, intent="i1", ts_ms=_NOW - 30_000)]

    result = build_live_chart(
        chart_window,
        entries,
        strategy_instance_id=SID,
        symbol="SPY",
        window=(_NOW - 6 * 60 * 60_000, _NOW + 60_000),
        now_ms=_NOW,
    )
    assert result.bars[0].source == "ibkr"
    assert result.resolution == "1m"
    # The one fill at _NOW-30s falls inside the passed window.
    assert len(result.fill_markers) == 1
    assert result.trading_date_open_ms == _NOW - 6 * 60 * 60_000


def test_live_window_falls_back_when_market_closed() -> None:
    # 2023-11-18 15:00 UTC is a Saturday → not a session → the day fallback.
    saturday_ms = 1_700_319_600_000
    open_ms, close_ms = live_window(saturday_ms)
    assert open_ms <= saturday_ms < close_ms
    assert close_ms - open_ms == 86_400_000


async def test_live_chart_before_session_open_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    open_ms = _NOW + 60_000
    close_ms = open_ms + 6 * 60 * 60_000

    async def validate_account(broker: str, account_id: str) -> str:
        return account_id

    async def unexpected_resolver(**kwargs) -> ChartWindowResult:
        pytest.fail("the range resolver must not run before the session opens")

    monkeypatch.setattr(panel_data_source, "_validate_account", validate_account)
    monkeypatch.setattr(
        panel_data_source,
        "_bot_status",
        lambda broker, sid: BotStatusView(
            strategy_instance_id=sid,
            broker=broker,
            symbol="SPY",
            mode="trade",
            quantity=1,
            running=True,
            phase="ON_DUTY",
            desired_state="RUNNING",
            active_run_id="run-1",
            duty_outcome=None,
            binding_created_at_ms=_NOW - 60_000,
            last_transition_at_ms=_NOW - 60_000,
        ),
    )
    monkeypatch.setattr(panel_data_source, "_read_order_journal", lambda *_args: [])
    monkeypatch.setattr(panel_data_source, "now_ms_utc", lambda: _NOW)
    monkeypatch.setattr(panel_data_source, "live_window", lambda now_ms: (open_ms, close_ms))
    monkeypatch.setattr(panel_data_source, "resolve_chart_window", unexpected_resolver)

    result = await panel_data_source.get_live_chart(
        "alpaca", "paper-account", SID, resolution="5s"
    )

    assert result.trading_date_open_ms == open_ms
    assert result.trading_date_close_ms == close_ms
    assert result.resolution == "5s"
    assert result.bars == []
    assert result.fill_markers == []
    assert result.overlay_notices == []


async def test_live_chart_forwards_selected_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.live_bar_aggregator import LIVE_BAR_AGGREGATOR

    open_ms = _NOW - 60_000
    close_ms = _NOW + 60_000
    captured_request: list[tuple[str, bool]] = []

    async def validate_account(broker: str, account_id: str) -> str:
        return account_id

    async def resolver(**kwargs) -> ChartWindowResult:
        captured_request.append(
            (kwargs["timeframe"], kwargs["polygon_overlay_enabled"])
        )
        return ChartWindowResult(
            bars=[], timeframe="5s", resolution="5s", is_streaming=True
        )

    subscribed: list[str] = []

    async def subscribe(symbol: str) -> None:
        subscribed.append(symbol)

    monkeypatch.setattr(panel_data_source, "_validate_account", validate_account)
    monkeypatch.setattr(
        panel_data_source,
        "_bot_status",
        lambda broker, sid: BotStatusView(
            strategy_instance_id=sid,
            broker=broker,
            symbol="SPY",
            mode="trade",
            quantity=1,
            running=True,
            phase="ON_DUTY",
            desired_state="RUNNING",
            active_run_id="run-1",
            duty_outcome=None,
            binding_created_at_ms=_NOW - 60_000,
            last_transition_at_ms=_NOW - 60_000,
        ),
    )
    monkeypatch.setattr(panel_data_source, "_read_order_journal", lambda *_args: [])
    monkeypatch.setattr(panel_data_source, "now_ms_utc", lambda: _NOW)
    monkeypatch.setattr(panel_data_source, "live_window", lambda now_ms: (open_ms, close_ms))
    monkeypatch.setattr(panel_data_source, "resolve_chart_window", resolver)
    monkeypatch.setattr(LIVE_BAR_AGGREGATOR, "ensure_subscribed_5s", subscribe)

    result = await panel_data_source.get_live_chart(
        "alpaca", "paper-account", SID, resolution="5s"
    )

    assert captured_request == [("5s", False)]
    assert subscribed == ["SPY"]
    assert result.resolution == "5s"
