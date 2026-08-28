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

from app.config import settings

Market = Literal["usa"]
Resolution = Literal["minute", "hour", "daily"]
DataType = Literal["trade", "quote"]
MetadataKind = Literal["market_hours", "symbol_properties"]


def lake_root() -> Path:
    """Absolute root of the immutable LEAN-format lake tree.

    Every relative path built by this module is resolved against this root.
    Readers (the LEAN readers in ``app.engine.data.lean_format``) and the
    writer (``ensure_data``) must agree byte-for-byte on where the tree
    lives, so both resolve it here rather than re-deriving it from
    ``LEAN_DATA_WRITE_ROOT``.
    """
    return Path(settings.LEAN_DATA_WRITE_ROOT) / "lake"


def staging_root() -> Path:
    """Absolute root of the per-attempt staging tree.

    Must sit on the same filesystem as :func:`lake_root` so promoting a
    staged file into the lake is a POSIX atomic ``rename(2)``.
    """
    return Path(settings.LEAN_DATA_WRITE_ROOT) / "staging"


@dataclass(frozen=True)
class LeanMinuteBarPath:
    market: Market
    symbol: str
    trading_date: date
    data_type: DataType

    def relative_path(self) -> PurePosixPath:
        return (
            PurePosixPath("equity")
            / self.market
            / "minute"
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
