"""Per-day coverage of one symbol's minute artifacts over a window: the read behind ``GET /api/data-lake/coverage``.

``GET /api/data-lake/instrument-data`` (the instrument place's Data tab) reads the same answer through the same
functions, so a window one refuses the other refuses with the same typed reason. Routes are transport: they map
:class:`CoverageRequestError` to their 422 and call :func:`read_coverage`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date

from app.data_lake import catalog_client, path_policy, root_identity
from app.data_lake.ensure_data import provider_for_data_type
from app.data_lake.run_readiness import coverage_gaps
from app.data_lake.types import (
    MAX_SYMBOL_LENGTH,
    MAX_TRADING_RANGE_DAYS,
    SYMBOL_RE,
    CoverageDay,
    CoverageResponse,
    DataType,
    PriceAdjustmentMode,
    trading_date_at_ms,
    trading_range_span_days,
)
from app.lean_sidecar.trading_calendar import session_windows_ms_utc


class CoverageRequestError(ValueError):
    """The symbol or window asked about cannot be read. ``reason`` is the typed code callers branch on."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message

    @property
    def detail(self) -> dict[str, str]:
        """The body a route answers with: ``{"reason", "message"}``."""
        return {"reason": self.reason, "message": self.message}


@dataclass(frozen=True)
class CoverageWindow:
    """A symbol and the inclusive trading dates asked about, once they have been accepted."""

    symbol: str
    start: date
    end: date


def coverage_window(symbol: str, start_trading_date_ms: int, end_trading_date_ms: int) -> CoverageWindow:
    """Accept a symbol and a window given as ``int64 ms UTC`` trading dates, or refuse them with a typed reason.

    Pure, so a caller that reads more than the coverage can refuse a request before it starts any read.
    """
    if not SYMBOL_RE.match(symbol) or len(symbol) > MAX_SYMBOL_LENGTH:
        raise CoverageRequestError(
            "invalid_symbol",
            f"symbol must match {SYMBOL_RE.pattern} and be at most {MAX_SYMBOL_LENGTH} chars: {symbol!r}",
        )
    try:
        start = trading_date_at_ms(start_trading_date_ms)
        end = trading_date_at_ms(end_trading_date_ms)
    except (OverflowError, OSError, ValueError) as exc:
        # A ms value outside the representable instant range. Typed, because
        # the alternative is a 500 on a caller sending seconds for ms -- the
        # single likeliest way to get this parameter wrong.
        raise CoverageRequestError(
            "invalid_trading_date_ms",
            f"trading-date window is not a representable ms-UTC instant "
            f"({start_trading_date_ms}..{end_trading_date_ms}); values are "
            f"milliseconds since the Unix epoch, anchored at the session open",
        ) from exc
    if start > end:
        raise CoverageRequestError("invalid_range", f"start_trading_date {start} is after end_trading_date {end}")
    # Same computation DataRunSpec's validator uses (types.py) — one shared
    # cap, one shared formula, so a write-window accepted by POST
    # /ensure-data can never be a read-window GET /coverage rejects.
    span_days = trading_range_span_days(start, end)
    if span_days > MAX_TRADING_RANGE_DAYS:
        raise CoverageRequestError("range_too_large", f"range is {span_days} days; max is {MAX_TRADING_RANGE_DAYS}")
    return CoverageWindow(symbol=symbol, start=start, end=end)


async def read_coverage(
    window: CoverageWindow,
    *,
    market: str,
    data_type: DataType,
    price_adjustment_mode: PriceAdjustmentMode,
) -> CoverageResponse:
    """Per-day artifact status for ``window``, keyed by the canonical NYSE calendar.

    Days are the calendar's own sessions in the window — weekends and holidays
    are simply absent, never listed and never invented. A session with no
    matching catalog row is reported honestly as ``status="missing"`` rather
    than omitted. The catalog pool must already exist: each caller makes it so
    before it reads.
    """
    provider = provider_for_data_type(data_type)
    # Resolved once and passed explicitly to both the catalog query and the
    # response (issue #1876): coverage defaults to the service's configured
    # active root.
    data_root_id = root_identity.active_root_id()
    # One schedule build for the whole range, not one per day: session_date
    # and its 09:30 ET open both come off the same SessionWindow, so no
    # per-day session_open_ms_utc() call re-queries pandas_market_calendars.
    # A per-day query pattern here measured ~10s at the 5-year cap.
    windows = session_windows_ms_utc(window.start, window.end)
    rows_by_date = {
        row.trading_date: row
        for row in await catalog_client.select_artifact_coverage(
            market=market,
            symbol=window.symbol,
            data_type=data_type,
            provider=provider,
            price_adjustment_mode=price_adjustment_mode,
            start_trading_date=window.start,
            end_trading_date=window.end,
            data_root_id=data_root_id,
        )
    }
    # The catalog calls a session complete once its price file is committed; a
    # split-adjusted reader also refuses a file without a valid adjustment
    # record. One stat and one small read per session, off the event loop.
    gaps = await asyncio.to_thread(
        coverage_gaps, list(rows_by_date.values()), path_policy.resolve_lake_root(price_adjustment_mode)
    )
    days = []
    for session in windows:
        row = rows_by_date.get(session.session_date)
        gap = gaps.get(session.session_date)
        days.append(
            CoverageDay(
                trading_date_ms=session.open_ms_utc,
                status="missing" if row is None else "needs_rebuild" if gap is not None else row.status,
                artifact_id=row.artifact_id if row is not None else None,
                rebuild_reason=gap,
                failure_reason=row.last_error if row is not None and row.status == "failed" else None,
            )
        )
    return CoverageResponse(
        market=market,
        symbol=window.symbol,
        data_type=data_type,
        resolution="minute",
        provider=provider,
        price_adjustment_mode=price_adjustment_mode,
        data_root_id=data_root_id,
        days=days,
    )
