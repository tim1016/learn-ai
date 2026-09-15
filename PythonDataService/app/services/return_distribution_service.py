"""Lake-backed orchestration for the return-distribution study.

ADR 0049: the lake is the single authority for historical bar data, so this
service only ever *computes* over lake bytes — but it is capture-on-demand:
when the read window (lead-in included, so the first session keeps its
previous close) has sessions the lake does not hold, they are delta-fetched
first through :func:`materialize_symbol_history` — the shared async
materialization seam, which waits out concurrent catalog claims and is
deadline-bounded, and which includes the factor and map files the study's
corporate-action adjustment reads — and the study then computes over what
the lake now holds. A capture that still leaves the window empty is
surfaced as a typed error carrying what *is* captured and why the capture
could not populate the symbol.

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
from decimal import Decimal
from pathlib import Path
from typing import Literal

from app.data_lake.factor_files import factor_multiplier_as_of, read_factor_rows
from app.data_lake.path_policy import minute_bar_market_root, resolve_lake_root
from app.data_lake.run_materialization import materialize_symbol_history
from app.data_lake.types import is_lake_addressable_symbol
from app.engine.data.lean_format import LeanMinuteDataReader
from app.engine.data.trade_bar import TradeBar
from app.lean_sidecar.trading_calendar import expected_sessions, session_windows_ms_utc
from app.lean_sidecar.workspace import SymbolValidationError, validate_symbol
from app.research.return_distribution import (
    ReturnDistributionResult,
    adjust_anchors,
    build_return_distribution,
    compute_daily_returns,
    extract_day_anchors,
)
from app.services.chart_bar_source import split_sessions_at_boundary
from app.services.chart_service import resolve_request_dates
from app.utils.session_anchors import et_date_at_ms
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

#: Below this many captured sessions the statistics are noise, not evidence.
MIN_SESSIONS = 30

#: Calendar days read before ``from_date`` so the window's first session has
#: a previous close to return against (weekends/holidays make the exact
#: lead-in session count vary; two weeks always covers it).
_READ_LEAD_IN_DAYS = 14

#: How long the on-demand capture may take before the request gives up
#: waiting and computes over whatever landed. Enforced by the materialization
#: seam's contention-wait deadline, not by this module.
_CAPTURE_TIMEOUT_SECONDS = 600

_CAPTURE_REQUESTER = "return-distribution-study"

#: The closed status vocabulary of the capture receipt — mirrors
#: ``SymbolHistoryMaterialization`` plus the "never ran" state.
CaptureStatus = Literal["not_attempted", "skipped", "complete", "partial", "failed"]


class SymbolNotCapturedError(Exception):
    """The lake holds no minute bars for the symbol and capture could not fill it.

    ``lake_addressable`` distinguishes the two refusal reasons explicitly:
    ``False`` only for the validate/addressability guards (the symbol can
    never live in the lake); ``True`` (the default) whenever a capture was
    possible — even one that ran, reported success, and still wrote no
    in-range sessions (a symbol with no trading history in the window).
    The router must not infer this from a nullable ``capture_note``.
    """

    def __init__(
        self,
        symbol: str,
        captured_symbols: list[str],
        capture_note: str | None = None,
        *,
        lake_addressable: bool = True,
    ) -> None:
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
        self.lake_addressable = lake_addressable


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
    """What the on-demand capture did, for the response meta.

    ``status`` is a closed contract (see :data:`CaptureStatus`):
    ``not_attempted`` — the lake already covered the read window;
    ``skipped`` — a precondition ruled the capture out; ``complete`` —
    the lake reports the window fully materialized; ``partial`` — some
    artifacts landed, some did not (``detail`` says which);
    ``failed`` — the capture errored outright (timeout, catalog down).
    """

    status: CaptureStatus
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


_NOT_ATTEMPTED = CaptureReceipt(status="not_attempted", fetched_artifact_count=0)


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
    """Does the lake already hold every completed session of the read window?

    The read window — and therefore the capture span — starts at the
    lead-in, not at ``from_date``: the first in-window session needs the
    previous session's close for its close-to-close and overnight returns,
    so a lake that holds the requested dates but not the fortnight before
    them is still missing data this study reads. The span is the
    completed-session prefix of that window (the same calendar split the
    chart composer uses, so the two can never disagree about which sessions
    are immutable history).
    """
    read_start = from_date - timedelta(days=_READ_LEAD_IN_DAYS)
    completed, _live, _boundary = split_sessions_at_boundary(
        read_start.isoformat(),
        to_date.isoformat(),
        now_ms,
        # The study consumes extended-hours bars, so a session is only
        # immutable for capture once its 20:00-ET tape has finished. With
        # the default RTH policy a request made after 16:00 ET would
        # capture and catalog today's partial day, and later probes would
        # see the date present and never repair the missing after-hours
        # bars.
        session="extended",
    )
    if not completed:
        return _CoverageProbe(needs_capture=False, capture_start=None, capture_end=None)
    capture_start, capture_end = completed[0].session_date, completed[-1].session_date
    reader = LeanMinuteDataReader([lake_root], session="extended")
    lake_dates = set(reader.iter_dates(symbol, read_start, to_date))
    window_sessions = expected_sessions(capture_start, capture_end)
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

    Delegates to the shared async materialization seam
    (:func:`materialize_symbol_history`), so concurrent cold requests for
    the same sessions coalesce onto one provider fetch through the catalog
    claims and the whole capture is bounded by the seam's
    ``fetch_timeout_seconds``. Best-effort by contract: provider, catalog,
    and timeout failures are contained into the receipt's ``failed``
    status (the study then computes over whatever the lake holds, or
    reports the typed not-captured error) — a genuine programming error
    still propagates.
    """
    materialized = await materialize_symbol_history(
        symbol=symbol,
        start=start,
        end=end,
        requester=_CAPTURE_REQUESTER,
        fetch_timeout_seconds=_CAPTURE_TIMEOUT_SECONDS,
    )
    return CaptureReceipt(
        status=materialized.status,
        fetched_artifact_count=materialized.fetched_artifact_count,
        detail=materialized.detail,
    )


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
        capture_note = (
            capture.detail if capture.status not in ("not_attempted",) else "capture was not attempted"
        )
        raise SymbolNotCapturedError(
            symbol, _captured_symbols(lake_root), capture_note=capture_note
        )
    expected = expected_sessions(from_date, to_date)

    windows = {
        w.session_date: (w.open_ms_utc, w.close_ms_utc)
        for w in session_windows_ms_utc(read_start, to_date)
    }
    bars_by_day = {d: reader.read_day(symbol, d) for d in lake_dates}
    anchors = extract_day_anchors(bars_by_day, windows)

    factor_rows = read_factor_rows(lake_root, market="usa", symbol=symbol)
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

    scheduled = expected_sessions(read_start, to_date)
    all_days = compute_daily_returns(adjust_anchors(anchors, factor_rows), scheduled_sessions=scheduled)
    days = [d for d in all_days if d.trading_date >= from_date]

    # The statistics floor applies to *usable* observations, after the
    # exclusion of captured sessions that produced no RTH bars: 30 artifacts
    # of which a handful are bar-less would otherwise advertise a sample the
    # study never actually computes over.
    if len(days) < MIN_SESSIONS:
        raise InsufficientCoverageError(requested=len(expected), available=len(days))

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
    without_prev = sum(1 for d in days if d.close_to_close_pct is None)
    if without_prev:
        warnings.append(
            f"{without_prev} session(s) in the window have no previous captured session "
            "to return against (window start or a capture gap); their close-to-close and "
            "overnight returns are omitted rather than spanning the gap"
        )
    if capture.status not in ("not_attempted", "complete"):
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
    from_ms_utc: int,
    to_ms_utc: int,
    bin_width_pct: float,
    span_pct: float,
    lake_root: Path | None = None,
) -> StudyOutcome:
    """Capture the window into the lake on demand, then study the lake bytes.

    The window arrives as int64 ms UTC instants (the wire convention) and is
    resolved to inclusive calendar dates here, inside Python, through the
    same authority the chart endpoint uses (``resolve_request_dates``: each
    ms value floors to its UTC calendar date — the anchor the Data Lab
    store commits, the exact inverse of the frontend's ``utcMsToIsoDate``).
    The workspace's half-open ``[start, end)`` numeric window maps exactly:
    its start anchors the UTC midnight of the first trading date and its
    end the final instant of the last.

    ``lake_root`` defaults to the raw lake root (deepest coverage); tests
    pin it to a seeded fixture root and stub the capture boundary
    (``_capture_missing_sessions``) — the real capture needs the catalog
    and the provider, neither of which exists in a unit-test process.
    """
    try:
        validated = validate_symbol(symbol)
    except SymbolValidationError as e:
        raise SymbolNotCapturedError(symbol, [], lake_addressable=False) from e
    if not is_lake_addressable_symbol(validated):
        raise SymbolNotCapturedError(symbol, [], lake_addressable=False)
    root = lake_root if lake_root is not None else resolve_lake_root("raw")
    from_iso, to_iso = resolve_request_dates(None, None, from_ms_utc, to_ms_utc)
    from_d = date.fromisoformat(from_iso)
    to_d = date.fromisoformat(to_iso)

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


class DayNotCapturedError(Exception):
    """The lake holds no minute bars for the requested trading date."""

    def __init__(self, symbol: str, trading_date: date) -> None:
        super().__init__(
            f"the data lake holds no minute bars for {symbol!r} on "
            f"{trading_date.isoformat()}"
        )
        self.symbol = symbol
        self.trading_date = trading_date


@dataclass(frozen=True)
class DayCandlesOutcome:
    """One day's extended-session minute bars on the study's price basis."""

    trading_date: date
    adjustment: Literal["split_and_dividend", "raw"]
    bars: list[TradeBar]


def _day_candles_sync(
    *,
    symbol: str,
    session_open_ms_utc: int,
    lake_root: Path,
) -> DayCandlesOutcome:
    # The anchor is the session's 09:30 ET open — a mid-day instant, so its
    # ET calendar date is unambiguous across DST.
    trading_date = et_date_at_ms(session_open_ms_utc)
    reader = LeanMinuteDataReader([lake_root], session="extended")
    bars = list(reader.read_day(symbol, trading_date))
    if not bars:
        raise DayNotCapturedError(symbol, trading_date)

    # One trading date → one cumulative multiplier: prices scale, volume
    # does not — the same LEAN semantics the study's anchors follow, from
    # the same raw root and the same factor file, so the candle pane's
    # basis cannot drift from the return being inspected.
    factor_rows = read_factor_rows(lake_root, market="usa", symbol=symbol)
    multiplier = factor_multiplier_as_of(factor_rows, trading_date)
    adjustment: Literal["split_and_dividend", "raw"] = (
        "split_and_dividend" if factor_rows else "raw"
    )
    if multiplier == Decimal(1):
        scaled = bars
    else:
        scaled = [
            TradeBar(
                symbol=b.symbol,
                open=b.open * multiplier,
                high=b.high * multiplier,
                low=b.low * multiplier,
                close=b.close * multiplier,
                volume=b.volume,
                start_ms=b.start_ms,
                end_ms=b.end_ms,
            )
            for b in bars
        ]
    logger.info(
        "[RETURN_DISTRIBUTION] day candles %s %s: %d bars, adjustment=%s",
        symbol,
        trading_date.isoformat(),
        len(scaled),
        adjustment,
        extra={"symbol": symbol, "trading_date": trading_date.isoformat(), "bars": len(scaled)},
    )
    return DayCandlesOutcome(
        trading_date=trading_date,
        adjustment=adjustment,
        bars=scaled,
    )


async def compute_day_candles(
    *,
    symbol: str,
    session_open_ms_utc: int,
    lake_root: Path | None = None,
) -> DayCandlesOutcome:
    """Read one captured trading day's minute bars on the study's price basis.

    No capture-on-demand: this read serves a day a study response already
    named, so the bytes are in the lake by construction; anything else is a
    typed 404. The zip parse runs off the event loop like the study's.
    """
    try:
        validated = validate_symbol(symbol)
    except SymbolValidationError as e:
        raise SymbolNotCapturedError(symbol, [], lake_addressable=False) from e
    if not is_lake_addressable_symbol(validated):
        raise SymbolNotCapturedError(symbol, [], lake_addressable=False)
    root = lake_root if lake_root is not None else resolve_lake_root("raw")
    return await asyncio.to_thread(
        _day_candles_sync,
        symbol=validated,
        session_open_ms_utc=session_open_ms_utc,
        lake_root=root,
    )
