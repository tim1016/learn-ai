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
    await catalog_client.fail_artifact(artifact_id=artifact_id, last_error="provider_no_data")

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

    prior = await catalog_client.refresh_complete_artifact(
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
    )
    await catalog_client.refresh_complete_artifact(artifact_id=artifact_id, worker_id="w-1", lease_ttl_ms=300_000)
    await catalog_client.complete_artifact(
        artifact_id=artifact_id,
        row_count=500,
        first_bar_start_ms=1,
        last_bar_start_ms=3,
        file_size_bytes=200,
        file_sha256="c" * 64,
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
    )
    await catalog_client.refresh_complete_artifact(artifact_id=artifact_id, worker_id="w-1", lease_ttl_ms=300_000)

    restored = await catalog_client.restore_complete_artifact(artifact_id=artifact_id, worker_id="w-1")
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
    )
    await catalog_client.refresh_complete_artifact(artifact_id=artifact_id, worker_id="w-1", lease_ttl_ms=300_000)

    restored = await catalog_client.restore_complete_artifact(artifact_id=artifact_id, worker_id="w-2")
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
    )
    restored = await catalog_client.restore_complete_artifact(artifact_id=artifact_id, worker_id="w-1")
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
    await catalog_client.fail_artifact(artifact_id=artifact_id, last_error="provider_api_error")

    state = await catalog_client.select_metadata_claim_state("d" * 64)
    assert state is not None
    assert state.id == artifact_id
    assert state.status == "failed"
    assert state.attempt_count == 1

    reclaimed = await catalog_client.steal_or_retry_minute_bar(
        artifact_id=state.id,
        worker_id="w-2",
        lease_ttl_ms=300_000,
        max_retries=3,
    )
    assert reclaimed is True


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
