"""Lake-backed orchestration for the return-distribution study.

ADR 0049: the lake is the single authority for historical bar data, so this
service only ever *computes* over lake bytes — but it is capture-on-demand:
when the requested window has sessions the lake does not hold, they are
delta-fetched into the lake first through :func:`ensure_data` (the same
machinery the Data Lab chart seam uses, with factor files included so the
study's corporate-action adjustment works for newly captured symbols), and
the study then reads what the lake now holds. A capture that still leaves
the window empty is surfaced as a typed error carrying what *is* captured
and why the capture could not populate the symbol.

All math lives in ``app/research/return_distribution.py``; this module only
loads bars, resolves the calendar windows, runs the capture boundary, and
keeps the heavy zip parse + reduction off the event loop
(``asyncio.to_thread`` — the same lesson ``derived_daily`` documents for
#1943: ~1 s of zip parsing per 500 sessions stalls a loop that also runs
live execution).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

import asyncpg

from app.data_lake.catalog_client import CatalogUnavailableError
from app.data_lake.ensure_data import ensure_data
from app.data_lake.path_policy import (
    LeanFactorFilePath,
    minute_bar_market_root,
    resolve_lake_root,
)
from app.data_lake.types import (
    DataRunSpec,
    is_lake_addressable_symbol,
    trading_date_to_calendar_anchor_ms,
)
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
from app.services.chart_bar_source import split_sessions_at_boundary
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

#: Below this many captured sessions the statistics are noise, not evidence.
MIN_SESSIONS = 30

#: Calendar days read before ``from_date`` so the window's first session has
#: a previous close to return against (weekends/holidays make the exact
#: lead-in session count vary; two weeks always covers it).
_READ_LEAD_IN_DAYS = 14

#: How long the on-demand capture may take before the request gives up
#: waiting and computes over whatever landed (the engine-side budget the
#: chart seam documents; catalog claims let a retry coalesce with the
#: still-running capture instead of re-fetching).
_CAPTURE_TIMEOUT_SECONDS = 600

_CAPTURE_REQUESTER = "return-distribution-study"


class SymbolNotCapturedError(Exception):
    """The lake holds no minute bars for the symbol and capture could not fill it."""

    def __init__(self, symbol: str, captured_symbols: list[str], capture_note: str | None = None) -> None:
        message = (
            f"the data lake holds no minute bars for {symbol!r} and the on-demand "
            f"capture could not populate it"
        )
        if capture_note:
            message += f" ({capture_note})"
        message += f"; captured symbols: {', '.join(captured_symbols) or '(none)'}"
        super().__init__(message)
        self.symbol = symbol
        self.captured_symbols = captured_symbols
        self.capture_note = capture_note


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
class CaptureReceipt:
    """What the on-demand capture did, for the response meta."""

    attempted: bool
    status: str
    fetched_artifact_count: int
    detail: str | None = None


@dataclass(frozen=True)
class StudyOutcome:
    """What the router needs beyond the pure result, for the meta/coverage."""

    result: ReturnDistributionResult
    requested_sessions: int
    missing_sessions: int
    excluded_sessions: int
    warnings: list[str]
    capture: CaptureReceipt


@dataclass(frozen=True)
class _CoverageProbe:
    """Whether the lake already covers the window, and the capture span if not."""

    needs_capture: bool
    capture_start: date | None
    capture_end: date | None


_NOT_ATTEMPTED = CaptureReceipt(attempted=False, status="not_attempted", fetched_artifact_count=0)


def _captured_symbols(root: Path) -> list[str]:
    minute_root = root.joinpath(*minute_bar_market_root("usa").parts)
    if not minute_root.exists():
        return []
    return sorted(p.name.upper() for p in minute_root.iterdir() if p.is_dir())


def _probe_lake_coverage(
    *,
    symbol: str,
    from_date: date,
    to_date: date,
    lake_root: Path,
    now_ms: int,
) -> _CoverageProbe:
    """Does the lake already hold every completed session of the window?

    The capture span is the completed-session prefix of the read window
    (lead-in included, so a cold symbol's first session still gets its
    previous close) — the same calendar split the chart composer uses, so
    the two can never disagree about which sessions are immutable history.
    """
    read_start = from_date - timedelta(days=_READ_LEAD_IN_DAYS)
    completed, _live, _boundary = split_sessions_at_boundary(
        read_start.isoformat(), to_date.isoformat(), now_ms
    )
    if not completed:
        return _CoverageProbe(needs_capture=False, capture_start=None, capture_end=None)
    capture_start, capture_end = completed[0].session_date, completed[-1].session_date
    reader = LeanMinuteDataReader([lake_root], session="extended")
    lake_dates = set(reader.iter_dates(symbol, read_start, to_date))
    window_sessions = expected_sessions(max(from_date, capture_start), capture_end)
    needs = any(d not in lake_dates for d in window_sessions)
    return _CoverageProbe(
        needs_capture=needs,
        capture_start=capture_start if needs else None,
        capture_end=capture_end if needs else None,
    )


async def _capture_missing_sessions(
    *,
    symbol: str,
    start: date,
    end: date,
) -> CaptureReceipt:
    """Delta-fetch the window's missing artifacts into the lake.

    Runs the same ``ensure_data`` pipeline the capture script and the chart
    seam use, with factor and map files included so newly captured symbols
    get the study's split+dividend adjustment. Best-effort by contract:
    provider/catalog/timeout failures are contained into the receipt (the
    study then computes over whatever the lake holds, or reports the typed
    not-captured error) — a genuine programming error still propagates.
    """
    from app.lean_sidecar.config import PINNED_LEAN_IMAGE_DIGEST

    if not PINNED_LEAN_IMAGE_DIGEST:
        return CaptureReceipt(
            attempted=False,
            status="skipped",
            fetched_artifact_count=0,
            detail="no pinned LEAN image digest; capture not attempted",
        )
    spec = DataRunSpec(
        request_id=uuid4(),
        run_type="chart",
        requester=_CAPTURE_REQUESTER,
        market="usa",
        symbols=[symbol],
        start_trading_date_ms=trading_date_to_calendar_anchor_ms(start),
        end_trading_date_ms=trading_date_to_calendar_anchor_ms(end),
        resolution="minute",
        data_types=["trade"],
        price_adjustment_mode="raw",
        provider="polygon",
        include_factor_files=True,
        include_map_files=True,
        include_daily_trade=False,
        lean_image_digest=PINNED_LEAN_IMAGE_DIGEST,
        fetch_timeout_seconds=_CAPTURE_TIMEOUT_SECONDS,
    )
    try:
        result = await ensure_data(spec)
    except (TimeoutError, CatalogUnavailableError, asyncpg.PostgresError) as exc:
        logger.warning(
            "[RETURN_DISTRIBUTION] on-demand capture for %s %s..%s failed (%s: %s)",
            symbol,
            start.isoformat(),
            end.isoformat(),
            type(exc).__name__,
            exc,
            extra={"symbol": symbol, "capture_start": start.isoformat(), "capture_end": end.isoformat()},
        )
        return CaptureReceipt(
            attempted=True,
            status="failed",
            fetched_artifact_count=0,
            detail=f"{type(exc).__name__}: {exc}",
        )
    detail = None
    if result.failures:
        detail = "; ".join(f"{f.reason}: {f.detail}" for f in result.failures[:3])
    return CaptureReceipt(
        attempted=True,
        status=str(result.overall_status),
        fetched_artifact_count=result.fetched_artifact_count,
        detail=detail,
    )


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
    capture: CaptureReceipt,
) -> StudyOutcome:
    read_start = from_date - timedelta(days=_READ_LEAD_IN_DAYS)
    reader = LeanMinuteDataReader([lake_root], session="extended")

    lake_dates = list(reader.iter_dates(symbol, read_start, to_date))
    in_range = [d for d in lake_dates if d >= from_date]
    if not in_range:
        capture_note = capture.detail if capture.attempted else "capture was not attempted"
        raise SymbolNotCapturedError(
            symbol, _captured_symbols(lake_root), capture_note=capture_note
        )
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
    if capture.attempted and capture.status not in ("complete", "COMPLETE"):
        warnings.append(
            f"on-demand capture status: {capture.status}"
            + (f" ({capture.detail})" if capture.detail else "")
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
        capture=capture,
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
    """Capture the window into the lake on demand, then study the lake bytes.

    ``lake_root`` defaults to the raw lake root (deepest coverage); tests
    pin it to a seeded fixture root and stub the capture boundary
    (``_capture_missing_sessions``) — the real capture needs the catalog
    and the provider, neither of which exists in a unit-test process.
    """
    try:
        validated = validate_symbol(symbol)
    except SymbolValidationError as e:
        raise SymbolNotCapturedError(symbol, []) from e
    if not is_lake_addressable_symbol(validated):
        raise SymbolNotCapturedError(symbol, [])
    root = lake_root if lake_root is not None else resolve_lake_root("raw")
    from_d = date.fromisoformat(from_date)
    to_d = date.fromisoformat(to_date)

    probe = await asyncio.to_thread(
        _probe_lake_coverage,
        symbol=validated,
        from_date=from_d,
        to_date=to_d,
        lake_root=root,
        now_ms=now_ms_utc(),
    )
    capture = _NOT_ATTEMPTED
    if probe.needs_capture and probe.capture_start is not None and probe.capture_end is not None:
        capture = await _capture_missing_sessions(
            symbol=validated,
            start=probe.capture_start,
            end=probe.capture_end,
        )

    return await asyncio.to_thread(
        _compute_sync,
        symbol=validated,
        from_date=from_d,
        to_date=to_d,
        bin_width_pct=bin_width_pct,
        span_pct=span_pct,
        lake_root=root,
        capture=capture,
    )
