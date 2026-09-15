"""Lake-backed orchestration for the return-distribution study.

ADR 0049: the lake is the single authority for historical bar data, so this
service reads the raw minute-bar root through the LEAN reader and applies
the lake's own factor files for corporate-action adjustment — it never
falls back to a live Polygon fetch. A symbol/window the lake does not hold
is surfaced as a typed error carrying what *is* captured, so the UI can
say "capture this in the Data Lab first" instead of implying a hole.

All math lives in ``app/research/return_distribution.py``; this module only
loads bars, resolves the calendar windows, and runs the heavy zip parse +
reduction off the event loop (``asyncio.to_thread`` — the same lesson
``derived_daily`` documents for #1943: ~1 s of zip parsing per 500 sessions
stalls a loop that also runs live execution).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from app.data_lake.path_policy import (
    LeanFactorFilePath,
    minute_bar_market_root,
    resolve_lake_root,
)
from app.data_lake.types import is_lake_addressable_symbol
from app.engine.data.lean_format import LeanMinuteDataReader
from app.lean_sidecar.trading_calendar import expected_sessions, session_windows_ms_utc
from app.lean_sidecar.workspace import SymbolValidationError, validate_symbol
from app.research.return_distribution import (
    ReturnDistributionResult,
    adjust_anchors,
    build_return_distribution,
    compute_daily_returns,
    extract_day_anchors,
    parse_factor_file,
)

logger = logging.getLogger(__name__)

#: Below this many captured sessions the statistics are noise, not evidence.
MIN_SESSIONS = 30

#: Calendar days read before ``from_date`` so the window's first session has
#: a previous close to return against (weekends/holidays make the exact
#: lead-in session count vary; two weeks always covers it).
_READ_LEAD_IN_DAYS = 14


class SymbolNotCapturedError(Exception):
    """The lake holds no minute bars for the symbol (in the window or at all)."""

    def __init__(self, symbol: str, captured_symbols: list[str]) -> None:
        super().__init__(
            f"the data lake holds no minute bars for {symbol!r}; "
            f"captured symbols: {', '.join(captured_symbols) or '(none)'}"
        )
        self.symbol = symbol
        self.captured_symbols = captured_symbols


class InsufficientCoverageError(Exception):
    """The lake holds fewer than MIN_SESSIONS sessions in the requested window."""

    def __init__(self, requested: int, available: int) -> None:
        super().__init__(
            f"insufficient lake coverage: {available} captured sessions in the window, "
            f"need at least {MIN_SESSIONS} (of {requested} scheduled)"
        )
        self.requested = requested
        self.available = available


@dataclass(frozen=True)
class StudyOutcome:
    """What the router needs beyond the pure result, for the meta/coverage."""

    result: ReturnDistributionResult
    requested_sessions: int
    missing_sessions: int
    excluded_sessions: int
    warnings: list[str]


def _captured_symbols(root: Path) -> list[str]:
    minute_root = root.joinpath(*minute_bar_market_root("usa").parts)
    if not minute_root.exists():
        return []
    return sorted(p.name.upper() for p in minute_root.iterdir() if p.is_dir())


def _load_factor_rows(root: Path, symbol: str) -> list:
    factor_path = root.joinpath(*LeanFactorFilePath(market="usa", symbol=symbol).relative_path().parts)
    if not factor_path.exists():
        return []
    rows = parse_factor_file(factor_path.read_text(encoding="ascii"))
    logger.info(
        "[RETURN_DISTRIBUTION] loaded %d factor rows for %s",
        len(rows),
        symbol,
        extra={"symbol": symbol, "factor_rows": len(rows)},
    )
    return rows


def _compute_sync(
    *,
    symbol: str,
    from_date: date,
    to_date: date,
    bin_width_pct: float,
    span_pct: float,
    lake_root: Path,
) -> StudyOutcome:
    read_start = from_date - timedelta(days=_READ_LEAD_IN_DAYS)
    reader = LeanMinuteDataReader([lake_root], session="extended")

    lake_dates = list(reader.iter_dates(symbol, read_start, to_date))
    in_range = [d for d in lake_dates if d >= from_date]
    if not in_range:
        raise SymbolNotCapturedError(symbol, _captured_symbols(lake_root))
    expected = expected_sessions(from_date, to_date)
    if len(in_range) < MIN_SESSIONS:
        raise InsufficientCoverageError(requested=len(expected), available=len(in_range))

    windows = {
        w.session_date: (w.open_ms_utc, w.close_ms_utc)
        for w in session_windows_ms_utc(read_start, to_date)
    }
    bars_by_day = {d: reader.read_day(symbol, d) for d in lake_dates}
    anchors = extract_day_anchors(bars_by_day, windows)

    factor_rows = _load_factor_rows(lake_root, symbol)
    warnings: list[str] = []
    adjustment: Literal["split_and_dividend", "raw"]
    if factor_rows:
        adjustment = "split_and_dividend"
    else:
        adjustment = "raw"
        warnings.append(
            "no factor file captured for this symbol: returns are unadjusted for "
            "splits and dividends, so corporate-action days can appear as outlier moves"
        )

    all_days = compute_daily_returns(adjust_anchors(anchors, factor_rows))
    days = [d for d in all_days if d.trading_date >= from_date]

    missing = len(expected) - len(in_range)
    if missing:
        warnings.append(
            f"{missing} scheduled session(s) in the window are not captured in the lake; "
            "the study covers what is captured"
        )
    excluded = len(in_range) - len(days)
    if excluded:
        warnings.append(
            f"{excluded} captured session(s) had no regular-session bars and were excluded"
        )

    result = build_return_distribution(
        days,
        bin_width_pct=bin_width_pct,
        span_pct=span_pct,
        adjustment=adjustment,
    )
    logger.info(
        "[RETURN_DISTRIBUTION] %s %s..%s: %d sessions, adjustment=%s",
        symbol,
        from_date.isoformat(),
        to_date.isoformat(),
        len(days),
        adjustment,
        extra={
            "symbol": symbol,
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
            "sessions": len(days),
            "adjustment": adjustment,
        },
    )
    return StudyOutcome(
        result=result,
        requested_sessions=len(expected),
        missing_sessions=missing,
        excluded_sessions=excluded,
        warnings=warnings,
    )


async def compute_return_distribution(
    *,
    symbol: str,
    from_date: str,
    to_date: str,
    bin_width_pct: float,
    span_pct: float,
    lake_root: Path | None = None,
) -> StudyOutcome:
    """Load lake minute bars for the window and run the distribution study.

    ``lake_root`` defaults to the raw lake root (deepest coverage); tests
    pin it to a seeded fixture root.
    """
    try:
        validated = validate_symbol(symbol)
    except SymbolValidationError as e:
        raise SymbolNotCapturedError(symbol, []) from e
    if not is_lake_addressable_symbol(validated):
        raise SymbolNotCapturedError(symbol, [])
    root = lake_root if lake_root is not None else resolve_lake_root("raw")
    return await asyncio.to_thread(
        _compute_sync,
        symbol=validated,
        from_date=date.fromisoformat(from_date),
        to_date=date.fromisoformat(to_date),
        bin_width_pct=bin_width_pct,
        span_pct=span_pct,
        lake_root=root,
    )
