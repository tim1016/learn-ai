"""The coordinator-owned backward Polygon widening walk (issue #2204 gate F5).

Everything here runs on the fleet-coordinator role only: the entire history
walk relocated out of ``chart_projection_service`` (which the Clerk imports
too), so a ``clerk_agent`` process never has the vendor fetch reachable from
its own import graph -- not merely unused at runtime behind a role check, but
structurally absent from the module the Clerk's own history assembly imports
(``app.services.broker_v2_panel.panel_chart_data_source`` ->
``chart_projection_service``, which no longer imports
``app.data_lake.polygon_fetcher`` at all).

Reached from exactly two callers, both coordinator-role: the real
``/internal/fleet/history/batch`` route
(``app.routers.internal_fleet.history_batch``) and the ``combined``-posture
in-process shortcut (``app.services.broker_v2_panel.history_batch_client``'s
``_local_batch_provider``, selected when this process's own fleet role is
``combined`` or ``fleet_coordinator`` -- a positive match, not merely "is not
a clerk agent").

This is a relocation of the pre-#2204 walk ``build_history_chart`` used to
run inline, not new calendar or candle math: :func:`fetch_complete_history_batch`
is the exact widen-backward-until-satisfied loop, unchanged in behavior.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.broker.fleet.history_batch import HISTORY_BATCH_INNER_TIMEOUT_S
from app.data_lake.polygon_fetcher import (
    PolygonBar,
    PolygonFetchError,
    fetch_aggregate_bars,
    polygon_history_floor,
)
from app.lean_sidecar.trading_calendar import session_close_ms_utc, session_start_for_bar_count
from app.schemas.broker_v2_panel import ChartBar, ChartHistoryTimeframe
from app.schemas.fleet_history_batch import HistoryBatchResponse
from app.services.broker_v2_panel.chart_projection_service import (
    HISTORY_TIMEFRAME_SPECS,
    MS_PER_DAY,
    notice_view,
)
from app.services.polygon_notice_classifier import (
    classify_polygon_exception,
    missing_polygon_api_key_notice,
)
from app.utils.session_anchors import et_date_at_ms
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

# A history bar source fetches aggregate bars for a symbol/date-range at a given
# Polygon multiplier/timespan. Injected so tests stay hermetic (no live HTTP).
# Coordinator-only (issue #2204): only `fetch_complete_history_batch` and its
# callers on the fleet-coordinator role ever construct one of these.
HistoryBarSource = Callable[[str, date, date, int, str], Awaitable[list[PolygonBar]]]


@dataclass(frozen=True)
class _HistoryWalkPlan:
    """The coordinator's own plan for one backward widening walk.

    Deliberately carries only what the walk itself needs -- no display
    fields, no placeholder, no ``dataclasses.replace`` -- unlike the Clerk's
    display-only plan (``chart_projection_service._HistoryPlan``), which this
    type does not share (issue #2204 gate F5).
    """

    multiplier: int
    timespan: str
    span_ms: int
    to_ms: int
    target_count: int
    fetch_start: date


def span_ms_for(multiplier: int, timespan: str) -> int:
    """The wire span, in ms, for one ``(multiplier, timespan)`` pair.

    The one span-map definition: shared by this module's own walk plan
    below and by the qualification-only recorded-history generator
    (``app.services.broker_v2_panel.qualification_recorded_history``, issue
    #2206), which reuses this exact function rather than carrying a second
    copy that could drift.
    """
    if timespan == "minute":
        return multiplier * 60_000
    if timespan == "hour":
        return multiplier * 3_600_000
    if timespan == "day":
        return multiplier * MS_PER_DAY
    raise ValueError(f"unsupported timespan: {timespan!r}")


def _plan_history_walk(
    timeframe: ChartHistoryTimeframe, as_of_ms: int, required_bar_count: int
) -> _HistoryWalkPlan:
    """The walk's own plan for one target bar count (issue #2204).

    Decoupled from the display+warmup policy: the walk only needs a target
    count to stop at, wherever that count came from -- the Clerk's own
    ``_plan_history`` derives it from the indicator-warmup policy, sent over
    the wire as ``required_bar_count``.
    """
    spec = HISTORY_TIMEFRAME_SPECS[timeframe]
    span_ms = span_ms_for(spec.multiplier, spec.timespan)
    fetch_start = session_start_for_bar_count(
        as_of_ms,
        target_bars=required_bar_count,
        bar_span_ms=None if spec.timespan == "day" else span_ms,
    )
    return _HistoryWalkPlan(
        multiplier=spec.multiplier,
        timespan=spec.timespan,
        span_ms=span_ms,
        to_ms=as_of_ms,
        target_count=required_bar_count,
        fetch_start=fetch_start,
    )


def _history_bar_end_ms(bar: PolygonBar, plan: _HistoryWalkPlan) -> int:
    if plan.timespan != "day":
        return bar.t_ms + plan.span_ms
    session_date = datetime.fromtimestamp(bar.t_ms / 1000, tz=UTC).astimezone(_ET).date()
    return session_close_ms_utc(session_date)


def _history_bar_is_complete(bar: PolygonBar, plan: _HistoryWalkPlan) -> bool:
    try:
        return _history_bar_end_ms(bar, plan) <= plan.to_ms
    except LookupError:
        return False


def _polygon_bar_to_chart_bar(bar: PolygonBar, *, end_ms: int) -> ChartBar:
    # Polygon-sourced history bars are truthfully tagged ``polygon`` (ADR 0032 amendment 1).
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


async def _fetch_history_bars(
    *,
    symbol: str,
    plan: _HistoryWalkPlan,
    bar_source: HistoryBarSource,
    floor: date,
) -> list[PolygonBar]:
    """Fetch backward until the bar budget is met or ``floor``, the provider's first day, is reached."""

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
        if len(bars_by_start) >= plan.target_count or range_start <= floor:
            break
        requested_days = max(1, (entitlement_end - range_start).days)
        range_end = range_start
        range_start = max(floor, range_start - timedelta(days=requested_days))
    return sorted(bars_by_start.values(), key=lambda bar: bar.t_ms)


async def fetch_complete_history_batch(
    *,
    symbol: str,
    timeframe: ChartHistoryTimeframe,
    required_bar_count: int,
    as_of_ms: int,
    bar_source: HistoryBarSource,
    today: date,
    deadline_s: float = HISTORY_BATCH_INNER_TIMEOUT_S,
) -> HistoryBatchResponse:
    """Run the full backward Polygon widening walk to completion (issue #2204).

    Fleet-coordinator-role-only: this is the function behind
    ``/internal/fleet/history/batch`` (``app.routers.internal_fleet``) and the
    ``combined``-posture local shortcut
    (``app.services.broker_v2_panel.history_batch_client``). It is the exact walk
    ``build_history_chart`` used to run inline before #2204 -- widen backward
    until ``required_bar_count`` bars are collected or the provider's history
    floor is reached -- relocated off the module the Clerk imports (gate F5).

    ``today`` is the ET date the provider's history is counted from
    (:func:`polygon_history_floor`). It is the real today, not the date of
    ``as_of_ms``: a finished run's tape ends in the past, and a walk that
    reached five years behind that end would ask for days the provider
    refuses, which leaves the whole batch with no bars.

    A Polygon fetch failure degrades into a ``polygon_*`` notice with no bars
    (issue #2203) rather than raising -- this function's caller always gets a
    complete, well-formed batch back. Notice codes come from the shared
    ``polygon_notice_classifier`` vocabulary, the same one the LIVE overlay
    uses, so the two panes cannot drift onto different codes.

    The whole walk is bounded by ``deadline_s``, the same bound the Clerk
    waits for this answer (``HISTORY_BATCH_INNER_TIMEOUT_S``): each vendor
    call has its own 30 s timeout, but a widening walk makes up to a dozen of
    them, and one that outlasted the Clerk's wait would run on with nobody to
    hand its bars to. A walk that reaches the deadline is cancelled and
    degrades like any other vendor failure: a ``polygon_fetch_error`` notice
    with no bars.
    """
    plan = _plan_history_walk(timeframe, as_of_ms, required_bar_count)
    notices = []
    try:
        polygon_bars = await asyncio.wait_for(
            _fetch_history_bars(
                symbol=symbol,
                plan=plan,
                bar_source=bar_source,
                floor=polygon_history_floor(today),
            ),
            timeout=deadline_s,
        )
    except PolygonFetchError as exc:
        polygon_bars = []
        notices.append(notice_view(classify_polygon_exception(exc)))
    except TimeoutError:
        polygon_bars = []
        logger.warning(
            "History walk reached its deadline before the bar budget was met",
            extra={"action": "history_walk_deadline_exceeded", "symbol": symbol, "deadline_s": deadline_s},
        )
        notices.append(
            notice_view(
                classify_polygon_exception(
                    PolygonFetchError(
                        f"Polygon history for {symbol} did not finish within {deadline_s:g} seconds.",
                        status_code=None,
                    )
                )
            )
        )
    bars = [
        _polygon_bar_to_chart_bar(bar, end_ms=_history_bar_end_ms(bar, plan))
        for bar in polygon_bars
    ]
    return HistoryBatchResponse(
        bars=bars, source="polygon", overlay_notices=notices, effective_as_of_ms=as_of_ms
    )


async def build_coordinator_history_batch(
    *,
    symbol: str,
    timeframe: ChartHistoryTimeframe,
    required_bar_count: int,
    as_of_ms: int,
    polygon_api_key: str,
) -> HistoryBatchResponse:
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
        return HistoryBatchResponse(
            bars=[],
            source="polygon",
            overlay_notices=[notice_view(missing_polygon_api_key_notice("Polygon history"))],
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
        today=et_date_at_ms(now_ms_utc()),
    )


__all__ = [
    "HistoryBarSource",
    "build_coordinator_history_batch",
    "fetch_complete_history_batch",
    "span_ms_for",
]
