"""Data availability and on-demand materialization for the LEAN engine.

The engine reads LEAN-format equity data from one or more roots (see
``LeanMinuteDataReader``, ``LeanDailyDataReader``). For the SPY / AAPL
reference fixtures the data is pre-baked in a read-only mount. For
arbitrary tickers the caller does not have a LEAN zip yet — this module
bridges that gap by:

1. Reporting which trading days are already covered across the configured
   roots (``check_availability``).
2. Materializing missing days into a *writable* cache root by calling the
   existing ``export_polygon_range_to_lean`` bridge (``ensure_range``).

Both entry points are resolution-aware. For ``"minute"`` a "day is
available" iff the per-day zip ``{YYYYMMDD}_trade.zip`` exists under
``equity/usa/minute/{symbol}/`` in some root. For ``"daily"`` a "day is
available" iff the single per-symbol history zip
``equity/usa/daily/{symbol}.zip`` contains a CSV row stamped with that
trading date in some root. The per-root ``sources`` breakdown honors the
same reference-first merge order that the readers use.

Keeping this logic behind a small service keeps the router thin and lets
the engine tests exercise availability checks without needing a live
Polygon client.
"""

from __future__ import annotations

import logging
import zipfile
from collections.abc import Container, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import groupby
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

Resolution = Literal["minute", "daily"]

# US equities open Monday–Friday. This filter is deliberately naive: it
# ignores exchange holidays. That is harmless for *reporting* — the
# downstream reader already skips dates with no zip file — but not for
# the completeness test `ensure_range` builds on it: a holiday is an
# expected day the provider will never supply, so a window containing
# one can never report complete and re-fetches on every call. That is
# the leak #1830 bounded (to the holiday itself) and could not close;
# closing it needs a holiday-aware expected-day set, which the data lake
# owns (#1825), not this store, which is due for retirement (#1840).
_WEEKEND = {5, 6}


def _iter_weekdays(start: date, end: date):
    current = start
    one_day = timedelta(days=1)
    while current <= end:
        if current.weekday() not in _WEEKEND:
            yield current
        current += one_day


def _missing_spans(start: date, end: date, missing: Container[date]) -> list[tuple[date, date]]:
    """Group missing days into contiguous fetch spans.

    Adjacency is read off the expected-day walk itself: two missing days
    share a span when no expected day between them was covered, so a
    Friday and the following Monday are one span. Deriving it any other
    way would restate ``_iter_weekdays``'s rule for what the next
    expected day is, and the two would silently disagree the moment that
    rule learns about exchange holidays.
    """
    spans: list[tuple[date, date]] = []
    for is_missing, days in groupby(_iter_weekdays(start, end), key=lambda day: day in missing):
        if is_missing:
            run = list(days)
            spans.append((run[0], run[-1]))
    return spans


def _minute_zip_filename(trading_date: date) -> str:
    return f"{trading_date.strftime('%Y%m%d')}_trade.zip"


def _minute_symbol_dir(root: Path, symbol: str) -> Path:
    return root / "equity" / "usa" / "minute" / symbol.lower()


def _daily_zip_path(root: Path, symbol: str) -> Path:
    return root / "equity" / "usa" / "daily" / f"{symbol.lower()}.zip"


def _read_daily_dates(zip_path: Path) -> set[date]:
    """Extract the set of trading dates present in a LEAN daily zip.

    Uses the same CSV format assumption as
    :func:`lean_format._parse_daily_csv_bytes`: each row begins with
    ``YYYYMMDD HH:MM``. We only need the dates for availability checks,
    so we skip the price/volume fields entirely — this keeps the
    availability endpoint cheap even for symbols with 20+ years of
    history (~5000 rows).
    """
    if not zip_path.exists():
        return set()
    dates: set[date] = set()
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            if not names:
                return set()
            with zf.open(names[0]) as f:
                for line in f.read().decode("ascii").splitlines():
                    if not line or len(line) < 8:
                        continue
                    date_str = line[:8]
                    if not date_str.isdigit():
                        continue
                    try:
                        dates.add(
                            date(
                                int(date_str[0:4]),
                                int(date_str[4:6]),
                                int(date_str[6:8]),
                            )
                        )
                    except ValueError:
                        continue
    except (zipfile.BadZipFile, KeyError) as exc:
        logger.warning("[AVAILABILITY] Failed reading daily zip %s: %s", zip_path, exc)
    return dates


@dataclass
class AvailabilityReport:
    symbol: str
    start: date
    end: date
    resolution: Resolution
    expected_days: int
    available_days: int
    missing_days: list[date] = field(default_factory=list)
    # Per-root breakdown: {root_path: [dates_found_in_that_root]}
    sources: dict[str, list[date]] = field(default_factory=dict)

    @property
    def is_complete(self) -> bool:
        return self.available_days >= self.expected_days

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "resolution": self.resolution,
            "expected_days": self.expected_days,
            "available_days": self.available_days,
            "is_complete": self.is_complete,
            "missing_days": [d.isoformat() for d in self.missing_days],
            "sources": {root: [d.isoformat() for d in dates] for root, dates in self.sources.items()},
        }


def check_availability(
    roots: Sequence[Path],
    symbol: str,
    start: date,
    end: date,
    *,
    resolution: Resolution = "minute",
) -> AvailabilityReport:
    """Scan the given roots and report which weekdays have data on disk.

    The first root that contains a given date "wins" for the ``sources``
    breakdown — matching the read-order used by the corresponding reader
    (``LeanMinuteDataReader`` / ``LeanDailyDataReader``).

    For ``resolution="minute"`` a day is "available" iff the per-day zip
    exists under that root. For ``resolution="daily"`` a day is
    "available" iff the per-symbol history zip under that root contains
    a CSV row stamped with that trading date. Each root's daily zip is
    read at most once per call.
    """
    if end < start:
        raise ValueError(f"end ({end}) must not precede start ({start})")

    expected = list(_iter_weekdays(start, end))
    sources: dict[str, list[date]] = {str(r): [] for r in roots}
    found: set[date] = set()

    if resolution == "minute":
        for trading_date in expected:
            filename = _minute_zip_filename(trading_date)
            for root in roots:
                path = _minute_symbol_dir(root, symbol) / filename
                if path.exists():
                    sources[str(root)].append(trading_date)
                    found.add(trading_date)
                    break
    elif resolution == "daily":
        # Read each root's daily zip once and cache the set of dates it
        # contributes; then walk expected weekdays assigning each to the
        # first root that has it.
        per_root_dates: list[tuple[Path, set[date]]] = [
            (root, _read_daily_dates(_daily_zip_path(root, symbol))) for root in roots
        ]
        for trading_date in expected:
            for root, root_dates in per_root_dates:
                if trading_date in root_dates:
                    sources[str(root)].append(trading_date)
                    found.add(trading_date)
                    break
    else:
        raise ValueError(f"Unsupported resolution {resolution!r}; expected 'minute' or 'daily'")

    missing = [d for d in expected if d not in found]

    return AvailabilityReport(
        symbol=symbol.upper(),
        start=start,
        end=end,
        resolution=resolution,
        expected_days=len(expected),
        available_days=len(found),
        missing_days=missing,
        sources=sources,
    )


def ensure_range(
    *,
    reference_roots: Sequence[Path],
    cache_root: Path,
    symbol: str,
    start: date,
    end: date,
    polygon: Any,
    adjusted: bool,
    resolution: Resolution = "minute",
) -> AvailabilityReport:
    """Guarantee the given date range is available, fetching into the cache.

    ``cache_root`` must be the **policy-keyed** root for the fetch's
    ``adjusted`` mode (see :mod:`app.engine.data.policy_store`) — the
    ``adjusted`` flag is keyword-required with no default so no caller
    can silently mix adjusted and raw bars in one tree again.

    Checks availability across ``reference_roots`` plus ``cache_root`` and,
    if anything is missing, invokes the Polygon→LEAN exporter to write the
    missing span into ``cache_root``. Returns the post-fetch availability
    report so callers can log what was materialized.

    The fetch-and-write happens under the store's per-symbol advisory
    lock with a re-check inside: two concurrent runs asking for the same
    symbol serialize, and the loser observes the winner's zips instead of
    re-fetching. Every fetch appends to the symbol's provenance document.

    Only the *missing* spans of the window are fetched, not the whole
    window (issue #1830). The Polygon aggregates endpoint is billed per
    request, so contiguous missing days are batched into one request
    each; a window that is wholly new, or extended at either end, still
    costs a single request, and alternating coverage costs one per gap.
    A window that can never report complete -- see the note on
    ``_WEEKEND`` -- still re-fetches on every call, but re-fetches only
    the days it is missing rather than re-exporting the whole range.
    """
    all_roots = [*reference_roots, cache_root]
    pre = check_availability(all_roots, symbol, start, end, resolution=resolution)
    if pre.is_complete:
        logger.info(
            "[ENGINE] %s data for %s %s..%s already complete (%d days)",
            resolution,
            symbol,
            start,
            end,
            pre.available_days,
        )
        return pre

    # Imported lazily to avoid pulling the Polygon stack when callers only
    # want to read a report (e.g. the availability endpoint).
    from app.engine.data.policy_store import record_fetch, symbol_write_lock
    from app.engine.data.polygon_export import export_polygon_range_to_lean
    from app.utils.timestamps import now_ms_utc

    cache_root.mkdir(parents=True, exist_ok=True)
    with symbol_write_lock(cache_root, symbol):
        # Re-check under the lock: a concurrent run may have just
        # materialized the same range while we waited.
        pre = check_availability(all_roots, symbol, start, end, resolution=resolution)
        if pre.is_complete:
            logger.info(
                "[ENGINE] %s data for %s %s..%s materialized by a concurrent run",
                resolution,
                symbol,
                start,
                end,
            )
            return pre

        spans = _missing_spans(start, end, set(pre.missing_days))
        logger.info(
            "[ENGINE] Materializing %s %s %s..%s into cache — %d/%d weekdays missing in %d span(s)",
            resolution,
            symbol,
            start,
            end,
            len(pre.missing_days),
            pre.expected_days,
            len(spans),
        )

        for span_start, span_end in spans:
            export_polygon_range_to_lean(
                polygon=polygon,
                output_root=cache_root,
                symbol=symbol.upper(),
                from_date=span_start.isoformat(),
                to_date=span_end.isoformat(),
                adjusted=adjusted,
                resolution=resolution,
            )
            record_fetch(
                cache_root,
                symbol,
                source="polygon",
                adjusted=adjusted,
                resolution=resolution,
                from_date=span_start.isoformat(),
                to_date=span_end.isoformat(),
                fetched_at_ms=now_ms_utc(),
            )

    return check_availability(all_roots, symbol, start, end, resolution=resolution)
