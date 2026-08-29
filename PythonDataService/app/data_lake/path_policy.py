"""Typed LEAN-path policy.

Sole authority for constructing LEAN on-disk paths. No string concatenation
of LEAN paths is permitted anywhere else in the codebase; a lint test enforces
that the substrings ``equity/usa/``, ``market-hours/``, ``symbol-properties/``
appear only in this module and its tests.

The lake roots (``resolve_lake_container`` / ``resolve_lake_root`` /
``resolve_staging_root``) live here for the same reason: one canonical answer
to "where is the lake on disk", so every consumer — ``ensure_data``, which
writes the artifacts, ``cache_import``, which imports them, the chart
split-read, and the sidecar mount — resolves the identical directory instead
of each re-deriving it from ``settings``.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 5.3
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Literal, get_args
from uuid import UUID

from app.config import settings
from app.data_lake.types import PriceAdjustmentMode

Market = Literal["usa"]
Resolution = Literal["minute", "hour", "daily"]
DataType = Literal["trade", "quote"]
MetadataKind = Literal["market_hours", "symbol_properties"]

_LAKE_DIR = "lake"
_STAGING_DIR = "staging"
# The closed set of directory names a lake root may have. Mirrors
# ``types.PriceAdjustmentMode``; restated as a runtime value because this
# module turns the mode into a filesystem path and a typing Literal does not
# survive to runtime.
_ADJUSTMENT_MODES: frozenset[str] = frozenset(get_args(PriceAdjustmentMode))


def lake_subpath(price_adjustment_mode: PriceAdjustmentMode) -> PurePosixPath:
    """Return the lake root's path *relative to the write root*.

    Exists for the one caller that cannot use :func:`resolve_lake_root`:
    ``app.lean_sidecar.lake_mount.launcher_host_lake_root`` builds the same
    location against the launcher **host**'s view of the volume, which is a
    different base than ``settings.LEAN_DATA_WRITE_ROOT``. Sharing the suffix
    here is what keeps the container-side and host-side answers from drifting
    -- they used to drift behind a duplicated ``LAKE_SUBDIR`` constant and a
    test pinning the two in lockstep.
    """
    return PurePosixPath(_LAKE_DIR) / price_adjustment_mode


def resolve_lake_container() -> Path:
    """Return the directory holding every per-mode lake root.

    Not itself a readable data root — LEAN is never pointed here, because
    ``equity/`` lives one level further down, inside a mode. Its one job is
    to answer "is this path part of the lake at all?" for callers that must
    refuse to treat any lake tree as their own writable store.
    """
    return Path(settings.LEAN_DATA_WRITE_ROOT) / _LAKE_DIR


def resolve_lake_root(price_adjustment_mode: PriceAdjustmentMode) -> Path:
    """Return the immutable-artifact root of the lake for one adjustment mode.

    This is the directory the LEAN readers are pointed at when
    ``DATA_LAKE_ENABLED`` is on. It is not created here — a missing root
    means "the lake holds nothing yet for this mode", which every reader must
    already handle as a per-day miss.

    **The mode is a path segment above the LEAN tree**, so ``equity/usa/...``
    still sits directly inside whatever root a reader is handed and the LEAN
    format is untouched. That placement is what lets raw and adjusted bytes
    for the same ``(market, symbol, trading_date, data_type)`` coexist: they
    resolve to different absolute paths while their catalog ``FilePath``
    stays byte-identical, because ``FilePath`` is root-relative and carries
    no root identity of its own. It also means the mode a run reads is
    structural rather than advisory — a reader cannot accidentally observe
    the other mode's bytes, because they are not in its tree.

    This replaces the whole-root mutual exclusion that stood here before
    (a ``.cache_import_adjustment_mode`` marker committing an entire tree to
    one mode and refusing the other, with a per-mode ``--lake-root`` as the
    operator's only way to hold both). That mechanism guarded a collision
    that this path shape makes structurally impossible; issue #1839 deleted
    it rather than teaching it a second mode.

    Every lake writer must still resolve its root here. A writer using a
    different root produces "phantom coverage": rows that look complete in
    the catalog but have no bytes where anything else looks.

    The mode passed here is the **run's** mode, not the artifact's. Artifact
    kinds with no adjustment mode of their own — factor files, map files, the
    market-hours and symbol-properties metadata — are adjustment-independent
    and are written into each mode root that needs them, because LEAN takes
    exactly one data root and must resolve them inside it. Their duplicated
    bytes are CSVs and one JSON; the minute-bar zips are the volume.

    The mode is validated here rather than trusted. It reaches this function
    from request input (``DataRunSpec.price_adjustment_mode``, and the
    coverage endpoint's query parameter), and it is now a path segment, so an
    unchecked value would be a traversal away from the lake. Pydantic's
    ``Literal`` already constrains both callers, but a type annotation is not
    a runtime boundary and this is the one place the segment is constructed —
    it is where the check belongs.
    """
    return _lake_root_within_container(price_adjustment_mode)


def _lake_root_within_container(price_adjustment_mode: str) -> Path:
    return _mode_subdir_within(resolve_lake_container(), price_adjustment_mode)


def _mode_subdir_within(container: Path, price_adjustment_mode: str) -> Path:
    """Build a mode-keyed subdirectory, refusing anything that escapes it.

    Two checks, because they fail differently. The membership test rejects
    the value; the containment test proves the *path* it produced is still
    inside its container, which is the property that actually matters and the
    one a future third mode could break by accident.

    Shared by the lake root and the staging root so the two cannot drift on
    what a mode segment is allowed to be.
    """
    if price_adjustment_mode not in _ADJUSTMENT_MODES:
        raise ValueError(
            f"{price_adjustment_mode!r} is not a price adjustment mode; expected one of "
            f"{', '.join(sorted(_ADJUSTMENT_MODES))}"
        )
    base = os.path.realpath(os.fspath(container))
    root = os.path.realpath(os.path.join(base, price_adjustment_mode))
    if not root.startswith(base.rstrip(os.sep) + os.sep):
        raise ValueError(f"{root!r} escapes its container {base!r}")
    return Path(root)


def resolve_staging_root(price_adjustment_mode: PriceAdjustmentMode) -> Path:
    """Return the per-attempt staging root that promotes into a lake root.

    Must share a filesystem with :func:`resolve_lake_root` so the promote is
    a rename (see ``app.data_lake.atomic.assert_same_filesystem``); both live
    under ``LEAN_DATA_WRITE_ROOT``, so they always do.

    Keyed by adjustment mode for the same reason the lake root is. A staged
    file is named by ``(request_id, worker_id, attempt)`` plus its
    root-relative destination path — and that relative path carries no mode
    (``LeanMinuteBarPath.relative_path`` deliberately does not), so raw and
    adjusted bytes for the same symbol and date name the *same* staging file.
    That was safe only while no caller reused a ``request_id`` across two
    concurrent modes. Partitioning here makes staging mirror the destination
    it promotes into, so the guarantee stops resting on an invariant
    maintained somewhere else (#1866 review).
    """
    return _mode_subdir_within(Path(settings.LEAN_DATA_WRITE_ROOT) / _STAGING_DIR, price_adjustment_mode)


def minute_bar_market_root(market: Market) -> PurePosixPath:
    """Return the market-wide minute-bar directory (no symbol/date/type yet).

    A caller that needs to *discover* what's already on disk for a market
    (rather than construct one artifact's fully-known path) still goes
    through path_policy for the prefix instead of hand-rolling
    ``equity/<market>/minute`` itself. ``LeanMinuteBarPath.relative_path``
    below builds on top of this so the segments are declared exactly once.
    """
    return PurePosixPath("equity") / market / "minute"


@dataclass(frozen=True)
class LeanMinuteBarPath:
    market: Market
    symbol: str
    trading_date: date
    data_type: DataType

    def relative_path(self) -> PurePosixPath:
        return (
            minute_bar_market_root(self.market)
            / self.symbol.lower()
            / f"{self.trading_date.strftime('%Y%m%d')}_{self.data_type}.zip"
        )


@dataclass(frozen=True)
class LeanDailyBarPath:
    market: Market
    symbol: str

    def relative_path(self) -> PurePosixPath:
        return PurePosixPath("equity") / self.market / "daily" / f"{self.symbol.lower()}.zip"


def corporate_action_dirs(market: Market) -> tuple[PurePosixPath, ...]:
    """The factor-file and map-file directories LEAN expects to find.

    LEAN's ``LocalDiskMapFileProvider`` warns when these are absent, and the
    sidecar's run classifier counts that warning against the run. The pre-lake
    staging path therefore creates them empty on every run
    (``staging.stage_empty_corporate_action_dirs``) even for a window with no
    corporate actions — an empty directory says "no corporate actions here",
    a missing one says "no idea".

    Lake mode cannot do the same thing at run time: the mount is read-only and
    LEAN's data folder *is* the lake. So the lake's writers create them
    instead, which is why this returns directories rather than files — see
    ``ensure_lean_readable_layout``.
    """
    return (
        PurePosixPath("equity") / market / "factor_files",
        PurePosixPath("equity") / market / "map_files",
    )


def ensure_lean_readable_layout(lake_root: Path, market: Market = "usa") -> None:
    """Create the directories LEAN needs present but the lake may never fill.

    Called by every writer that can bring a lake into existence — the live
    ``ensure_data`` pipeline and ``cache_import`` — rather than by the
    sidecar's preflight, which is a pure read of a read-only mount and must
    stay one. Idempotent.
    """
    for relative in corporate_action_dirs(market):
        (lake_root / Path(*relative.parts)).mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class LeanFactorFilePath:
    market: Market
    symbol: str

    def relative_path(self) -> PurePosixPath:
        return PurePosixPath("equity") / self.market / "factor_files" / f"{self.symbol.lower()}.csv"


@dataclass(frozen=True)
class LeanMapFilePath:
    market: Market
    symbol: str

    def relative_path(self) -> PurePosixPath:
        return PurePosixPath("equity") / self.market / "map_files" / f"{self.symbol.lower()}.csv"


@dataclass(frozen=True)
class LeanMetadataPath:
    kind: MetadataKind

    def relative_path(self) -> PurePosixPath:
        if self.kind == "market_hours":
            return PurePosixPath("market-hours") / "market-hours-database.json"
        if self.kind == "symbol_properties":
            return PurePosixPath("symbol-properties") / "symbol-properties-database.csv"
        raise ValueError(f"unknown metadata kind: {self.kind!r}")


def staging_path_for(
    rel_lake_path: PurePosixPath,
    request_id: UUID,
    worker_id: str,
    attempt: int,
) -> PurePosixPath:
    """Build the per-attempt staging path for a given final relative path.

    Structurally prevents retry/parallel-worker collisions: every attempt
    writes to its own subtree under staging/. The atomic rename promotes
    the .tmp file to its final position in the lake.
    """
    return (
        PurePosixPath("staging")
        / str(request_id)
        / worker_id
        / f"attempt_{attempt}"
        / rel_lake_path.with_suffix(rel_lake_path.suffix + ".tmp")
    )
