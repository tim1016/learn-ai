"""One-time migration: give the lake tree its adjustment-mode segment (#1839).

Before, every artifact lived directly under ``<LEAN_DATA_WRITE_ROOT>/lake``
and the tree's adjustment mode was recorded — at most — in a marker file,
because raw and adjusted bytes for one ``(symbol, date)`` resolved to the
same path and could not coexist. The mode is now a path segment
(``path_policy.resolve_lake_root``), so this moves the existing tree into the
mode it has always actually held.

**Everything already in the lake is raw.** ``DataRunSpec.price_adjustment_mode``
was pinned ``Literal["raw"]`` and ``polygon_fetcher`` hardcoded
``adjusted=false``, so the live pipeline could not have produced anything
else. The one way a tree could hold something else is a ``cache_import`` run
of an adjusted cache, which stamped a marker saying so — this script reads
that marker when present and refuses rather than guessing.

Catalog rows need no migration: ``FilePath`` is root-relative and the segment
is added *above* the LEAN tree, so every stored path stays byte-identical.

Idempotent: a lake already holding only mode directories is left alone.

Usage (inside the data-plane container, which has the volume mounted):

    python -m scripts.migrate_lake_to_mode_roots [--apply]

Without ``--apply`` it prints what it would move and changes nothing.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from app.data_lake.path_policy import resolve_lake_container, resolve_lake_root
from app.data_lake.types import PriceAdjustmentMode

logger = logging.getLogger("migrate_lake_to_mode_roots")

# The marker the deleted whole-root gate used to write. Read here (and only
# here) so a tree that a cache_import run committed to a non-raw mode is not
# silently relabelled as raw.
LEGACY_MODE_MARKER = ".cache_import_adjustment_mode"

_MODES: tuple[PriceAdjustmentMode, ...] = ("raw", "polygon_split_adjusted", "lean_adjusted")


def legacy_tree_mode(container: Path) -> PriceAdjustmentMode:
    """Return the adjustment mode the pre-#1839 tree actually holds."""
    marker = container / LEGACY_MODE_MARKER
    if not marker.is_file():
        return "raw"
    recorded = marker.read_text().strip()
    if recorded not in _MODES:
        raise SystemExit(
            f"{marker} records adjustment mode {recorded!r}, which is not a known mode. "
            "Refusing to guess where this tree belongs — resolve it by hand."
        )
    return recorded  # type: ignore[return-value]


def plan_moves(container: Path) -> list[Path]:
    """Top-level entries that are lake content rather than a mode root."""
    if not container.is_dir():
        return []
    return sorted(
        entry
        for entry in container.iterdir()
        if entry.name not in _MODES and entry.name != LEGACY_MODE_MARKER
    )


def migrate(*, apply: bool) -> int:
    container = resolve_lake_container()
    moves = plan_moves(container)
    if not moves:
        logger.info("%s already holds only mode roots; nothing to do", container)
        return 0

    mode = legacy_tree_mode(container)
    destination = resolve_lake_root(mode)
    logger.info("moving %d top-level entries from %s into %s", len(moves), container, destination)
    for entry in moves:
        logger.info("  %s -> %s", entry.name, destination / entry.name)

    if not apply:
        logger.info("dry run; re-run with --apply to move them")
        return 0

    destination.mkdir(parents=True, exist_ok=True)
    for entry in moves:
        target = destination / entry.name
        if target.exists():
            raise SystemExit(
                f"{target} already exists; refusing to merge two trees. Resolve by hand — "
                "silently merging could pair a catalog row with bytes it does not describe."
            )
        # Same filesystem by construction (both under the lake container), so
        # this is a rename, not a copy: no window where an artifact is half
        # present, and no second copy of the bytes on disk.
        entry.rename(target)
    (container / LEGACY_MODE_MARKER).unlink(missing_ok=True)
    logger.info("done; %s now holds mode roots only", container)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="perform the moves (default: dry run)")
    args = parser.parse_args()
    return migrate(apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
