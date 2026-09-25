"""The one reclaim protocol a lost catalog claim goes through (#2452 review).

``catalog_client.reclaim_after_lost_claim`` is shared by minute bars, factor
files and metadata rows. Every scenario here runs twice: against the real
SQL on Postgres (skipped when ``POSTGRES_URL`` is unset) and against
``tests/_helpers/fake_lake_catalog.FakeCatalog`` — so the in-memory catalog
that every PR-gated ``ensure_data`` test drives is held to the answers the
SQL gives: an expired lease is stealable, a spent retry budget is terminal,
and a row another worker took a moment ago is contention.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from datetime import date

import pytest

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.catalog_client import ArtifactClaimState, ReclaimedLease, ReclaimRefused
from app.data_lake.types import ArtifactIdentity
from tests._helpers.fake_lake_catalog import FakeCatalog, install_fake_catalog

pytestmark = pytest.mark.asyncio

_MAX_RETRIES = 3
_TTL_MS = 300_000
_KINDS = ("time_series_bars", "factor_file")


def _identity(kind: str) -> ArtifactIdentity:
    if kind == "factor_file":
        return ArtifactIdentity(
            artifact_kind="factor_file", market="usa", symbol="SPY", provider="polygon", price_adjustment_mode="raw"
        )
    return ArtifactIdentity(
        artifact_kind="time_series_bars",
        market="usa",
        symbol="SPY",
        trading_date=date(2024, 5, 20),
        resolution="minute",
        data_type="trade",
        provider="polygon",
        price_adjustment_mode="raw",
    )


async def _claim(identity: ArtifactIdentity, worker_id: str) -> int:
    claim = (
        catalog_client.claim_corp_action_artifact
        if identity.artifact_kind == "factor_file"
        else catalog_client.claim_minute_bar
    )
    artifact_id = await claim(
        identity=identity, worker_id=worker_id, lease_ttl_ms=_TTL_MS, data_contract_hash="a" * 64, file_path="x"
    )
    assert artifact_id is not None
    return artifact_id


def _lookup(identity: ArtifactIdentity) -> Callable[[], Awaitable[ArtifactClaimState | None]]:
    if identity.artifact_kind == "factor_file":
        return lambda: catalog_client.select_corp_action_claim_state(identity)
    return lambda: catalog_client.select_minute_bar_claim_state(identity)


async def _fail(artifact_id: int, worker_id: str, generation: int = catalog_client.INITIAL_LEASE_GENERATION) -> None:
    assert await catalog_client.fail_artifact(
        artifact_id, "provider_api_error", worker_id=worker_id, lease_generation=generation
    )


async def _reclaim(
    read_claim_state: Callable[[], Awaitable[ArtifactClaimState | None]],
) -> ReclaimedLease | ReclaimRefused:
    return await catalog_client.reclaim_after_lost_claim(
        read_claim_state, worker_id="w-new", lease_ttl_ms=_TTL_MS, max_retries=_MAX_RETRIES
    )


# ---------------------------------------------------------------------------
# The two catalogs. Only what the claim_* / fail_artifact surface cannot do —
# age a lease, spend a retry budget, read the lease columns back — differs.
# ---------------------------------------------------------------------------


class _InMemory:
    def __init__(self, fake: FakeCatalog) -> None:
        self._fake = fake

    async def expire_lease(self, artifact_id: int) -> None:
        self._fake.rows[artifact_id]["lease_expires_at_ms"] = 1

    async def set_attempt_count(self, artifact_id: int, attempt_count: int) -> None:
        self._fake.rows[artifact_id]["attempt_count"] = attempt_count

    async def lease(self, artifact_id: int) -> tuple[str, str | None, int, int]:
        row = self._fake.rows[artifact_id]
        return row["status"], row["lease_owner"], row["lease_generation"], row["attempt_count"]


class _Postgres:
    async def expire_lease(self, artifact_id: int) -> None:
        async with catalog_client.connection() as conn:
            await conn.execute('UPDATE "DataLakeArtifacts" SET "LeaseExpiresAtMs" = 1 WHERE "Id" = $1', artifact_id)

    async def set_attempt_count(self, artifact_id: int, attempt_count: int) -> None:
        async with catalog_client.connection() as conn:
            await conn.execute(
                'UPDATE "DataLakeArtifacts" SET "AttemptCount" = $1 WHERE "Id" = $2', attempt_count, artifact_id
            )

    async def lease(self, artifact_id: int) -> tuple[str, str | None, int, int]:
        async with catalog_client.connection() as conn:
            row = await conn.fetchrow(
                'SELECT "Status", "LeaseOwner", "LeaseGeneration", "AttemptCount" FROM "DataLakeArtifacts" '
                'WHERE "Id" = $1',
                artifact_id,
            )
        assert row is not None
        return row["Status"], row["LeaseOwner"], row["LeaseGeneration"], row["AttemptCount"]


Catalog = _InMemory | _Postgres


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


async def _a_live_lease_is_contention(catalog: Catalog, kind: str) -> None:
    identity = _identity(kind)
    artifact_id = await _claim(identity, "w-orig")

    outcome = await _reclaim(_lookup(identity))

    assert outcome == ReclaimRefused(reason="lease_timeout", detail="another worker holds the lease", attempt_count=1)
    assert await catalog.lease(artifact_id) == ("fetching", "w-orig", 1, 1), "a live lease must be left alone"


async def _an_expired_lease_is_reclaimed(catalog: Catalog, kind: str) -> None:
    identity = _identity(kind)
    artifact_id = await _claim(identity, "w-dead")
    await catalog.expire_lease(artifact_id)

    outcome = await _reclaim(_lookup(identity))

    assert outcome == ReclaimedLease(artifact_id=artifact_id, lease_generation=2)
    assert await catalog.lease(artifact_id) == ("fetching", "w-new", 2, 2)


async def _a_failed_row_within_its_budget_is_reclaimed(catalog: Catalog, kind: str) -> None:
    identity = _identity(kind)
    artifact_id = await _claim(identity, "w-orig")
    await _fail(artifact_id, "w-orig")

    outcome = await _reclaim(_lookup(identity))

    assert outcome == ReclaimedLease(artifact_id=artifact_id, lease_generation=2)
    assert await catalog.lease(artifact_id) == ("fetching", "w-new", 2, 2)


async def _a_failed_row_past_its_budget_is_terminal(catalog: Catalog, kind: str) -> None:
    identity = _identity(kind)
    artifact_id = await _claim(identity, "w-orig")
    await _fail(artifact_id, "w-orig")
    await catalog.set_attempt_count(artifact_id, _MAX_RETRIES)

    outcome = await _reclaim(_lookup(identity))

    assert outcome == ReclaimRefused(
        reason="fetch_timeout",
        detail=f"exhausted {_MAX_RETRIES} attempt(s); last error: provider_api_error",
        attempt_count=_MAX_RETRIES,
    )
    assert await catalog.lease(artifact_id) == ("failed", None, 1, _MAX_RETRIES)


async def _a_row_reclaimed_between_lookup_and_steal_is_contention(catalog: Catalog, kind: str) -> None:
    """The race the re-read exists for. Two callers see the same failed,
    retryable row; the other one wins the steal. This caller's snapshot
    still says ``'failed'``, and classifying from it reported a row that is
    live — about to complete — as terminally exhausted."""
    identity = _identity(kind)
    artifact_id = await _claim(identity, "w-orig")
    await _fail(artifact_id, "w-orig")
    lookup = _lookup(identity)
    reads: list[ArtifactClaimState | None] = []

    async def _read_then_lose_the_row() -> ArtifactClaimState | None:
        state = await lookup()
        reads.append(state)
        if len(reads) == 1:
            won = await catalog_client.steal_or_retry_minute_bar(
                artifact_id=artifact_id, worker_id="w-sibling", lease_ttl_ms=_TTL_MS, max_retries=_MAX_RETRIES
            )
            assert won is not None
        return state

    outcome = await _reclaim(_read_then_lose_the_row)

    assert outcome == ReclaimRefused(reason="lease_timeout", detail="another worker holds the lease", attempt_count=1)
    assert reads[0] is not None and reads[0].status == "failed", "the snapshot this caller decided from was stale"
    assert len(reads) == 2, "a refused steal must be classified from a re-read, not the snapshot"
    assert await catalog.lease(artifact_id) == ("fetching", "w-sibling", 2, 2)


async def _a_row_that_is_not_there_is_contention(catalog: Catalog, kind: str) -> None:
    outcome = await _reclaim(_lookup(_identity(kind)))

    assert outcome == ReclaimRefused(reason="lease_timeout", detail="another worker holds the lease", attempt_count=1)


async def _an_exempt_error_is_retried_past_the_ceiling(catalog: Catalog, kind: str) -> None:
    """The metadata bootstrap's ``launcher_unreachable`` (#1889)."""
    identity = _identity(kind)
    artifact_id = await _claim(identity, "w-orig")
    assert await catalog_client.fail_artifact(
        artifact_id,
        "launcher_unreachable",
        worker_id="w-orig",
        lease_generation=catalog_client.INITIAL_LEASE_GENERATION,
    )
    await catalog.set_attempt_count(artifact_id, _MAX_RETRIES + 5)

    outcome = await catalog_client.reclaim_after_lost_claim(
        _lookup(identity),
        worker_id="w-new",
        lease_ttl_ms=_TTL_MS,
        max_retries=_MAX_RETRIES,
        retry_ceiling_exempt_errors=frozenset({"launcher_unreachable"}),
    )

    assert outcome == ReclaimedLease(artifact_id=artifact_id, lease_generation=2)


_SCENARIOS = pytest.mark.parametrize(
    "scenario",
    [
        _a_live_lease_is_contention,
        _an_expired_lease_is_reclaimed,
        _a_failed_row_within_its_budget_is_reclaimed,
        _a_failed_row_past_its_budget_is_terminal,
        _a_row_reclaimed_between_lookup_and_steal_is_contention,
        _a_row_that_is_not_there_is_contention,
        _an_exempt_error_is_retried_past_the_ceiling,
    ],
    ids=lambda scenario: scenario.__name__.strip("_"),
)
_EACH_KIND = pytest.mark.parametrize("kind", _KINDS)


@_SCENARIOS
@_EACH_KIND
async def test_reclaim_after_lost_claim_in_memory(scenario, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    await scenario(_InMemory(install_fake_catalog(monkeypatch)), kind)


def _postgres_url() -> str:
    url = settings.POSTGRES_URL or os.getenv("POSTGRES_URL", "")
    if not url:
        pytest.skip("POSTGRES_URL not configured")
    return url


@pytest.fixture
async def clean_artifacts():
    """Truncate DataLakeArtifacts before and after (guarded by conftest's ephemeral-target check)."""
    _postgres_url()
    await catalog_client.close_pool()
    await catalog_client.init_pool()
    async with catalog_client.connection() as conn:
        await conn.execute('TRUNCATE TABLE "DataLakeArtifacts" RESTART IDENTITY CASCADE')
    yield
    async with catalog_client.connection() as conn:
        await conn.execute('TRUNCATE TABLE "DataLakeArtifacts" RESTART IDENTITY CASCADE')
    await catalog_client.close_pool()


@_SCENARIOS
@_EACH_KIND
async def test_reclaim_after_lost_claim_on_postgres(scenario, kind: str, clean_artifacts) -> None:
    await scenario(_Postgres(), kind)
