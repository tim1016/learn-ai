"""Root-dimension test matrix (issue #1876 PR A, issue #1878 PR B of #1861).

Proves every artifact kind — minute bars, aggregated bars, corporate
actions, metadata — carries the root dimension through catalog_client:
every claim records the identity's ``data_root_id`` on the row, and every
identity/coverage read is scoped by it, not just minute bars.

PR A (#1876) kept the old mode-only partial unique indexes in place, so a
lookup scoped to a *different* root than the one a row was claimed under
correctly failed to find it, but two rows could not yet coexist for the
same identity tuple differing only by root. PR B (#1878) rebuilds those
indexes with ``DataRootId`` leading — the ``TestCrossRootCoexistence``
class below is the direct proof that a claim for an identity already taken
in one root still succeeds in a different root, which is the regression
#1861 exists to prevent: importing into a second physical root must never
make the first root appear covered, nor collide with it.

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


class TestCrossRootCoexistence:
    """Issue #1878 (PR B of #1861): the same non-root identity dimensions,
    claimed under two different roots, must both succeed — the index
    rebuild's whole point. Every ``claim_*`` function's ``ON CONFLICT``
    target now leads with ``DataRootId``, so a second claim for an identity
    already taken in root A no longer collides with root A's row just
    because every *other* dimension matches; it only collides with a prior
    claim in its *own* root (proven by the same-root dedup cases below).
    """

    # All four claim_* functions share the same call shape (identity,
    # worker_id, lease_ttl_ms, data_contract_hash, file_path), so the
    # cross-root-coexistence property is exercised once per family via
    # parametrize rather than four near-identical test bodies.
    @pytest.mark.parametrize(
        "claim_fn,identity_fn,data_contract_hash,file_path",
        [
            pytest.param(catalog_client.claim_minute_bar, _minute_bar_identity, "a" * 64, "x.zip", id="minute_bar"),
            pytest.param(
                catalog_client.claim_aggregated_bar_artifact,
                _aggregated_bar_identity,
                "b" * 64,
                "equity/usa/daily/spy.zip",
                id="aggregated_bar",
            ),
            pytest.param(
                catalog_client.claim_corp_action_artifact,
                _corp_action_identity,
                "c" * 64,
                "equity/usa/factor_files/spy.csv",
                id="corp_action",
            ),
            pytest.param(
                catalog_client.claim_metadata_artifact,
                _metadata_identity,
                "d" * 64,
                "market-hours-database.json",
                id="metadata",
            ),
        ],
    )
    async def test_same_identity_different_root_both_claims_succeed(
        self, clean_artifacts, pool, claim_fn, identity_fn, data_contract_hash, file_path
    ):
        a = await claim_fn(
            identity=identity_fn(_ROOT_A),
            worker_id="w-1",
            lease_ttl_ms=300_000,
            data_contract_hash=data_contract_hash,
            file_path=file_path,
        )
        b = await claim_fn(
            identity=identity_fn(_ROOT_B),
            worker_id="w-1",
            lease_ttl_ms=300_000,
            data_contract_hash=data_contract_hash,
            file_path=file_path,
        )

        assert a is not None
        assert b is not None
        assert a != b

    async def test_minute_bar_same_identity_same_root_second_claim_still_dedupes(self, clean_artifacts, pool):
        a = await catalog_client.claim_minute_bar(
            identity=_minute_bar_identity(_ROOT_A),
            worker_id="w-1",
            lease_ttl_ms=300_000,
            data_contract_hash="a" * 64,
            file_path="x.zip",
        )
        b = await catalog_client.claim_minute_bar(
            identity=_minute_bar_identity(_ROOT_A),
            worker_id="w-2",
            lease_ttl_ms=300_000,
            data_contract_hash="a" * 64,
            file_path="x.zip",
        )

        assert a is not None
        assert b is None  # same root, same identity -- still a genuine conflict

    async def test_raw_and_split_adjusted_modes_still_coexist_post_migration(self, clean_artifacts, pool):
        """Regression guard: the mode-coexistence property #1839 established
        (raw and polygon_split_adjusted claim independently) must survive
        the index rebuild -- PriceAdjustmentMode is still a full member of
        every rebuilt key, just no longer the leading one."""
        raw_identity = _minute_bar_identity(_ROOT_A)
        adjusted_identity = ArtifactIdentity(
            artifact_kind="time_series_bars",
            market="usa",
            symbol="SPY",
            trading_date=date(2024, 5, 20),
            resolution="minute",
            data_type="trade",
            provider="polygon",
            price_adjustment_mode="polygon_split_adjusted",
            data_root_id=_ROOT_A,
        )

        raw_claim = await catalog_client.claim_minute_bar(
            identity=raw_identity, worker_id="w-1", lease_ttl_ms=300_000, data_contract_hash="a" * 64, file_path="x.zip"
        )
        adjusted_claim = await catalog_client.claim_minute_bar(
            identity=adjusted_identity, worker_id="w-1", lease_ttl_ms=300_000, data_contract_hash="a" * 64, file_path="x.zip"
        )

        assert raw_claim is not None
        assert adjusted_claim is not None
        assert raw_claim != adjusted_claim

    async def test_storage_totals_isolated_per_root_even_with_coexisting_identities(self, clean_artifacts, pool):
        """Extends TestObservatoryRootScoping's isolation proof to the
        coexistence case specifically: the *same* identity claimed and
        completed in both roots must not have its bytes double-counted or
        cross-attributed -- each root's total reflects only its own row."""
        artifact_id_a = await catalog_client.claim_aggregated_bar_artifact(
            identity=_aggregated_bar_identity(_ROOT_A),
            worker_id="w-1",
            lease_ttl_ms=300_000,
            data_contract_hash="e" * 64,
            file_path="equity/usa/daily/spy.zip",
        )
        artifact_id_b = await catalog_client.claim_aggregated_bar_artifact(
            identity=_aggregated_bar_identity(_ROOT_B),
            worker_id="w-1",
            lease_ttl_ms=300_000,
            data_contract_hash="e" * 64,
            file_path="equity/usa/daily/spy.zip",
        )
        await catalog_client.complete_artifact(
            artifact_id=artifact_id_a, row_count=1, first_bar_start_ms=0, last_bar_start_ms=1,
            file_size_bytes=100, file_sha256="e" * 64,
        )
        await catalog_client.complete_artifact(
            artifact_id=artifact_id_b, row_count=1, first_bar_start_ms=0, last_bar_start_ms=1,
            file_size_bytes=999, file_sha256="e" * 64,
        )

        totals_a = await catalog_client.select_storage_totals_by_kind("usa", data_root_id=_ROOT_A)
        totals_b = await catalog_client.select_storage_totals_by_kind("usa", data_root_id=_ROOT_B)

        assert sum(t.total_bytes for t in totals_a) == 100
        assert sum(t.total_bytes for t in totals_b) == 999

    async def test_importing_into_root_b_leaves_root_as_availability_unchanged(self, clean_artifacts, pool):
        """The regression #1861 exists to prevent, stated directly: claiming
        (importing) an identity into root B after root A already has the
        identical identity complete must not change what root A reports as
        covered."""
        artifact_id_a = await catalog_client.claim_minute_bar(
            identity=_minute_bar_identity(_ROOT_A), worker_id="w-1", lease_ttl_ms=300_000,
            data_contract_hash="a" * 64, file_path="x.zip",
        )
        await catalog_client.complete_artifact(
            artifact_id=artifact_id_a, row_count=1, first_bar_start_ms=0, last_bar_start_ms=1,
            file_size_bytes=10, file_sha256="a" * 64,
        )
        coverage_a_before = await catalog_client.select_coverage_minute_bars(
            market="usa", symbol="SPY", data_type="trade",
            start_trading_date=date(2024, 5, 20), end_trading_date=date(2024, 5, 20),
            price_adjustment_mode="raw", data_root_id=_ROOT_A,
        )

        artifact_id_b = await catalog_client.claim_minute_bar(
            identity=_minute_bar_identity(_ROOT_B), worker_id="w-1", lease_ttl_ms=300_000,
            data_contract_hash="a" * 64, file_path="x.zip",
        )
        await catalog_client.complete_artifact(
            artifact_id=artifact_id_b, row_count=1, first_bar_start_ms=0, last_bar_start_ms=1,
            file_size_bytes=10, file_sha256="a" * 64,
        )
        coverage_a_after = await catalog_client.select_coverage_minute_bars(
            market="usa", symbol="SPY", data_type="trade",
            start_trading_date=date(2024, 5, 20), end_trading_date=date(2024, 5, 20),
            price_adjustment_mode="raw", data_root_id=_ROOT_A,
        )

        assert [r.id for r in coverage_a_before] == [r.id for r in coverage_a_after]
