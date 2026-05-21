"""Postgres catalog client — asyncpg with parameterized SQL.

Schema-write path: Slice 1b. This module in Slice 1a is read-only:
just a connection pool and a coverage SELECT.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.4
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date

import asyncpg

from app.config import settings
from app.data_lake.types import ArtifactRecord

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def init_pool() -> None:
    """Create the global asyncpg pool. Idempotent."""
    global _pool
    if _pool is not None:
        return
    if not settings.POSTGRES_URL:
        raise RuntimeError(
            "POSTGRES_URL is empty; cannot initialize catalog_client. "
            "Set the env var or disable the data lake (DATA_LAKE_ENABLED=False)."
        )
    _pool = await asyncpg.create_pool(
        settings.POSTGRES_URL,
        min_size=1,
        max_size=10,
        command_timeout=30,
    )
    logger.info("data_lake.catalog_client: asyncpg pool initialized")


async def close_pool() -> None:
    """Close the global asyncpg pool. Idempotent."""
    global _pool
    if _pool is None:
        return
    await _pool.close()
    _pool = None
    logger.info("data_lake.catalog_client: asyncpg pool closed")


@asynccontextmanager
async def connection():  # type: ignore[return]
    """Yield a connection from the pool. Pool must be initialized first."""
    if _pool is None:
        raise RuntimeError("asyncpg pool not initialized; call init_pool() first")
    async with _pool.acquire() as conn:
        yield conn


async def select_coverage_minute_bars(
    market: str,
    symbol: str,
    data_type: str,
    start_trading_date: date,
    end_trading_date: date,
) -> list[ArtifactRecord]:
    """Return all complete minute-bar artifacts for the given window.

    Used by ensure_data to compute which dates already exist on disk before
    deciding what to fetch. In Slice 1a there are no rows; this returns an
    empty list and exercises the schema/query end-to-end.
    """
    query = """
        SELECT "Id", "ArtifactKind", "Market", "Symbol", "TradingDate",
               "Resolution", "DataType", "Provider", "PriceAdjustmentMode",
               "DataContractHash", "FilePath",
               COALESCE("FileSha256", '') AS file_sha256,
               "RowCount", "FirstBarStartMs", "LastBarStartMs"
          FROM "DataLakeArtifacts"
         WHERE "ArtifactKind" = 'time_series_bars'
           AND "Resolution" = 'minute'
           AND "Market" = $1
           AND "Symbol" = $2
           AND "DataType" = $3
           AND "TradingDate" BETWEEN $4 AND $5
           AND "Status" = 'complete'
         ORDER BY "TradingDate"
    """
    async with connection() as conn:
        rows = await conn.fetch(query, market, symbol, data_type, start_trading_date, end_trading_date)
    return [
        ArtifactRecord(
            id=r["Id"],
            artifact_kind=r["ArtifactKind"],
            market=r["Market"],
            symbol=r["Symbol"],
            trading_date=r["TradingDate"],
            resolution=r["Resolution"],
            data_type=r["DataType"],
            provider=r["Provider"],
            price_adjustment_mode=r["PriceAdjustmentMode"],
            data_contract_hash=r["DataContractHash"],
            file_path=r["FilePath"],
            file_sha256=r["file_sha256"],
            row_count=r["RowCount"],
            first_bar_start_ms=r["FirstBarStartMs"],
            last_bar_start_ms=r["LastBarStartMs"],
        )
        for r in rows
    ]
