"""Typed LEAN-path policy.

Sole authority for constructing LEAN on-disk paths. No string concatenation
of LEAN paths is permitted anywhere else in the codebase; a lint test enforces
that the substrings ``equity/usa/``, ``market-hours/``, ``symbol-properties/``
appear only in this module and its tests.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 5.3
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import UUID

Market = Literal["usa"]
Resolution = Literal["minute", "hour", "daily"]
DataType = Literal["trade", "quote"]
MetadataKind = Literal["market_hours", "symbol_properties"]


def resolve_lake_root() -> Path:
    """The single canonical write root every lake writer must share.

    Wraps ``app.config.settings.LEAN_DATA_WRITE_ROOT`` so any writer that
    needs "the" configured root -- ``ensure_data``'s live fetch pipeline,
    and ``app.data_lake.cache_import``'s canonical-root check -- reads it
    from one place rather than each computing ``Path(settings...)`` itself.

    Catalog rows are root-relative: ``FilePath`` carries no root identity of
    its own. A writer that used a *different* root would produce rows
    ``ensure_data`` can never actually find once it resolves coverage under
    the real configured root -- "phantom coverage" that looks complete in
    the catalog but has no bytes at the root anything else looks under. The
    full root-identity (``data_root_id``) design that would let more than
    one physical root coexist honestly is ledgered for the flag-flip
    integration slice; until then there is exactly one canonical root, and
    this is it.
    """
    from app.config import settings  # lazy: keep this module import-time dependency-free

    return Path(settings.LEAN_DATA_WRITE_ROOT)


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
