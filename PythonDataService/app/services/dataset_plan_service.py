"""Fetch-free dataset planning (data-lab workspace redesign PRD §12).

``POST /api/dataset/plan`` resolves a recipe into a receipt the Angular
Data Lab renders verbatim: session window, exchange sessions, output
columns, workload estimate (typed as an estimate), companion
dependencies, and timeframe advice. Planning touches ONLY the local
NYSE calendar (``pandas_market_calendars``) — it never calls Polygon.
"""

from __future__ import annotations

import importlib.metadata
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from app.lean_sidecar.trading_calendar import (
    expected_sessions,
    next_trading_day,
    session_open_ms_utc,
    session_windows_ms_utc,
)
from app.models.requests import DatasetPlanRequest
from app.schemas.dataset_plan import DatasetPlanResponse
from app.services.chart_service import get_allowed_timeframes
from app.services.dataset_service import calculate_dynamic_indicators, project_output_columns

_ET = ZoneInfo("America/New_York")

EXCHANGE = "NYSE"
CALENDAR_TIMEZONE = "America/New_York"

# Seconds per bar timespan unit for intraday arithmetic.
_TIMESPAN_UNIT_SECONDS: dict[str, int] = {
    "second": 1,
    "minute": 60,
    "hour": 3_600,
}
# Approximate scheduled sessions per bar unit for day-and-above timespans.
_SESSIONS_PER_UNIT: dict[str, int] = {
    "day": 1,
    "week": 5,
    "month": 21,
    "quarter": 63,
    "year": 252,
}
# Timespans whose Polygon aggregates carry vwap/transactions fields.
_VWAP_TIMESPANS = frozenset({"second", "minute"})

_PROJECTION_FRAME_ROWS = 300


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _et_date_of_ms(ts_ms: int) -> date:
    return pd.Timestamp(ts_ms, unit="ms", tz="UTC").tz_convert(_ET).date()


def _resolve_window(request: DatasetPlanRequest) -> tuple[int, int, date, date]:
    """Resolve the half-open numeric window and the session-enumeration range.

    Date-only intent resolves session-open → following-session-open via
    the canonical calendar — never ``T23:59:59`` and never a hard-coded
    390-minute session. Numeric overrides take precedence per-field.
    The returned dates bound the exchange-session enumeration.
    """
    from_date = _parse_date(request.from_date)
    to_date = _parse_date(request.to_date)

    window_start = (
        request.start_ms_utc
        if request.start_ms_utc is not None
        else session_open_ms_utc(from_date)
    )
    window_end = (
        request.end_ms_utc
        if request.end_ms_utc is not None
        else session_open_ms_utc(next_trading_day(to_date))
    )
    if window_end <= window_start:
        raise ValueError(
            f"resolved window is empty: window_start_ms_utc={window_start} "
            f"must be before window_end_ms_utc={window_end}"
        )

    if request.start_ms_utc is not None or request.end_ms_utc is not None:
        enum_start = _et_date_of_ms(window_start)
        # window_end is exclusive — the last date that can contain a bar is
        # the ET date one millisecond before it.
        enum_end = _et_date_of_ms(window_end - 1)
    else:
        enum_start, enum_end = from_date, to_date
    if enum_end < enum_start:
        raise ValueError(
            f"resolved session range is empty: {enum_start.isoformat()} after {enum_end.isoformat()}"
        )
    return window_start, window_end, enum_start, enum_end


def _estimate_bars(
    request: DatasetPlanRequest,
    enum_start: date,
    enum_end: date,
    window_start_ms: int,
    window_end_ms: int,
) -> tuple[int, list[str]]:
    """Arithmetic bar-count estimate from scheduled sessions — no fetching."""
    sessions = session_windows_ms_utc(enum_start, enum_end)
    assumptions = [
        "estimated_bars is pure arithmetic from the scheduled NYSE session windows; no bars were fetched",
        "early-close half-days contribute their real (shorter) scheduled span",
    ]

    unit_seconds = _TIMESPAN_UNIT_SECONDS.get(request.timespan)
    if unit_seconds is not None:
        bar_span_ms = request.multiplier * unit_seconds * 1000
        # Clip each scheduled session to the resolved half-open window so a
        # numeric override cannot inflate the estimate.
        estimated = sum(
            max(
                0,
                (min(w.close_ms_utc, window_end_ms) - max(w.open_ms_utc, window_start_ms)) // bar_span_ms,
            )
            for w in sessions
        )
        if request.session == "extended":
            assumptions.append(
                "session='extended' estimate counts RTH-scheduled minutes only; "
                "pre/post-market bars are NOT included and increase the real count"
            )
    else:
        sessions_per_bar = _SESSIONS_PER_UNIT[request.timespan] * request.multiplier
        estimated = max(1, len(sessions) // sessions_per_bar) if sessions else 0
        assumptions.append(
            f"day-and-above estimate assumes ~{_SESSIONS_PER_UNIT[request.timespan]} scheduled sessions "
            f"per {request.timespan} bar; holiday clustering shifts the real count"
        )

    if request.forward_fill:
        assumptions.append("forward_fill=True synthesizes bars for missing minutes; the real count can be higher")
    return estimated, assumptions


def _projection_frame(request: DatasetPlanRequest) -> pd.DataFrame:
    """Synthetic frame carrying exactly the columns a real processed frame would.

    Column PRESENCE is deterministic from the recipe (OHLCV always;
    ``PC`` when include_previous_close; vwap/transactions for
    second/minute aggregates per Polygon's response shape; ``session``
    always — the export pipeline tags it whenever from/to are given).
    Row VALUES are irrelevant: only names flow into the projection.
    """
    n = _PROJECTION_FRAME_ROWS
    close = pd.Series(100.0 + (pd.Series(range(n)) % 7) * 0.25)
    df = pd.DataFrame(
        {
            "timestamp": pd.Series(range(n), dtype="int64") * 60_000,
            "open": close - 0.1,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": pd.Series([1_000] * n, dtype="int64"),
        }
    )
    if request.timespan in _VWAP_TIMESPANS:
        df["vwap"] = close
        df["transactions"] = pd.Series([10] * n, dtype="int64")
    df["session"] = "rth"
    if request.include_previous_close:
        df["PC"] = close.shift(1).bfill()
    return df


def _companion_dependencies(request: DatasetPlanRequest) -> tuple[list[str], list[str]]:
    """Companion source names the generation run would fetch, plus warnings."""
    deps: list[str] = []
    warnings: list[str] = []
    if request.include_previous_close:
        deps.append("previous_close")
    if request.include_splits:
        deps.append("splits")
    if request.include_dividends:
        deps.append("dividends")
    if request.adjust_for_dividends:
        deps.append("dividends")
        warnings.append(
            "adjust_for_dividends=True fetches the dividends reference companion "
            "and subtracts each dividend from pre-ex-date bars"
        )
    if request.include_ticker_overview:
        deps.append("ticker_overview")
    if request.include_news:
        deps.append("news")
    if request.include_financials:
        deps.append("financials")
    if request.include_trades:
        deps.append("trades")
        warnings.append(
            "trades.csv is TICK-LEVEL — millions of rows for long windows; "
            "capped server-side at 500k rows"
        )
    if request.include_quotes:
        deps.append("quotes")
        warnings.append(
            "quotes.csv is TICK-LEVEL NBBO — millions of rows for long windows; "
            "capped server-side at 500k rows"
        )
    if request.options_companion and request.options_companion.enabled:
        deps.append("options_companion")
        sides = sum((request.options_companion.include_calls, request.options_companion.include_puts))
        slots = 2 * request.options_companion.strikes_each_side + 1
        warnings.append(
            f"options_companion issues Polygon snapshot calls for up to "
            f"{sides * slots} side/slot series across the session range; exact contract "
            f"count depends on listed expiries (dte_distance={request.options_companion.dte_distance})"
        )
    return deps, warnings


def build_dataset_plan(request: DatasetPlanRequest) -> DatasetPlanResponse:
    """Resolve a :class:`DatasetPlanRequest` into a fetch-free receipt.

    Raises :class:`ValueError` for an unresolvable window; the router
    maps that to a 422.
    """
    window_start, window_end, enum_start, enum_end = _resolve_window(request)

    session_dates = expected_sessions(enum_start, enum_end)

    estimated_bars, assumptions = _estimate_bars(
        request, enum_start, enum_end, window_start, window_end
    )

    frame = _projection_frame(request)
    _, column_meta = calculate_dynamic_indicators(frame, request.indicator_entries)
    output_columns = project_output_columns(frame, column_meta)

    if request.timespan in _VWAP_TIMESPANS:
        assumptions.append(
            f"vwap/transactions columns assumed present in Polygon {request.timespan} aggregates"
        )

    deps, warnings = _companion_dependencies(request)
    allowed, _estimates, recommended = get_allowed_timeframes(
        request.from_date, request.to_date, request.session
    )

    calendar_version = importlib.metadata.version("pandas_market_calendars")
    provenance = (
        f"sessions from pandas_market_calendars NYSE schedule "
        f"(app.lean_sidecar.trading_calendar, pandas_market_calendars {calendar_version}); "
        f"bar estimate is session-window arithmetic; no network calls were made"
    )

    return DatasetPlanResponse(
        ticker=request.ticker,
        window_start_ms_utc=window_start,
        window_end_ms_utc=window_end,
        exchange_sessions=[d.isoformat() for d in session_dates],
        session_count=len(session_dates),
        output_columns=output_columns,
        output_column_count=len(output_columns),
        estimated_bars=estimated_bars,
        estimate_assumptions=assumptions,
        estimate_provenance=provenance,
        companion_dependencies=deps,
        warnings=warnings,
        allowed_timeframes=allowed,
        recommended_timeframes=[recommended],
        exchange=EXCHANGE,
        calendar_timezone=CALENDAR_TIMEZONE,
        calendar_version=calendar_version,
    )
