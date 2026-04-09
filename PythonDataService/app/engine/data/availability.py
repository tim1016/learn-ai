"""Data availability and on-demand materialization for the LEAN engine.

The engine reads LEAN-format minute zips from one or more roots (see
``LeanMinuteDataReader``). For the SPY reference fixture the data is
pre-baked in a read-only mount. For arbitrary tickers the caller does not
have a LEAN zip yet — this module bridges that gap by:

1. Reporting which trading days are already covered across the configured
   roots (``check_availability``).
2. Materializing missing days into a *writable* cache root by calling the
   existing ``export_polygon_range_to_lean`` bridge (``ensure_range``).

Keeping this logic behind a small service keeps the router thin and lets
the engine tests exercise availability checks without needing a live
Polygon client.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

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


def _zip_filename(trading_date: date) -> str:
    return f"{trading_date.strftime('%Y%m%d')}_trade.zip"


def _symbol_dir(root: Path, symbol: str) -> Path:
    return root / "equity" / "usa" / "minute" / symbol.lower()


@dataclass
class AvailabilityReport:
    symbol: str
    start: date
    end: date
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
            "expected_days": self.expected_days,
            "available_days": self.available_days,
            "is_complete": self.is_complete,
            "missing_days": [d.isoformat() for d in self.missing_days],
            "sources": {
                root: [d.isoformat() for d in dates]
                for root, dates in self.sources.items()
            },
        }


def check_availability(
    roots: Sequence[Path],
    symbol: str,
    start: date,
    end: date,
) -> AvailabilityReport:
    """Scan the given roots and report which weekdays have a zip on disk.

    The first root that contains a given date "wins" for the ``sources``
    breakdown — matching the read-order used by ``LeanMinuteDataReader``.
    """
    if end < start:
        raise ValueError(f"end ({end}) must not precede start ({start})")

    expected = list(_iter_weekdays(start, end))
    sources: dict[str, list[date]] = {str(r): [] for r in roots}
    found: set[date] = set()

    for trading_date in expected:
        filename = _zip_filename(trading_date)
        for root in roots:
            path = _symbol_dir(root, symbol) / filename
            if path.exists():
                sources[str(root)].append(trading_date)
                found.add(trading_date)
                break

    missing = [d for d in expected if d not in found]

    return AvailabilityReport(
        symbol=symbol.upper(),
        start=start,
        end=end,
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
    pre = check_availability(all_roots, symbol, start, end)
    if pre.is_complete:
        logger.info(
            "[ENGINE] Data for %s %s..%s already complete (%d days)",
            symbol,
            start,
            end,
            pre.available_days,
        )
        return pre

    logger.info(
        "[ENGINE] Materializing %s %s..%s into cache — %d/%d weekdays missing",
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
    )

    return check_availability(all_roots, symbol, start, end)
