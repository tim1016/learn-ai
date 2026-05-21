"""Live-Postgres unit tests for sweep.reclaim_expired_leases.

The sweep is not yet scheduled (Slice 4 wires the cron). This test exercises
the SQL primitive directly.
"""

from __future__ import annotations

import os
from datetime import date

import asyncpg
import pytest

from app.config import settings
from app.data_lake import catalog_client, sweep
from app.data_lake.types import ArtifactIdentity

pytestmark = pytest.mark.asyncio


def _postgres_url() -> str:
    url = settings.POSTGRES_URL or os.getenv("POSTGRES_URL", "")
    if not url:
        pytest.skip("POSTGRES_URL not configured")
    return url


@pytest.fixture
async def clean_artifacts():
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


@pytest.fixture
async def pool():
    await catalog_client.init_pool()
    yield
    await catalog_client.close_pool()


async def test_reclaim_marks_expired_fetching_rows_failed(clean_artifacts, pool):
    identity = ArtifactIdentity(
        artifact_kind="time_series_bars",
        market="usa",
        symbol="SPY",
        trading_date=date(2024, 5, 20),
        resolution="minute",
        data_type="trade",
        provider="polygon",
        price_adjustment_mode="raw",
    )
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity,
        worker_id="w-1",
        lease_ttl_ms=300_000,
        data_contract_hash="a" * 64,
        file_path="x.zip",
    )
    # Force the lease to be expired.
    conn = await asyncpg.connect(_postgres_url())
    try:
        await conn.execute(
            'UPDATE "DataLakeArtifacts" SET "LeaseExpiresAtMs" = 1 WHERE "Id" = $1',
            artifact_id,
        )
    finally:
        await conn.close()

    n = await sweep.reclaim_expired_leases()
    assert n == 1

    conn = await asyncpg.connect(_postgres_url())
    try:
        row = await conn.fetchrow(
            'SELECT "Status", "LastError" FROM "DataLakeArtifacts" WHERE "Id" = $1',
            artifact_id,
        )
    finally:
        await conn.close()
    assert row["Status"] == "failed"
    assert row["LastError"] == "lease_expired"


async def test_reclaim_leaves_valid_lease_alone(clean_artifacts, pool):
    identity = ArtifactIdentity(
        artifact_kind="time_series_bars",
        market="usa",
        symbol="SPY",
        trading_date=date(2024, 5, 20),
        resolution="minute",
        data_type="trade",
        provider="polygon",
        price_adjustment_mode="raw",
    )
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity,
        worker_id="w-1",
        lease_ttl_ms=300_000,
        data_contract_hash="a" * 64,
        file_path="x.zip",
    )
    n = await sweep.reclaim_expired_leases()
    assert n == 0

    conn = await asyncpg.connect(_postgres_url())
    try:
        status = await conn.fetchval(
            'SELECT "Status" FROM "DataLakeArtifacts" WHERE "Id" = $1',
            artifact_id,
        )
    finally:
        await conn.close()
    assert status == "fetching"
