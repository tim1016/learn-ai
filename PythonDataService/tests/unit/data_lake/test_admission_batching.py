"""The batched admission relay: one set-based lookup for requests already pending (#3047).

Fast tier drives the real :class:`AdmissionBatcher` loop against a fake
lookup — no database — proving the batching contract: a lone request never
waits, requests pending together share one SQL round trip, duplicates
deduplicate only inside their batch (the next check is fresh), an outage
answers the whole batch once with the typed refusal, and the call bound
still fires. The database tier proves the set-based lookup answers exactly
what the per-file check answers, and that revocation lands on the very
next batch — including for unchanged bytes.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from uuid import UUID

import asyncpg
import pytest

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.admission import AdmissionBatcher, AdmissionCheck, LakeAdmissionError
from app.data_lake.catalog_client import CatalogUnavailableError

ROOT = UUID(int=3047)


def _check(n: int, *, digest: str | None = None) -> AdmissionCheck:
    return AdmissionCheck(
        data_root_id=ROOT,
        price_adjustment_mode="polygon_split_adjusted",
        file_path=f"equity/usa/minute/spy/{n}_trade.zip",
        file_sha256=digest or f"{n:064d}",
        file_size_bytes=1000 + n,
    )


def _admit_all(requests: list[tuple[UUID, str, str, str, int]]) -> frozenset[int]:
    return frozenset(range(len(requests)))


def _admit_none(requests: list[tuple[UUID, str, str, str, int]]) -> frozenset[int]:
    return frozenset()


def test_a_lone_request_never_waits_for_a_batch() -> None:
    """One pending request runs alone, immediately — no gathering delay (#3047 AC 1)."""
    calls: list[int] = []

    async def lookup(requests: list[tuple[UUID, str, str, str, int]]) -> frozenset[int]:
        calls.append(len(requests))
        return _admit_all(requests)

    batcher = AdmissionBatcher(pool_max_size=1, lookup=lookup)
    try:
        started = time.monotonic()
        assert batcher.submit(_check(1)) is True
        elapsed = time.monotonic() - started
    finally:
        batcher.close()

    assert calls == [1], "a lone request is one lookup, dispatched at once"
    assert elapsed < 5.0, "nothing waited for a second worker to join"


def test_requests_pending_together_share_one_lookup() -> None:
    """While one lookup is in flight, later arrivals queue behind it — and drain together (#3047 AC 1)."""
    first_in_flight = threading.Event()
    release_first = threading.Event()

    async def lookup(requests: list[tuple[UUID, str, str, str, int]]) -> frozenset[int]:
        if not first_in_flight.is_set():
            first_in_flight.set()
            assert release_first.wait(timeout=5.0), "the second submit landed while the first lookup ran"
            return _admit_all(requests)
        return _admit_all(requests[:1])  # only the first tuple of the shared batch admits

    batcher = AdmissionBatcher(pool_max_size=1, lookup=lookup)
    verdicts: dict[int, object] = {}
    try:
        first = threading.Thread(target=lambda: verdicts.__setitem__(1, batcher.submit(_check(1))), daemon=True)
        first.start()
        assert first_in_flight.wait(timeout=5.0), "the first lookup is in flight"

        second = threading.Thread(target=lambda: verdicts.__setitem__(2, batcher.submit(_check(2))), daemon=True)
        third = threading.Thread(target=lambda: verdicts.__setitem__(3, batcher.submit(_check(3))), daemon=True)
        second.start()
        third.start()
        time.sleep(0.1)  # both deposits land while the loop is busy inside the first lookup
        release_first.set()

        first.join(timeout=5.0)
        second.join(timeout=5.0)
        third.join(timeout=5.0)
    finally:
        release_first.set()
        batcher.close()

    assert verdicts == {1: True, 2: True, 3: False}, "each requester got its own decision from the shared lookup"
    facts = batcher.facts()
    assert facts["requests"] == 3 and facts["sql_lookups"] == 2, "three requests, two round trips — never three"
    assert facts["batch_sizes"].get(2) == 1, "the queued pair drained as one batch"


def test_duplicate_tuples_share_one_lookup_only_within_their_batch() -> None:
    """Identical tuples deduplicate for one lookup; the next check runs fresh (#3047 AC 2)."""
    seen: list[list[tuple[UUID, str, str, str, int]]] = []

    async def lookup(requests: list[tuple[UUID, str, str, str, int]]) -> frozenset[int]:
        seen.append(list(requests))
        return _admit_all(requests)

    batcher = AdmissionBatcher(pool_max_size=1, lookup=lookup)
    try:
        assert batcher.submit(_check(7)) is True
        assert batcher.submit(_check(7)) is True  # the same bytes, checked again: a fresh lookup
    finally:
        batcher.close()

    assert [len(requests) for requests in seen] == [1, 1], "no positive receipt survived the first round trip"
    facts = batcher.facts()
    assert facts["sql_lookups"] == 2 and facts["round_trips_saved"] == 0

    together = AdmissionBatcher(pool_max_size=1, lookup=lookup)
    try:
        first_in_flight = threading.Event()
        release = threading.Event()

        original = together._lookup

        async def gated(requests: list[tuple[UUID, str, str, str, int]]) -> frozenset[int]:
            if not first_in_flight.is_set():
                first_in_flight.set()
                assert release.wait(timeout=5.0)
            assert original is not None
            return await original(requests)

        together._lookup = gated
        twin_a = threading.Thread(target=lambda: together.submit(_check(9)), daemon=True)
        twin_b = threading.Thread(target=lambda: together.submit(_check(9)), daemon=True)
        twin_a.start()
        assert first_in_flight.wait(timeout=5.0)
        twin_b.start()
        time.sleep(0.1)
        release.set()
        twin_a.join(timeout=5.0)
        twin_b.join(timeout=5.0)
    finally:
        release.set()
        together.close()
    assert len(seen[-1]) == 1, "twins inside one batch deduplicated to one tuple"


def test_an_outage_refuses_every_pending_requester_promptly() -> None:
    """The catalog's typed refusal reaches each pending requester fast — never a full timeout per artifact (#3047 AC 5)."""
    attempts: list[int] = []
    in_flight = threading.Event()
    release = threading.Event()
    recovered = threading.Event()

    async def lookup(requests: list[tuple[UUID, str, str, str, int]]) -> frozenset[int]:
        attempts.append(len(requests))
        if not recovered.is_set():
            in_flight.set()
            assert release.wait(timeout=5.0)
            raise CatalogUnavailableError("catalog Postgres is unreachable")
        return _admit_all(requests)

    batcher = AdmissionBatcher(pool_max_size=1, lookup=lookup)
    failures: dict[int, BaseException | bool] = {}
    arrivals: dict[int, float] = {}
    try:
        def submit(n: int) -> None:
            started = time.monotonic()
            failures[n] = _capture(batcher.submit, _check(n))
            arrivals[n] = time.monotonic() - started

        first = threading.Thread(target=submit, args=(1,), daemon=True)
        first.start()
        assert in_flight.wait(timeout=5.0), "the failing lookup is in flight"

        # The outage's batch-mate queues behind the failing lookup; when the
        # outage lands, its own batch fails at once too — not after a full
        # call bound of waiting per artifact.
        second = threading.Thread(target=submit, args=(2,), daemon=True)
        second.start()
        time.sleep(0.1)
        release.set()

        first.join(timeout=5.0)
        second.join(timeout=5.0)

        # The next requester after recovery is served, not poisoned.
        recovered.set()
        assert batcher.submit(_check(3)) is True
    finally:
        release.set()
        batcher.close()

    assert isinstance(failures.get(1), CatalogUnavailableError), "the failing lookup's requester got the typed outage"
    assert isinstance(failures.get(2), CatalogUnavailableError), "the queued requester got the same typed outage"
    assert arrivals[2] < 5.0, "the second refusal arrived promptly, not at a full timeout"
    facts = batcher.facts()
    assert facts["outages"] == 2 and facts["sql_lookups"] == 1, "one outage attempt per batch, one serving lookup after recovery"


def _capture(submit: Callable[[AdmissionCheck], bool], check: AdmissionCheck) -> BaseException | bool:
    try:
        return submit(check)
    except BaseException as exc:  # the thread's only job is to park the failure for the assertion
        return exc


def test_the_call_bound_is_preserved() -> None:
    """A verdict that never arrives fails at the bound, with the relay's taxonomy (#3047)."""

    async def never(requests: list[tuple[UUID, str, str, str, int]]) -> frozenset[int]:
        threading.Event().wait()  # the lookup never returns
        return frozenset()

    batcher = AdmissionBatcher(pool_max_size=1, lookup=never, call_bound_s=0.1)
    try:
        started = time.monotonic()
        with pytest.raises(LakeAdmissionError, match="no admission verdict in time"):
            batcher.submit(_check(1))
        elapsed = time.monotonic() - started
    finally:
        batcher.close()
    assert elapsed < 5.0, "the bound fired promptly, not at some outer timeout"


# ── Database tier: set-based parity and revocation ─────────────────────────

def _postgres_url() -> str:
    import os

    url = settings.POSTGRES_URL or os.getenv("POSTGRES_URL", "")
    if not url:
        pytest.skip("POSTGRES_URL not configured")
    return url


@pytest.fixture
async def clean_artifacts():
    """Truncate DataLakeArtifacts before+after each test (the write-ops pattern)."""
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


async def _insert_receipt(
    *, root_id: UUID, mode: str, path: str, sha: str, size: int
) -> None:
    conn = await asyncpg.connect(_postgres_url())
    try:
        await conn.execute(
            """
            INSERT INTO "DataLakeArtifacts" (
                "ArtifactKind", "Market", "Symbol", "TradingDate", "Resolution", "DataType",
                "Provider", "ProviderParams", "PriceAdjustmentMode", "DataContractHash",
                "FilePath", "Status", "LeaseOwner", "LeaseExpiresAtMs", "LeaseGeneration",
                "AttemptCount", "FetchedAtMs", "DataRootId", "FileSha256", "FileSizeBytes"
            ) VALUES (
                'time_series_bars', 'usa', 'SPY', '2025-01-02', 'minute', 'trade',
                'polygon', '{}'::jsonb, $3, 'dc', $4, 'complete', NULL, NULL, 1,
                1, 1, $1, $2, $5
            )
            """,
            root_id, sha, mode, path, size,
        )
    finally:
        await conn.close()


async def _revoke_receipt(path: str) -> None:
    conn = await asyncpg.connect(_postgres_url())
    try:
        await conn.execute(
            "UPDATE \"DataLakeArtifacts\" SET \"Status\" = 'failed' WHERE \"FilePath\" = $1",
            path,
        )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_batched_decisions_match_individual_checks(clean_artifacts, pool) -> None:
    """The set-based lookup answers exactly the per-file check at the same boundary (#3047 AC 4)."""
    from app.data_lake import catalog_client

    admitted = _check(1)
    wrong_digest = _check(2, digest="f" * 64)
    wrong_size = AdmissionCheck(ROOT, admitted.price_adjustment_mode, admitted.file_path, admitted.file_sha256, 999_999)
    wrong_mode = AdmissionCheck(ROOT, "raw", admitted.file_path, admitted.file_sha256, admitted.file_size_bytes)
    wrong_path = AdmissionCheck(ROOT, admitted.price_adjustment_mode, "equity/elsewhere.zip", admitted.file_sha256, admitted.file_size_bytes)
    wrong_root = AdmissionCheck(UUID(int=99), admitted.price_adjustment_mode, admitted.file_path, admitted.file_sha256, admitted.file_size_bytes)

    await _insert_receipt(
        root_id=admitted.data_root_id, mode=admitted.price_adjustment_mode,
        path=admitted.file_path, sha=admitted.file_sha256, size=admitted.file_size_bytes,
    )

    checks = [admitted, wrong_digest, wrong_size, wrong_mode, wrong_path, wrong_root]
    batched = await catalog_client.has_committed_file_receipts([check.as_tuple() for check in checks])
    individual = [
        await catalog_client.has_committed_file_receipt(*check.as_tuple()) for check in checks
    ]
    assert sorted(batched) == sorted(index for index, present in enumerate(individual) if present)
    assert batched == frozenset({0}), "root, mode, path, hash, size and committed status all participate"


@pytest.mark.asyncio
async def test_revoking_a_receipt_refuses_the_next_batch_including_unchanged_bytes(clean_artifacts, pool) -> None:
    """No positive receipt survives a round trip: a revoked day refuses its next read (#3047 AC 3)."""
    from app.data_lake import catalog_client

    check = _check(5)
    await _insert_receipt(
        root_id=check.data_root_id, mode=check.price_adjustment_mode,
        path=check.file_path, sha=check.file_sha256, size=check.file_size_bytes,
    )
    assert await catalog_client.has_committed_file_receipts([check.as_tuple()]) == frozenset({0})

    await _revoke_receipt(check.file_path)

    assert await catalog_client.has_committed_file_receipts([check.as_tuple()]) == frozenset(), (
        "the identical tuple — same bytes, same digest — is refused on its very next check"
    )
