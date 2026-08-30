"""Postgres catalog client — asyncpg with parameterized SQL.

Schema-write path: Slice 1b. This module in Slice 1a is read-only:
just a connection pool and a coverage SELECT.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.4
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal
from uuid import UUID

import asyncpg

from app.config import settings
from app.data_lake import root_identity
from app.data_lake.types import (
    ArtifactDetail,
    ArtifactIdentity,
    ArtifactRecord,
    StorageKindTotal,
    SymbolCoverageSpan,
)
from app.lean_sidecar.trading_calendar import session_open_ms_utc

logger = logging.getLogger(__name__)

# Keyed by the running event loop, not held as one process-global pool: an
# asyncpg pool is bound to the loop that created it, and this module has
# two independent callers that each own a loop of their own — the FastAPI
# app loop serving /api/data-lake/* requests, and the Python engine's
# dedicated materialization loop (app.data_lake.run_materialization's
# _materialization_loop, for backtests running on worker threads with no
# loop of their own). A single global pool binds to whichever of them
# calls init_pool() first; the other then hits asyncpg's cross-loop error
# on every subsequent call. Weak-keyed so a loop that is garbage collected
# without an explicit close_pool() call (routine in tests, where pytest-
# asyncio hands out a fresh loop per test function) does not pin its pool
# here forever.
_pools: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncpg.Pool] = weakref.WeakKeyDictionary()


async def init_pool() -> None:
    """Create the asyncpg pool for the calling loop. Idempotent per loop."""
    loop = asyncio.get_running_loop()
    if loop in _pools:
        return
    if not settings.POSTGRES_URL:
        raise RuntimeError(
            "POSTGRES_URL is empty; cannot initialize catalog_client. "
            "Set the env var or disable the data lake (DATA_LAKE_ENABLED=False)."
        )
    _pools[loop] = await asyncpg.create_pool(
        settings.POSTGRES_URL,
        min_size=1,
        max_size=10,
        command_timeout=30,
    )
    logger.info("data_lake.catalog_client: asyncpg pool initialized for loop %s", id(loop))


async def close_pool() -> None:
    """Close the calling loop's asyncpg pool. Idempotent per loop.

    Only ever touches the pool bound to the loop this coroutine is running
    on — it cannot reach into another loop's pool. A caller that wants a
    clean slate for a loop it does not control (uncommon; today only test
    teardown ever calls this) gets no signal that another loop still holds
    one, which is the same trade-off two independent pools always carry.
    """
    loop = asyncio.get_running_loop()
    pool = _pools.pop(loop, None)
    if pool is None:
        return
    try:
        await pool.close()
    except RuntimeError:
        # Defensive: closing a pool from the loop that created it should
        # always succeed, but fall back to terminate() rather than leave a
        # half-torn-down pool if asyncpg ever disagrees.
        with contextlib.suppress(Exception):
            pool.terminate()
    logger.info("data_lake.catalog_client: asyncpg pool closed for loop %s", id(loop))


@asynccontextmanager
async def connection():  # type: ignore[return]
    """Yield a connection from the calling loop's pool. Pool must be initialized first."""
    loop = asyncio.get_running_loop()
    pool = _pools.get(loop)
    if pool is None:
        raise RuntimeError("asyncpg pool not initialized; call init_pool() first")
    async with pool.acquire() as conn:
        yield conn


def _resolve_data_root_id(data_root_id: UUID | None) -> UUID:
    """Default a caller-omitted ``data_root_id`` to the service's configured
    active root (issue #1876's "active-root defaults" acceptance bullet:
    coverage, storage summaries, ensure-data, backfill, and observatory
    listings default to it). Every read/write below that isn't keyed by an
    ``ArtifactIdentity`` (which carries its own default, see types.py) takes
    ``data_root_id`` this way rather than requiring every existing caller to
    pass it explicitly."""
    return data_root_id if data_root_id is not None else root_identity.active_root_id()


async def select_complete_metadata_artifact(
    data_contract_hash: str,
    data_root_id: UUID | None = None,
) -> ArtifactRecord | None:
    """Return a complete metadata artifact row by data_contract_hash, or None.

    Used by ensure_data Phase 0 bootstrap when claim_metadata_artifact returns
    None (conflict — already exists). Returns the existing complete row so the
    caller can reuse it without calling the launcher again.

    ``data_root_id`` defaults to the service's configured active root
    (issue #1876) — every identity lookup is root-scoped so a metadata row
    minted for a different physical root can never be picked here.
    """
    root_id = _resolve_data_root_id(data_root_id)
    query = """
        SELECT "Id", "ArtifactKind", "Market", "Symbol", "TradingDate",
               "Resolution", "DataType", "Provider", "PriceAdjustmentMode",
               "DataContractHash", "FilePath",
               COALESCE("FileSha256", '') AS file_sha256,
               "RowCount", "FirstBarStartMs", "LastBarStartMs", "FileSizeBytes",
               "DataRootId"
          FROM "DataLakeArtifacts"
         WHERE "ArtifactKind" = 'metadata'
           AND "DataContractHash" = $1
           AND "DataRootId" = $2
           AND "Status" = 'complete'
         LIMIT 1
    """
    async with connection() as conn:
        row = await conn.fetchrow(query, data_contract_hash, root_id)
    if row is None:
        return None
    return ArtifactRecord(
        id=row["Id"],
        artifact_kind=row["ArtifactKind"],
        market=row["Market"],
        symbol=row["Symbol"],
        trading_date=row["TradingDate"],
        resolution=row["Resolution"],
        data_type=row["DataType"],
        provider=row["Provider"],
        price_adjustment_mode=row["PriceAdjustmentMode"],
        data_contract_hash=row["DataContractHash"],
        file_path=row["FilePath"],
        file_sha256=row["file_sha256"],
        row_count=row["RowCount"],
        first_bar_start_ms=row["FirstBarStartMs"],
        last_bar_start_ms=row["LastBarStartMs"],
        file_size_bytes=row["FileSizeBytes"],
        data_root_id=row["DataRootId"],
    )


async def select_complete_corp_action_artifact(
    identity: ArtifactIdentity,
) -> ArtifactRecord | None:
    """Return a complete factor_file or map_file artifact row, or None.

    Used by ensure_data Pass 1 when claim_corp_action_artifact returns None
    (conflict — already exists). Returns the existing complete row.

    Root-scoped by ``identity.data_root_id`` (issue #1876): full artifact
    identity is data_root_id + price_adjustment_mode + the dimensions below,
    so a corp-action row minted for a different physical root can never be
    picked here.
    """
    query = """
        SELECT "Id", "ArtifactKind", "Market", "Symbol", "TradingDate",
               "Resolution", "DataType", "Provider", "PriceAdjustmentMode",
               "DataContractHash", "FilePath",
               COALESCE("FileSha256", '') AS file_sha256,
               "RowCount", "FirstBarStartMs", "LastBarStartMs", "FileSizeBytes",
               "DataRootId"
          FROM "DataLakeArtifacts"
         WHERE "ArtifactKind" = $1
           AND "Market" = $2
           AND "Symbol" = $3
           AND "Provider" = $4
           AND "PriceAdjustmentMode" = $5
           AND "DataRootId" = $6
           AND "Status" = 'complete'
         LIMIT 1
    """
    async with connection() as conn:
        row = await conn.fetchrow(
            query,
            identity.artifact_kind,
            identity.market,
            identity.symbol,
            identity.provider,
            identity.price_adjustment_mode,
            identity.data_root_id,
        )
    if row is None:
        return None
    return ArtifactRecord(
        id=row["Id"],
        artifact_kind=row["ArtifactKind"],
        market=row["Market"],
        symbol=row["Symbol"],
        trading_date=row["TradingDate"],
        resolution=row["Resolution"],
        data_type=row["DataType"],
        provider=row["Provider"],
        price_adjustment_mode=row["PriceAdjustmentMode"],
        data_contract_hash=row["DataContractHash"],
        file_path=row["FilePath"],
        file_sha256=row["file_sha256"],
        row_count=row["RowCount"],
        first_bar_start_ms=row["FirstBarStartMs"],
        last_bar_start_ms=row["LastBarStartMs"],
        file_size_bytes=row["FileSizeBytes"],
        data_root_id=row["DataRootId"],
    )


async def select_complete_aggregated_bar_artifact(
    identity: ArtifactIdentity,
) -> ArtifactRecord | None:
    """Return a complete daily time_series_bars artifact row, or None.

    Used by ensure_data Pass 2 when claim_aggregated_bar_artifact returns None
    (conflict — already exists). Returns the existing complete row.

    Root-scoped by ``identity.data_root_id`` (issue #1876) — see
    select_complete_corp_action_artifact's docstring above.
    """
    query = """
        SELECT "Id", "ArtifactKind", "Market", "Symbol", "TradingDate",
               "Resolution", "DataType", "Provider", "PriceAdjustmentMode",
               "DataContractHash", "FilePath",
               COALESCE("FileSha256", '') AS file_sha256,
               "RowCount", "FirstBarStartMs", "LastBarStartMs", "FileSizeBytes",
               "DataRootId"
          FROM "DataLakeArtifacts"
         WHERE "ArtifactKind" = 'time_series_bars'
           AND "Resolution" = $1
           AND "Market" = $2
           AND "Symbol" = $3
           AND "DataType" = $4
           AND "Provider" = $5
           AND "PriceAdjustmentMode" = $6
           AND "DataRootId" = $7
           AND "Status" = 'complete'
         LIMIT 1
    """
    async with connection() as conn:
        row = await conn.fetchrow(
            query,
            identity.resolution,
            identity.market,
            identity.symbol,
            identity.data_type,
            identity.provider,
            identity.price_adjustment_mode,
            identity.data_root_id,
        )
    if row is None:
        return None
    return ArtifactRecord(
        id=row["Id"],
        artifact_kind=row["ArtifactKind"],
        market=row["Market"],
        symbol=row["Symbol"],
        trading_date=row["TradingDate"],
        resolution=row["Resolution"],
        data_type=row["DataType"],
        provider=row["Provider"],
        price_adjustment_mode=row["PriceAdjustmentMode"],
        data_contract_hash=row["DataContractHash"],
        file_path=row["FilePath"],
        file_sha256=row["file_sha256"],
        row_count=row["RowCount"],
        first_bar_start_ms=row["FirstBarStartMs"],
        last_bar_start_ms=row["LastBarStartMs"],
        file_size_bytes=row["FileSizeBytes"],
        data_root_id=row["DataRootId"],
    )


async def select_coverage_minute_bars(
    market: str,
    symbol: str,
    data_type: str,
    start_trading_date: date | None,
    end_trading_date: date | None,
    *,
    price_adjustment_mode: str,
    data_root_id: UUID | None = None,
) -> list[ArtifactRecord]:
    """Return all complete minute-bar artifacts for the given window and
    adjustment mode.

    Used by ensure_data to compute which dates already exist on disk before
    deciding what to fetch. In Slice 1a there are no rows; this returns an
    empty list and exercises the schema/query end-to-end.

    ``start_trading_date``/``end_trading_date`` may both be ``None`` for an
    unbounded, symbol-wide query — the daily-trade rollup's source-of-truth
    read (see ``ensure_data._process_daily_trade_artifact``), which derives
    from every complete minute-trade artifact the catalog holds for the
    symbol, not just the window one ``ensure_data`` call happened to request.
    A caller passes both bounds or neither; there is no legitimate
    half-open case.

    ``price_adjustment_mode`` is required (keyword-only): once more than one
    mode can exist for the same (market, symbol, date, data_type) --
    true since app.data_lake.cache_import can catalog a
    'polygon_split_adjusted' row alongside ensure_data's 'raw' rows -- an
    unscoped query would pick an arbitrary row for the caller's identity.
    Every call site in the repo already passes this explicitly.

    ``data_root_id`` defaults to the service's configured active root
    (issue #1876) for the same reason — a coexisting row from a different
    physical root must never be picked here either.
    """
    root_id = _resolve_data_root_id(data_root_id)
    query = """
        SELECT "Id", "ArtifactKind", "Market", "Symbol", "TradingDate",
               "Resolution", "DataType", "Provider", "PriceAdjustmentMode",
               "DataContractHash", "FilePath",
               COALESCE("FileSha256", '') AS file_sha256,
               "RowCount", "FirstBarStartMs", "LastBarStartMs", "FileSizeBytes",
               "DataRootId"
          FROM "DataLakeArtifacts"
         WHERE "ArtifactKind" = 'time_series_bars'
           AND "Resolution" = 'minute'
           AND "Market" = $1
           AND "Symbol" = $2
           AND "DataType" = $3
           AND ($4::date IS NULL OR "TradingDate" >= $4)
           AND ($5::date IS NULL OR "TradingDate" <= $5)
           AND "PriceAdjustmentMode" = $6
           AND "DataRootId" = $7
           AND "Status" = 'complete'
         ORDER BY "TradingDate"
    """
    async with connection() as conn:
        rows = await conn.fetch(
            query, market, symbol, data_type, start_trading_date, end_trading_date, price_adjustment_mode, root_id
        )
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
            file_size_bytes=r["FileSizeBytes"],
            data_root_id=r["DataRootId"],
        )
        for r in rows
    ]


@dataclass(frozen=True)
class MinuteBarLeaseStatus:
    """Current row state for one minute-bar claim, independent of Status.

    ``select_coverage_minute_bars`` above only ever answers "is it
    complete?" — the caller can't tell an in-flight claim from one that
    has already permanently failed. This lets a caller (the backfill
    job's lease-wait loop, #1836) distinguish the two without re-running
    ensure_data's whole per-day pipeline on every poll.
    """

    status: Literal["fetching", "complete", "failed"]
    lease_expires_at_ms: int | None
    last_error: str | None
    error_message: str | None


async def select_minute_bar_lease_status(identity: ArtifactIdentity) -> MinuteBarLeaseStatus | None:
    """Return the current row state for one minute-bar identity, or None
    if no row exists for it yet. A single indexed SELECT — no join, no
    aggregation; cheap to poll.

    Root-scoped by ``identity.data_root_id`` (issue #1876).
    """
    if identity.artifact_kind != "time_series_bars" or identity.resolution != "minute":
        raise ValueError(f"select_minute_bar_lease_status called with non-minute-bar identity: {identity!r}")
    query = """
        SELECT "Status", "LeaseExpiresAtMs", "LastError", "ErrorMessage"
          FROM "DataLakeArtifacts"
         WHERE "ArtifactKind" = 'time_series_bars'
           AND "Resolution" = 'minute'
           AND "Market" = $1
           AND "Symbol" = $2
           AND "TradingDate" = $3
           AND "DataType" = $4
           AND "Provider" = $5
           AND "PriceAdjustmentMode" = $6
           AND "DataRootId" = $7
         LIMIT 1
    """
    async with connection() as conn:
        row = await conn.fetchrow(
            query,
            identity.market,
            identity.symbol,
            identity.trading_date,
            identity.data_type,
            identity.provider,
            identity.price_adjustment_mode,
            identity.data_root_id,
        )
    if row is None:
        return None
    return MinuteBarLeaseStatus(
        status=row["Status"],
        lease_expires_at_ms=row["LeaseExpiresAtMs"],
        last_error=row["LastError"],
        error_message=row["ErrorMessage"],
    )


# ---------------------------------------------------------------------------
# Slice 1b write operations
# ---------------------------------------------------------------------------


class CatalogSchemaNotReadyError(RuntimeError):
    """A claim_* INSERT's ON CONFLICT target matches no constraint or index
    in the connected database.

    Wraps Postgres SQLSTATE 42P10 (``invalid_column_reference`` --
    asyncpg's ``InvalidColumnReferenceError``), which Postgres raises when an
    ``ON CONFLICT (...) WHERE ...`` clause names a column set that no unique
    or exclusion index currently matches. In practice this almost always
    means the database schema doesn't yet match what this code expects: a
    mid-deploy race where python-service started serving traffic before
    Backend's EF Core migration finished applying. compose.yaml health-gates
    Backend on python-service, not the other way around, and Backend applies
    migrations during its own startup -- so there is a real window, on every
    deploy that ships a migration touching one of these partial unique
    indexes (e.g. 20260830120000_ActivateDataRootScopedCatalogIdentity),
    where python-service is already reachable but the index a claim_*
    function's ON CONFLICT target names does not exist yet. Safe to retry
    after a short delay once the migration completes; not safe to retry
    immediately in a tight loop.
    """


async def _claim_fetchval(conn: asyncpg.Connection, query: str, *args: Any) -> int | None:
    """Run one claim_*'s ``INSERT ... ON CONFLICT ... RETURNING "Id"`` and
    translate a conflict-target mismatch (Postgres 42P10) into
    :class:`CatalogSchemaNotReadyError` instead of letting the raw asyncpg
    exception surface as an opaque 500. Any other ``PostgresError`` is
    re-raised unchanged -- this narrowly targets the one mid-deploy race
    described in :class:`CatalogSchemaNotReadyError`'s docstring, not
    Postgres errors in general.
    """
    try:
        return await conn.fetchval(query, *args)
    except asyncpg.PostgresError as exc:
        if exc.sqlstate == "42P10":
            raise CatalogSchemaNotReadyError(
                f"ON CONFLICT target matches no constraint/index (Postgres 42P10): {exc}"
            ) from exc
        raise


async def claim_minute_bar(
    identity: ArtifactIdentity,
    worker_id: str,
    lease_ttl_ms: int,
    data_contract_hash: str,
    file_path: str,
    provider_params: dict[str, Any] | None = None,
) -> int | None:
    """Atomic claim for a minute-resolution time_series_bars artifact.

    Returns the new row's Id when this caller is the winner; returns None when
    a row already exists for this identity tuple (someone else has it).

    Matches the partial unique index uq_data_lake_artifacts_minute_bars:
      (DataRootId, Market, Symbol, TradingDate, DataType, Provider, PriceAdjustmentMode)
       WHERE ArtifactKind='time_series_bars' AND Resolution='minute'
    The ON CONFLICT clause repeats the partial index's WHERE predicate, per
    Postgres' requirement for partial-index conflict targets.

    ``provider_params`` is stored verbatim as the row's ``ProviderParams``
    jsonb column. Defaults to ``{}`` (unchanged behavior for the live-fetch
    pipeline's existing callers); ``app.data_lake.cache_import`` passes the
    original per-symbol provenance so it survives on the row itself as an
    audit trail, rather than being fetched-and-discarded.

    Records ``identity.data_root_id`` on the new row and leads the conflict
    target with it (issue #1878, PR B of #1861): a claim for the same
    symbol/date/mode identity in a *different* root no longer collides with
    this one — that is the regression this redesign exists to prevent.
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
            "AttemptCount", "FetchedAtMs", "DataRootId"
        )
        VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
            $11, 'fetching', $12, $13, 1, $14, $15
        )
        ON CONFLICT ("DataRootId", "Market", "Symbol", "TradingDate", "DataType",
                     "Provider", "PriceAdjustmentMode")
            WHERE "ArtifactKind" = 'time_series_bars' AND "Resolution" = 'minute'
        DO NOTHING
        RETURNING "Id";
    """
    async with connection() as conn:
        return await _claim_fetchval(
            conn,
            query,
            identity.artifact_kind,
            identity.market,
            identity.symbol,
            identity.trading_date,
            identity.resolution,
            identity.data_type,
            identity.provider,
            json.dumps(provider_params or {}),
            identity.price_adjustment_mode,
            data_contract_hash,
            file_path,
            worker_id,
            now_ms + lease_ttl_ms,
            now_ms,
            identity.data_root_id,
        )


@dataclass(frozen=True)
class ArtifactClaimState:
    """The existing row's claim state, for a caller that lost a claim insert.

    A ``claim_*`` function's ``ON CONFLICT ... DO NOTHING`` returns nothing
    when a row already exists, and the matching ``select_complete_*`` /
    ``select_coverage_*`` only sees ``'complete'`` rows — so a caller that
    lost the race to a ``'failed'`` or lease-expired ``'fetching'`` row has
    no way to find its ``Id`` (needed by :func:`steal_or_retry_minute_bar`,
    itself generic over artifact kind) or to tell that case apart from a
    live, actively-leased fetch. This is that lookup's result — shared by
    :func:`select_minute_bar_claim_state` and :func:`select_metadata_claim_state`
    since the shape carries no kind-specific field.
    """

    id: int
    status: str
    attempt_count: int
    last_error: str | None


async def select_minute_bar_claim_state(identity: ArtifactIdentity) -> ArtifactClaimState | None:
    """Look up the existing minute-bar row's claim state, at any status.

    Matches the same identity tuple as ``claim_minute_bar``'s partial unique
    index, plus ``identity.data_root_id`` (issue #1876). Returns None only
    if the row was deleted between the failed claim and this lookup (not
    expected in practice — rows are never deleted).
    """
    query = """
        SELECT "Id", "Status", "AttemptCount", "LastError"
          FROM "DataLakeArtifacts"
         WHERE "ArtifactKind" = 'time_series_bars'
           AND "Resolution" = 'minute'
           AND "Market" = $1
           AND "Symbol" = $2
           AND "TradingDate" = $3
           AND "DataType" = $4
           AND "Provider" = $5
           AND "PriceAdjustmentMode" = $6
           AND "DataRootId" = $7
    """
    async with connection() as conn:
        row = await conn.fetchrow(
            query,
            identity.market,
            identity.symbol,
            identity.trading_date,
            identity.data_type,
            identity.provider,
            identity.price_adjustment_mode,
            identity.data_root_id,
        )
    if row is None:
        return None
    return ArtifactClaimState(
        id=row["Id"],
        status=row["Status"],
        attempt_count=row["AttemptCount"],
        last_error=row["LastError"],
    )


async def select_metadata_claim_state(
    data_contract_hash: str, data_root_id: UUID | None = None
) -> ArtifactClaimState | None:
    """Look up the existing metadata row's claim state, at any status.

    ``claim_metadata_artifact``'s ``ON CONFLICT ... DO NOTHING`` returns
    nothing when a row already exists, and ``select_complete_metadata_artifact``
    only sees ``'complete'`` rows — the same gap ``select_minute_bar_claim_state``
    closes for bars, reused here (the returned shape carries no bar-specific
    field). Without this lookup, a settled ``'failed'`` row — e.g. from a
    launcher outage — reads identically to live contention forever: every
    future bootstrap attempt sees "not complete" and reports lease_timeout,
    even though nothing is actually in flight and :func:`steal_or_retry_minute_bar`
    could reclaim it immediately.

    ``data_root_id`` defaults to the service's configured active root
    (issue #1876).
    """
    root_id = _resolve_data_root_id(data_root_id)
    query = """
        SELECT "Id", "Status", "AttemptCount", "LastError"
          FROM "DataLakeArtifacts"
         WHERE "ArtifactKind" = 'metadata'
           AND "DataContractHash" = $1
           AND "DataRootId" = $2
    """
    async with connection() as conn:
        row = await conn.fetchrow(query, data_contract_hash, root_id)
    if row is None:
        return None
    return ArtifactClaimState(
        id=row["Id"],
        status=row["Status"],
        attempt_count=row["AttemptCount"],
        last_error=row["LastError"],
    )


async def complete_artifact(
    artifact_id: int,
    row_count: int,
    first_bar_start_ms: int,
    last_bar_start_ms: int,
    file_size_bytes: int,
    file_sha256: str,
    data_contract_hash: str | None = None,
) -> None:
    """Transition an artifact from 'fetching' → 'complete' with byte metadata.

    No-op if the row is not currently 'fetching' (defensive against stale
    callers; the sweep is the only legitimate source of late writes).

    ``data_contract_hash`` is optional and defaults to leaving the column
    untouched (``COALESCE`` against the existing value) — the vast majority
    of callers complete a row whose contract hash was fixed at claim time and
    never changes. A rebuild via refresh_complete_artifact is the exception:
    it can complete onto a *different* source set (see
    ``ensure_data._process_daily_trade_artifact``), and must pass the newly
    computed hash here or the stale one persists forever, making every
    subsequent ensure_data call detect a mismatch and rebuild again.
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
               "DataContractHash" = COALESCE($8, "DataContractHash"),
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
            data_contract_hash,
        )


async def restore_complete_artifact(artifact_id: int, worker_id: str) -> bool:
    """Undo a refresh_complete_artifact() that failed before writing anything new.

    refresh_complete_artifact() only touches Status/LeaseOwner/
    LeaseExpiresAtMs/AttemptCount when it transitions 'complete' → 'fetching'
    — RowCount/FileSha256/FileSizeBytes/DataContractHash/FilePath all still
    describe the pre-rebuild artifact. If the rebuild then fails before
    atomic_write_and_promote runs (e.g. a source file read error), the old
    file on disk was never touched either, so restoring Status alone is
    sufficient to put the row back exactly as it was.

    Callers must use this only when the failure happened before any new
    bytes were promoted — a failure after promotion has already replaced the
    file, and fail_artifact() is the correct transition there instead.
    Scoped to the caller's own worker_id so a worker never resurrects a row
    it doesn't hold the lease on. Returns True when the row was restored.
    """
    query = """
        UPDATE "DataLakeArtifacts"
           SET "Status" = 'complete',
               "LeaseOwner" = NULL,
               "LeaseExpiresAtMs" = NULL
         WHERE "Id" = $1
           AND "LeaseOwner" = $2
           AND "Status" = 'fetching';
    """
    async with connection() as conn:
        result = await conn.execute(query, artifact_id, worker_id)
    n = int(result.rsplit(" ", 1)[-1])
    return n > 0


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
      (DataRootId, Market, Symbol, ArtifactKind, Provider, PriceAdjustmentMode)
       WHERE ArtifactKind IN ('factor_file','map_file')
    The ON CONFLICT clause repeats the partial index's WHERE predicate, per
    Postgres' requirement for partial-index conflict targets.

    Records ``identity.data_root_id`` on the new row and leads the conflict
    target with it (issue #1878, PR B of #1861).
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
            "AttemptCount", "FetchedAtMs", "DataRootId"
        )
        VALUES (
            $1, $2, $3, NULL, NULL, NULL, $4, $5, $6, $7,
            $8, 'fetching', $9, $10, 1, $11, $12
        )
        ON CONFLICT ("DataRootId", "Market", "Symbol", "ArtifactKind", "Provider", "PriceAdjustmentMode")
            WHERE "ArtifactKind" IN ('factor_file', 'map_file')
        DO NOTHING
        RETURNING "Id";
    """
    async with connection() as conn:
        return await _claim_fetchval(
            conn,
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
            identity.data_root_id,
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
      (DataRootId, DataContractHash)
       WHERE ArtifactKind = 'metadata'
    The ON CONFLICT clause repeats the partial index's WHERE predicate, per
    Postgres' requirement for partial-index conflict targets.

    Records ``identity.data_root_id`` on the new row and leads the conflict
    target with it (issue #1878, PR B of #1861).
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
            "AttemptCount", "FetchedAtMs", "DataRootId"
        )
        VALUES (
            'metadata', $1, $2, NULL, NULL, NULL, $3, $4, NULL, $5,
            $6, 'fetching', $7, $8, 1, $9, $10
        )
        ON CONFLICT ("DataRootId", "DataContractHash")
            WHERE "ArtifactKind" = 'metadata'
        DO NOTHING
        RETURNING "Id";
    """
    async with connection() as conn:
        return await _claim_fetchval(
            conn,
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
            identity.data_root_id,
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
      (DataRootId, Market, Symbol, Resolution, DataType, Provider, PriceAdjustmentMode)
       WHERE ArtifactKind = 'time_series_bars' AND Resolution IN ('hour','daily')
    The ON CONFLICT clause repeats the partial index's WHERE predicate, per
    Postgres' requirement for partial-index conflict targets.

    Records ``identity.data_root_id`` on the new row and leads the conflict
    target with it (issue #1878, PR B of #1861).
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
            "AttemptCount", "FetchedAtMs", "DataRootId"
        )
        VALUES (
            $1, $2, $3, NULL, $4, $5, $6, $7, $8, $9,
            $10, 'fetching', $11, $12, 1, $13, $14
        )
        ON CONFLICT ("DataRootId", "Market", "Symbol", "Resolution", "DataType",
                     "Provider", "PriceAdjustmentMode")
            WHERE "ArtifactKind" = 'time_series_bars'
              AND "Resolution" IN ('hour', 'daily')
        DO NOTHING
        RETURNING "Id";
    """
    async with connection() as conn:
        return await _claim_fetchval(
            conn,
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
            identity.data_root_id,
        )


async def refresh_complete_artifact(
    artifact_id: int,
    worker_id: str,
    lease_ttl_ms: int,
) -> PriorArtifactMetadata | None:
    """Force-refresh transition: 'complete' → 'fetching' for a re-fetch or rebuild.

    Row-id-keyed and artifact-kind-agnostic — used for a minute-bar
    day-refresh (a provider correction), for rebuilding a daily-trade
    aggregate whose source minute set has grown or changed (see
    ``ensure_data._process_daily_trade_artifact``), and for rebuilding a
    factor_file whose history window has widened (see
    ``ensure_data._process_factor_file_artifact``). Returns the prior
    file_path + file_sha256 so the caller can preserve them if the new write
    fails validation. Returns None when the row isn't currently 'complete'
    (refresh has no work to do — e.g. a race with another worker).
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


# ---------------------------------------------------------------------------
# Task 5 observatory read projections
#
# Thin, read-only SELECTs backing the Observatory endpoints in
# app/routers/data_lake.py. Unlike the ensure_data-facing selects above
# (which only ever want 'complete' rows to decide what's reusable), these
# return every status — an operator view needs to see fetching/failed rows
# too, not just what's usable.
#
# Where a projection's shape matches a wire response one-for-one, the SELECT
# returns the types.py Pydantic model directly (matching the convention
# select_coverage_minute_bars already sets with ArtifactRecord above) — SQL
# column aliases do the field-name mapping, and session_open_ms_or_none is
# the one shared helper for the lone TradingDate -> trading_date_ms
# transform, so there's exactly one place that does that conversion.
# ---------------------------------------------------------------------------


def session_open_ms_or_none(trading_date: date | None) -> int | None:
    """Project an optional TradingDate column to its canonical ET session-open
    anchor (int64 ms UTC), or None when there's no date to convert."""
    return session_open_ms_utc(trading_date) if trading_date is not None else None


@dataclass(frozen=True)
class ArtifactCoverageRow:
    """Kept as an internal row type (not a types.py model): the coverage
    endpoint merges these by date against the canonical calendar's session
    list, which is router-side request-shaping, not a catalog projection."""

    trading_date: date
    status: str
    artifact_id: int


async def select_artifact_coverage(
    market: str,
    symbol: str,
    data_type: str,
    provider: str,
    price_adjustment_mode: str,
    start_trading_date: date,
    end_trading_date: date,
    data_root_id: UUID | None = None,
) -> list[ArtifactCoverageRow]:
    """Return per-day minute-bar artifact status in the window, any status.

    Only 'time_series_bars'/'minute' rows carry a per-day TradingDate —
    hour/daily aggregated-bar artifacts cover a symbol's whole history in one
    row (see uq_data_lake_artifacts_aggregated_bars), so day-keyed coverage
    is meaningful only at minute resolution.

    ``data_root_id`` defaults to the service's configured active root
    (issue #1876) — coverage is an active-root-default listing.
    """
    root_id = _resolve_data_root_id(data_root_id)
    query = """
        SELECT "TradingDate" AS trading_date, "Status" AS status, "Id" AS artifact_id
          FROM "DataLakeArtifacts"
         WHERE "ArtifactKind" = 'time_series_bars'
           AND "Resolution" = 'minute'
           AND "Market" = $1
           AND "Symbol" = $2
           AND "DataType" = $3
           AND "Provider" = $4
           AND "PriceAdjustmentMode" = $5
           AND "TradingDate" BETWEEN $6 AND $7
           AND "DataRootId" = $8
         ORDER BY "TradingDate"
    """
    async with connection() as conn:
        rows = await conn.fetch(
            query,
            market,
            symbol,
            data_type,
            provider,
            price_adjustment_mode,
            start_trading_date,
            end_trading_date,
            root_id,
        )
    return [ArtifactCoverageRow(**dict(r)) for r in rows]


async def select_artifact_by_id(artifact_id: int) -> ArtifactDetail | None:
    """Return the full receipt for one catalog row, or None when it doesn't exist.

    ``content_hash`` is None (not the empty string) until the row reaches
    Status='complete' and FileSha256 is actually populated — no COALESCE
    here, unlike select_coverage_minute_bars, which is allowed to default
    the column because its Status='complete' filter guarantees a real hash.

    Includes the failure diagnostics fail_artifact() persists (AttemptCount,
    LastError, ErrorMessage) — a 'failed' row's receipt is not "full"
    without them.

    Deliberately not root-scoped (issue #1876): a lookup by row Id is
    already unambiguous, and this may return a row from a root other than
    the service's active one — ``data_root_id`` is included in the
    response precisely so that isn't misleading.
    """
    query = """
        SELECT "Id" AS id, "ArtifactKind" AS artifact_kind, "Market" AS market,
               "Symbol" AS symbol, "TradingDate" AS trading_date,
               "Resolution" AS resolution, "DataType" AS data_type,
               "Provider" AS provider, "ProviderParams" AS provider_params,
               "PriceAdjustmentMode" AS price_adjustment_mode,
               "DataRootId" AS data_root_id,
               "DataContractHash" AS data_contract_hash,
               "FileSha256" AS content_hash,
               "FilePath" AS file_path, "FileSizeBytes" AS file_size_bytes,
               "Status" AS status, "RowCount" AS row_count,
               "FirstBarStartMs" AS first_bar_start_ms,
               "LastBarStartMs" AS last_bar_start_ms,
               "FetchedAtMs" AS fetched_at_ms, "CompletedAtMs" AS completed_at_ms,
               "AttemptCount" AS attempt_count, "LastError" AS last_error,
               "ErrorMessage" AS error_message
          FROM "DataLakeArtifacts"
         WHERE "Id" = $1
    """
    async with connection() as conn:
        row = await conn.fetchrow(query, artifact_id)
    if row is None:
        return None
    data = dict(row)
    trading_date = data.pop("trading_date")
    raw_provider_params = data.pop("provider_params")
    return ArtifactDetail(
        **data,
        trading_date_ms=session_open_ms_or_none(trading_date),
        # asyncpg returns jsonb as raw text unless a codec is registered; no
        # codec is registered here (see init_pool), so decode explicitly.
        provider_params=json.loads(raw_provider_params) if raw_provider_params else {},
    )


async def select_storage_totals_by_kind(market: str, data_root_id: UUID | None = None) -> list[StorageKindTotal]:
    """Complete-artifact counts and bytes, grouped by kind (+ resolution).

    Scoped to Status='complete': only completed artifacts have bytes on
    disk to count; fetching/failed rows have no FileSizeBytes. Every column
    is an identity projection, so the aliased row maps straight onto the
    model.

    ``data_root_id`` defaults to the service's configured active root
    (issue #1876) — storage summaries are an active-root-default listing.
    """
    root_id = _resolve_data_root_id(data_root_id)
    query = """
        SELECT "ArtifactKind" AS artifact_kind, "Resolution" AS resolution,
               COUNT(*) AS artifact_count,
               COALESCE(SUM("FileSizeBytes"), 0) AS total_bytes
          FROM "DataLakeArtifacts"
         WHERE "Market" = $1
           AND "DataRootId" = $2
           AND "Status" = 'complete'
         GROUP BY "ArtifactKind", "Resolution"
         ORDER BY "ArtifactKind", "Resolution" NULLS FIRST
    """
    async with connection() as conn:
        rows = await conn.fetch(query, market, root_id)
    return [StorageKindTotal(**dict(r)) for r in rows]


async def select_symbol_coverage_spans(market: str, data_root_id: UUID | None = None) -> list[SymbolCoverageSpan]:
    """Per-symbol day-keyed coverage span over complete minute-bar artifacts.

    Resolution='minute' is the filter, not just ArtifactKind='time_series_bars':
    hour/daily aggregated-bar rows carry TradingDate=NULL (one row per
    symbol's whole history — see uq_data_lake_artifacts_aggregated_bars), so
    without this filter a symbol with ONLY aggregated data would still
    produce a row here (MIN/MAX NULL, artifact_count=0 since COUNT ignores
    NULLs) — a fabricated placeholder for a symbol with no day-keyed
    coverage at all, not the honest absence the "span" concept documents.

    ``data_root_id`` defaults to the service's configured active root
    (issue #1876) — storage summaries are an active-root-default listing.
    """
    root_id = _resolve_data_root_id(data_root_id)
    query = """
        SELECT "Symbol" AS symbol,
               MIN("TradingDate") AS first_trading_date,
               MAX("TradingDate") AS last_trading_date,
               COUNT("TradingDate") AS artifact_count
          FROM "DataLakeArtifacts"
         WHERE "Market" = $1
           AND "ArtifactKind" = 'time_series_bars'
           AND "Resolution" = 'minute'
           AND "Status" = 'complete'
           AND "Symbol" IS NOT NULL
           AND "DataRootId" = $2
         GROUP BY "Symbol"
         ORDER BY "Symbol"
    """
    async with connection() as conn:
        rows = await conn.fetch(query, market, root_id)
    return [
        SymbolCoverageSpan(
            symbol=r["symbol"],
            first_trading_date_ms=session_open_ms_or_none(r["first_trading_date"]),
            last_trading_date_ms=session_open_ms_or_none(r["last_trading_date"]),
            artifact_count=r["artifact_count"],
        )
        for r in rows
    ]
