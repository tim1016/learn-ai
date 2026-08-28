"""Typed LEAN-path policy.

Sole authority for constructing LEAN on-disk paths. No string concatenation
of LEAN paths is permitted anywhere else in the codebase; a lint test enforces
that the substrings ``equity/usa/``, ``market-hours/``, ``symbol-properties/``
appear only in this module and its tests.

The two lake roots (``resolve_lake_root`` / ``resolve_staging_root``) live here
for the same reason: one canonical answer to "where is the lake on disk", so
its two direct consumers — ``ensure_data``, which writes the artifacts, and the
chart split-read, which reads them — resolve the identical directory instead of
each re-deriving it from ``settings``.

:func:`lake_serves` is here on the same principle: one canonical answer to
"is the lake the authority for *this* request", so the four read seams that
must agree cannot drift apart.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 5.3
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import UUID

from app.config import settings

Market = Literal["usa"]
Resolution = Literal["minute", "hour", "daily"]
DataType = Literal["trade", "quote"]
MetadataKind = Literal["market_hours", "symbol_properties"]

_LAKE_DIR = "lake"
_STAGING_DIR = "staging"


def lake_holds_adjustment_mode(*, adjusted: bool) -> bool:
    """Can the lake hold bars for a request in this adjustment mode?

    **The lake's live materialization pipeline produces raw bars only.** That
    is a type, not a policy: ``DataRunSpec.price_adjustment_mode`` is
    ``Literal["raw"]``, so ``ensure_data`` cannot be asked for anything else.
    This function is where that fact is written down once, so the four read
    seams below encode it in one place rather than four.

    A tree that ``cache_import`` stamped ``polygon_split_adjusted`` does not
    widen the answer, deliberately. Such a tree is *frozen*: readable, but not
    extendable by any live run, because the shared write seam
    (``atomic.check_write_mode_compatible``) refuses a raw write into it.
    Serving adjusted requests from it would hand out a lake that silently
    stops covering any window past the import date — a worse failure than not
    serving them at all, because it looks like success.

    Adjusted-from-the-lake is real work, not a flag: LEAN's own model is raw
    bytes plus factor files with the adjustment applied at read time, and the
    Python engine's reader does not apply factor files today. It is booked as
    the successor to this slice — and it is a prerequisite for ADR 0049's
    obligation on #1840, which cannot delete the policy tree while that tree
    is still the only answer for an adjusted request.
    """
    return not adjusted


def lake_serves(*, adjusted: bool) -> bool:
    """Is the lake the authority for this request right now?

    :func:`lake_holds_adjustment_mode` and the flag. The three seams that
    must agree on a *live* request ask this one: the engine's root resolver
    (``policy_store.resolve_data_roots``), the engine's materializer
    (``routers.engine``), and the LEAN sidecar's preflight
    (``lean_sidecar.lake_mount.lake_mount_enabled``). The chart split-read
    asks :func:`lake_holds_adjustment_mode` directly instead, because
    ``chart_service`` has already checked the flag before calling it and a
    second check there would only make the module's unit behaviour depend on
    a global its caller owns.

    The first two are load-bearing in a way that stays invisible until it
    breaks: a run that materializes into the lake but reads from the policy
    store fetches bars nobody reads and then reads bars nobody fetched, and
    the symptom — an empty backtest after a successful fetch — points at
    neither seam.

    **What an adjusted request gets instead** is what it got yesterday: the
    pre-lake policy store, unchanged. That is the deliberate choice of
    carry-forward item A2, and the two alternatives are both worse. Refusing
    outright — what the tree did before this slice — turns every default
    backtest into a 409 the moment the flag flips, because the engine's
    synthesized default ``DataPolicy`` is ``adjusted=True``; a flag flip that
    bricks the primary surface is an outage, not a rollout. Flipping that
    default to raw instead would silently change the numbers every existing
    caller gets, which is the exact silent swap this design refuses
    everywhere else. Serving the mode the lake actually holds, and leaving
    the other where it already worked, is the only option that neither breaks
    a caller nor lies to one.
    """
    return bool(settings.DATA_LAKE_ENABLED) and lake_holds_adjustment_mode(adjusted=adjusted)


def resolve_lake_root() -> Path:
    """Return the immutable-artifact root of the data lake.

    This is the directory the LEAN readers are pointed at when
    ``DATA_LAKE_ENABLED`` is on. It is not created here — a missing root
    means "the lake holds nothing yet", which every reader must already
    handle as a per-day miss.

    Catalog rows are root-relative: ``FilePath`` carries no root identity of
    its own, so every lake writer must resolve the root here — a writer using
    a different root produces "phantom coverage": rows that look complete in
    the catalog but have no bytes where anything else looks. The full
    root-identity (``data_root_id``) design that would let more than one
    physical root coexist honestly is ledgered for the flag-flip slice
    (#1839); until then there is exactly one canonical root, and this is it.
    """
    return Path(settings.LEAN_DATA_WRITE_ROOT) / _LAKE_DIR


def resolve_staging_root() -> Path:
    """Return the per-attempt staging root that promotes into the lake root.

    Must share a filesystem with :func:`resolve_lake_root` so the promote
    is a rename (see ``app.data_lake.atomic.assert_same_filesystem``).
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
