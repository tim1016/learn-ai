"""Live-Postgres unit tests for catalog_client write operations.

Skips when POSTGRES_URL is unset (same pattern as test_schema_drift.py).
Tests clean up after themselves via TRUNCATE in a function-scoped fixture.
"""

from __future__ import annotations

import os
from datetime import date

import asyncpg
import pytest

from app.config import settings
from app.data_lake import catalog_client
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

    ok = await catalog_client.steal_or_retry_minute_bar(
        artifact_id=artifact_id,
        worker_id="w-new",
        lease_ttl_ms=300_000,
        max_retries=3,
    )
    assert ok is True

    conn = await asyncpg.connect(_postgres_url())
    try:
        row = await conn.fetchrow(
            'SELECT "Status", "LeaseOwner", "AttemptCount" FROM "DataLakeArtifacts" WHERE "Id" = $1',
            artifact_id,
        )
    finally:
        await conn.close()
    assert row["Status"] == "fetching"
    assert row["LeaseOwner"] == "w-new"
    assert row["AttemptCount"] == 2  # incremented from 1


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
    )
    ok = await catalog_client.steal_or_retry_minute_bar(
        artifact_id=artifact_id,
        worker_id="w-2",
        lease_ttl_ms=300_000,
        max_retries=3,
    )
    assert ok is True


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

    ok = await catalog_client.steal_or_retry_minute_bar(
        artifact_id=artifact_id,
        worker_id="w-2",
        lease_ttl_ms=300_000,
        max_retries=3,
    )
    assert ok is False


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
    )

    prior = await catalog_client.refresh_complete_minute_bar(
        artifact_id=artifact_id,
        worker_id="w-1",
        lease_ttl_ms=300_000,
    )
    assert prior is not None
    assert prior.prior_file_path == "equity/usa/minute/spy/20240520_trade.zip"
    assert prior.prior_file_sha256 == "b" * 64


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
    prior = await catalog_client.refresh_complete_minute_bar(
        artifact_id=artifact_id,
        worker_id="w-1",
        lease_ttl_ms=300_000,
    )
    assert prior is None
