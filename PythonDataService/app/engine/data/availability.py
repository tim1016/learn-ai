"""Data availability for the LEAN engine's on-disk readers.

The engine reads LEAN-format equity data from one or more roots (see
``LeanMinuteDataReader``, ``LeanDailyDataReader``). Those readers skip a
session that has no data on disk without a word — right for a caller that
reports coverage beside the bars it returns, wrong for a run, whose numbers
from a partial series look exactly like numbers from a complete one. This
module answers the question the readers do not ask:

1. Which scheduled trading sessions the configured roots cover
   (``check_availability``).
2. Why a window is refused when they do not cover all of them
   (``MissingSessionsError``, which names the gaps as session ranges).

Coverage is resolution-aware. For ``"minute"`` a "day is available" iff
the per-day zip ``{YYYYMMDD}_trade.zip`` exists under
``equity/usa/minute/{symbol}/`` in some root. For ``"daily"`` a "day is
available" iff the single per-symbol history zip
``equity/usa/daily/{symbol}.zip`` contains a CSV row stamped with that
trading date in some root. The per-root ``sources`` breakdown honors the
same reference-first merge order that the readers use.
"""

from __future__ import annotations

import logging
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from itertools import groupby
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

Resolution = Literal["minute", "daily"]

def _expected_sessions(start: date, end: date) -> list[date]:
    """Every scheduled NYSE session in ``[start, end]``, from the canonical calendar.

    ``check_availability`` used to walk weekdays here and count every
    exchange holiday as a day that was perpetually "missing", so a window
    containing one never reported complete. That was tolerable while the
    report only decorated a UI; it is not for a preflight that refuses a
    run on an incomplete window (PRD #1926) — a two-year window contains
    roughly eighteen closures. Imported here rather than at module top so
    importing this module stays cheap for callers that never ask.
    """
    from app.lean_sidecar.trading_calendar import expected_sessions

    return expected_sessions(start, end)


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
    # ``missing_days`` as (first, last) runs of consecutive trading sessions.
    missing_spans: list[tuple[date, date]] = field(default_factory=list)
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


# A window with holes scattered through it is still refused whole; past this
# many ranges the message counts the rest instead of listing them.
_SHOWN_SPANS = 10


def _describe_span(span: tuple[date, date]) -> str:
    first, last = span
    return first.isoformat() if first == last else f"{first.isoformat()}..{last.isoformat()}"


class MissingSessionsError(ValueError):
    """A finite read window whose scheduled sessions are not all on disk.

    The one "these sessions are missing" refusal. Raised at a data-loading
    boundary instead of letting the reader skip the absent sessions
    (#2445) — the Spec data-source factory, the sweep's snapshot capture —
    and the text a Grid Search preflight refusal is built on: the temporal
    rule for finite ingestion is to fail fast, never to repair. The message
    names the symbol, how many of the window's sessions are missing, and
    the gaps as contiguous session ranges — a year-long hole is one range,
    not 250 dates.
    """

    def __init__(self, report: AvailabilityReport) -> None:
        self.report = report
        spans = report.missing_spans
        shown = ", ".join(_describe_span(span) for span in spans[:_SHOWN_SPANS])
        more = f" (+{len(spans) - _SHOWN_SPANS} more gaps)" if len(spans) > _SHOWN_SPANS else ""
        super().__init__(
            f"missing data: {report.symbol} has no {report.resolution} bars for "
            f"{len(report.missing_days)} of {report.expected_days} trading sessions in "
            f"{report.start.isoformat()}..{report.end.isoformat()} — missing {shown}{more}"
        )


def check_availability(
    roots: Sequence[Path],
    symbol: str,
    start: date,
    end: date,
    *,
    resolution: Resolution = "minute",
) -> AvailabilityReport:
    """Scan the given roots and report which trading sessions have data on disk.

    Expected days are the canonical calendar's scheduled sessions — half
    days included, weekends and exchange closures excluded — so a window is
    complete exactly when every session the market held is on disk.

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

    expected = _expected_sessions(start, end)
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
        # contributes; then walk expected sessions assigning each to the
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
    # Adjacency is read off the expected sessions, so two missing sessions
    # share a span when no session between them was covered (a Friday and
    # the following Monday are one span) and a closure never starts or
    # extends one: nothing is missing on a day the market never opened.
    missing_spans: list[tuple[date, date]] = []
    for is_missing, days in groupby(expected, key=lambda day: day not in found):
        if is_missing:
            run = list(days)
            missing_spans.append((run[0], run[-1]))

    return AvailabilityReport(
        symbol=symbol.upper(),
        start=start,
        end=end,
        resolution=resolution,
        expected_days=len(expected),
        available_days=len(found),
        missing_days=missing,
        missing_spans=missing_spans,
        sources=sources,
    )
