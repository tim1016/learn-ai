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
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

Resolution = Literal["minute", "daily"]

# US equities open Monday–Friday. This filter is deliberately naive: it
# ignores exchange holidays because the downstream reader already skips
# dates with no zip file. The goal here is only to avoid reporting Sundays
# as "missing" and confusing users.
_WEEKEND = {5, 6}


def _iter_weekdays(start: date, end: date):
    current = start
    one_day = timedelta(days=1)
    while current <= end:
        if current.weekday() not in _WEEKEND:
            yield current
        current += one_day


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
    resolution: Resolution = "minute",
) -> AvailabilityReport:
    """Guarantee the given date range is available, fetching into the cache.

    Checks availability across ``reference_roots`` plus ``cache_root`` and,
    if anything is missing, invokes the Polygon→LEAN exporter to write the
    missing span into ``cache_root``. Returns the post-fetch availability
    report so callers can log what was materialized.

    The function always exports the *full requested range* rather than
    cherry-picking missing days because the Polygon aggregates endpoint is
    billed per request, and fetching one range is cheaper than N one-day
    requests for sparse gaps.
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

    logger.info(
        "[ENGINE] Materializing %s %s %s..%s into cache — %d/%d weekdays missing",
        resolution,
        symbol,
        start,
        end,
        len(pre.missing_days),
        pre.expected_days,
    )

    # Imported lazily to avoid pulling the Polygon stack when callers only
    # want to read a report (e.g. the availability endpoint).
    from app.engine.data.polygon_export import export_polygon_range_to_lean

    cache_root.mkdir(parents=True, exist_ok=True)
    export_polygon_range_to_lean(
        polygon=polygon,
        output_root=cache_root,
        symbol=symbol.upper(),
        from_date=start.isoformat(),
        to_date=end.isoformat(),
        resolution=resolution,
    )

    return check_availability(all_roots, symbol, start, end, resolution=resolution)
