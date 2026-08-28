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
from pathlib import PurePosixPath
from typing import Literal
from uuid import UUID

Market = Literal["usa"]
Resolution = Literal["minute", "hour", "daily"]
DataType = Literal["trade", "quote"]
MetadataKind = Literal["market_hours", "symbol_properties"]


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
