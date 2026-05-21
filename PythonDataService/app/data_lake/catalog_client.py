"""Postgres catalog client — asyncpg with parameterized SQL.

Schema-write path: Slice 1b. This module in Slice 1a is read-only:
just a connection pool and a coverage SELECT.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.4
"""

from __future__ import annotations

import contextlib
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date

import asyncpg

from app.config import settings
from app.data_lake.types import ArtifactIdentity, ArtifactRecord

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
    """Close the global asyncpg pool. Idempotent.

    If the pool's event loop is already closed (e.g., a stale pool from a
    prior test's event loop), fall back to terminate() so the global is
    always reset to None.
    """
    global _pool
    if _pool is None:
        return
    try:
        await _pool.close()
    except RuntimeError:
        # Pool's event loop is closed (common in test teardown when multiple
        # async tests share the module-level pool global across event loops).
        # Force-terminate without awaiting to clear the global.
        with contextlib.suppress(Exception):
            _pool.terminate()
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


# ---------------------------------------------------------------------------
# Slice 1b write operations
# ---------------------------------------------------------------------------


async def claim_minute_bar(
    identity: ArtifactIdentity,
    worker_id: str,
    lease_ttl_ms: int,
    data_contract_hash: str,
    file_path: str,
) -> int | None:
    """Atomic claim for a minute-resolution time_series_bars artifact.

    Returns the new row's Id when this caller is the winner; returns None when
    a row already exists for this identity tuple (someone else has it).

    Matches the partial unique index uq_data_lake_artifacts_minute_bars:
      (Market, Symbol, TradingDate, DataType, Provider, PriceAdjustmentMode)
       WHERE ArtifactKind='time_series_bars' AND Resolution='minute'
    The ON CONFLICT clause repeats the partial index's WHERE predicate, per
    Postgres' requirement for partial-index conflict targets.
    """
    if identity.artifact_kind != "time_series_bars" or identity.resolution != "minute":
        raise ValueError(f"claim_minute_bar called with non-minute-bar identity: {identity!r}")
    now_ms = int(time.time() * 1000)
    query = """
        INSERT INTO "DataLakeArtifacts" (
            "ArtifactKind", "Market", "Symbol", "TradingDate",
            "Resolution", "DataType", "Provider", "ProviderParams",
            "PriceAdjustmentMode", "DataContractHash",
            "FilePath", "Status", "LeaseOwner", "LeaseExpiresAtMs",
            "AttemptCount", "FetchedAtMs"
        )
        VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
            $11, 'fetching', $12, $13, 1, $14
        )
        ON CONFLICT ("Market", "Symbol", "TradingDate", "DataType",
                     "Provider", "PriceAdjustmentMode")
            WHERE "ArtifactKind" = 'time_series_bars' AND "Resolution" = 'minute'
        DO NOTHING
        RETURNING "Id";
    """
    async with connection() as conn:
        return await conn.fetchval(
            query,
            identity.artifact_kind,
            identity.market,
            identity.symbol,
            identity.trading_date,
            identity.resolution,
            identity.data_type,
            identity.provider,
            "{}",  # ProviderParams (jsonb; populated by fetcher in 1c)
            identity.price_adjustment_mode,
            data_contract_hash,
            file_path,
            worker_id,
            now_ms + lease_ttl_ms,
            now_ms,
        )


async def complete_artifact(
    artifact_id: int,
    row_count: int,
    first_bar_start_ms: int,
    last_bar_start_ms: int,
    file_size_bytes: int,
    file_sha256: str,
) -> None:
    """Transition an artifact from 'fetching' → 'complete' with byte metadata.

    No-op if the row is not currently 'fetching' (defensive against stale
    callers; the sweep is the only legitimate source of late writes).
    """
    now_ms = int(time.time() * 1000)
    query = """
        UPDATE "DataLakeArtifacts"
           SET "Status" = 'complete',
               "RowCount" = $2,
               "FirstBarStartMs" = $3,
               "LastBarStartMs" = $4,
               "FileSizeBytes" = $5,
               "FileSha256" = $6,
               "CompletedAtMs" = $7,
               "LeaseOwner" = NULL,
               "LeaseExpiresAtMs" = NULL
         WHERE "Id" = $1
           AND "Status" = 'fetching';
    """
    async with connection() as conn:
        await conn.execute(
            query,
            artifact_id,
            row_count,
            first_bar_start_ms,
            last_bar_start_ms,
            file_size_bytes,
            file_sha256,
            now_ms,
        )


async def fail_artifact(
    artifact_id: int,
    last_error: str,
    error_message: str | None = None,
) -> None:
    """Transition an artifact to 'failed' with diagnostic info.

    The row stays in the catalog as an audit record; future ensure_data calls
    may retry it via steal_or_retry_minute_bar (Task 7).
    """
    query = """
        UPDATE "DataLakeArtifacts"
           SET "Status" = 'failed',
               "LastError" = $2,
               "ErrorMessage" = $3,
               "LeaseOwner" = NULL,
               "LeaseExpiresAtMs" = NULL
         WHERE "Id" = $1;
    """
    async with connection() as conn:
        await conn.execute(query, artifact_id, last_error, error_message)


async def refresh_lease(
    artifact_id: int,
    worker_id: str,
    lease_ttl_ms: int,
) -> bool:
    """Heartbeat: extend a lease as long as the calling worker still owns it.

    Returns True when the lease was updated; False when worker_id no longer
    matches LeaseOwner (the lease may have been stolen by the sweep).
    """
    now_ms = int(time.time() * 1000)
    query = """
        UPDATE "DataLakeArtifacts"
           SET "LeaseExpiresAtMs" = $3
         WHERE "Id" = $1
           AND "LeaseOwner" = $2
           AND "Status" = 'fetching';
    """
    async with connection() as conn:
        result = await conn.execute(query, artifact_id, worker_id, now_ms + lease_ttl_ms)
    # asyncpg returns "UPDATE n" — parse the row count.
    n = int(result.rsplit(" ", 1)[-1])
    return n > 0


async def steal_or_retry_minute_bar(
    artifact_id: int,
    worker_id: str,
    lease_ttl_ms: int,
    max_retries: int,
) -> bool:
    """Reclaim an artifact whose lease expired OR retry a failed artifact.

    Eligibility:
      - Status='fetching' AND LeaseExpiresAtMs < now_ms  (lease expired), OR
      - Status='failed' AND AttemptCount < max_retries  (retryable failure)

    Returns True when the row was updated to 'fetching' under the new worker;
    False when no eligible row exists (e.g., already complete, already
    re-claimed by another worker, or failed beyond max_retries).
    """
    now_ms = int(time.time() * 1000)
    query = """
        UPDATE "DataLakeArtifacts"
           SET "Status" = 'fetching',
               "LeaseOwner" = $2,
               "LeaseExpiresAtMs" = $3,
               "AttemptCount" = "AttemptCount" + 1,
               "LastError" = NULL
         WHERE "Id" = $1
           AND (
                  ("Status" = 'fetching' AND "LeaseExpiresAtMs" < $4)
               OR ("Status" = 'failed' AND "AttemptCount" < $5)
           );
    """
    async with connection() as conn:
        result = await conn.execute(
            query,
            artifact_id,
            worker_id,
            now_ms + lease_ttl_ms,
            now_ms,
            max_retries,
        )
    n = int(result.rsplit(" ", 1)[-1])
    return n > 0


@dataclass(frozen=True)
class PriorArtifactMetadata:
    prior_file_path: str
    prior_file_sha256: str


async def claim_corp_action_artifact(
    identity: ArtifactIdentity,
    worker_id: str,
    lease_ttl_ms: int,
    data_contract_hash: str,
    file_path: str,
) -> int | None:
    """Atomic claim for a factor_file or map_file artifact.

    Returns the new row's Id when this caller is the winner; returns None when
    a row already exists for this identity tuple (someone else has it).

    Matches the partial unique index uq_data_lake_artifacts_corp_actions:
      (Market, Symbol, ArtifactKind, Provider, PriceAdjustmentMode)
       WHERE ArtifactKind IN ('factor_file','map_file')
    The ON CONFLICT clause repeats the partial index's WHERE predicate, per
    Postgres' requirement for partial-index conflict targets.
    """
    if identity.artifact_kind not in ("factor_file", "map_file"):
        raise ValueError(f"claim_corp_action_artifact called with non-corp-action identity: {identity!r}")
    now_ms = int(time.time() * 1000)
    query = """
        INSERT INTO "DataLakeArtifacts" (
            "ArtifactKind", "Market", "Symbol", "TradingDate",
            "Resolution", "DataType", "Provider", "ProviderParams",
            "PriceAdjustmentMode", "DataContractHash",
            "FilePath", "Status", "LeaseOwner", "LeaseExpiresAtMs",
            "AttemptCount", "FetchedAtMs"
        )
        VALUES (
            $1, $2, $3, NULL, NULL, NULL, $4, $5, $6, $7,
            $8, 'fetching', $9, $10, 1, $11
        )
        ON CONFLICT ("Market", "Symbol", "ArtifactKind", "Provider", "PriceAdjustmentMode")
            WHERE "ArtifactKind" IN ('factor_file', 'map_file')
        DO NOTHING
        RETURNING "Id";
    """
    async with connection() as conn:
        return await conn.fetchval(
            query,
            identity.artifact_kind,
            identity.market,
            identity.symbol,
            identity.provider,
            "{}",  # ProviderParams (jsonb)
            identity.price_adjustment_mode,
            data_contract_hash,
            file_path,
            worker_id,
            now_ms + lease_ttl_ms,
            now_ms,
        )


async def claim_metadata_artifact(
    identity: ArtifactIdentity,
    worker_id: str,
    lease_ttl_ms: int,
    data_contract_hash: str,
    file_path: str,
) -> int | None:
    """Atomic claim for a metadata artifact.

    Returns the new row's Id when this caller is the winner; returns None when
    a row already exists for this data_contract_hash (someone else has it).

    Matches the partial unique index uq_data_lake_artifacts_metadata:
      (DataContractHash)
       WHERE ArtifactKind = 'metadata'
    The ON CONFLICT clause repeats the partial index's WHERE predicate, per
    Postgres' requirement for partial-index conflict targets.
    """
    if identity.artifact_kind != "metadata":
        raise ValueError(f"claim_metadata_artifact called with non-metadata identity: {identity!r}")
    now_ms = int(time.time() * 1000)
    query = """
        INSERT INTO "DataLakeArtifacts" (
            "ArtifactKind", "Market", "Symbol", "TradingDate",
            "Resolution", "DataType", "Provider", "ProviderParams",
            "PriceAdjustmentMode", "DataContractHash",
            "FilePath", "Status", "LeaseOwner", "LeaseExpiresAtMs",
            "AttemptCount", "FetchedAtMs"
        )
        VALUES (
            'metadata', $1, $2, NULL, NULL, NULL, $3, $4, NULL, $5,
            $6, 'fetching', $7, $8, 1, $9
        )
        ON CONFLICT ("DataContractHash")
            WHERE "ArtifactKind" = 'metadata'
        DO NOTHING
        RETURNING "Id";
    """
    async with connection() as conn:
        return await conn.fetchval(
            query,
            identity.market,
            identity.symbol,
            identity.provider,
            "{}",  # ProviderParams (jsonb)
            data_contract_hash,
            file_path,
            worker_id,
            now_ms + lease_ttl_ms,
            now_ms,
        )


async def claim_aggregated_bar_artifact(
    identity: ArtifactIdentity,
    worker_id: str,
    lease_ttl_ms: int,
    data_contract_hash: str,
    file_path: str,
) -> int | None:
    """Atomic claim for a hour- or daily-resolution time_series_bars artifact.

    Returns the new row's Id when this caller is the winner; returns None when
    a row already exists for this identity tuple (someone else has it).

    Matches the partial unique index uq_data_lake_artifacts_aggregated_bars:
      (Market, Symbol, Resolution, DataType, Provider, PriceAdjustmentMode)
       WHERE ArtifactKind = 'time_series_bars' AND Resolution IN ('hour','daily')
    The ON CONFLICT clause repeats the partial index's WHERE predicate, per
    Postgres' requirement for partial-index conflict targets.
    """
    if identity.artifact_kind != "time_series_bars" or identity.resolution not in ("hour", "daily"):
        raise ValueError(f"claim_aggregated_bar_artifact called with non-aggregated-bar identity: {identity!r}")
    now_ms = int(time.time() * 1000)
    query = """
        INSERT INTO "DataLakeArtifacts" (
            "ArtifactKind", "Market", "Symbol", "TradingDate",
            "Resolution", "DataType", "Provider", "ProviderParams",
            "PriceAdjustmentMode", "DataContractHash",
            "FilePath", "Status", "LeaseOwner", "LeaseExpiresAtMs",
            "AttemptCount", "FetchedAtMs"
        )
        VALUES (
            $1, $2, $3, NULL, $4, $5, $6, $7, $8, $9,
            $10, 'fetching', $11, $12, 1, $13
        )
        ON CONFLICT ("Market", "Symbol", "Resolution", "DataType",
                     "Provider", "PriceAdjustmentMode")
            WHERE "ArtifactKind" = 'time_series_bars'
              AND "Resolution" IN ('hour', 'daily')
        DO NOTHING
        RETURNING "Id";
    """
    async with connection() as conn:
        return await conn.fetchval(
            query,
            identity.artifact_kind,
            identity.market,
            identity.symbol,
            identity.resolution,
            identity.data_type,
            identity.provider,
            "{}",  # ProviderParams (jsonb)
            identity.price_adjustment_mode,
            data_contract_hash,
            file_path,
            worker_id,
            now_ms + lease_ttl_ms,
            now_ms,
        )


async def refresh_complete_minute_bar(
    artifact_id: int,
    worker_id: str,
    lease_ttl_ms: int,
) -> PriorArtifactMetadata | None:
    """Force-refresh transition: 'complete' → 'fetching' for a re-fetch.

    Returns the prior file_path + file_sha256 so the caller can preserve them
    if the new fetch fails validation. Returns None when the row isn't
    currently 'complete' (refresh has no work to do).
    """
    now_ms = int(time.time() * 1000)
    query = """
        UPDATE "DataLakeArtifacts"
           SET "Status" = 'fetching',
               "LeaseOwner" = $2,
               "LeaseExpiresAtMs" = $3,
               "AttemptCount" = "AttemptCount" + 1
         WHERE "Id" = $1
           AND "Status" = 'complete'
        RETURNING "FilePath", "FileSha256";
    """
    async with connection() as conn:
        row = await conn.fetchrow(
            query,
            artifact_id,
            worker_id,
            now_ms + lease_ttl_ms,
        )
    if row is None:
        return None
    return PriorArtifactMetadata(
        prior_file_path=row["FilePath"],
        prior_file_sha256=row["FileSha256"],
    )
