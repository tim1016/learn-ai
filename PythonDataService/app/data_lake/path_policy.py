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

from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import UUID

from app.config import settings
from app.data_lake.types import PriceAdjustmentMode

Market = Literal["usa"]
Resolution = Literal["minute", "hour", "daily"]
DataType = Literal["trade", "quote"]
MetadataKind = Literal["market_hours", "symbol_properties"]

_LAKE_DIR = "lake"
_STAGING_DIR = "staging"


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
    """
    return Path(settings.LEAN_DATA_WRITE_ROOT) / lake_subpath(price_adjustment_mode)


def resolve_staging_root() -> Path:
    """Return the per-attempt staging root that promotes into a lake root.

    Must share a filesystem with :func:`resolve_lake_root` so the promote is
    a rename (see ``app.data_lake.atomic.assert_same_filesystem``); both live
    under ``LEAN_DATA_WRITE_ROOT``, so they always do.

    Deliberately *not* keyed by adjustment mode. Staging paths are already
    collision-free by ``(request_id, worker_id, attempt)`` — see
    :func:`staging_path_for` — and a run carries a single mode, so a second
    dimension here would partition nothing. The mode belongs to the durable
    tree, not to per-attempt scratch.
    """
    return Path(settings.LEAN_DATA_WRITE_ROOT) / _STAGING_DIR


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
