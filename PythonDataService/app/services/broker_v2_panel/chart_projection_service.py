"""Chart projection — LIVE (today) + bounded HISTORY panes (spec §8).

Two contracts:

- **LIVE** (``build_live_chart``): today's NY session bars for the bot's symbol
  from the IBKR live strain, Polygon-filled when the live feed is down, plus
  today's fill markers. Reuses the existing ``live_chart_window`` resolver
  (the 7-day cap is untouched).

- **HISTORY** (``build_history_chart``): a NEW bounded contract with a fixed
  preset → aggregation ladder (1D→1m, 5D→5m, 1M→30m, 3M→1h, 1Y→1d, All→1d) and
  a response-size bound. Never a lifetime of 1-minute bars in one response.

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
from app.lean_sidecar.trading_calendar import current_trading_session_window
from app.schemas.broker_v2_panel import (
    ChartBar,
    ChartFillMarker,
    ChartHistoryPreset,
    ChartHistoryResponse,
    ChartLiveResponse,
    ChartOverlayNoticeView,
)
from app.services.live_chart_window import ChartWindowResult

MS_PER_DAY = 86_400_000

# Preset → (aggregation label, Polygon multiplier, Polygon timespan, lookback ms).
# ``All`` is bounded to a large-but-finite lookback so the response never grows
# without limit; a genuinely older bot is truncated (``truncated=True``).
_HISTORY_LADDER: dict[
    ChartHistoryPreset, tuple[str, int, str, int]
] = {
    "1D": ("1m", 1, "minute", 1 * MS_PER_DAY),
    "5D": ("5m", 5, "minute", 5 * MS_PER_DAY),
    "1M": ("30m", 30, "minute", 31 * MS_PER_DAY),
    "3M": ("1h", 1, "hour", 92 * MS_PER_DAY),
    "1Y": ("1d", 1, "day", 366 * MS_PER_DAY),
    "All": ("1d", 1, "day", 5 * 366 * MS_PER_DAY),
}

# Response-size bound per pane (§8). A preset that would return more bars is
# truncated to the most recent ``_MAX_HISTORY_BARS`` and flagged.
_MAX_HISTORY_BARS = 3_000


class ChartPresetError(ValueError):
    """Raised when a history preset is outside the closed ladder."""


# A history bar source fetches aggregate bars for a symbol/date-range at a given
# Polygon multiplier/timespan. Injected so tests stay hermetic (no live HTTP).
HistoryBarSource = Callable[[str, date, date, int, str], Awaitable[list[PolygonBar]]]


def coerce_history_preset(raw: str) -> ChartHistoryPreset:
    """Validate and return a history preset from raw query input."""
    value = raw.strip()
    if value not in _HISTORY_LADDER:
        raise ChartPresetError(
            "preset must be one of 1D, 5D, 1M, 3M, 1Y, All"
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
    aggregation: str
    multiplier: int
    timespan: str
    from_ms: int
    to_ms: int
    span_ms: int


def _plan_history(preset: ChartHistoryPreset, now_ms: int) -> _HistoryPlan:
    aggregation, multiplier, timespan, lookback_ms = _HISTORY_LADDER[preset]
    span_ms = {"minute": multiplier * 60_000, "hour": multiplier * 3_600_000, "day": multiplier * MS_PER_DAY}[
        timespan
    ]
    return _HistoryPlan(
        aggregation=aggregation,
        multiplier=multiplier,
        timespan=timespan,
        from_ms=max(0, now_ms - lookback_ms),
        to_ms=now_ms,
        span_ms=span_ms,
    )


def history_fill_window(preset: ChartHistoryPreset, now_ms: int) -> tuple[int, int]:
    """Return the exact half-open fill-marker interval for one history pane."""
    plan = _plan_history(preset, now_ms)
    return plan.from_ms, plan.to_ms


async def build_history_chart(
    preset: ChartHistoryPreset,
    fills: Sequence[FillRecord],
    *,
    strategy_instance_id: str,
    symbol: str,
    bar_source: HistoryBarSource,
    now_ms: int,
) -> ChartHistoryResponse:
    """Build the bounded HISTORY pane for a fixed preset (§8).

    Enforces the preset → aggregation ladder and the response-size bound. The
    existing 7-day live resolver is not touched — this endpoint owns its own
    bounded contract. ``bar_source`` is injected so tests stay hermetic.
    """
    plan = _plan_history(preset, now_ms)
    start = datetime.fromtimestamp(plan.from_ms / 1000, tz=UTC).date()
    end = datetime.fromtimestamp(plan.to_ms / 1000, tz=UTC).date() + timedelta(days=1)

    polygon_bars = await bar_source(symbol, start, end, plan.multiplier, plan.timespan)
    polygon_bars = [bar for bar in polygon_bars if plan.from_ms <= bar.t_ms < plan.to_ms]
    polygon_bars.sort(key=lambda bar: bar.t_ms)

    truncated = len(polygon_bars) > _MAX_HISTORY_BARS
    if truncated:
        polygon_bars = polygon_bars[-_MAX_HISTORY_BARS:]

    bars = [_polygon_bar_to_chart_bar(bar, span_ms=plan.span_ms) for bar in polygon_bars]
    markers = markers_in_window(fills, from_ms=plan.from_ms, to_ms=plan.to_ms)
    return ChartHistoryResponse(
        strategy_instance_id=strategy_instance_id,
        symbol=symbol,
        preset=preset,
        aggregation=plan.aggregation,  # type: ignore[arg-type]
        from_ms=plan.from_ms,
        to_ms=plan.to_ms,
        bars=bars,
        fill_markers=markers,
        truncated=truncated,
        as_of_ms=now_ms,
    )
