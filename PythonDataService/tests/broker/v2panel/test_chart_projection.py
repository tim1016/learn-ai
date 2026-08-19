"""Tests for the chart projections (S1, spec §8).

Covers bounded Polygon timeframes and their display-bar counts, the LIVE pane
source-tagging + fill markers, and a regression that the 7-day live resolver
cap is not widened.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.broker.alpaca.adapter import from_alpaca_trade_update
from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.contract.models import OrderSide
from app.data_lake.polygon_fetcher import PolygonBar
from app.lean_sidecar.trading_calendar import (
    expected_sessions,
    session_close_ms_utc,
    session_open_ms_utc,
    session_start_for_bar_count,
)
from app.schemas.broker_bots import BotStatusView
from app.services.broker_v2_panel import (
    chart_projection_service,
    panel_chart_data_source,
    panel_data_source,
)
from app.services.broker_v2_panel.chart_projection_service import (
    ChartTimeframeError,
    aggregator_bars_to_chart_bars,
    build_history_chart,
    build_live_chart,
    coerce_history_timeframe,
    history_fill_window,
    live_window,
)
from app.services.chart_indicator_service import ChartIndicatorService
from app.services.live_chart_window import MAX_CHART_RANGE_MS, ChartWindowResult
from tests.broker.v2panel.fixtures import SID

_NOW = 1_700_000_000_000
_GOOGL_FIXTURE = (
    Path(__file__).parents[2]
    / "fixtures"
    / "golden"
    / "alpaca-sqlite-execution"
    / "googl_round_trip"
)


def _sqlite_fill(
    *,
    event_key: str,
    side: OrderSide = OrderSide.BUY,
    quantity: float = 100.0,
    price: float = 500.0,
    filled_at_ms: int = _NOW,
) -> FillRecord:
    return FillRecord(
        account_id="test-acct",
        sid=SID,
        intent_id="intent",
        order_ref=f"learn-ai/{SID}/v1:intent",
        event_key=event_key,
        symbol="SPY",
        side=side,
        quantity=quantity,
        fill_price=price,
        filled_at_ms=filled_at_ms,
        fee=None,
    )


def _googl_fixture_fills() -> tuple[tuple[FillRecord, ...], int, int, int]:
    """Build chart inputs from the S0 websocket fixture, never JSONL evidence."""
    fixture_input = json.loads((_GOOGL_FIXTURE / "input.json").read_text(encoding="utf-8"))
    expected = json.loads((_GOOGL_FIXTURE / "expected.json").read_text(encoding="utf-8"))
    strategy = fixture_input["strategy_instance"]
    fills: list[FillRecord] = []
    for frame in fixture_input["trade_updates"]:
        payload = frame["data"]
        event = from_alpaca_trade_update(payload)
        order = payload["order"]
        assert event.execution_id is not None
        assert event.quantity is not None
        assert event.price is not None
        fills.append(
            FillRecord(
                account_id=fixture_input["account_id"],
                sid=strategy["strategy_instance_id"],
                intent_id=order["client_order_id"].rsplit(":", 1)[1],
                order_ref=order["client_order_id"],
                event_key=event.execution_id,
                symbol=order["symbol"],
                side=OrderSide(order["side"]),
                quantity=event.quantity,
                fill_price=event.price,
                filled_at_ms=event.occurred_at_ms,
                fee=None,
            )
        )
    session = fixture_input["session"]
    fills_today = expected["projection"]["bot_metrics"][strategy["strategy_instance_id"]]["fills_today"]
    return tuple(fills), int(session["session_open_ms"]), int(session["session_close_ms"]), fills_today


def test_seven_day_live_resolver_cap_unchanged() -> None:
    """Regression: the existing 7-day cap is not widened by the history contract."""
    assert MAX_CHART_RANGE_MS == 7 * 86_400_000


@pytest.mark.parametrize(
    ("timeframe", "multiplier", "timespan", "display_bars"),
    [
        ("1m", 1, "minute", 300),
        ("15m", 15, "minute", 300),
        ("30m", 30, "minute", 300),
        ("1h", 1, "hour", 300),
        ("1d", 1, "day", 260),
    ],
)
async def test_history_timeframe_maps_to_fixed_bar_window(
    timeframe: str,
    multiplier: int,
    timespan: str,
    display_bars: int,
) -> None:
    observed: list[tuple[int, str]] = []

    async def _source(symbol, start, end, source_multiplier, source_timespan):
        observed.append((source_multiplier, source_timespan))
        return []

    result = await build_history_chart(
        timeframe,  # type: ignore[arg-type]
        [],
        strategy_instance_id=SID,
        symbol="SPY",
        bar_source=_source,
        now_ms=_NOW,
    )
    assert result.timeframe == timeframe
    assert observed
    assert set(observed) == {(multiplier, timespan)}
    assert result.truncated is False
    assert display_bars > 0


_TIMEFRAME_SPAN_MS: dict[str, int] = {
    "1m": 60_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "1d": 86_400_000,
}


def _complete_bars(*, span_ms: int, count: int, now_ms: int = _NOW) -> list[PolygonBar]:
    """Build ``count`` bars that have all fully closed by ``now_ms``.

    The newest bar closes exactly at ``now_ms``, so every bar is complete and
    none is filtered by the still-open-candle guard.
    """
    return [
        PolygonBar(
            t_ms=now_ms - (i + 1) * span_ms,
            open=1.0,
            high=2.0,
            low=0.5,
            close=1.5,
            volume=10,
            vwap=1.2,
            n=3,
        )
        for i in reversed(range(count))
    ]


def _complete_daily_bars(*, count: int, now_ms: int = _NOW) -> list[PolygonBar]:
    start = session_start_for_bar_count(
        now_ms,
        target_bars=count,
        bar_span_ms=None,
    )
    end = datetime.fromtimestamp(now_ms / 1000, tz=UTC).date()
    sessions = [
        session
        for session in expected_sessions(start, end)
        if session_close_ms_utc(session) <= now_ms
    ][-count:]
    return [
        PolygonBar(
            t_ms=int(
                datetime.combine(
                    session,
                    time.min,
                    tzinfo=ZoneInfo("America/New_York"),
                ).timestamp()
                * 1000
            ),
            open=1.0,
            high=2.0,
            low=0.5,
            close=1.5,
            volume=10,
            vwap=1.2,
            n=3,
        )
        for session in sessions
    ]


@pytest.mark.parametrize(
    ("timeframe", "display_bars"),
    [("1m", 300), ("15m", 300), ("30m", 300), ("1h", 300), ("1d", 260)],
)
async def test_history_truncates_at_each_configured_display_cap(
    timeframe: str,
    display_bars: int,
) -> None:
    """Each timeframe's own cap is exercised, not just the pytest parameter.

    The previous version asserted ``display_bars > 0``, which passes for any
    cap and so could not catch a regression in ``_HISTORY_TIMEFRAME_SPECS``.
    """
    span_ms = _TIMEFRAME_SPAN_MS[timeframe]
    supplied = (
        _complete_daily_bars(count=display_bars + 1)
        if timeframe == "1d"
        else _complete_bars(span_ms=span_ms, count=display_bars + 1)
    )

    async def _source(symbol, start, end, source_multiplier, source_timespan):
        return list(supplied)

    result = await build_history_chart(
        timeframe,  # type: ignore[arg-type]
        [],
        strategy_instance_id=SID,
        symbol="SPY",
        bar_source=_source,
        now_ms=_NOW,
    )

    assert result.truncated is True
    assert len(result.bars) == display_bars
    # The oldest supplied bar is the one dropped, so the retained window starts
    # one span later than what the source returned.
    assert result.bars[0].start_ms == supplied[1].t_ms
    if timeframe == "1d":
        assert result.bars[-1].end_ms <= _NOW
    else:
        assert result.bars[-1].end_ms == _NOW


async def test_history_keeps_indicator_warmup_outside_the_display_window() -> None:
    supplied = _complete_bars(span_ms=60_000, count=800)

    async def _source(symbol, start, end, source_multiplier, source_timespan):
        return list(supplied)

    result = await build_history_chart(
        "1m",
        [],
        strategy_instance_id=SID,
        symbol="SPY",
        bar_source=_source,
        now_ms=_NOW,
    )

    assert len(result.bars) == 300
    assert len(result.indicator_bars) >= 800
    assert result.indicator_bar_budget == 2_800
    assert result.indicator_bar_budget_satisfied is False
    assert result.indicator_bars[-300:] == result.bars
    _, indicators = ChartIndicatorService().compute(
        "SPY",
        [
            {
                "t": bar.end_ms,
                "o": float(bar.open),
                "h": float(bar.high),
                "l": float(bar.low),
                "c": float(bar.close),
                "v": bar.volume,
            }
            for bar in result.indicator_bars
        ],
        [{"name": "ema", "params": {"length": 500}}],
    )
    assert [indicator["id"] for indicator in indicators] == ["ema_500"]


async def test_history_extends_backward_when_sparse_aggregates_underfill_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(chart_projection_service, "_INDICATOR_WARMUP_BARS", 3)
    supplied = _complete_bars(span_ms=60_000, count=303)
    calls: list[tuple[date, date]] = []

    async def _source(symbol, start, end, source_multiplier, source_timespan):
        calls.append((start, end))
        return list(supplied[-300:] if len(calls) == 1 else supplied[:3])

    result = await build_history_chart(
        "1m",
        [],
        strategy_instance_id=SID,
        symbol="ILLQ",
        bar_source=_source,
        now_ms=_NOW,
    )

    assert len(calls) == 2
    assert len(result.indicator_bars) == 303
    assert result.indicator_bar_budget == 303
    assert result.indicator_bar_budget_satisfied is True


async def test_history_never_fetches_before_polygon_two_year_entitlement() -> None:
    starts: list[date] = []

    async def _source(symbol, start, end, source_multiplier, source_timespan):
        starts.append(start)
        return []

    result = await build_history_chart(
        "1d",
        [],
        strategy_instance_id=SID,
        symbol="SPY",
        bar_source=_source,
        now_ms=_NOW,
    )

    assert starts == [date(2021, 11, 14)]
    assert result.indicator_bar_budget_satisfied is False


async def test_daily_bar_completes_at_session_close_not_midnight_plus_one_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        chart_projection_service._HISTORY_TIMEFRAME_SPECS,
        "1d",
        chart_projection_service._HistoryTimeframeSpec(1, "day", 1),
    )
    monkeypatch.setattr(chart_projection_service, "_INDICATOR_WARMUP_BARS", 0)
    now_ms = int(datetime(2026, 1, 6, 22, tzinfo=UTC).timestamp() * 1000)
    bar_start_ms = int(
        datetime(2026, 1, 6, tzinfo=ZoneInfo("America/New_York")).timestamp()
        * 1000
    )
    daily_bar = PolygonBar(
        t_ms=bar_start_ms,
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        volume=10,
        vwap=1.2,
        n=3,
    )

    async def _source(symbol, start, end, source_multiplier, source_timespan):
        return [daily_bar]

    result = await build_history_chart(
        "1d",
        [],
        strategy_instance_id=SID,
        symbol="SPY",
        bar_source=_source,
        now_ms=now_ms,
    )

    assert [bar.start_ms for bar in result.bars] == [bar_start_ms]
    assert result.indicator_bar_budget_satisfied is True


@pytest.mark.parametrize("timeframe", ["1m", "15m", "30m", "1h", "1d"])
async def test_history_excludes_the_still_open_candle(timeframe: str) -> None:
    """A bar whose span has not elapsed is not a finished candle.

    Regression for a filter that tested only the bar's start: at 10:30 the 1h
    bar opened at 10:00 was returned as complete even though it closes at
    11:00. Bars are labelled by their close (``temporal-rigor.md``), so an
    unelapsed span must be withheld rather than drawn.
    """
    span_ms = _TIMEFRAME_SPAN_MS[timeframe]
    closed = (
        _complete_daily_bars(count=2)
        if timeframe == "1d"
        else _complete_bars(span_ms=span_ms, count=2)
    )
    still_open_start = (
        int(
            datetime.combine(
                datetime.fromtimestamp(_NOW / 1000, tz=UTC).date(),
                time.min,
                tzinfo=ZoneInfo("America/New_York"),
            ).timestamp()
            * 1000
        )
        if timeframe == "1d"
        else _NOW - span_ms // 2
    )
    still_open = PolygonBar(
        t_ms=still_open_start,
        open=9.0,
        high=9.0,
        low=9.0,
        close=9.0,
        volume=1,
        vwap=9.0,
        n=1,
    )

    async def _source(symbol, start, end, source_multiplier, source_timespan):
        return [*closed, still_open]

    result = await build_history_chart(
        timeframe,  # type: ignore[arg-type]
        [],
        strategy_instance_id=SID,
        symbol="SPY",
        bar_source=_source,
        now_ms=_NOW,
    )

    assert [bar.start_ms for bar in result.bars] == [bar.t_ms for bar in closed]
    assert all(bar.end_ms <= _NOW for bar in result.bars)


def test_unknown_timeframe_is_rejected() -> None:
    with pytest.raises(ChartTimeframeError):
        coerce_history_timeframe("5m")


def test_history_fill_window_uses_only_the_display_budget() -> None:
    expected_start = session_start_for_bar_count(
        _NOW,
        target_bars=300,
        bar_span_ms=60_000,
    )

    from_ms, to_ms = history_fill_window("1m", _NOW)

    assert from_ms == session_open_ms_utc(expected_start)
    assert to_ms == _NOW


async def test_history_bars_are_polygon_tagged_and_bounded() -> None:
    # Return more than the 300 displayed 1m bars; the response must keep only
    # the newest visual window rather than returning the entire fetched range.
    bars = [
        PolygonBar(
            t_ms=_NOW - (i + 1) * 60_000,
            open=1.0,
            high=2.0,
            low=0.5,
            close=1.5,
            volume=10,
            vwap=1.2,
            n=3,
        )
        for i in range(350)
    ]

    async def _source(symbol, start, end, multiplier, timespan):
        return bars

    result = await build_history_chart(
        "1m",
        [],
        strategy_instance_id=SID,
        symbol="SPY",
        bar_source=_source,
        now_ms=_NOW,
    )
    assert result.truncated is True
    assert len(result.bars) == 300
    assert all(bar.source == "polygon" for bar in result.bars)
    # Bars are sorted ascending by start_ms after truncation.
    starts = [bar.start_ms for bar in result.bars]
    assert starts == sorted(starts)
    assert result.from_ms == starts[0]


async def test_history_fill_markers_within_window() -> None:
    # Derive the boundary from the timeframe's own window rather than repeating
    # a lookback constant here: a test that hard-codes the window silently stops
    # testing exclusion the moment the lookback widens.
    window_from_ms, _ = history_fill_window("1m", _NOW)
    fills = [
        _sqlite_fill(event_key="exec-in-window", filled_at_ms=_NOW - 30_000),
        # Before the 1m fetch window opens → excluded.
        _sqlite_fill(
            event_key="exec-outside-window",
            quantity=50,
            price=400.0,
            filled_at_ms=window_from_ms - 60_000,
        ),
    ]

    async def _source(symbol, start, end, multiplier, timespan):
        return []

    result = await build_history_chart(
        "1m",
        fills,
        strategy_instance_id=SID,
        symbol="SPY",
        bar_source=_source,
        now_ms=_NOW,
    )
    assert len(result.fill_markers) == 1
    assert result.fill_markers[0].side == "buy"
    assert result.fill_markers[0].price == 500.0


def test_aggregator_bars_to_chart_bars_maps_fields_and_decimals() -> None:
    """Extracted helper (shared with GalleryHub): exact field/decimal mapping."""
    from decimal import Decimal

    from app.broker.ibkr.models import IbkrMinuteBar

    bar = IbkrMinuteBar(
        symbol="SPY",
        start_ms=_NOW - 60_000,
        end_ms=_NOW,
        open=Decimal("500.10"),
        high=Decimal("501.25"),
        low=Decimal("499.75"),
        close=Decimal("500.50"),
        volume=1234,
        fetched_at_ms=_NOW,
        source="ibkr",
    )

    result = aggregator_bars_to_chart_bars([bar])

    assert len(result) == 1
    chart_bar = result[0]
    assert chart_bar.start_ms == _NOW - 60_000
    assert chart_bar.end_ms == _NOW
    assert chart_bar.open == "500.10"
    assert chart_bar.high == "501.25"
    assert chart_bar.low == "499.75"
    assert chart_bar.close == "500.50"
    assert chart_bar.volume == 1234
    assert chart_bar.source == "ibkr"


def test_aggregator_bars_to_chart_bars_empty_input_returns_empty_list() -> None:
    assert aggregator_bars_to_chart_bars([]) == []


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
    fills = [_sqlite_fill(event_key="exec-live", filled_at_ms=_NOW - 30_000)]

    result = build_live_chart(
        chart_window,
        fills,
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


def test_live_chart_markers_match_googl_fixture_fills_today() -> None:
    """The S0 GOOGL execution slices produce one chart marker each in-session."""
    from app.services.live_chart_window import ChartWindowResult

    fills, open_ms, close_ms, fills_today = _googl_fixture_fills()
    chart = build_live_chart(
        ChartWindowResult(bars=[], timeframe="1m", resolution="1m", is_streaming=False),
        fills,
        strategy_instance_id="sqlite-cohort-googl-0810",
        symbol="GOOGL",
        window=(open_ms, close_ms),
        now_ms=close_ms - 1,
    )

    assert len(chart.fill_markers) == fills_today == 3
    assert [marker.order_ref for marker in chart.fill_markers] == [
        "learn-ai/sqlite-cohort-googl-0810/v1:googl-buy-intent-1",
        "learn-ai/sqlite-cohort-googl-0810/v1:googl-buy-intent-1",
        "learn-ai/sqlite-cohort-googl-0810/v1:googl-sell-intent-1",
    ]


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

    async def symbol_and_fills(*_args, **_kwargs):
        return "SPY", ()

    async def unexpected_resolver(**kwargs) -> ChartWindowResult:
        pytest.fail("the range resolver must not run before the session opens")

    monkeypatch.setattr(panel_chart_data_source, "resolve_symbol_and_fills", symbol_and_fills)
    monkeypatch.setattr(panel_chart_data_source, "now_ms_utc", lambda: _NOW)
    monkeypatch.setattr(
        panel_chart_data_source,
        "live_window",
        lambda now_ms: (open_ms, close_ms),
    )
    monkeypatch.setattr(
        panel_chart_data_source,
        "resolve_chart_window",
        unexpected_resolver,
    )

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

    async def symbol_and_fills(*_args, **_kwargs):
        return "SPY", ()

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

    monkeypatch.setattr(panel_chart_data_source, "resolve_symbol_and_fills", symbol_and_fills)
    monkeypatch.setattr(panel_chart_data_source, "now_ms_utc", lambda: _NOW)
    monkeypatch.setattr(
        panel_chart_data_source,
        "live_window",
        lambda now_ms: (open_ms, close_ms),
    )
    monkeypatch.setattr(panel_chart_data_source, "resolve_chart_window", resolver)
    monkeypatch.setattr(LIVE_BAR_AGGREGATOR, "ensure_subscribed_5s", subscribe)

    result = await panel_data_source.get_live_chart(
        "alpaca", "paper-account", SID, resolution="5s"
    )

    assert captured_request == [("5s", False)]
    assert subscribed == ["SPY"]
    assert result.resolution == "5s"


async def test_live_snapshot_uses_its_captured_sqlite_fills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_fills = (_sqlite_fill(event_key="sqlite-captured"),)
    captured: list[tuple[FillRecord, ...]] = []

    async def panel_with_entries(*_args: object, **_kwargs: object):
        return SimpleNamespace(symbol="SPY"), [], expected_fills

    async def build_from_fills(
        _sid: str,
        _symbol: str,
        fills: tuple[FillRecord, ...],
        **_kwargs: object,
    ) -> SimpleNamespace:
        captured.append(fills)
        return SimpleNamespace()

    monkeypatch.setattr(
        panel_chart_data_source,
        "get_panel_with_chart_fills",
        panel_with_entries,
    )
    monkeypatch.setattr(panel_chart_data_source, "_build_live_chart_from_fills", build_from_fills)

    panel, _chart = await panel_chart_data_source.get_live_snapshot_parts(
        "alpaca",
        "paper-account",
        SID,
        resolution="1m",
    )

    assert panel.symbol == "SPY"
    assert captured == [expected_fills]


async def test_active_history_uses_sqlite_chart_evidence_not_legacy_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_fills = (_sqlite_fill(event_key="sqlite-history", filled_at_ms=_NOW - 1),)
    calls: list[tuple[str, str, str, int, int]] = []
    status = BotStatusView(
        strategy_instance_id=SID,
        broker="alpaca",
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
    )

    async def validate_account(_broker: str, account_id: str) -> str:
        return account_id

    async def chart_evidence(
        broker: str,
        account_id: str,
        sid: str,
        *,
        from_ms: int,
        to_ms: int,
    ) -> SimpleNamespace:
        calls.append((broker, account_id, sid, from_ms, to_ms))
        return SimpleNamespace(status=status, fills=SimpleNamespace(fills=expected_fills))

    async def empty_bars(*_args: object, **_kwargs: object) -> list[PolygonBar]:
        return []

    monkeypatch.setattr(
        panel_chart_data_source,
        "validate_panel_account_scope",
        validate_account,
    )
    monkeypatch.setattr(panel_chart_data_source, "now_ms_utc", lambda: _NOW)
    monkeypatch.setattr(
        panel_chart_data_source,
        "history_fill_window",
        lambda timeframe, now_ms: (_NOW - 86_400_000, now_ms),
    )
    monkeypatch.setattr(panel_chart_data_source, "read_sqlite_chart_evidence", chart_evidence)
    monkeypatch.setattr(panel_chart_data_source, "fetch_aggregate_bars", empty_bars)

    result = await panel_chart_data_source.get_history_chart(
        "alpaca",
        "paper-account",
        SID,
        "1m",
    )

    assert calls == [("alpaca", "paper-account", SID, _NOW - 86_400_000, _NOW)]
    assert [marker.order_ref for marker in result.fill_markers] == [expected_fills[0].order_ref]
