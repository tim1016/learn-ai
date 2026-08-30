"""Live-Postgres unit tests for catalog_client write operations.

Skips when POSTGRES_URL is unset (same pattern as test_schema_drift.py).
Tests clean up after themselves via TRUNCATE in a function-scoped fixture.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import time
from datetime import date
from pathlib import Path, PurePosixPath
from uuid import uuid4

import asyncpg
import pytest

from app.config import settings
from app.data_lake import atomic, catalog_client
from app.data_lake.types import ArtifactIdentity

pytestmark = pytest.mark.asyncio


def _postgres_url() -> str:
    url = settings.POSTGRES_URL or os.getenv("POSTGRES_URL", "")
    if not url:
        pytest.skip("POSTGRES_URL not configured")
    return url


@pytest.fixture
async def clean_artifacts():
    """Truncate DataLakeArtifacts before+after each test."""
    conn = await asyncpg.connect(_postgres_url())
    try:
        await conn.execute('TRUNCATE TABLE "DataLakeArtifacts" RESTART IDENTITY CASCADE')
    finally:
        await conn.close()
    yield
    conn = await asyncpg.connect(_postgres_url())
    try:
        await conn.execute('TRUNCATE TABLE "DataLakeArtifacts" RESTART IDENTITY CASCADE')
    finally:
        await conn.close()


def _minute_identity(date_val: date = date(2024, 5, 20)) -> ArtifactIdentity:
    return ArtifactIdentity(
        artifact_kind="time_series_bars",
        market="usa",
        symbol="SPY",
        trading_date=date_val,
        resolution="minute",
        data_type="trade",
        provider="polygon",
        price_adjustment_mode="raw",
    )


@pytest.fixture
async def pool():
    # Force-reset any stale pool left by a prior test (different event loop).
    await catalog_client.close_pool()
    await catalog_client.init_pool()
    yield
    await catalog_client.close_pool()


async def test_claim_minute_bar_inserts_row_and_returns_id(clean_artifacts, pool):
    artifact_id = await catalog_client.claim_minute_bar(
        identity=_minute_identity(),
        worker_id="w-1",
        lease_ttl_ms=300_000,
        data_contract_hash="a" * 64,
        file_path="equity/usa/minute/spy/20240520_trade.zip",
    )
    assert isinstance(artifact_id, int)


async def test_claim_minute_bar_returns_none_on_conflict(clean_artifacts, pool):
    identity = _minute_identity()
    a = await catalog_client.claim_minute_bar(
        identity=identity,
        worker_id="w-1",
        lease_ttl_ms=300_000,
        data_contract_hash="a" * 64,
        file_path="x.zip",
    )
    b = await catalog_client.claim_minute_bar(
        identity=identity,
        worker_id="w-2",
        lease_ttl_ms=300_000,
        data_contract_hash="a" * 64,
        file_path="x.zip",
    )
    assert a is not None
    assert b is None  # second claim loses


async def test_select_minute_bar_claim_state_returns_none_for_no_row(clean_artifacts, pool):
    assert await catalog_client.select_minute_bar_claim_state(_minute_identity()) is None


async def test_select_minute_bar_claim_state_finds_a_failed_row(clean_artifacts, pool):
    """The lookup a caller that lost claim_minute_bar's conflict needs.

    ``select_coverage_minute_bars`` cannot see this row (it is not
    'complete'); this is the id + status lookup that lets a caller reclaim
    it via ``steal_or_retry_minute_bar`` instead of reporting contention.
    """
    identity = _minute_identity()
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity,
        worker_id="w-1",
        lease_ttl_ms=300_000,
        data_contract_hash="a" * 64,
        file_path="x.zip",
    )
    await catalog_client.fail_artifact(
        artifact_id=artifact_id,
        last_error="provider_no_data",
        worker_id="w-1",
        lease_generation=catalog_client.INITIAL_LEASE_GENERATION,
    )

    state = await catalog_client.select_minute_bar_claim_state(identity)

    assert state is not None
    assert state.id == artifact_id
    assert state.status == "failed"
    assert state.attempt_count == 1
    assert state.last_error == "provider_no_data"


async def test_select_minute_bar_claim_state_finds_an_active_fetch(clean_artifacts, pool):
    """A live lease reads as 'fetching', distinct from a failed row."""
    identity = _minute_identity()
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity,
        worker_id="w-1",
        lease_ttl_ms=300_000,
        data_contract_hash="a" * 64,
        file_path="x.zip",
    )

    state = await catalog_client.select_minute_bar_claim_state(identity)

    assert state is not None
    assert state.id == artifact_id
    assert state.status == "fetching"
    assert state.last_error is None


async def test_complete_artifact_updates_to_complete(clean_artifacts, pool):
    identity = _minute_identity()
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity,
        worker_id="w-1",
        lease_ttl_ms=300_000,
        data_contract_hash="a" * 64,
        file_path="x.zip",
    )
    assert artifact_id is not None
    await catalog_client.complete_artifact(
        artifact_id=artifact_id,
        row_count=390,
        first_bar_start_ms=1_716_206_400_000,
        last_bar_start_ms=1_716_229_740_000,
        file_size_bytes=12345,
        file_sha256="b" * 64,
        lease_generation=catalog_client.INITIAL_LEASE_GENERATION,
    )

    conn = await asyncpg.connect(_postgres_url())
    try:
        row = await conn.fetchrow(
            'SELECT "Status", "RowCount", "FileSha256", "CompletedAtMs" FROM "DataLakeArtifacts" WHERE "Id" = $1',
            artifact_id,
        )
    finally:
        await conn.close()
    assert row["Status"] == "complete"
    assert row["RowCount"] == 390
    assert row["FileSha256"] == "b" * 64
    assert row["CompletedAtMs"] is not None


async def test_fail_artifact_updates_to_failed(clean_artifacts, pool):
    identity = _minute_identity()
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity,
        worker_id="w-1",
        lease_ttl_ms=300_000,
        data_contract_hash="a" * 64,
        file_path="x.zip",
    )
    assert artifact_id is not None
    await catalog_client.fail_artifact(
        artifact_id=artifact_id,
        last_error="provider_rate_limited",
        error_message="429 from Polygon",
        worker_id="w-1",
        lease_generation=catalog_client.INITIAL_LEASE_GENERATION,
    )
    conn = await asyncpg.connect(_postgres_url())
    try:
        row = await conn.fetchrow(
            'SELECT "Status", "LastError", "ErrorMessage" FROM "DataLakeArtifacts" WHERE "Id" = $1',
            artifact_id,
        )
    finally:
        await conn.close()
    assert row["Status"] == "failed"
    assert row["LastError"] == "provider_rate_limited"


async def test_refresh_lease_extends_expiry(clean_artifacts, pool):
    identity = _minute_identity()
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity,
        worker_id="w-1",
        lease_ttl_ms=300_000,
        data_contract_hash="a" * 64,
        file_path="x.zip",
    )
    assert artifact_id is not None

    # Read initial lease expiry.
    conn = await asyncpg.connect(_postgres_url())
    try:
        before = await conn.fetchval(
            'SELECT "LeaseExpiresAtMs" FROM "DataLakeArtifacts" WHERE "Id" = $1',
            artifact_id,
        )
    finally:
        await conn.close()

    ok = await catalog_client.refresh_lease(
        artifact_id=artifact_id,
        worker_id="w-1",
        lease_ttl_ms=600_000,
        lease_generation=catalog_client.INITIAL_LEASE_GENERATION,
    )
    assert ok is True

    conn = await asyncpg.connect(_postgres_url())
    try:
        after = await conn.fetchval(
            'SELECT "LeaseExpiresAtMs" FROM "DataLakeArtifacts" WHERE "Id" = $1',
            artifact_id,
        )
    finally:
        await conn.close()
    assert after > before


async def test_refresh_lease_rejects_wrong_owner(clean_artifacts, pool):
    identity = _minute_identity()
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity,
        worker_id="w-1",
        lease_ttl_ms=300_000,
        data_contract_hash="a" * 64,
        file_path="x.zip",
    )
    assert artifact_id is not None
    ok = await catalog_client.refresh_lease(
        artifact_id=artifact_id,
        worker_id="w-IMPOSTOR",
        lease_ttl_ms=600_000,
        lease_generation=catalog_client.INITIAL_LEASE_GENERATION,
    )
    assert ok is False


async def test_steal_or_retry_steals_expired_lease(clean_artifacts, pool):
    identity = _minute_identity()
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity,
        worker_id="w-orig",
        lease_ttl_ms=300_000,
        data_contract_hash="a" * 64,
        file_path="x.zip",
    )
    assert artifact_id is not None

    # Force the lease to be expired.
    conn = await asyncpg.connect(_postgres_url())
    try:
        await conn.execute(
            'UPDATE "DataLakeArtifacts" SET "LeaseExpiresAtMs" = 1 WHERE "Id" = $1',
            artifact_id,
        )
    finally:
        await conn.close()

    new_generation = await catalog_client.steal_or_retry_minute_bar(
        artifact_id=artifact_id,
        worker_id="w-new",
        lease_ttl_ms=300_000,
        max_retries=3,
    )
    # issue #1888: the steal must mint a fencing generation strictly past
    # the one the original claim recorded, not just report a bare boolean.
    assert new_generation == catalog_client.INITIAL_LEASE_GENERATION + 1

    conn = await asyncpg.connect(_postgres_url())
    try:
        row = await conn.fetchrow(
            'SELECT "Status", "LeaseOwner", "AttemptCount", "LeaseGeneration" FROM "DataLakeArtifacts" WHERE "Id" = $1',
            artifact_id,
        )
    finally:
        await conn.close()
    assert row["Status"] == "fetching"
    assert row["LeaseOwner"] == "w-new"
    assert row["AttemptCount"] == 2  # incremented from 1
    assert row["LeaseGeneration"] == new_generation


async def test_steal_or_retry_retries_failed_under_max(clean_artifacts, pool):
    identity = _minute_identity()
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity,
        worker_id="w-1",
        lease_ttl_ms=300_000,
        data_contract_hash="a" * 64,
        file_path="x.zip",
    )
    assert artifact_id is not None
    await catalog_client.fail_artifact(
        artifact_id=artifact_id,
        last_error="provider_api_error",
        worker_id="w-1",
        lease_generation=catalog_client.INITIAL_LEASE_GENERATION,
    )
    new_generation = await catalog_client.steal_or_retry_minute_bar(
        artifact_id=artifact_id,
        worker_id="w-2",
        lease_ttl_ms=300_000,
        max_retries=3,
    )
    assert new_generation == catalog_client.INITIAL_LEASE_GENERATION + 1


async def test_steal_or_retry_reactivates_a_stale_row_unconditionally(clean_artifacts, pool):
    """Codex P1, PR #1884. A 'stale' row (currently only metadata rows reach
    this status, via mark_metadata_artifacts_stale_for_path) must reclaim
    with no lease-expiry or retry-count gate -- unlike the 'fetching'/
    'failed' branches, whose caller
    (metadata_bundle._claim_and_complete_metadata_row) reaches this reclaim
    path only after already re-extracting and re-verifying fresh bytes on
    disk for this exact row's digest moments earlier in the same call, so
    there is no "still in flight elsewhere" ambiguity to gate against."""
    identity = _minute_identity()
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity,
        worker_id="w-1",
        lease_ttl_ms=300_000,
        data_contract_hash="a" * 64,
        file_path="x.zip",
    )
    assert artifact_id is not None
    await catalog_client.complete_artifact(
        artifact_id=artifact_id,
        row_count=1,
        first_bar_start_ms=0,
        last_bar_start_ms=0,
        file_size_bytes=1,
        file_sha256="b" * 64,
        lease_generation=catalog_client.INITIAL_LEASE_GENERATION,
    )

    # Force the row into 'stale' directly (unit-level: isolates this
    # function from mark_metadata_artifacts_stale_for_path's own behavior,
    # which is exercised separately below).
    conn = await asyncpg.connect(_postgres_url())
    try:
        await conn.execute('UPDATE "DataLakeArtifacts" SET "Status" = $1 WHERE "Id" = $2', "stale", artifact_id)
    finally:
        await conn.close()

    new_generation = await catalog_client.steal_or_retry_minute_bar(
        artifact_id=artifact_id,
        worker_id="w-new",
        lease_ttl_ms=300_000,
        max_retries=0,  # deliberately zero: a stale reclaim must not be gated by retry budget
    )
    assert new_generation == catalog_client.INITIAL_LEASE_GENERATION + 1

    conn = await asyncpg.connect(_postgres_url())
    try:
        row = await conn.fetchrow('SELECT "Status", "LeaseOwner" FROM "DataLakeArtifacts" WHERE "Id" = $1', artifact_id)
    finally:
        await conn.close()
    assert row["Status"] == "fetching"
    assert row["LeaseOwner"] == "w-new"


async def test_steal_or_retry_rejects_failed_at_max(clean_artifacts, pool):
    identity = _minute_identity()
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity,
        worker_id="w-1",
        lease_ttl_ms=300_000,
        data_contract_hash="a" * 64,
        file_path="x.zip",
    )
    assert artifact_id is not None

    # Force AttemptCount to max.
    conn = await asyncpg.connect(_postgres_url())
    try:
        await conn.execute(
            'UPDATE "DataLakeArtifacts" SET "Status" = $1, "AttemptCount" = $2 WHERE "Id" = $3',
            "failed",
            3,
            artifact_id,
        )
    finally:
        await conn.close()

    new_generation = await catalog_client.steal_or_retry_minute_bar(
        artifact_id=artifact_id,
        worker_id="w-2",
        lease_ttl_ms=300_000,
        max_retries=3,
    )
    assert new_generation is None


async def test_refresh_complete_returns_prior_metadata(clean_artifacts, pool):
    identity = _minute_identity()
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity,
        worker_id="w-1",
        lease_ttl_ms=300_000,
        data_contract_hash="a" * 64,
        file_path="equity/usa/minute/spy/20240520_trade.zip",
    )
    assert artifact_id is not None
    await catalog_client.complete_artifact(
        artifact_id=artifact_id,
        row_count=390,
        first_bar_start_ms=1,
        last_bar_start_ms=2,
        file_size_bytes=100,
        file_sha256="b" * 64,
        lease_generation=catalog_client.INITIAL_LEASE_GENERATION,
    )

    prior = await catalog_client.refresh_complete_artifact(
        artifact_id=artifact_id,
        worker_id="w-1",
        lease_ttl_ms=300_000,
    )
    assert prior is not None
    assert prior.prior_file_path == "equity/usa/minute/spy/20240520_trade.zip"
    assert prior.prior_file_sha256 == "b" * 64
    # issue #1888: a rebuild reclaim mints a new fencing generation, strictly
    # past the one the original claim/completion recorded.
    assert prior.new_lease_generation == catalog_client.INITIAL_LEASE_GENERATION + 1


async def test_refresh_complete_returns_none_when_not_complete(clean_artifacts, pool):
    identity = _minute_identity()
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity,
        worker_id="w-1",
        lease_ttl_ms=300_000,
        data_contract_hash="a" * 64,
        file_path="x.zip",
    )
    assert artifact_id is not None  # still 'fetching', not 'complete'
    prior = await catalog_client.refresh_complete_artifact(
        artifact_id=artifact_id,
        worker_id="w-1",
        lease_ttl_ms=300_000,
    )
    assert prior is None


async def test_complete_artifact_persists_a_new_data_contract_hash_on_rebuild(clean_artifacts, pool):
    """#1873 review fix: a rebuild (refresh_complete_artifact then
    complete_artifact with the newly computed hash) must persist that hash —
    otherwise every later ensure_data call sees the same stale mismatch and
    rebuilds again forever."""
    identity = _minute_identity()
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity,
        worker_id="w-1",
        lease_ttl_ms=300_000,
        data_contract_hash="a" * 64,
        file_path="x.zip",
    )
    assert artifact_id is not None
    await catalog_client.complete_artifact(
        artifact_id=artifact_id,
        row_count=390,
        first_bar_start_ms=1,
        last_bar_start_ms=2,
        file_size_bytes=100,
        file_sha256="b" * 64,
        lease_generation=catalog_client.INITIAL_LEASE_GENERATION,
    )
    prior = await catalog_client.refresh_complete_artifact(artifact_id=artifact_id, worker_id="w-1", lease_ttl_ms=300_000)
    assert prior is not None
    await catalog_client.complete_artifact(
        artifact_id=artifact_id,
        row_count=500,
        first_bar_start_ms=1,
        last_bar_start_ms=3,
        file_size_bytes=200,
        file_sha256="c" * 64,
        lease_generation=prior.new_lease_generation,
        data_contract_hash="d" * 64,
    )

    conn = await asyncpg.connect(_postgres_url())
    try:
        row = await conn.fetchrow('SELECT "DataContractHash" FROM "DataLakeArtifacts" WHERE "Id" = $1', artifact_id)
    finally:
        await conn.close()
    assert row["DataContractHash"] == "d" * 64


async def test_complete_artifact_leaves_data_contract_hash_untouched_when_omitted(clean_artifacts, pool):
    """The common (non-rebuild) path never passes data_contract_hash — the
    column set at claim time must survive complete_artifact unchanged."""
    identity = _minute_identity()
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity,
        worker_id="w-1",
        lease_ttl_ms=300_000,
        data_contract_hash="a" * 64,
        file_path="x.zip",
    )
    assert artifact_id is not None
    await catalog_client.complete_artifact(
        artifact_id=artifact_id,
        row_count=390,
        first_bar_start_ms=1,
        last_bar_start_ms=2,
        file_size_bytes=100,
        file_sha256="b" * 64,
        lease_generation=catalog_client.INITIAL_LEASE_GENERATION,
    )

    conn = await asyncpg.connect(_postgres_url())
    try:
        row = await conn.fetchrow('SELECT "DataContractHash" FROM "DataLakeArtifacts" WHERE "Id" = $1', artifact_id)
    finally:
        await conn.close()
    assert row["DataContractHash"] == "a" * 64


async def test_restore_complete_artifact_undoes_a_refresh(clean_artifacts, pool):
    """A rebuild that fails before writing anything new must be undoable back
    to 'complete' with its pre-rebuild metadata intact (#1873 review fix —
    aggregated-bar and corp-action artifacts have no steal_or_retry path, so
    a bare fail_artifact() here would strand the row forever)."""
    identity = _minute_identity()
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity,
        worker_id="w-1",
        lease_ttl_ms=300_000,
        data_contract_hash="a" * 64,
        file_path="x.zip",
    )
    assert artifact_id is not None
    await catalog_client.complete_artifact(
        artifact_id=artifact_id,
        row_count=390,
        first_bar_start_ms=1,
        last_bar_start_ms=2,
        file_size_bytes=100,
        file_sha256="b" * 64,
        lease_generation=catalog_client.INITIAL_LEASE_GENERATION,
    )
    prior = await catalog_client.refresh_complete_artifact(
        artifact_id=artifact_id, worker_id="w-1", lease_ttl_ms=300_000
    )
    assert prior is not None

    # The refresh minted a new generation; the restore must present that one,
    # not the generation the row carried before the reclaim.
    assert (
        await catalog_client.restore_complete_artifact(
            artifact_id=artifact_id, worker_id="w-1", lease_generation=catalog_client.INITIAL_LEASE_GENERATION
        )
        is False
    ), "a pre-refresh generation must not be able to restore the row"

    restored = await catalog_client.restore_complete_artifact(
        artifact_id=artifact_id, worker_id="w-1", lease_generation=prior.new_lease_generation
    )
    assert restored is True

    conn = await asyncpg.connect(_postgres_url())
    try:
        row = await conn.fetchrow(
            'SELECT "Status", "FileSha256", "DataContractHash", "LeaseOwner" FROM "DataLakeArtifacts" WHERE "Id" = $1',
            artifact_id,
        )
    finally:
        await conn.close()
    assert row["Status"] == "complete"
    assert row["FileSha256"] == "b" * 64  # untouched by the failed rebuild
    assert row["DataContractHash"] == "a" * 64
    assert row["LeaseOwner"] is None


async def test_restore_complete_artifact_rejects_a_different_worker(clean_artifacts, pool):
    """Scoped to the caller's own worker_id, same as refresh_lease — a worker
    must not resurrect a row it doesn't hold the lease on."""
    identity = _minute_identity()
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity,
        worker_id="w-1",
        lease_ttl_ms=300_000,
        data_contract_hash="a" * 64,
        file_path="x.zip",
    )
    assert artifact_id is not None
    await catalog_client.complete_artifact(
        artifact_id=artifact_id,
        row_count=390,
        first_bar_start_ms=1,
        last_bar_start_ms=2,
        file_size_bytes=100,
        file_sha256="b" * 64,
        lease_generation=catalog_client.INITIAL_LEASE_GENERATION,
    )
    await catalog_client.refresh_complete_artifact(artifact_id=artifact_id, worker_id="w-1", lease_ttl_ms=300_000)

    restored = await catalog_client.restore_complete_artifact(
        artifact_id=artifact_id, worker_id="w-2", lease_generation=catalog_client.INITIAL_LEASE_GENERATION
    )
    assert restored is False


async def test_restore_complete_artifact_returns_false_when_not_fetching(clean_artifacts, pool):
    """A row that was never refreshed (still plain 'complete') has nothing
    to undo — the 'fetching' guard must reject it, not silently no-op true."""
    identity = _minute_identity()
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity,
        worker_id="w-1",
        lease_ttl_ms=300_000,
        data_contract_hash="a" * 64,
        file_path="x.zip",
    )
    assert artifact_id is not None
    await catalog_client.complete_artifact(
        artifact_id=artifact_id,
        row_count=390,
        first_bar_start_ms=1,
        last_bar_start_ms=2,
        file_size_bytes=100,
        file_sha256="b" * 64,
        lease_generation=catalog_client.INITIAL_LEASE_GENERATION,
    )
    restored = await catalog_client.restore_complete_artifact(
        artifact_id=artifact_id, worker_id="w-1", lease_generation=catalog_client.INITIAL_LEASE_GENERATION
    )
    assert restored is False


# ---------------------------------------------------------------------------
# Task 9: claim ops for corp-action, metadata, aggregated-bar
# ---------------------------------------------------------------------------


def _corp_action_identity(artifact_kind: str = "factor_file") -> ArtifactIdentity:
    return ArtifactIdentity(
        artifact_kind=artifact_kind,
        market="usa",
        symbol="SPY",
        trading_date=None,
        resolution=None,
        data_type=None,
        provider="polygon",
        price_adjustment_mode="raw",
    )


def _metadata_identity() -> ArtifactIdentity:
    return ArtifactIdentity(
        artifact_kind="metadata",
        market=None,
        symbol=None,
        trading_date=None,
        resolution=None,
        data_type=None,
        provider="lean_image_extract",
        price_adjustment_mode=None,
    )


def _aggregated_bar_identity() -> ArtifactIdentity:
    return ArtifactIdentity(
        artifact_kind="time_series_bars",
        market="usa",
        symbol="SPY",
        trading_date=None,
        resolution="daily",
        data_type="trade",
        provider="polygon",
        price_adjustment_mode="raw",
    )


async def test_claim_corp_action_artifact_inserts_and_conflicts(clean_artifacts, pool):
    identity = _corp_action_identity("factor_file")
    a = await catalog_client.claim_corp_action_artifact(
        identity=identity,
        worker_id="w-1",
        lease_ttl_ms=300_000,
        data_contract_hash="c" * 64,
        file_path="equity/usa/factor_files/spy.csv",
    )
    assert isinstance(a, int)

    b = await catalog_client.claim_corp_action_artifact(
        identity=identity,
        worker_id="w-2",
        lease_ttl_ms=300_000,
        data_contract_hash="c" * 64,
        file_path="equity/usa/factor_files/spy.csv",
    )
    assert b is None  # second claim loses


async def test_claim_metadata_artifact_inserts_and_conflicts(clean_artifacts, pool):
    identity = _metadata_identity()
    a = await catalog_client.claim_metadata_artifact(
        identity=identity,
        worker_id="w-1",
        lease_ttl_ms=300_000,
        data_contract_hash="d" * 64,
        file_path="market-hours-database.json",
    )
    assert isinstance(a, int)

    b = await catalog_client.claim_metadata_artifact(
        identity=identity,
        worker_id="w-2",
        lease_ttl_ms=300_000,
        data_contract_hash="d" * 64,
        file_path="market-hours-database.json",
    )
    assert b is None  # second claim loses


async def test_select_metadata_claim_state_returns_none_for_no_row(clean_artifacts, pool):
    assert await catalog_client.select_metadata_claim_state("d" * 64) is None


async def test_select_metadata_claim_state_finds_a_failed_row_reclaimable_by_steal_or_retry(clean_artifacts, pool):
    """The lookup a caller that lost claim_metadata_artifact's conflict needs.

    Unlike claim_minute_bar, claim_metadata_artifact's ON CONFLICT DO NOTHING
    has no reclaim path of its own — this is the id + status lookup that lets
    a caller reclaim a settled failure via steal_or_retry_minute_bar (which
    operates purely on Id/Status/AttemptCount, so it works for any artifact
    kind) instead of reporting contention on it forever.
    """
    identity = _metadata_identity()
    artifact_id = await catalog_client.claim_metadata_artifact(
        identity=identity,
        worker_id="w-1",
        lease_ttl_ms=300_000,
        data_contract_hash="d" * 64,
        file_path="market-hours-database.json",
    )
    await catalog_client.fail_artifact(
        artifact_id=artifact_id,
        last_error="provider_api_error",
        worker_id="w-1",
        lease_generation=catalog_client.INITIAL_LEASE_GENERATION,
    )

    state = await catalog_client.select_metadata_claim_state("d" * 64)
    assert state is not None
    assert state.id == artifact_id
    assert state.status == "failed"
    assert state.attempt_count == 1

    reclaimed_generation = await catalog_client.steal_or_retry_minute_bar(
        artifact_id=state.id,
        worker_id="w-2",
        lease_ttl_ms=300_000,
        max_retries=3,
    )
    assert reclaimed_generation == catalog_client.INITIAL_LEASE_GENERATION + 1


async def test_claim_aggregated_bar_artifact_inserts_and_conflicts(clean_artifacts, pool):
    identity = _aggregated_bar_identity()
    a = await catalog_client.claim_aggregated_bar_artifact(
        identity=identity,
        worker_id="w-1",
        lease_ttl_ms=300_000,
        data_contract_hash="e" * 64,
        file_path="equity/usa/daily/spy.zip",
    )
    assert isinstance(a, int)

    b = await catalog_client.claim_aggregated_bar_artifact(
        identity=identity,
        worker_id="w-2",
        lease_ttl_ms=300_000,
        data_contract_hash="e" * 64,
        file_path="equity/usa/daily/spy.zip",
    )
    assert b is None  # second claim loses


# ---------------------------------------------------------------------------
# #1879 (PR C of #1861): metadata rows carry a real PriceAdjustmentMode,
# and mark_metadata_artifacts_stale_for_path scopes staleness by it.
# ---------------------------------------------------------------------------


async def test_claim_metadata_artifact_records_the_price_adjustment_mode(clean_artifacts, pool):
    """Pre-#1879, PriceAdjustmentMode was hardcoded NULL for every metadata
    row regardless of ``identity.price_adjustment_mode``. Populating it is
    what lets mark_metadata_artifacts_stale_for_path scope by mode instead
    of staling a sibling mode's still-valid row (FilePath alone is identical
    across modes -- see that function's own docstring)."""
    identity = ArtifactIdentity(
        artifact_kind="metadata",
        market=None,
        symbol=None,
        provider="lean_image_extract",
        price_adjustment_mode="raw",
    )
    artifact_id = await catalog_client.claim_metadata_artifact(
        identity=identity,
        worker_id="w-1",
        lease_ttl_ms=300_000,
        data_contract_hash="f" * 64,
        file_path="market-hours/market-hours-database.json",
    )
    async with catalog_client.connection() as conn:
        row = await conn.fetchrow('SELECT "PriceAdjustmentMode" FROM "DataLakeArtifacts" WHERE "Id" = $1', artifact_id)
    assert row["PriceAdjustmentMode"] == "raw"


async def test_claim_metadata_artifact_still_writes_null_when_identity_carries_none(clean_artifacts, pool):
    """Existing callers (pre-#1879) construct their identity with
    ``price_adjustment_mode=None`` and must keep writing NULL, unchanged."""
    artifact_id = await catalog_client.claim_metadata_artifact(
        identity=_metadata_identity(),
        worker_id="w-1",
        lease_ttl_ms=300_000,
        data_contract_hash="f" * 64,
        file_path="market-hours/market-hours-database.json",
    )
    async with catalog_client.connection() as conn:
        row = await conn.fetchrow('SELECT "PriceAdjustmentMode" FROM "DataLakeArtifacts" WHERE "Id" = $1', artifact_id)
    assert row["PriceAdjustmentMode"] is None


def _metadata_identity_with_mode(mode: str) -> ArtifactIdentity:
    return ArtifactIdentity(
        artifact_kind="metadata",
        market=None,
        symbol=None,
        provider="lean_image_extract",
        price_adjustment_mode=mode,
    )


async def _complete_metadata_row(identity: ArtifactIdentity, dch: str, file_path: str) -> int:
    artifact_id = await catalog_client.claim_metadata_artifact(
        identity=identity, worker_id="w-1", lease_ttl_ms=300_000, data_contract_hash=dch, file_path=file_path
    )
    assert artifact_id is not None
    await catalog_client.complete_artifact(
        artifact_id=artifact_id,
        row_count=1,
        first_bar_start_ms=0,
        last_bar_start_ms=0,
        file_size_bytes=10,
        file_sha256="a" * 64,
        lease_generation=catalog_client.INITIAL_LEASE_GENERATION,
    )
    return artifact_id


async def test_mark_metadata_artifacts_stale_for_path_stales_only_the_same_root_and_mode(clean_artifacts, pool):
    root_id = _metadata_identity().data_root_id
    file_path = "market-hours/market-hours-database.json"

    old_raw = await _complete_metadata_row(_metadata_identity_with_mode("raw"), "g" * 64, file_path)
    sibling_mode = await _complete_metadata_row(_metadata_identity_with_mode("polygon_split_adjusted"), "h" * 64, file_path)
    new_raw = await _complete_metadata_row(_metadata_identity_with_mode("raw"), "i" * 64, file_path)

    staled = await catalog_client.mark_metadata_artifacts_stale_for_path(
        data_root_id=root_id, price_adjustment_mode="raw", file_path=file_path, keep_artifact_id=new_raw
    )

    assert staled == 1
    async with catalog_client.connection() as conn:
        rows = {
            r["Id"]: r["Status"]
            for r in await conn.fetch('SELECT "Id", "Status" FROM "DataLakeArtifacts" WHERE "ArtifactKind" = \'metadata\'')
        }
    assert rows[old_raw] == "stale", "the superseded 'raw' row must be staled"
    assert rows[sibling_mode] == "complete", "a different mode's row for the identical FilePath must survive untouched"
    assert rows[new_raw] == "complete", "the row just kept must stay complete"


async def test_mark_metadata_artifacts_stale_for_path_stales_a_legacy_null_mode_row(clean_artifacts, pool):
    """Codex P2, PR #1884. Rows written by the pre-#1879 code recorded
    PriceAdjustmentMode = NULL (claim_metadata_artifact's existing
    "identity carries None" test above). In SQL, NULL = 'raw' is never
    true, so the predicate could previously never match a legacy NULL-mode
    row even when its FilePath is the exact same physical file a freshly-
    completed mode-tagged row now supersedes -- letting it persist forever
    as a phantom 'complete' duplicate."""
    root_id = _metadata_identity().data_root_id
    file_path = "market-hours/market-hours-database.json"

    legacy_null_mode = await _complete_metadata_row(_metadata_identity(), "j" * 64, file_path)
    new_raw = await _complete_metadata_row(_metadata_identity_with_mode("raw"), "k" * 64, file_path)

    staled = await catalog_client.mark_metadata_artifacts_stale_for_path(
        data_root_id=root_id, price_adjustment_mode="raw", file_path=file_path, keep_artifact_id=new_raw
    )

    assert staled == 1
    async with catalog_client.connection() as conn:
        rows = {
            r["Id"]: r["Status"]
            for r in await conn.fetch('SELECT "Id", "Status" FROM "DataLakeArtifacts" WHERE "ArtifactKind" = \'metadata\'')
        }
    assert rows[legacy_null_mode] == "stale", "a legacy NULL-mode row for the same physical path must be staled"
    assert rows[new_raw] == "complete"


async def test_mark_metadata_artifacts_stale_for_path_with_no_keeper_stales_every_complete_row(clean_artifacts, pool):
    """keep_artifact_id is optional (metadata_bundle's interest_rate=None
    branch has no new row to keep at all -- there is no interest-rate DCH
    to claim one under for a digest with no interest-rate data). Omitting
    it must stale every complete row for the path unconditionally, not
    raise or silently keep everything."""
    root_id = _metadata_identity().data_root_id
    file_path = "alternative/interest-rate/usa/interest-rate.csv"

    old_row = await _complete_metadata_row(_metadata_identity_with_mode("raw"), "l" * 64, file_path)

    staled = await catalog_client.mark_metadata_artifacts_stale_for_path(
        data_root_id=root_id, price_adjustment_mode="raw", file_path=file_path
    )

    assert staled == 1
    async with catalog_client.connection() as conn:
        row = await conn.fetchrow('SELECT "Status" FROM "DataLakeArtifacts" WHERE "Id" = $1', old_row)
    assert row["Status"] == "stale"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Issue #1888: fence the artifact lease against a zombie writer.
#
# The publication (authorize -> rename -> receipt) happens inside one catalog
# transaction holding the artifact's row lock, so a competing steal is
# serialized behind it and must re-evaluate against committed state. These
# tests drive the production functions -- catalog_client.publish_under_lease
# and atomic.publish_artifact -- rather than re-implementing the gate, so
# reverting either the lock or the generation guards flips them red.
# ---------------------------------------------------------------------------


async def _lease_row(artifact_id: int) -> asyncpg.Record:
    async with catalog_client.connection() as conn:
        row = await conn.fetchrow(
            'SELECT "Status", "LeaseOwner", "LeaseGeneration", "LeaseExpiresAtMs", '
            '"FileSha256", "RowCount", "LastError" '
            'FROM "DataLakeArtifacts" WHERE "Id" = $1',
            artifact_id,
        )
    assert row is not None
    return row


async def _claim_a(rel_path: PurePosixPath, lease_ttl_ms: int = 300_000) -> int:
    artifact_id = await catalog_client.claim_minute_bar(
        identity=_minute_identity(),
        worker_id="writer-a",
        lease_ttl_ms=lease_ttl_ms,
        data_contract_hash="a" * 64,
        file_path=str(rel_path),
    )
    assert artifact_id is not None
    return artifact_id


async def _expire_lease(artifact_id: int) -> None:
    async with catalog_client.connection() as conn:
        await conn.execute('UPDATE "DataLakeArtifacts" SET "LeaseExpiresAtMs" = 1 WHERE "Id" = $1', artifact_id)


async def test_zombie_writer_cannot_overwrite_winners_file_after_lease_steal(clean_artifacts, pool, tmp_path: Path):
    """B steals before A ever reaches the lock; A must refuse without renaming.

    The original #1888 scenario, end to end across both systems the race
    spans: A claims, A's lease expires and B steals it, B publishes its own
    bytes for real, and only then does A -- unaware it lost -- attempt to
    publish its stale bytes.
    """
    lake_root = tmp_path / "lake"
    staging_root = tmp_path / "staging"
    lake_root.mkdir()
    staging_root.mkdir()
    rel_path = PurePosixPath("equity/usa/minute/spy/20240520_trade.zip")

    artifact_id = await _claim_a(rel_path)
    a_generation = catalog_client.INITIAL_LEASE_GENERATION

    await _expire_lease(artifact_id)
    b_generation = await catalog_client.steal_or_retry_minute_bar(
        artifact_id=artifact_id,
        worker_id="writer-b",
        lease_ttl_ms=300_000,
        max_retries=3,
    )
    assert b_generation == a_generation + 1

    b_content = b"winner-bytes-from-writer-b"
    b_sha = await atomic.publish_artifact(
        content=b_content,
        lake_root=lake_root,
        staging_root=staging_root,
        rel_lake_path=rel_path,
        request_id=uuid4(),
        worker_id="writer-b",
        attempt=1,
        artifact_id=artifact_id,
        lease_generation=b_generation,
        row_count=1,
        first_bar_start_ms=1,
        last_bar_start_ms=2,
    )

    final_path = lake_root / Path(*rel_path.parts)
    assert final_path.read_bytes() == b_content

    a_content = b"stale-bytes-from-writer-a"
    with pytest.raises(catalog_client.ArtifactLeaseLostError):
        await atomic.publish_artifact(
            content=a_content,
            lake_root=lake_root,
            staging_root=staging_root,
            rel_lake_path=rel_path,
            request_id=uuid4(),
            worker_id="writer-a",
            attempt=1,
            artifact_id=artifact_id,
            lease_generation=a_generation,
            row_count=999,
            first_bar_start_ms=999,
            last_bar_start_ms=999,
        )

    assert final_path.read_bytes() == b_content, "writer A's stale publish must not overwrite writer B's file"

    row = await _lease_row(artifact_id)
    assert row["FileSha256"] == b_sha, "the catalog must still record B's winning hash, never A's stale one"
    assert row["RowCount"] == 1
    assert row["Status"] == "complete"


async def test_a_concurrent_steal_cannot_interleave_with_a_held_publication(
    clean_artifacts, pool, tmp_path: Path
):
    """The interleaving a check-then-rename design cannot close.

    A is authorized and pauses *inside* the publication -- between the
    authorization and the rename -- while B tries to steal the same row.
    Under the old stage/confirm/promote sequence B would win the steal here
    and A would then rename its stale bytes over the winner's path. Holding
    the row lock across the rename makes B block until A commits, after
    which B's ``WHERE`` re-evaluates against a row that is now 'complete'
    and no longer stealable.

    B runs in its own thread with its own event loop (and therefore its own
    asyncpg pool): ``promote`` is synchronous and runs inline while the lock
    is held, so a same-loop competitor could not get a turn to prove
    anything.

    Timing note: A's lease is pinned to expire a few seconds out, and
    ``promote`` waits until that instant has genuinely passed before letting
    B run. Without that wait B's steal would find a live lease and return
    None for a reason unrelated to the lock, making the test vacuous. The
    margin is generous in the direction that matters -- a slow machine makes
    A's authorization fail loudly rather than making this pass for the wrong
    reason.
    """
    lake_root = tmp_path / "lake"
    staging_root = tmp_path / "staging"
    lake_root.mkdir()
    staging_root.mkdir()
    rel_path = PurePosixPath("equity/usa/minute/spy/20240520_trade.zip")
    final_path = lake_root / Path(*rel_path.parts)

    artifact_id = await _claim_a(rel_path)
    a_generation = catalog_client.INITIAL_LEASE_GENERATION

    expires_at_ms = int(time.time() * 1000) + 4_000
    async with catalog_client.connection() as conn:
        await conn.execute(
            'UPDATE "DataLakeArtifacts" SET "LeaseExpiresAtMs" = $2 WHERE "Id" = $1', artifact_id, expires_at_ms
        )

    b_result: dict[str, object] = {}
    b_started = threading.Event()
    b_done = threading.Event()

    def run_b() -> None:
        async def _steal() -> None:
            await catalog_client.init_pool()
            try:
                b_started.set()
                b_result["generation"] = await catalog_client.steal_or_retry_minute_bar(
                    artifact_id=artifact_id,
                    worker_id="writer-b",
                    lease_ttl_ms=300_000,
                    max_retries=3,
                )
            finally:
                await catalog_client.close_pool()

        try:
            asyncio.run(_steal())
        except Exception as exc:  # surfaced by the assertions below, never swallowed
            b_result["error"] = repr(exc)
        finally:
            b_done.set()

    b_thread = threading.Thread(target=run_b, daemon=True)
    observed: dict[str, object] = {}
    a_content = b"winner-bytes-from-writer-a"

    def promote() -> None:
        # Inside publish_under_lease's transaction, holding the row lock.
        while time.time() * 1000 <= expires_at_ms:
            time.sleep(0.05)
        b_thread.start()
        assert b_started.wait(timeout=10), "writer B never reached its steal"
        observed["b_finished_while_locked"] = b_done.wait(timeout=1.5)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        final_path.write_bytes(a_content)

    await catalog_client.publish_under_lease(
        artifact_id=artifact_id,
        worker_id="writer-a",
        lease_generation=a_generation,
        promote=promote,
        row_count=7,
        first_bar_start_ms=11,
        last_bar_start_ms=22,
        file_size_bytes=len(a_content),
        file_sha256=hashlib.sha256(a_content).hexdigest(),
    )

    b_thread.join(timeout=30)
    assert b_done.is_set(), "writer B never finished after the publication committed"
    assert "error" not in b_result, f"writer B raised: {b_result.get('error')}"

    assert observed["b_finished_while_locked"] is False, (
        "writer B completed its steal while writer A held the publication lock -- "
        "the row lock does not span the rename"
    )
    assert b_result["generation"] is None, (
        "writer B stole a row that writer A had already committed as complete -- "
        "B did not re-evaluate its predicate after the lock was released"
    )

    row = await _lease_row(artifact_id)
    assert row["Status"] == "complete"
    assert row["LeaseGeneration"] == a_generation, "no steal may have incremented the generation"
    assert row["FileSha256"] == hashlib.sha256(a_content).hexdigest()
    assert final_path.read_bytes() == a_content


async def test_publication_refuses_an_expired_lease_even_with_no_competitor(
    clean_artifacts, pool, tmp_path: Path
):
    """Expiry alone fences the writer.

    A writer paused past its lease TTL used to pass authorization as long as
    nobody had raced it yet -- so a zombie could publish arbitrarily late
    while a sweeper was simultaneously eligible to take the row from under
    it. The lock is not the only thing being checked.
    """
    lake_root = tmp_path / "lake"
    staging_root = tmp_path / "staging"
    lake_root.mkdir()
    staging_root.mkdir()
    rel_path = PurePosixPath("equity/usa/minute/spy/20240520_trade.zip")
    artifact_id = await _claim_a(rel_path)
    await _expire_lease(artifact_id)

    promoted = False

    def promote() -> None:
        nonlocal promoted
        promoted = True

    with pytest.raises(catalog_client.ArtifactLeaseLostError, match="not authorized to publish"):
        await catalog_client.publish_under_lease(
            artifact_id=artifact_id,
            worker_id="writer-a",
            lease_generation=catalog_client.INITIAL_LEASE_GENERATION,
            promote=promote,
            row_count=1,
            first_bar_start_ms=1,
            last_bar_start_ms=2,
            file_size_bytes=3,
            file_sha256="f" * 64,
        )

    assert promoted is False, "an expired lease must never reach the rename"
    assert (await _lease_row(artifact_id))["Status"] == "fetching"


async def test_a_stale_writer_cannot_mutate_the_winners_generation(clean_artifacts, pool, tmp_path: Path):
    """Every lease-holder mutation is fenced, not just the completion.

    Owner alone cannot discriminate: ensure_data's ``_WORKER_ID`` is
    per-process, so two concurrent operations in one process present the same
    lease owner and only the generation tells them apart. A stale writer that
    could still fail, heartbeat, or restore the row would clobber the winner
    just as surely as one that could complete it.
    """
    rel_path = PurePosixPath("equity/usa/minute/spy/20240520_trade.zip")
    artifact_id = await _claim_a(rel_path)
    a_generation = catalog_client.INITIAL_LEASE_GENERATION

    await _expire_lease(artifact_id)
    # Same worker id as A on purpose -- this is the in-process case, where the
    # generation is the only thing standing between the two.
    b_generation = await catalog_client.steal_or_retry_minute_bar(
        artifact_id=artifact_id,
        worker_id="writer-a",
        lease_ttl_ms=300_000,
        max_retries=3,
    )
    assert b_generation == a_generation + 1
    before = await _lease_row(artifact_id)

    assert (
        await catalog_client.fail_artifact(
            artifact_id,
            "provider_api_error",
            "stale writer's late failure",
            worker_id="writer-a",
            lease_generation=a_generation,
        )
        is False
    ), "a stale generation must not be able to fail the winner's row"

    assert (
        await catalog_client.refresh_lease(
            artifact_id=artifact_id,
            worker_id="writer-a",
            lease_ttl_ms=900_000,
            lease_generation=a_generation,
        )
        is False
    ), "a stale generation must not be able to heartbeat the winner's lease"

    assert (
        await catalog_client.restore_complete_artifact(artifact_id, "writer-a", a_generation) is False
    ), "a stale generation must not be able to restore the winner's row to complete"

    assert (
        await catalog_client.complete_artifact(
            artifact_id=artifact_id,
            row_count=999,
            first_bar_start_ms=999,
            last_bar_start_ms=999,
            file_size_bytes=999,
            file_sha256="9" * 64,
            lease_generation=a_generation,
        )
        is False
    ), "a stale generation must not be able to complete the winner's row"

    after = await _lease_row(artifact_id)
    assert dict(after) == dict(before), "no stale-generation call may have changed the row"

    # The winner's own generation still works, so the fence is not blanket denial.
    assert (
        await catalog_client.refresh_lease(
            artifact_id=artifact_id,
            worker_id="writer-a",
            lease_ttl_ms=900_000,
            lease_generation=b_generation,
        )
        is True
    )


async def test_completing_clears_a_reclaimed_rows_stale_error_message(clean_artifacts, pool):
    """A row that failed, was reclaimed, and then succeeded must stop
    advertising the old failure -- the Observatory artifact receipt renders
    any non-empty error message, including on complete rows."""
    rel_path = PurePosixPath("equity/usa/minute/spy/20240520_trade.zip")
    artifact_id = await _claim_a(rel_path)
    await catalog_client.fail_artifact(
        artifact_id,
        "launcher_unreachable",
        "LEAN launcher did not answer",
        worker_id="writer-a",
        lease_generation=catalog_client.INITIAL_LEASE_GENERATION,
    )
    retry_generation = await catalog_client.steal_or_retry_minute_bar(
        artifact_id=artifact_id,
        worker_id="writer-a",
        lease_ttl_ms=300_000,
        max_retries=3,
    )
    assert retry_generation is not None

    assert (
        await catalog_client.complete_artifact(
            artifact_id=artifact_id,
            row_count=1,
            first_bar_start_ms=1,
            last_bar_start_ms=2,
            file_size_bytes=3,
            file_sha256="c" * 64,
            lease_generation=retry_generation,
        )
        is True
    )

    async with catalog_client.connection() as conn:
        row = await conn.fetchrow(
            'SELECT "Status", "LastError", "ErrorMessage" FROM "DataLakeArtifacts" WHERE "Id" = $1', artifact_id
        )
    assert row["Status"] == "complete"
    assert row["LastError"] is None
    assert row["ErrorMessage"] is None


async def test_mark_complete_artifact_failed_refuses_a_claimed_row(clean_artifacts, pool):
    """The unleased failure path only ever transitions 'complete' -> 'failed'.

    Cache-import uses it when the catalog claims a hash the destination file
    contradicts; it must not reach a row another writer has since claimed for
    a refresh.
    """
    rel_path = PurePosixPath("equity/usa/minute/spy/20240520_trade.zip")
    artifact_id = await _claim_a(rel_path)

    assert await catalog_client.mark_complete_artifact_failed(artifact_id, "io_error", "disk disagrees") is False
    assert (await _lease_row(artifact_id))["Status"] == "fetching"

    assert (
        await catalog_client.complete_artifact(
            artifact_id=artifact_id,
            row_count=1,
            first_bar_start_ms=1,
            last_bar_start_ms=2,
            file_size_bytes=3,
            file_sha256="c" * 64,
            lease_generation=catalog_client.INITIAL_LEASE_GENERATION,
        )
        is True
    )
    assert await catalog_client.mark_complete_artifact_failed(artifact_id, "io_error", "disk disagrees") is True
    assert (await _lease_row(artifact_id))["Status"] == "failed"
