"""Which lake sessions a run can read, and which a backfill has to rebuild.

The catalog calls a minute session complete once its price file is committed.
A split-adjusted run needs more: an adjustment record beside the file, bound
to the corporate-action version the symbol's prices are currently adjusted
against (``adjustment_versions.verify_adjustment_receipt``). A file written
before that record existed, or against a version since superseded, is
complete in the catalog and refused by every reader.

Two views answer for it:

* ``coverage_gaps`` checks one symbol's sessions against the files on disk,
  the way a writer checks a file before it reuses it.
* ``fold_run_readiness`` counts a whole lake from the version the catalog
  recorded for each session, so it opens one snapshot per symbol instead of
  one record per session.

A session the lake never fetched is a third thing: neither view lists it,
because no row exists for it. A symbol backfilled in two stretches holds a
first and a last session with a hole between them, and a run laid over the
hole is refused. ``gapped_symbols`` finds those symbols from the counts
alone; ``span_gaps`` measures one symbol's holes from its held sessions.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from itertools import groupby
from pathlib import Path

from app.data_lake.adjustment_versions import AdjustmentVersionError, adjusted_file_gap, current_version
from app.data_lake.catalog_client import ArtifactCoverageRow, SymbolRevisionCount
from app.data_lake.types import AdjustedFileGap, PriceAdjustmentMode, SymbolRunReadiness
from app.lean_sidecar.trading_calendar import expected_sessions, session_open_ms_utc


def needs_adjustment_record(price_adjustment_mode: PriceAdjustmentMode) -> bool:
    """Only split-adjusted files carry an adjustment record; raw prices have nothing to adjust."""
    return price_adjustment_mode == "polygon_split_adjusted"


def coverage_gaps(rows: Sequence[ArtifactCoverageRow], lake_root: Path) -> dict[date, AdjustedFileGap]:
    """The complete sessions among ``rows`` whose file under ``lake_root`` lacks a valid receipt, and why.

    Empty for a root whose files need no adjustment record.
    """
    gaps: dict[date, AdjustedFileGap] = {}
    for row in rows:
        if row.status != "complete":
            continue
        gap = adjusted_file_gap(lake_root / row.file_path, row.file_sha256 or "", row.symbol)
        if gap is not None:
            gaps[row.trading_date] = gap
    return gaps


def current_versions(lake_root: Path, symbols: Iterable[str]) -> dict[str, str | None]:
    """Each symbol's current corporate-action version; ``None`` where the lake holds no snapshot of one."""
    versions: dict[str, str | None] = {}
    for symbol in symbols:
        try:
            versions[symbol] = current_version(lake_root, symbol)
        except AdjustmentVersionError:
            # No adjusted capture has run for this symbol yet, so no session of it is readable.
            versions[symbol] = None
    return versions


@dataclass(frozen=True)
class SpanGaps:
    """The scheduled sessions between a symbol's first and last held one that the lake does not hold."""

    session_count: int
    #: Unbroken stretches of missing sessions: one long hole and fifty scattered days count differently.
    gap_count: int
    #: Missing sessions older than the provider's history, which no backfill can fetch.
    unfillable_count: int
    #: The first and last missing session a backfill can fetch; ``None`` when there are none.
    fill_first: date | None
    fill_last: date | None


_NO_GAPS = SpanGaps(session_count=0, gap_count=0, unfillable_count=0, fill_first=None, fill_last=None)


def gapped_symbols(counts: Sequence[SymbolRevisionCount]) -> list[str]:
    """The symbols holding fewer sessions than the calendar schedules between their first and last one."""
    spans: dict[str, tuple[date, date, int]] = {}
    for count in counts:
        first, last, held = spans.get(count.symbol, (count.first_trading_date, count.last_trading_date, 0))
        spans[count.symbol] = (min(first, count.first_trading_date), max(last, count.last_trading_date), held + count.session_count)
    if not spans:
        return []
    # One schedule for the widest span, read per symbol: a schedule per symbol costs more than the catalog query.
    sessions = expected_sessions(min(first for first, _, _ in spans.values()), max(last for _, last, _ in spans.values()))
    return sorted(
        symbol
        for symbol, (first, last, held) in spans.items()
        if held < bisect_right(sessions, last) - bisect_left(sessions, first)
    )


def span_gaps(held: Collection[date], floor: date) -> SpanGaps:
    """The holes between the first and last of ``held``; ``floor`` is the oldest session the provider still serves."""
    stretches = [
        list(days)
        for is_held, days in groupby(expected_sessions(min(held), max(held)), key=set(held).__contains__)
        if not is_held
    ]
    missing = [day for stretch in stretches for day in stretch]
    fillable = [day for day in missing if day >= floor]
    return SpanGaps(
        session_count=len(missing),
        gap_count=len(stretches),
        unfillable_count=len(missing) - len(fillable),
        fill_first=fillable[0] if fillable else None,
        fill_last=fillable[-1] if fillable else None,
    )


def fold_run_readiness(
    counts: Sequence[SymbolRevisionCount],
    versions: Mapping[str, str | None] | None,
    gaps: Mapping[str, SpanGaps] | None = None,
) -> list[SymbolRunReadiness]:
    """One readiness row per symbol, in symbol order.

    ``versions`` is ``None`` for a mode whose files need no adjustment
    record: every complete session is then readable. Otherwise a session is
    readable when the catalog recorded the symbol's current version for it.
    ``gaps`` holds the symbols with a hole in their span; one it leaves out has none.
    """
    by_symbol: dict[str, list[SymbolRevisionCount]] = {}
    for count in counts:
        by_symbol.setdefault(count.symbol, []).append(count)

    rows: list[SymbolRunReadiness] = []
    for symbol in sorted(by_symbol):
        groups = by_symbol[symbol]
        current = None if versions is None else versions.get(symbol)
        unreadable = [
            group for group in groups if versions is not None and (current is None or group.revision != current)
        ]
        rebuildable = [group for group in unreadable if not group.before_floor]
        session_count = sum(group.session_count for group in groups)
        holes = (gaps or {}).get(symbol, _NO_GAPS)
        rows.append(
            SymbolRunReadiness(
                symbol=symbol,
                first_trading_date_ms=session_open_ms_utc(min(group.first_trading_date for group in groups)),
                last_trading_date_ms=session_open_ms_utc(max(group.last_trading_date for group in groups)),
                session_count=session_count,
                run_ready_count=session_count - sum(group.session_count for group in unreadable),
                unrebuildable_count=sum(group.session_count for group in unreadable if group.before_floor),
                rebuild_first_trading_date_ms=(
                    session_open_ms_utc(min(group.first_trading_date for group in rebuildable))
                    if rebuildable
                    else None
                ),
                rebuild_last_trading_date_ms=(
                    session_open_ms_utc(max(group.last_trading_date for group in rebuildable))
                    if rebuildable
                    else None
                ),
                gap_session_count=holes.session_count,
                gap_count=holes.gap_count,
                unfillable_gap_session_count=holes.unfillable_count,
                gap_first_trading_date_ms=None if holes.fill_first is None else session_open_ms_utc(holes.fill_first),
                gap_last_trading_date_ms=None if holes.fill_last is None else session_open_ms_utc(holes.fill_last),
            )
        )
    return rows
