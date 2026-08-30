"""Root-dimension test matrix (issue #1876, PR A of #1861).

Proves every artifact kind — minute bars, aggregated bars, corporate
actions, metadata — carries the root dimension through catalog_client:
every claim records the identity's ``data_root_id`` on the row, and every
identity/coverage read is scoped by it, not just minute bars.

The existing partial unique indexes are kept as-is this slice (PR B adds
multi-root uniqueness), so two rows cannot yet coexist for the same
identity tuple differing only by root. The load-bearing property this file
proves instead — the one that actually matters for correctness today — is
that a lookup scoped to a *different* root than the one a row was claimed
under does not find that row, even though every other identity dimension
matches exactly. That is what "every identity/coverage catalog query
filters by root UUID" (acceptance criterion) means in a single-populated-
root world.

Live-Postgres tests, same skip/cleanup pattern as test_catalog_write_ops.py.
"""

from __future__ import annotations

import os
from datetime import date
from uuid import UUID

import asyncpg
import pytest

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.types import ArtifactIdentity

pytestmark = pytest.mark.asyncio

_ROOT_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_ROOT_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


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
    await catalog_client.close_pool()
    await catalog_client.init_pool()
    yield
    await catalog_client.close_pool()


def _minute_bar_identity(root_id: UUID) -> ArtifactIdentity:
    return ArtifactIdentity(
        artifact_kind="time_series_bars",
        market="usa",
        symbol="SPY",
        trading_date=date(2024, 5, 20),
        resolution="minute",
        data_type="trade",
        provider="polygon",
        price_adjustment_mode="raw",
        data_root_id=root_id,
    )


def _aggregated_bar_identity(root_id: UUID) -> ArtifactIdentity:
    return ArtifactIdentity(
        artifact_kind="time_series_bars",
        market="usa",
        symbol="SPY",
        resolution="daily",
        data_type="trade",
        provider="polygon",
        price_adjustment_mode="raw",
        data_root_id=root_id,
    )


def _corp_action_identity(root_id: UUID) -> ArtifactIdentity:
    return ArtifactIdentity(
        artifact_kind="factor_file",
        market="usa",
        symbol="SPY",
        provider="polygon",
        price_adjustment_mode="raw",
        data_root_id=root_id,
    )


def _metadata_identity(root_id: UUID) -> ArtifactIdentity:
    return ArtifactIdentity(
        artifact_kind="metadata",
        provider="lean_image_extract",
        data_root_id=root_id,
    )


class TestMinuteBarRootScoping:
    async def test_claim_records_the_identitys_root(self, clean_artifacts, pool):
        artifact_id = await catalog_client.claim_minute_bar(
            identity=_minute_bar_identity(_ROOT_A),
            worker_id="w-1",
            lease_ttl_ms=300_000,
            data_contract_hash="a" * 64,
            file_path="equity/usa/minute/spy/20240520_trade.zip",
        )
        detail = await catalog_client.select_artifact_by_id(artifact_id)
        assert detail is not None
        assert detail.data_root_id == _ROOT_A

    async def test_coverage_read_is_invisible_from_a_different_root(self, clean_artifacts, pool):
        await catalog_client.claim_minute_bar(
            identity=_minute_bar_identity(_ROOT_A),
            worker_id="w-1",
            lease_ttl_ms=300_000,
            data_contract_hash="a" * 64,
            file_path="x.zip",
        )
        await catalog_client.complete_artifact(
            artifact_id=(await catalog_client.select_minute_bar_claim_state(_minute_bar_identity(_ROOT_A))).id,
            row_count=1,
            first_bar_start_ms=0,
            last_bar_start_ms=1,
            file_size_bytes=10,
            file_sha256="a" * 64,
        )

        seen_by_a = await catalog_client.select_coverage_minute_bars(
            market="usa",
            symbol="SPY",
            data_type="trade",
            start_trading_date=date(2024, 5, 20),
            end_trading_date=date(2024, 5, 20),
            price_adjustment_mode="raw",
            data_root_id=_ROOT_A,
        )
        seen_by_b = await catalog_client.select_coverage_minute_bars(
            market="usa",
            symbol="SPY",
            data_type="trade",
            start_trading_date=date(2024, 5, 20),
            end_trading_date=date(2024, 5, 20),
            price_adjustment_mode="raw",
            data_root_id=_ROOT_B,
        )

        assert len(seen_by_a) == 1
        assert seen_by_b == []

    async def test_claim_state_lookup_is_root_scoped(self, clean_artifacts, pool):
        await catalog_client.claim_minute_bar(
            identity=_minute_bar_identity(_ROOT_A),
            worker_id="w-1",
            lease_ttl_ms=300_000,
            data_contract_hash="a" * 64,
            file_path="x.zip",
        )

        assert await catalog_client.select_minute_bar_claim_state(_minute_bar_identity(_ROOT_A)) is not None
        assert await catalog_client.select_minute_bar_claim_state(_minute_bar_identity(_ROOT_B)) is None

    async def test_lease_status_lookup_is_root_scoped(self, clean_artifacts, pool):
        await catalog_client.claim_minute_bar(
            identity=_minute_bar_identity(_ROOT_A),
            worker_id="w-1",
            lease_ttl_ms=300_000,
            data_contract_hash="a" * 64,
            file_path="x.zip",
        )

        assert await catalog_client.select_minute_bar_lease_status(_minute_bar_identity(_ROOT_A)) is not None
        assert await catalog_client.select_minute_bar_lease_status(_minute_bar_identity(_ROOT_B)) is None


class TestAggregatedBarRootScoping:
    async def test_claim_records_the_identitys_root(self, clean_artifacts, pool):
        artifact_id = await catalog_client.claim_aggregated_bar_artifact(
            identity=_aggregated_bar_identity(_ROOT_A),
            worker_id="w-1",
            lease_ttl_ms=300_000,
            data_contract_hash="b" * 64,
            file_path="equity/usa/daily/spy.zip",
        )
        detail = await catalog_client.select_artifact_by_id(artifact_id)
        assert detail is not None
        assert detail.data_root_id == _ROOT_A

    async def test_complete_lookup_is_invisible_from_a_different_root(self, clean_artifacts, pool):
        artifact_id = await catalog_client.claim_aggregated_bar_artifact(
            identity=_aggregated_bar_identity(_ROOT_A),
            worker_id="w-1",
            lease_ttl_ms=300_000,
            data_contract_hash="b" * 64,
            file_path="equity/usa/daily/spy.zip",
        )
        await catalog_client.complete_artifact(
            artifact_id=artifact_id,
            row_count=1,
            first_bar_start_ms=0,
            last_bar_start_ms=1,
            file_size_bytes=10,
            file_sha256="b" * 64,
        )

        assert await catalog_client.select_complete_aggregated_bar_artifact(_aggregated_bar_identity(_ROOT_A)) is not None
        assert await catalog_client.select_complete_aggregated_bar_artifact(_aggregated_bar_identity(_ROOT_B)) is None


class TestCorpActionRootScoping:
    async def test_claim_records_the_identitys_root(self, clean_artifacts, pool):
        artifact_id = await catalog_client.claim_corp_action_artifact(
            identity=_corp_action_identity(_ROOT_A),
            worker_id="w-1",
            lease_ttl_ms=300_000,
            data_contract_hash="c" * 64,
            file_path="equity/usa/factor_files/spy.csv",
        )
        detail = await catalog_client.select_artifact_by_id(artifact_id)
        assert detail is not None
        assert detail.data_root_id == _ROOT_A

    async def test_complete_lookup_is_invisible_from_a_different_root(self, clean_artifacts, pool):
        artifact_id = await catalog_client.claim_corp_action_artifact(
            identity=_corp_action_identity(_ROOT_A),
            worker_id="w-1",
            lease_ttl_ms=300_000,
            data_contract_hash="c" * 64,
            file_path="equity/usa/factor_files/spy.csv",
        )
        await catalog_client.complete_artifact(
            artifact_id=artifact_id,
            row_count=1,
            first_bar_start_ms=0,
            last_bar_start_ms=1,
            file_size_bytes=10,
            file_sha256="c" * 64,
        )

        assert await catalog_client.select_complete_corp_action_artifact(_corp_action_identity(_ROOT_A)) is not None
        assert await catalog_client.select_complete_corp_action_artifact(_corp_action_identity(_ROOT_B)) is None


class TestMetadataRootScoping:
    async def test_claim_records_the_identitys_root(self, clean_artifacts, pool):
        artifact_id = await catalog_client.claim_metadata_artifact(
            identity=_metadata_identity(_ROOT_A),
            worker_id="w-1",
            lease_ttl_ms=300_000,
            data_contract_hash="d" * 64,
            file_path="market-hours-database.json",
        )
        detail = await catalog_client.select_artifact_by_id(artifact_id)
        assert detail is not None
        assert detail.data_root_id == _ROOT_A

    async def test_complete_lookup_is_invisible_from_a_different_root(self, clean_artifacts, pool):
        artifact_id = await catalog_client.claim_metadata_artifact(
            identity=_metadata_identity(_ROOT_A),
            worker_id="w-1",
            lease_ttl_ms=300_000,
            data_contract_hash="d" * 64,
            file_path="market-hours-database.json",
        )
        await catalog_client.complete_artifact(
            artifact_id=artifact_id,
            row_count=1,
            first_bar_start_ms=0,
            last_bar_start_ms=1,
            file_size_bytes=10,
            file_sha256="d" * 64,
        )

        assert await catalog_client.select_complete_metadata_artifact("d" * 64, data_root_id=_ROOT_A) is not None
        assert await catalog_client.select_complete_metadata_artifact("d" * 64, data_root_id=_ROOT_B) is None

    async def test_claim_state_lookup_is_root_scoped(self, clean_artifacts, pool):
        artifact_id = await catalog_client.claim_metadata_artifact(
            identity=_metadata_identity(_ROOT_A),
            worker_id="w-1",
            lease_ttl_ms=300_000,
            data_contract_hash="d" * 64,
            file_path="market-hours-database.json",
        )
        await catalog_client.fail_artifact(artifact_id=artifact_id, last_error="provider_api_error")

        assert (await catalog_client.select_metadata_claim_state("d" * 64, data_root_id=_ROOT_A)) is not None
        assert (await catalog_client.select_metadata_claim_state("d" * 64, data_root_id=_ROOT_B)) is None


class TestObservatoryRootScoping:
    """Storage summaries and the coverage-by-day projection are also
    active-root-default listings (issue #1876)."""

    async def test_storage_totals_scoped_by_root(self, clean_artifacts, pool):
        artifact_id = await catalog_client.claim_aggregated_bar_artifact(
            identity=_aggregated_bar_identity(_ROOT_A),
            worker_id="w-1",
            lease_ttl_ms=300_000,
            data_contract_hash="e" * 64,
            file_path="equity/usa/daily/spy.zip",
        )
        await catalog_client.complete_artifact(
            artifact_id=artifact_id,
            row_count=1,
            first_bar_start_ms=0,
            last_bar_start_ms=1,
            file_size_bytes=123,
            file_sha256="e" * 64,
        )

        totals_a = await catalog_client.select_storage_totals_by_kind("usa", data_root_id=_ROOT_A)
        totals_b = await catalog_client.select_storage_totals_by_kind("usa", data_root_id=_ROOT_B)

        assert sum(t.total_bytes for t in totals_a) == 123
        assert totals_b == []

    async def test_symbol_coverage_spans_scoped_by_root(self, clean_artifacts, pool):
        artifact_id = await catalog_client.claim_minute_bar(
            identity=_minute_bar_identity(_ROOT_A),
            worker_id="w-1",
            lease_ttl_ms=300_000,
            data_contract_hash="f" * 64,
            file_path="x.zip",
        )
        await catalog_client.complete_artifact(
            artifact_id=artifact_id,
            row_count=1,
            first_bar_start_ms=0,
            last_bar_start_ms=1,
            file_size_bytes=10,
            file_sha256="f" * 64,
        )

        spans_a = await catalog_client.select_symbol_coverage_spans("usa", data_root_id=_ROOT_A)
        spans_b = await catalog_client.select_symbol_coverage_spans("usa", data_root_id=_ROOT_B)

        assert len(spans_a) == 1
        assert spans_b == []

    async def test_artifact_coverage_scoped_by_root(self, clean_artifacts, pool):
        artifact_id = await catalog_client.claim_minute_bar(
            identity=_minute_bar_identity(_ROOT_A),
            worker_id="w-1",
            lease_ttl_ms=300_000,
            data_contract_hash="g" * 64,
            file_path="x.zip",
        )
        await catalog_client.complete_artifact(
            artifact_id=artifact_id,
            row_count=1,
            first_bar_start_ms=0,
            last_bar_start_ms=1,
            file_size_bytes=10,
            file_sha256="g" * 64,
        )

        coverage_a = await catalog_client.select_artifact_coverage(
            market="usa",
            symbol="SPY",
            data_type="trade",
            provider="polygon",
            price_adjustment_mode="raw",
            start_trading_date=date(2024, 5, 20),
            end_trading_date=date(2024, 5, 20),
            data_root_id=_ROOT_A,
        )
        coverage_b = await catalog_client.select_artifact_coverage(
            market="usa",
            symbol="SPY",
            data_type="trade",
            provider="polygon",
            price_adjustment_mode="raw",
            start_trading_date=date(2024, 5, 20),
            end_trading_date=date(2024, 5, 20),
            data_root_id=_ROOT_B,
        )

        assert len(coverage_a) == 1
        assert coverage_b == []

    async def test_artifact_by_id_crosses_roots_and_reports_which_one_answered(self, clean_artifacts, pool):
        """select_artifact_by_id is deliberately not root-scoped (issue
        #1876) — an Id lookup is already unambiguous, but the response must
        still say which root the row belongs to."""
        artifact_id = await catalog_client.claim_minute_bar(
            identity=_minute_bar_identity(_ROOT_B),
            worker_id="w-1",
            lease_ttl_ms=300_000,
            data_contract_hash="h" * 64,
            file_path="x.zip",
        )

        detail = await catalog_client.select_artifact_by_id(artifact_id)

        assert detail is not None
        assert detail.data_root_id == _ROOT_B
