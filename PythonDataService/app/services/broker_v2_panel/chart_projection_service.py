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
``fetch_complete_history_batch`` runs the entire backward widening loop and
every vendor call, and is the fleet-coordinator-role-only entry point reached
through ``app.routers.internal_fleet``'s ``/internal/fleet/history/batch``
operation (or, for the legacy/development ``combined`` posture with no
coordinator/clerk split, called in-process by
``app.services.broker_v2_panel.history_batch_client``).
``build_history_chart`` no longer walks anything itself -- it calls an
injected ``HistoryBatchProvider`` exactly once and turns the one
already-complete batch it gets back into the display/indicator windows,
truncation flag and fill markers, exactly as before. This is a relocation of
the existing walk, not new calendar or candle math.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.contract.models import OrderSide
from app.broker.ibkr.bar_models import IbkrMinuteBar
from app.data_lake.polygon_fetcher import PolygonBar, PolygonFetchError, fetch_aggregate_bars
from app.lean_sidecar.trading_calendar import (
    current_trading_session_window,
    session_close_ms_utc,
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
from app.services.polygon_notice_classifier import (
    classify_polygon_exception,
    missing_polygon_api_key_notice,
)

MS_PER_DAY = 86_400_000
_POLYGON_HISTORY_YEARS = 2
_ET = ZoneInfo("America/New_York")

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

#: The largest ``required_bar_count`` any timeframe's display+warmup policy
#: can produce (issue #2204 FR-007): the internal history-batch request's
#: bound derives from this single source rather than repeating a magic number
#: at the request-schema boundary (``app.routers.internal_fleet``).
MAX_HISTORY_REQUIRED_BAR_COUNT = (
    max(spec.display_bars for spec in _HISTORY_TIMEFRAME_SPECS.values())
    + _INDICATOR_WARMUP_BARS
)


class ChartTimeframeError(ValueError):
    """Raised when a Polygon timeframe is outside the closed selector set."""


class _PolygonFailure(Protocol):
    """Structural type shared by ``PolygonNotice`` and ``ChartOverlayNotice``.

    Lets :func:`_notice_view` convert either the LIVE pane's per-session
    ``live_chart_window.ChartOverlayNotice`` or the HISTORY pane's bare
    ``PolygonNotice`` into the wire view without one caller importing the
    other's notice type.
    """

    code: str
    message: str


def _notice_view(notice: _PolygonFailure) -> ChartOverlayNoticeView:
    return ChartOverlayNoticeView(code=notice.code, message=notice.message, source="polygon")


# A history bar source fetches aggregate bars for a symbol/date-range at a given
# Polygon multiplier/timespan. Injected so tests stay hermetic (no live HTTP).
# Coordinator-only (issue #2204): only `fetch_complete_history_batch` and its
# callers on the fleet-coordinator role ever construct one of these.
HistoryBarSource = Callable[[str, date, date, int, str], Awaitable[list[PolygonBar]]]


@dataclass(frozen=True)
class CompleteHistoryBatch:
    """One already-complete history batch: no further vendor calls needed.

    The wire shape ``app.routers.internal_fleet``'s history-batch operation
    returns and ``app.services.broker_v2_panel.history_batch_client``
    reconstructs on the Clerk side. ``bars`` are sorted ascending by
    ``start_ms`` and already carry the coordinator's ms->ET candle-close math
    (``_history_bar_end_ms``); ``build_history_chart`` only slices and
    truncates them, it never recomputes them.
    """

    bars: list[ChartBar]
    overlay_notices: list[ChartOverlayNoticeView]
    effective_as_of_ms: int


# A history batch provider answers one (symbol, timeframe, required_bar_count,
# as_of_ms) request with a *complete* batch -- the Clerk-side seam
# `build_history_chart` calls exactly once per public history attempt
# (FR-008). The production implementation is either a direct in-process call
# to `fetch_complete_history_batch` (the `combined` posture: no
# coordinator/clerk split) or `RemoteHistoryBatchClient.fetch_batch` (a real
# fleet-enrolled clerk_agent); tests inject a fake.
HistoryBatchProvider = Callable[
    [str, ChartHistoryTimeframe, int, int], Awaitable[CompleteHistoryBatch]
]


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


def _polygon_bar_to_chart_bar(bar: PolygonBar, *, end_ms: int) -> ChartBar:
    # Polygon-sourced history bars are truthfully tagged ``polygon`` (§8).
    return ChartBar(
        start_ms=bar.t_ms,
        end_ms=end_ms,
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
    notices = [_notice_view(notice) for notice in chart_window.overlay_notices]
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


def _subtract_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def _polygon_history_floor(now_ms: int) -> date:
    today = datetime.fromtimestamp(now_ms / 1000, tz=UTC).date()
    return _subtract_years(today, _POLYGON_HISTORY_YEARS)


def _history_bar_end_ms(bar: PolygonBar, plan: _HistoryPlan) -> int:
    if plan.timespan != "day":
        return bar.t_ms + plan.span_ms
    session_date = datetime.fromtimestamp(bar.t_ms / 1000, tz=UTC).astimezone(_ET).date()
    return session_close_ms_utc(session_date)


def _history_bar_is_complete(bar: PolygonBar, plan: _HistoryPlan) -> bool:
    try:
        return _history_bar_end_ms(bar, plan) <= plan.to_ms
    except LookupError:
        return False


async def _fetch_history_bars(
    *,
    symbol: str,
    plan: _HistoryPlan,
    bar_source: HistoryBarSource,
) -> list[PolygonBar]:
    """Fetch backward until the bar budget or Polygon entitlement is exhausted."""

    floor = _polygon_history_floor(plan.to_ms)
    range_start = max(plan.fetch_start, floor)
    entitlement_end = datetime.fromtimestamp(plan.to_ms / 1000, tz=UTC).date() + timedelta(days=1)
    range_end = entitlement_end
    bars_by_start: dict[int, PolygonBar] = {}
    while True:
        batch = await bar_source(
            symbol,
            range_start,
            range_end,
            plan.multiplier,
            plan.timespan,
        )
        bars_by_start.update(
            (bar.t_ms, bar)
            for bar in batch
            if _history_bar_is_complete(bar, plan)
        )
        if len(bars_by_start) >= plan.indicator_bars or range_start <= floor:
            break
        requested_days = max(1, (entitlement_end - range_start).days)
        range_end = range_start
        range_start = max(floor, range_start - timedelta(days=requested_days))
    return sorted(bars_by_start.values(), key=lambda bar: bar.t_ms)


def _history_fetch_plan(
    timeframe: ChartHistoryTimeframe, as_of_ms: int, required_bar_count: int
) -> _HistoryPlan:
    """The walk's own plan for one target bar count (issue #2204).

    Deliberately decoupled from the display+warmup policy: the walk only
    needs a target count to stop at, wherever that count came from.
    ``_plan_history`` (below) derives that count from the indicator-warmup
    policy for the Clerk's own planning; the fleet-coordinator role
    (``fetch_complete_history_batch``) derives it from the Clerk's requested
    ``required_bar_count`` instead, without needing to know anything about
    warmup or display budgets. ``display_from_ms`` is a placeholder here
    (unused by the walk or the completeness/end-ms math) -- only
    ``_plan_history`` fills in the real value.
    """
    spec = _HISTORY_TIMEFRAME_SPECS[timeframe]
    span_ms = {"minute": spec.multiplier * 60_000, "hour": spec.multiplier * 3_600_000, "day": spec.multiplier * MS_PER_DAY}[
        spec.timespan
    ]
    fetch_start = session_start_for_bar_count(
        as_of_ms,
        target_bars=required_bar_count,
        bar_span_ms=None if spec.timespan == "day" else span_ms,
    )
    return _HistoryPlan(
        multiplier=spec.multiplier,
        timespan=spec.timespan,
        display_from_ms=as_of_ms,
        to_ms=as_of_ms,
        span_ms=span_ms,
        display_bars=spec.display_bars,
        indicator_bars=required_bar_count,
        fetch_start=fetch_start,
    )


def _plan_history(timeframe: ChartHistoryTimeframe, now_ms: int) -> _HistoryPlan:
    spec = _HISTORY_TIMEFRAME_SPECS[timeframe]
    indicator_bars = spec.display_bars + _INDICATOR_WARMUP_BARS
    plan = _history_fetch_plan(timeframe, now_ms, indicator_bars)
    display_start = session_start_for_bar_count(
        now_ms,
        target_bars=spec.display_bars,
        bar_span_ms=None if spec.timespan == "day" else plan.span_ms,
    )
    return dataclasses.replace(
        plan, display_from_ms=session_open_ms_utc(display_start)
    )


def history_fill_window(timeframe: ChartHistoryTimeframe, now_ms: int) -> tuple[int, int]:
    """Return the display-bounded fill interval for one history request."""
    plan = _plan_history(timeframe, now_ms)
    return plan.display_from_ms, plan.to_ms


async def fetch_complete_history_batch(
    *,
    symbol: str,
    timeframe: ChartHistoryTimeframe,
    required_bar_count: int,
    as_of_ms: int,
    bar_source: HistoryBarSource,
) -> CompleteHistoryBatch:
    """Run the full backward Polygon widening walk to completion (issue #2204).

    Fleet-coordinator-role-only: this is the function behind
    ``/internal/fleet/history/batch`` (``app.routers.internal_fleet``) and the
    ``combined``-posture local shortcut
    (``app.services.broker_v2_panel.history_batch_client``). It is the exact walk
    ``build_history_chart`` used to run inline before #2204 -- widen backward
    until ``required_bar_count`` bars are collected or the two-year
    entitlement floor is reached -- unchanged in behavior, just callable with
    an explicit target count instead of deriving one from the indicator-warmup
    policy itself (that policy now lives one layer up, in ``_plan_history``,
    which the Clerk uses to compute the ``required_bar_count`` it sends).

    A Polygon fetch failure degrades into a ``polygon_*`` notice with no bars
    (issue #2203) rather than raising -- this function's caller always gets a
    complete, well-formed batch back. Notice codes come from the shared
    ``polygon_notice_classifier`` vocabulary, the same one the LIVE overlay
    uses, so the two panes cannot drift onto different codes.
    """
    plan = _history_fetch_plan(timeframe, as_of_ms, required_bar_count)
    notices: list[ChartOverlayNoticeView] = []
    try:
        polygon_bars = await _fetch_history_bars(
            symbol=symbol,
            plan=plan,
            bar_source=bar_source,
        )
    except PolygonFetchError as exc:
        polygon_bars = []
        notices.append(_notice_view(classify_polygon_exception(exc)))
    bars = [
        _polygon_bar_to_chart_bar(bar, end_ms=_history_bar_end_ms(bar, plan))
        for bar in polygon_bars
    ]
    return CompleteHistoryBatch(
        bars=bars, overlay_notices=notices, effective_as_of_ms=as_of_ms
    )


async def build_coordinator_history_batch(
    *,
    symbol: str,
    timeframe: ChartHistoryTimeframe,
    required_bar_count: int,
    as_of_ms: int,
    polygon_api_key: str,
) -> CompleteHistoryBatch:
    """The fleet-coordinator role's one entry point for a Clerk's request.

    Pairs the live Polygon fetch with :func:`fetch_complete_history_batch`.
    Reused by both the real ``/internal/fleet/history/batch`` route handler
    and the ``combined``-posture in-process shortcut
    (``app.services.broker_v2_panel.history_batch_client``), so there is exactly one place that
    decides what "the coordinator serves one history batch" means.

    A present-but-empty ``polygon_api_key`` (a misconfigured coordinator, or a
    ``combined``-posture deployment with no Polygon key at all) degrades into
    the same ``polygon_api_key_missing`` notice ``build_history_chart`` used
    to raise for directly (issue #2203) -- checked here, before ever touching
    Polygon, because an empty key never reaches the vendor.
    """
    if not polygon_api_key:
        return CompleteHistoryBatch(
            bars=[],
            overlay_notices=[
                _notice_view(missing_polygon_api_key_notice("Polygon history"))
            ],
            effective_as_of_ms=as_of_ms,
        )

    async def _bar_source(
        bar_symbol: str, start: date, end: date, multiplier: int, timespan: str
    ) -> list[PolygonBar]:
        return await fetch_aggregate_bars(
            bar_symbol, start, end, polygon_api_key, multiplier=multiplier, timespan=timespan
        )

    return await fetch_complete_history_batch(
        symbol=symbol,
        timeframe=timeframe,
        required_bar_count=required_bar_count,
        as_of_ms=as_of_ms,
        bar_source=_bar_source,
    )


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
    batch = await batch_provider(symbol, timeframe, plan.indicator_bars, now_ms)
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
