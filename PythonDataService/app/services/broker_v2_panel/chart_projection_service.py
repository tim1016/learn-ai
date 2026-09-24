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

Issue #2204 splits the HISTORY contract's Polygon walk into two layers:
``app.services.broker_v2_panel.history_batch_walk.fetch_complete_history_batch``
runs the entire backward widening loop and every vendor call, and is the
fleet-coordinator-role-only entry point reached through
``app.routers.internal_fleet``'s ``/internal/fleet/history/batch`` operation
(or, for the legacy/development ``combined`` posture with no
coordinator/clerk split, called in-process by
``app.services.broker_v2_panel.history_batch_client``). That module -- not
this one -- imports the Polygon vendor fetch (issue #2204 gate F5): a
``clerk_agent`` process's history assembly reaches this module and no
further, so the vendor fetch is structurally unreachable from it, not merely
unused at runtime. ``build_history_chart`` no longer walks anything itself
-- it calls an injected ``HistoryBatchProvider`` exactly once and turns the
one already-complete batch it gets back into the display/indicator windows,
truncation flag and fill markers, exactly as before. This is a relocation of
the existing walk, not new calendar or candle math.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.contract.models import OrderSide
from app.broker.ibkr.bar_models import IbkrMinuteBar
from app.lean_sidecar.trading_calendar import (
    current_trading_session_window,
    session_open_ms_utc,
    session_start_for_bar_count,
)
from app.schemas.broker_v2_panel import (
    ChartBar,
    ChartFeedView,
    ChartFillMarker,
    ChartHistoryResponse,
    ChartHistoryTimeframe,
    ChartLiveResponse,
    ChartOverlayNoticeView,
)
from app.schemas.fleet_history_batch import HistoryBatchQuery, HistoryBatchResponse
from app.services.dataset_service import INDICATOR_CONFIGS
from app.services.indicator_warmup_policy import configured_indicator_warmup_bars
from app.services.live_chart_window import ChartFeedState, ChartFeedStatus, ChartWindowResult

MS_PER_DAY = 86_400_000

@dataclass(frozen=True)
class _HistoryTimeframeSpec:
    multiplier: int
    timespan: str
    display_bars: int


#: The closed HISTORY timeframe vocabulary's display/vendor parameters.
#: Public: ``app.services.broker_v2_panel.history_batch_walk`` (issue #2204
#: gate F5's coordinator-only walk) imports this too, for the
#: multiplier/timespan its own walk-plan needs -- one source, so the two
#: sides cannot drift onto different per-timeframe bar spans.
HISTORY_TIMEFRAME_SPECS: dict[ChartHistoryTimeframe, _HistoryTimeframeSpec] = {
    "1m": _HistoryTimeframeSpec(1, "minute", 300),
    "15m": _HistoryTimeframeSpec(15, "minute", 300),
    "30m": _HistoryTimeframeSpec(30, "minute", 300),
    "1h": _HistoryTimeframeSpec(1, "hour", 300),
    "1d": _HistoryTimeframeSpec(1, "day", 260),
}

_INDICATOR_WARMUP_BARS = configured_indicator_warmup_bars(INDICATOR_CONFIGS)

#: The largest ``required_bar_count`` any timeframe's display+warmup policy
#: can produce (issue #2204 FR-007): the internal history-batch request's
#: bound derives from this single source rather than repeating a magic number
#: at the request-schema boundary (``app.schemas.fleet_history_batch``).
MAX_HISTORY_REQUIRED_BAR_COUNT = (
    max(spec.display_bars for spec in HISTORY_TIMEFRAME_SPECS.values())
    + _INDICATOR_WARMUP_BARS
)


class ChartTimeframeError(ValueError):
    """Raised when a Polygon timeframe is outside the closed selector set."""


class _PolygonFailure(Protocol):
    """Structural type shared by ``PolygonNotice`` and ``ChartOverlayNotice``.

    Lets :func:`notice_view` convert either the LIVE pane's per-session
    ``live_chart_window.ChartOverlayNotice`` or the HISTORY pane's bare
    ``PolygonNotice`` into the wire view without one caller importing the
    other's notice type.
    """

    code: str
    message: str


def notice_view(notice: _PolygonFailure) -> ChartOverlayNoticeView:
    """The one Polygon-failure -> wire-view conversion (public: also used by
    ``app.services.broker_v2_panel.history_batch_walk``, issue #2204 gate F5)."""
    return ChartOverlayNoticeView(code=notice.code, message=notice.message, source="polygon")


# A history batch provider answers one query with a *complete* batch -- the
# Clerk-side seam `build_history_chart` calls exactly once per public history
# attempt (FR-008). The production implementation is either a direct
# in-process call to `history_batch_walk.fetch_complete_history_batch` (the
# `combined`/`fleet_coordinator` posture) or `RemoteHistoryBatchClient.fetch_batch`
# (a real fleet-enrolled clerk_agent); tests inject a fake. Takes the one
# validated `HistoryBatchQuery` object, not four positional primitives two of
# which are bare ints (issue #2204 gate F2).
HistoryBatchProvider = Callable[[HistoryBatchQuery], Awaitable[HistoryBatchResponse]]


def coerce_history_timeframe(raw: str) -> ChartHistoryTimeframe:
    """Validate and return a Polygon timeframe from raw query input."""
    value = raw.strip()
    if value not in HISTORY_TIMEFRAME_SPECS:
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


@dataclass(frozen=True)
class _ChartFeedPolicy:
    """How one chart-line state reads (#2355): its copy, and whether it speaks.

    ``show_notice`` puts the chart's notice on screen; ``attention_required``
    makes that notice an alarm. The client reads both and keeps no state list
    of its own.
    """

    headline: str
    explanation: str
    next_step: str | None
    show_notice: bool
    attention_required: bool


# The chart line is separate from the bot's feed, so a chart that stopped
# drawing says what froze: the chart's own view, not necessarily the bot.
_CHART_FEED_POLICY: dict[ChartFeedState, _ChartFeedPolicy] = {
    "LIVE": _ChartFeedPolicy(
        "Chart feed live",
        "The chart's IBKR bar line is delivering bars within its expected cadence.",
        None,
        show_notice=False,
        attention_required=False,
    ),
    "NOT_EXPECTED": _ChartFeedPolicy(
        "No live chart bar expected",
        "The chart draws regular-session IBKR bars; none is due now.",
        None,
        show_notice=False,
        attention_required=False,
    ),
    "STARTING": _ChartFeedPolicy(
        "Chart feed starting",
        "The chart's IBKR bar line is waiting for its first bar of the session.",
        None,
        show_notice=True,
        attention_required=False,
    ),
    "STALLED": _ChartFeedPolicy(
        "Chart feed stalled",
        "The chart's IBKR bar line has not delivered a bar within its expected "
        "cadence, so the chart has stopped at its last candle. The bot's feed "
        "is a separate line.",
        "Do not read the chart as current. The service restarts the line on its "
        "own; if it stays stalled, check the Gateway connection and the IBKR "
        "real-time bar line capacity.",
        show_notice=True,
        attention_required=True,
    ),
    "ERRORED": _ChartFeedPolicy(
        "Chart feed interrupted",
        "The chart's IBKR bar line failed and is being retried, so the chart has "
        "stopped at its last candle. The bot's feed is a separate line.",
        "Do not read the chart as current. If the error persists, check the "
        "Gateway connection and the IBKR real-time bar line capacity.",
        show_notice=True,
        attention_required=True,
    ),
    "RECOVERING": _ChartFeedPolicy(
        "Chart feed recovering",
        "The chart's IBKR bar line is being resubscribed after a broker "
        "reconnect; candles missed meanwhile are not backfilled.",
        "Do not read the chart as current until it draws a new candle.",
        show_notice=True,
        attention_required=True,
    ),
}


def chart_feed_view(feed: ChartFeedStatus) -> ChartFeedView:
    """Present the chart line's own state with backend-authored copy (#2355)."""
    policy = _CHART_FEED_POLICY[feed.state]
    return ChartFeedView(
        state=feed.state,
        headline=policy.headline,
        explanation=policy.explanation,
        next_step=policy.next_step,
        show_notice=policy.show_notice,
        attention_required=policy.attention_required,
        last_bar_at_ms=feed.last_bar_ms,
        last_error=feed.last_error,
    )


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
    notices = [notice_view(notice) for notice in chart_window.overlay_notices]
    return ChartLiveResponse(
        strategy_instance_id=strategy_instance_id,
        symbol=symbol,
        trading_date_open_ms=open_ms,
        trading_date_close_ms=close_ms,
        resolution=chart_window.resolution,
        bars=bars,
        fill_markers=markers,
        overlay_notices=notices,
        feed=chart_feed_view(chart_window.feed),
        as_of_ms=now_ms,
    )


@dataclass(frozen=True)
class _HistoryPlan:
    """The Clerk's own display-only plan for one HISTORY request.

    Issue #2204 gate F5: no walk fields (multiplier, timespan, span,
    fetch_start) and no placeholder -- those belong entirely to the
    coordinator-only ``history_batch_walk._HistoryWalkPlan``, which this
    Clerk-side type shares nothing with. This plan only ever bounds the
    display/fill window and the indicator-bar budget the Clerk asks the
    batch provider for.
    """

    display_from_ms: int
    to_ms: int
    display_bars: int
    indicator_bars: int


def _plan_history(timeframe: ChartHistoryTimeframe, now_ms: int) -> _HistoryPlan:
    spec = HISTORY_TIMEFRAME_SPECS[timeframe]
    indicator_bars = spec.display_bars + _INDICATOR_WARMUP_BARS
    span_ms = {
        "minute": spec.multiplier * 60_000,
        "hour": spec.multiplier * 3_600_000,
        "day": spec.multiplier * MS_PER_DAY,
    }[spec.timespan]
    display_start = session_start_for_bar_count(
        now_ms,
        target_bars=spec.display_bars,
        bar_span_ms=None if spec.timespan == "day" else span_ms,
    )
    return _HistoryPlan(
        display_from_ms=session_open_ms_utc(display_start),
        to_ms=now_ms,
        display_bars=spec.display_bars,
        indicator_bars=indicator_bars,
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
    batch_provider: HistoryBatchProvider,
    now_ms: int,
) -> ChartHistoryResponse:
    """Build the bounded Polygon series for a selected timeframe (§8).

    The returned candles are the newest complete display window for the chosen
    timeframe. Indicator clients therefore receive one coherent candle set per
    selection, rather than resampling or reusing a prior timeframe locally.

    Issue #2204: this function no longer walks Polygon itself. It calls
    ``batch_provider`` exactly once -- one internal request per public history
    attempt, however many vendor calls the fleet-coordinator role needed to
    satisfy it -- and turns the one complete batch it gets back into the
    display/indicator windows and truncation flag. A batch's
    ``overlay_notices`` (a Polygon failure, or ``coordinator_unavailable`` if
    the internal hop itself failed) pass straight through with no bars
    fabricated, exactly as the old inline classification did (issue #2203).
    """
    plan = _plan_history(timeframe, now_ms)
    query = HistoryBatchQuery(
        symbol=symbol,
        timeframe=timeframe,
        required_bar_count=plan.indicator_bars,
        as_of_ms=now_ms,
    )
    batch = await batch_provider(query)
    notices = list(batch.overlay_notices)
    polygon_bars = batch.bars

    truncated = len(polygon_bars) > plan.display_bars
    indicator_bars = polygon_bars[-plan.indicator_bars :]
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
        indicator_bar_budget=plan.indicator_bars,
        indicator_bar_budget_satisfied=len(indicator_bars) >= plan.indicator_bars,
        fill_markers=markers,
        truncated=truncated,
        overlay_notices=notices,
        as_of_ms=now_ms,
    )
