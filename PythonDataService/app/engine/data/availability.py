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
   (``MissingSessionsError``, which names the gaps as session ranges, and
   any file that is on disk but cannot be read).

A session is available exactly when its reader would read bars for it, and
the reader's own parser is what answers — never a looser scan here (#2445
review). For ``"minute"`` that means the per-day zip
``{YYYYMMDD}_trade.zip`` under ``equity/usa/minute/{symbol}/`` which
``LeanMinuteDataReader`` selects (the first root holding one) yields at
least one bar of the requested session. For ``"daily"`` it means
``LeanDailyDataReader`` parses a bar for that trading date out of the
per-symbol history zip ``equity/usa/daily/{symbol}.zip``. A file the reader
cannot decode is reported as *unreadable*, apart from *missing*: a backfill
does not repair it (#2489). The per-root ``sources`` breakdown honors the
same reference-first order that the readers use.
"""

from __future__ import annotations

import logging
import zipfile
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from itertools import groupby
from pathlib import Path
from typing import Any, Literal

from app.engine.data.lean_format import LeanDailyDataReader, LeanMinuteDataReader

logger = logging.getLogger(__name__)

Resolution = Literal["minute", "daily"]
Session = Literal["regular", "extended"]

# What the readers raise on a file they cannot decode: a damaged, truncated or
# empty archive (``BadZipFile``, ``zlib.error``, ``EOFError``, and
# ``IndexError`` for an archive with no member), a row they cannot convert
# (``ValueError``, which covers ``UnicodeDecodeError``; ``decimal.InvalidOperation``
# is an ``ArithmeticError``), or a file the process cannot open (``OSError``).
_DECODE_ERRORS: tuple[type[Exception], ...] = (
    zipfile.BadZipFile,
    zlib.error,
    EOFError,
    IndexError,
    ValueError,
    ArithmeticError,
    OSError,
)


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


@dataclass(frozen=True)
class UnreadableFile:
    """A data file that is on disk but that its reader cannot decode."""

    path: str
    reason: str


def _unreadable(path: Path | str, exc: Exception) -> UnreadableFile:
    unreadable = UnreadableFile(path=str(path), reason=f"{type(exc).__name__}: {exc}")
    logger.warning(
        "[AVAILABILITY] a data file is on disk but cannot be read",
        extra={"path": unreadable.path, "reason": unreadable.reason},
    )
    return unreadable


def _selected_minute_zip(roots: Sequence[Path], symbol: str, trading_date: date) -> tuple[Path, Path] | None:
    """The ``(root, zip)`` ``LeanMinuteDataReader`` reads for a session: the first root holding one.

    The reader never looks past that zip — a later root's copy of the same
    session is shadowed even when the first holds no usable bar — so
    availability does not either (#2475 review).
    """
    filename = _minute_zip_filename(trading_date)
    for root in roots:
        path = _minute_symbol_dir(root, symbol) / filename
        if path.exists():
            return root, path
    return None


@dataclass(frozen=True)
class _MinuteVerdict:
    """What the minute reader makes of one day's zip: whether it yields a bar, or why it cannot decode it."""

    has_bars: bool
    unreadable: UnreadableFile | None = None


def _read_minute_zip(path: Path, symbol: str, trading_date: date, session: Session) -> _MinuteVerdict:
    """The minute reader's verdict on one day's zip, parsed once per version of the file.

    Parsing is the price of asking the reader instead of a second parser:
    about 10 ms a session, most of it the reader's per-day calendar lookup,
    so 500 sessions cost about 5 s the first time. The verdict is cached
    against the file's inode, size, mtime and ctime, so checking an
    unchanged file again costs one ``stat`` (500 sessions: about 13 ms, as
    the existence check did) and a rewritten one is parsed afresh.
    """
    stat = path.stat()
    version = (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    return _parse_minute_zip(str(path), version, symbol.upper(), trading_date, session)


@lru_cache(maxsize=32_768)
def _parse_minute_zip(
    path: str,
    version: tuple[int, int, int, int],
    symbol: str,
    trading_date: date,
    session: Session,
) -> _MinuteVerdict:
    """The uncached body of :func:`_read_minute_zip`; ``version`` only keys the cache."""
    try:
        reader = LeanMinuteDataReader(Path(path).parent, session=session)
        bars = reader.parse_day_zip(Path(path).read_bytes(), symbol, trading_date)
    except _DECODE_ERRORS as exc:
        return _MinuteVerdict(has_bars=False, unreadable=_unreadable(path, exc))
    return _MinuteVerdict(has_bars=bool(bars))


def _read_daily_dates(root: Path, symbol: str) -> set[date] | UnreadableFile:
    """The sessions ``root``'s daily history holds a bar for, asked of the daily reader itself.

    ``LeanDailyDataReader`` picks the zip member and parses the rows, and it
    drops a row it cannot read (wrong column count, a stamp without its
    time) without a word. So a session is available exactly when the reader
    yields a bar for it — never merely because a line begins with its date,
    which is how a looser scan of its own here once admitted a truncated row
    the run then skipped (#2445 review). One parser of the format. Parsing a
    21-year history costs ~17 ms against the scan's ~1.5 ms, once per root
    per check. A history the reader cannot decode at all is unreadable, not
    empty: the reader aborts on it (#2489).
    """
    try:
        return set(LeanDailyDataReader(root).available_dates(symbol))
    except _DECODE_ERRORS as exc:
        return _unreadable(_daily_zip_path(root, symbol), exc)


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
    # Sessions whose data is on disk but cannot be read, and the files at fault.
    unreadable_days: list[date] = field(default_factory=list)
    unreadable_files: list[UnreadableFile] = field(default_factory=list)
    # Per-root breakdown: {root_path: [dates_found_in_that_root]}
    sources: dict[str, list[date]] = field(default_factory=dict)

    @property
    def is_complete(self) -> bool:
        return self.available_days >= self.expected_days and not self.unreadable_files

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
            "unreadable_days": [d.isoformat() for d in self.unreadable_days],
            "unreadable_files": [
                {"path": file.path, "reason": file.reason} for file in self.unreadable_files
            ],
            "sources": {root: [d.isoformat() for d in dates] for root, dates in self.sources.items()},
        }


# A window with holes scattered through it is still refused whole; past this
# many ranges the message counts the rest instead of listing them.
_SHOWN_SPANS = 10
# Likewise for unreadable files, whose full paths are long.
_SHOWN_FILES = 3


def _describe_span(span: tuple[date, date]) -> str:
    first, last = span
    return first.isoformat() if first == last else f"{first.isoformat()}..{last.isoformat()}"


def _describe_window(report: AvailabilityReport, count: int) -> str:
    return f"{count} of {report.expected_days} trading sessions in {report.start.isoformat()}..{report.end.isoformat()}"


def _describe_missing(report: AvailabilityReport) -> str:
    spans = report.missing_spans
    shown = ", ".join(_describe_span(span) for span in spans[:_SHOWN_SPANS])
    more = f" (+{len(spans) - _SHOWN_SPANS} more gaps)" if len(spans) > _SHOWN_SPANS else ""
    return (
        f"missing data: {report.symbol} has no {report.resolution} bars for "
        f"{_describe_window(report, len(report.missing_days))} — missing {shown}{more}"
    )


def _describe_unreadable(report: AvailabilityReport) -> str:
    files = report.unreadable_files
    shown = ", ".join(f"{file.path} ({file.reason})" for file in files[:_SHOWN_FILES])
    more = f" (+{len(files) - _SHOWN_FILES} more files)" if len(files) > _SHOWN_FILES else ""
    return (
        f"unreadable data: {report.symbol} {report.resolution} data for "
        f"{_describe_window(report, len(report.unreadable_days))} is on disk but cannot be read — {shown}{more}"
    )


class MissingSessionsError(ValueError):
    """A finite read window whose scheduled sessions the readers cannot all supply.

    The one "these sessions cannot be read" refusal. Raised at a
    data-loading boundary instead of letting the reader skip the absent
    sessions (#2445) — the Spec data-source factory, the sweep's snapshot
    capture — and the text a Grid Search preflight refusal is built on: the
    temporal rule for finite ingestion is to fail fast, never to repair.
    The message names the symbol and says, apart, which sessions are
    missing — as contiguous session ranges, so a year-long hole is one
    range, not 250 dates — and which files are on disk but cannot be read,
    by path and decode error (#2489).
    """

    def __init__(self, report: AvailabilityReport) -> None:
        self.report = report
        parts = []
        if report.unreadable_files:
            parts.append(_describe_unreadable(report))
        if report.missing_days or not report.unreadable_files:
            parts.append(_describe_missing(report))
        super().__init__("; ".join(parts))


def check_availability(
    roots: Sequence[Path],
    symbol: str,
    start: date,
    end: date,
    *,
    resolution: Resolution = "minute",
    session: Session = "regular",
) -> AvailabilityReport:
    """Report which trading sessions the given roots hold bars for.

    Expected days are the canonical calendar's scheduled sessions — half
    days included, weekends and exchange closures excluded — so a window is
    complete exactly when the reader would read bars for every session the
    market held.

    The root that supplies a session is the one its reader reads — matching
    ``LeanMinuteDataReader`` / ``LeanDailyDataReader`` — and ``sources``
    records it.

    For ``resolution="minute"`` a session is available iff the zip the
    reader selects for it (the first root's that exists) yields at least
    one bar under ``session``, the reader's session filter: to a
    regular-session run a zip holding only pre-market bars is missing. A
    zip the reader cannot decode makes its session unreadable.

    For ``resolution="daily"`` a session is available iff the daily reader
    parses a bar for it out of the per-symbol history under some root, the
    first root winning. The reader merges every root's history and aborts on
    one it cannot decode, so a single unreadable history makes every
    session unreadable. Each root's daily zip is parsed once per call.
    """
    if end < start:
        raise ValueError(f"end ({end}) must not precede start ({start})")

    expected = _expected_sessions(start, end)
    sources: dict[str, list[date]] = {str(r): [] for r in roots}
    found: set[date] = set()
    unreadable_days: list[date] = []
    unreadable_files: list[UnreadableFile] = []

    if resolution == "minute":
        for trading_date in expected:
            selected = _selected_minute_zip(roots, symbol, trading_date)
            if selected is None:
                continue
            root, path = selected
            verdict = _read_minute_zip(path, symbol, trading_date, session)
            if verdict.unreadable is not None:
                unreadable_days.append(trading_date)
                unreadable_files.append(verdict.unreadable)
            elif verdict.has_bars:
                sources[str(root)].append(trading_date)
                found.add(trading_date)
    elif resolution == "daily":
        readable: list[tuple[Path, set[date]]] = []
        for root in roots:
            dates = _read_daily_dates(root, symbol)
            if isinstance(dates, UnreadableFile):
                unreadable_files.append(dates)
            else:
                readable.append((root, dates))
        if unreadable_files:
            # A later root's copy of a session does not save it: the reader
            # aborts before it gets there (#2475 review).
            unreadable_days = list(expected)
        else:
            for trading_date in expected:
                for root, root_dates in readable:
                    if trading_date in root_dates:
                        sources[str(root)].append(trading_date)
                        found.add(trading_date)
                        break
    else:
        raise ValueError(f"Unsupported resolution {resolution!r}; expected 'minute' or 'daily'")

    unreadable = set(unreadable_days)
    missing = [d for d in expected if d not in found and d not in unreadable]
    # Adjacency is read off the expected sessions, so two missing sessions
    # share a span when every session between them is missing too (a Friday
    # and the following Monday are one span) and a closure never starts or
    # extends one: nothing is missing on a day the market never opened.
    missing_set = set(missing)
    missing_spans: list[tuple[date, date]] = []
    for is_missing, days in groupby(expected, key=missing_set.__contains__):
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
        unreadable_days=unreadable_days,
        unreadable_files=unreadable_files,
        sources=sources,
    )
