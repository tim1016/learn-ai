"""Chart projection — LIVE (today) + bounded Polygon timeframe panes (spec §8).

Two contracts:

- **LIVE** (``build_live_chart``): today's NY session bars for the bot's symbol
  from the IBKR live strain, Polygon-filled when the live feed is down, plus
  today's fill markers. Reuses the existing ``live_chart_window`` resolver
  (the 7-day cap is untouched).

- **Polygon** (``build_history_chart``): a bounded contract with five explicit
  timeframes. Each has a target display-bar count plus a catalog-derived
  indicator warmup budget. The fetch start comes from completed NYSE session
  spans, not calendar-day padding.

Both panes decorate bars with truthful ``ibkr`` / ``polygon`` / ``mixed``
source tags and project fill markers from SQLite-native ``FillRecord`` values.
All timestamps are ``int64 ms UTC``; "today" is the canonical NY trading date.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.contract.models import OrderSide
from app.broker.ibkr.models import IbkrMinuteBar
from app.data_lake.polygon_fetcher import PolygonBar
from app.lean_sidecar.trading_calendar import (
    current_trading_session_window,
    session_open_ms_utc,
    session_start_for_bar_count,
)
from app.schemas.broker_v2_panel import (
    ChartBar,
    ChartFillMarker,
    ChartHistoryResponse,
    ChartHistoryTimeframe,
    ChartLiveResponse,
    ChartOverlayNoticeView,
)
from app.services.dataset_service import INDICATOR_CONFIGS
from app.services.indicator_warmup_policy import configured_indicator_warmup_bars
from app.services.live_chart_window import ChartWindowResult

MS_PER_DAY = 86_400_000

@dataclass(frozen=True)
class _HistoryTimeframeSpec:
    multiplier: int
    timespan: str
    display_bars: int


_HISTORY_TIMEFRAME_SPECS: dict[ChartHistoryTimeframe, _HistoryTimeframeSpec] = {
    "1m": _HistoryTimeframeSpec(1, "minute", 300),
    "15m": _HistoryTimeframeSpec(15, "minute", 300),
    "30m": _HistoryTimeframeSpec(30, "minute", 300),
    "1h": _HistoryTimeframeSpec(1, "hour", 300),
    "1d": _HistoryTimeframeSpec(1, "day", 260),
}

_INDICATOR_WARMUP_BARS = configured_indicator_warmup_bars(INDICATOR_CONFIGS)


class ChartTimeframeError(ValueError):
    """Raised when a Polygon timeframe is outside the closed selector set."""


# A history bar source fetches aggregate bars for a symbol/date-range at a given
# Polygon multiplier/timespan. Injected so tests stay hermetic (no live HTTP).
HistoryBarSource = Callable[[str, date, date, int, str], Awaitable[list[PolygonBar]]]


def coerce_history_timeframe(raw: str) -> ChartHistoryTimeframe:
    """Validate and return a Polygon timeframe from raw query input."""
    value = raw.strip()
    if value not in _HISTORY_TIMEFRAME_SPECS:
        raise ChartTimeframeError(
            "timeframe must be one of 1m, 15m, 30m, 1h, 1d"
        )
    return value  # type: ignore[return-value]


def _ibkr_bar_to_chart_bar(bar: IbkrMinuteBar) -> ChartBar:
    return ChartBar(
        start_ms=bar.start_ms,
        end_ms=bar.end_ms,
        open=str(bar.open),
        high=str(bar.high),
        low=str(bar.low),
        close=str(bar.close),
        volume=int(bar.volume),
        source=bar.source,
    )


def aggregator_bars_to_chart_bars(bars: Sequence[IbkrMinuteBar]) -> list[ChartBar]:
    """Map live-aggregator ring-buffer bars to ``ChartBar`` (§8).

    Canonical implementation: this module (extracted from the LIVE-pane
    mapping originally inlined in ``build_live_chart``). Reused verbatim by
    the bot gallery's ``GalleryHub`` so the two panes never diverge on
    decimal/field handling for the same aggregator bar shape.
    """
    return [_ibkr_bar_to_chart_bar(bar) for bar in bars]


def _polygon_bar_to_chart_bar(bar: PolygonBar, *, span_ms: int) -> ChartBar:
    # Polygon-sourced history bars are truthfully tagged ``polygon`` (§8).
    return ChartBar(
        start_ms=bar.t_ms,
        end_ms=bar.t_ms + span_ms,
        open=str(bar.open),
        high=str(bar.high),
        low=str(bar.low),
        close=str(bar.close),
        volume=int(bar.volume),
        source="polygon",
    )


# Canonical fill→marker projection, shared by this module's LIVE/HISTORY panes
# and the bot gallery wall (``gallery_hub.GalleryHub``) — promoted from a
# module-private helper so the gallery reuses it instead of redefining fill→
# marker mapping (CLAUDE.md single-source-of-truth rule, guiding philosophy #5).
def fill_to_marker(fill: FillRecord) -> ChartFillMarker:
    side = "buy" if fill.side is OrderSide.BUY else "sell"
    return ChartFillMarker(
        filled_at_ms=fill.filled_at_ms,
        side=side,
        quantity=fill.quantity,
        price=fill.fill_price,
        order_ref=fill.order_ref,
        event_key=fill.event_key,
    )


def markers_in_window(
    fills: Sequence[FillRecord],
    *,
    from_ms: int,
    to_ms: int,
) -> list[ChartFillMarker]:
    return [
        fill_to_marker(fill)
        for fill in sorted(fills, key=lambda fill: (fill.filled_at_ms, fill.event_key))
        if from_ms <= fill.filled_at_ms < to_ms
    ]


def live_window(now_ms: int) -> tuple[int, int]:
    """Return the (open_ms, close_ms) window for today's LIVE pane (§8, §15).

    "Today" is the canonical NY trading date. On a session day the window is the
    real session open→close (respecting half-days); when the market is closed
    (weekend/holiday) it falls back to the NY calendar day so the response still
    carries the day's persisted bars and markers without fabricating a session.

    This is the single source of the live window: the caller uses it to bound
    the resolver fetch AND passes the same window into :func:`build_live_chart`,
    so the bars, the markers, and the response's ``trading_date_*`` boundaries
    can never diverge.
    """
    session = current_trading_session_window(now_ms)
    if session is not None:
        return session.open_ms_utc, session.close_ms_utc
    day_start = datetime.fromtimestamp(now_ms / 1000, tz=UTC)
    open_ms = int(
        day_start.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000
    )
    return open_ms, open_ms + MS_PER_DAY


def build_live_chart(
    chart_window: ChartWindowResult,
    fills: Sequence[FillRecord],
    *,
    strategy_instance_id: str,
    symbol: str,
    window: tuple[int, int],
    now_ms: int,
) -> ChartLiveResponse:
    """Build the LIVE pane from bars + SQLite-native fill markers (§8).

    ``chart_window`` is the output of ``live_chart_window.resolve_chart_window``
    (source tags already truthful). ``window`` is the canonical today-window from
    :func:`live_window` — the same one the resolver fetch was bounded by, so bars
    and markers share one window.
    """
    open_ms, close_ms = window

    bars = aggregator_bars_to_chart_bars(chart_window.bars)
    markers = markers_in_window(fills, from_ms=open_ms, to_ms=close_ms)
    notices = [
        ChartOverlayNoticeView(code=notice.code, message=notice.message, source="polygon")
        for notice in chart_window.overlay_notices
    ]
    return ChartLiveResponse(
        strategy_instance_id=strategy_instance_id,
        symbol=symbol,
        trading_date_open_ms=open_ms,
        trading_date_close_ms=close_ms,
        resolution=chart_window.resolution,
        bars=bars,
        fill_markers=markers,
        overlay_notices=notices,
        as_of_ms=now_ms,
    )


@dataclass(frozen=True)
class _HistoryPlan:
    multiplier: int
    timespan: str
    display_from_ms: int
    to_ms: int
    span_ms: int
    display_bars: int
    indicator_bars: int
    fetch_start: date


def _plan_history(timeframe: ChartHistoryTimeframe, now_ms: int) -> _HistoryPlan:
    spec = _HISTORY_TIMEFRAME_SPECS[timeframe]
    span_ms = {"minute": spec.multiplier * 60_000, "hour": spec.multiplier * 3_600_000, "day": spec.multiplier * MS_PER_DAY}[
        spec.timespan
    ]
    indicator_bars = spec.display_bars + _INDICATOR_WARMUP_BARS
    fetch_start = session_start_for_bar_count(
        now_ms,
        target_bars=indicator_bars,
        bar_span_ms=None if spec.timespan == "day" else span_ms,
    )
    display_start = session_start_for_bar_count(
        now_ms,
        target_bars=spec.display_bars,
        bar_span_ms=None if spec.timespan == "day" else span_ms,
    )
    return _HistoryPlan(
        multiplier=spec.multiplier,
        timespan=spec.timespan,
        display_from_ms=session_open_ms_utc(display_start),
        to_ms=now_ms,
        span_ms=span_ms,
        display_bars=spec.display_bars,
        indicator_bars=indicator_bars,
        fetch_start=fetch_start,
    )


def history_fill_window(timeframe: ChartHistoryTimeframe, now_ms: int) -> tuple[int, int]:
    """Return the display-bounded fill interval for one history request."""
    plan = _plan_history(timeframe, now_ms)
    return plan.display_from_ms, plan.to_ms


async def build_history_chart(
    timeframe: ChartHistoryTimeframe,
    fills: Sequence[FillRecord],
    *,
    strategy_instance_id: str,
    symbol: str,
    bar_source: HistoryBarSource,
    now_ms: int,
) -> ChartHistoryResponse:
    """Build the bounded Polygon series for a selected timeframe (§8).

    The returned candles are the newest complete display window for the chosen
    timeframe. Indicator clients therefore receive one coherent candle set per
    selection, rather than resampling or reusing a prior timeframe locally.
    """
    plan = _plan_history(timeframe, now_ms)
    start = plan.fetch_start
    end = datetime.fromtimestamp(plan.to_ms / 1000, tz=UTC).date() + timedelta(days=1)

    polygon_bars = await bar_source(symbol, start, end, plan.multiplier, plan.timespan)
    # A bar is labelled by its close (temporal-rigor.md), so a bar whose span
    # has not elapsed is still open and must not be drawn as a finished candle:
    # at 10:30 the 1h bar starting 10:00 does not close until 11:00.
    polygon_bars = [
        bar
        for bar in polygon_bars
        if bar.t_ms + plan.span_ms <= plan.to_ms
    ]
    polygon_bars.sort(key=lambda bar: bar.t_ms)

    truncated = len(polygon_bars) > plan.display_bars
    polygon_bars = polygon_bars[-plan.indicator_bars :]
    indicator_bars = [
        _polygon_bar_to_chart_bar(bar, span_ms=plan.span_ms)
        for bar in polygon_bars
    ]
    bars = indicator_bars[-plan.display_bars :]
    display_from_ms = bars[0].start_ms if bars else plan.display_from_ms
    markers = markers_in_window(fills, from_ms=display_from_ms, to_ms=plan.to_ms)
    return ChartHistoryResponse(
        strategy_instance_id=strategy_instance_id,
        symbol=symbol,
        timeframe=timeframe,
        from_ms=display_from_ms,
        to_ms=plan.to_ms,
        bars=bars,
        indicator_bars=indicator_bars,
        fill_markers=markers,
        truncated=truncated,
        as_of_ms=now_ms,
    )
