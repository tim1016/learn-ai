"""Live-Postgres unit tests for the Task 5 observatory read projections.

Skips when POSTGRES_URL is unset (same pattern as test_catalog_write_ops.py
and test_schema_drift.py). Exercises select_artifact_coverage,
select_artifact_by_id, select_storage_totals_by_kind, and
select_symbol_coverage_spans against a real schema — the row under test is
inserted via the catalog's own claim/complete write path (claim_minute_bar +
complete_artifact), not hand-crafted SQL, so the read projections are
verified against exactly what the write path produces.
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


@pytest.fixture
async def pool():
    # Force-reset any stale pool left by a prior test (different event loop).
    await catalog_client.close_pool()
    await catalog_client.init_pool()
    yield
    await catalog_client.close_pool()


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


async def test_observatory_selects_read_a_real_complete_artifact(clean_artifacts, pool):
    """One completed minute-bar row, read back through all four new SELECTs."""
    identity = _minute_identity()
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity,
        worker_id="w-1",
        lease_ttl_ms=300_000,
        data_contract_hash="a" * 64,
        file_path="equity/usa/minute/spy/20240520_trade.zip",
    )
    assert isinstance(artifact_id, int)
    await catalog_client.complete_artifact(
        artifact_id,
        row_count=390,
        first_bar_start_ms=1716196200000,
        last_bar_start_ms=1716219540000,
        file_size_bytes=123456,
        file_sha256="b" * 64,
    )

    # select_artifact_coverage: the completed day shows up with its real status.
    coverage = await catalog_client.select_artifact_coverage(
        market="usa",
        symbol="SPY",
        data_type="trade",
        provider="polygon",
        price_adjustment_mode="raw",
        start_trading_date=date(2024, 5, 20),
        end_trading_date=date(2024, 5, 24),
    )
    assert len(coverage) == 1
    assert coverage[0].trading_date == date(2024, 5, 20)
    assert coverage[0].status == "complete"
    assert coverage[0].artifact_id == artifact_id

    # select_artifact_by_id: full receipt round-trips through real Postgres,
    # including the jsonb ProviderParams decode.
    detail = await catalog_client.select_artifact_by_id(artifact_id)
    assert detail is not None
    assert detail.content_hash == "b" * 64
    assert detail.data_contract_hash == "a" * 64
    assert detail.file_size_bytes == 123456
    assert detail.provider_params == {}
    assert detail.status == "complete"
    assert detail.trading_date == date(2024, 5, 20)

    # select_storage_totals_by_kind: one complete time_series_bars/minute row.
    totals = await catalog_client.select_storage_totals_by_kind("usa")
    assert len(totals) == 1
    assert totals[0].artifact_kind == "time_series_bars"
    assert totals[0].resolution == "minute"
    assert totals[0].artifact_count == 1
    assert totals[0].total_bytes == 123456

    # select_symbol_coverage_spans: SPY's span is the single completed day.
    spans = await catalog_client.select_symbol_coverage_spans("usa")
    assert len(spans) == 1
    assert spans[0].symbol == "SPY"
    assert spans[0].first_trading_date == date(2024, 5, 20)
    assert spans[0].last_trading_date == date(2024, 5, 20)
    assert spans[0].artifact_count == 1


async def test_observatory_selects_honest_empty_on_a_truncated_catalog(clean_artifacts, pool):
    """No rows at all: every projection returns an empty list, not an error."""
    coverage = await catalog_client.select_artifact_coverage(
        market="usa",
        symbol="SPY",
        data_type="trade",
        provider="polygon",
        price_adjustment_mode="raw",
        start_trading_date=date(2024, 5, 20),
        end_trading_date=date(2024, 5, 24),
    )
    assert coverage == []
    assert await catalog_client.select_artifact_by_id(1) is None
    assert await catalog_client.select_storage_totals_by_kind("usa") == []
    assert await catalog_client.select_symbol_coverage_spans("usa") == []
