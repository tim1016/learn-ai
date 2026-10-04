"""Which lake sessions a run can read, and which a backfill has to rebuild.

The catalog calls a minute session complete once its price file is committed.
A split-adjusted run needs more: an adjustment record beside the file, bound
to the corporate-action version the symbol's prices are currently adjusted
against (``adjustment_versions.verify_adjustment_receipt``). A file written
before that record existed, or against a version since superseded, is
complete in the catalog and refused by every reader.

Two views answer for it:

* ``coverage_gaps`` checks one symbol's sessions against the files on disk,
  with the reader's own verifier.
* ``fold_run_readiness`` counts a whole lake from the version the catalog
  recorded for each session, so it opens one snapshot per symbol instead of
  one record per session.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from pathlib import Path

from app.data_lake.adjustment_versions import AdjustmentVersionError, adjusted_file_gap, current_version
from app.data_lake.catalog_client import ArtifactCoverageRow, SymbolRevisionCount
from app.data_lake.types import AdjustedFileGap, PriceAdjustmentMode, SymbolRunReadiness
from app.lean_sidecar.trading_calendar import session_open_ms_utc


def needs_adjustment_record(price_adjustment_mode: PriceAdjustmentMode) -> bool:
    """Only split-adjusted files carry an adjustment record; raw prices have nothing to adjust."""
    return price_adjustment_mode == "polygon_split_adjusted"


def coverage_gaps(
    rows: Sequence[ArtifactCoverageRow], lake_root: Path, symbol: str
) -> dict[date, AdjustedFileGap]:
    """The complete sessions among ``rows`` that a reader on ``lake_root`` refuses, and why."""
    gaps: dict[date, AdjustedFileGap] = {}
    for row in rows:
        if row.status != "complete":
            continue
        gap = adjusted_file_gap(lake_root / row.file_path, row.file_sha256 or "", symbol)
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


def fold_run_readiness(
    counts: Sequence[SymbolRevisionCount], versions: Mapping[str, str | None] | None
) -> list[SymbolRunReadiness]:
    """One readiness row per symbol, in symbol order.

    ``versions`` is ``None`` for a mode whose files need no adjustment
    record: every complete session is then readable. Otherwise a session is
    readable when the catalog recorded the symbol's current version for it.
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
            )
        )
    return rows
