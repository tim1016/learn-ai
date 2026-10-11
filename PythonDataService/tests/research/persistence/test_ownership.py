"""Research runtime ownership: singleton authority, generation-stamped jobs, scoped recovery (#3042).

Drives the real ownership module against ephemeral Postgres and the sweep
against FakeRedis: the API's restart skips a live coordinator's jobs, a
duplicate coordinator cannot become ready, a lost lock connection fences the
generation, and legacy rows keep today's lifecycle.
"""

from __future__ import annotations

import asyncio

import asyncpg
import pytest

from app.jobs import progress
from app.research.persistence import ownership
from tests.jobs.conftest import _FakeRedis
from tests.research.conftest import _ephemeral_url


async def _clear_jobs(conn: asyncpg.Connection, *job_ids: str) -> None:
    """Rows from a previous run of this file would collide; each test owns its ids."""
    await conn.execute("DELETE FROM research_job_owners WHERE job_id = ANY($1)", list(job_ids))


def _sync(protected):
    """The sweep takes a sync callable; wrap the async ownership lookup for a worker thread."""

    def read() -> set[str]:
        return asyncio.run(protected())

    return read


async def test_a_second_coordinator_cannot_become_ready_while_the_first_holds_authority() -> None:
    dsn = _ephemeral_url()
    from app.research.persistence.schema import ensure_schema

    setup = await asyncpg.connect(dsn)
    try:
        await ensure_schema(setup)
    finally:
        await setup.close()
    first = await ownership.claim_coordinator_authority(dsn=dsn, instance_id="research-1", role="research")
    # The singleton row persists across deployments; only the advance is this test's.",
    assert first.boot_generation >= 1

    with pytest.raises(ownership.CoordinatorHeldElsewhere, match="another research coordinator"):
        await ownership.claim_coordinator_authority(dsn=dsn, instance_id="research-2", role="research")

    await first.release()
    second = await ownership.claim_coordinator_authority(dsn=dsn, instance_id="research-2", role="research")
    assert second.boot_generation == first.boot_generation + 1, "a released runtime is dead; the next claim advances past it"
    await second.release()


async def test_the_sweep_skips_a_live_coordinators_jobs_and_closes_older_generations_and_legacy(
    conn: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    dsn = _ephemeral_url()
    fake = _FakeRedis()
    monkeypatch.setattr(progress, "get_redis", lambda: fake)
    await _clear_jobs(conn, "job-live", "job-dead")

    authority = await ownership.claim_coordinator_authority(dsn=dsn, instance_id="research-1", role="research")
    try:
        await ownership.record_dispatch(conn, authority, job_id="job-live", kind="golden_search", record_id="study-1")
        # A job of a dead generation (an older runtime that lost the lock).
        await conn.execute(
            """
            INSERT INTO research_job_owners (job_id, kind, record_id, owner_instance_id, boot_generation, dispatched_at_ms)
            VALUES ('job-dead', 'grid_search', 'search-1', 'research-0', $1, 0)
            """,
            authority.boot_generation - 1,
        )
        # job-legacy is a job with no ownership row at all.
        for job_id, status in (("job-live", "running"), ("job-dead", "queued"), ("job-legacy", "running")):
            fake.job(job_id, status)
            fake.lease(job_id)

        async def protected() -> set[str]:
            probe = await asyncpg.connect(dsn)
            try:
                return await ownership.protected_job_ids(probe)
            finally:
                await probe.close()

        failed = await asyncio.to_thread(progress.fail_jobs_without_a_worker, _sync(protected))
    finally:
        await authority.release()
        await _clear_jobs(conn, "job-live", "job-dead")

    assert failed == ["job-dead", "job-legacy"], "only dead-generation and legacy work is closed"
    assert fake.hashes.get("job:job-live:state") == {"id": "job-live", "status": "running"}, (
        f"the live coordinator's job record is untouched: {fake.hashes.get('job-live:state')}"
    )
    assert fake.strings.get("job:job-live:lease") == "1", "…and so is its lease"
    assert fake.hashes["job:job-dead:state"]["status"] == "failed"
    assert fake.hashes["job:job-legacy:state"]["status"] == "failed"
    assert "job:job-dead:lease" not in fake.strings and "job:job-legacy:lease" not in fake.strings


async def test_lost_authority_connection_fences_the_generation(conn: asyncpg.Connection) -> None:
    dsn = _ephemeral_url()
    authority = await ownership.claim_coordinator_authority(dsn=dsn, instance_id="research-1", role="research")
    ownership.set_process_authority(authority)
    successor = None
    try:
        await authority.connection.close()  # the network died; the lock died with it
        assert not authority.connected

        with pytest.raises(ownership.CoordinatorHeldElsewhere, match="authority connection was lost"):
            await ownership.stamp_dispatch(job_id="job-x", kind="golden_search", record_id="study-1")

        # Reconnecting cannot resurrect the generation: a fresh claim mints a new one.
        ownership.set_process_authority(None)
        successor = await ownership.claim_coordinator_authority(dsn=dsn, instance_id="research-1b", role="research")
        assert successor.boot_generation == authority.boot_generation + 1
    finally:
        ownership.set_process_authority(None)
        if successor is not None:
            await successor.release()


async def test_ownership_never_touches_the_scientific_identity(conn: asyncpg.Connection) -> None:
    from app.research.golden_search.evaluator import context_digest, evaluation_key

    authority = ownership.RuntimeAuthority(
        instance_id="i", role="research", boot_generation=7, connection=None  # type: ignore[arg-type]
    )
    before = evaluation_key("digest", "p" * 64, (0, 1), "base", False)
    try:
        await ownership.record_dispatch(conn, authority, job_id="job-i", kind="recency", record_id="launch-1")
        assert evaluation_key("digest", "p" * 64, (0, 1), "base", False) == before
    finally:
        await _clear_jobs(conn, "job-i")
    assert context_digest({"strategy_key": "s", "code_identity": {}}) == context_digest({"strategy_key": "s", "code_identity": {}})


async def test_dispatch_refusal_when_a_live_coordinator_outranks_this_process() -> None:
    ownership.set_process_authority(None, held_elsewhere=True)
    try:
        with pytest.raises(ownership.CoordinatorHeldElsewhere, match="singleton"):
            await ownership.stamp_dispatch(job_id="job-y", kind="walk_forward", record_id="study-2")
    finally:
        ownership.set_process_authority(None)


async def test_abandoned_generations_are_enumerable_for_the_research_restart(conn: asyncpg.Connection) -> None:
    dsn = _ephemeral_url()
    await _clear_jobs(conn, "job-old", "job-new")
    authority = await ownership.claim_coordinator_authority(dsn=dsn, instance_id="research-1", role="research")
    try:
        await conn.execute(
            """
            INSERT INTO research_job_owners (job_id, kind, record_id, owner_instance_id, boot_generation, dispatched_at_ms)
            VALUES ('job-old', 'grid_search', 's', 'r0', $1, 0), ('job-new', 'grid_search', 's', 'r1', $2, 0)
            """,
            authority.boot_generation - 1,
            authority.boot_generation,
        )
        abandoned = await ownership.abandoned_job_ids(conn, up_to_generation=authority.boot_generation)
        assert "job-old" in abandoned and "job-new" not in abandoned, (
            "this generation's jobs stay; strictly older ones are enumerable for recovery"
        )
    finally:
        await authority.release()
        await _clear_jobs(conn, "job-old", "job-new")


async def test_a_degraded_startup_without_a_database_keeps_the_legacy_lifecycle() -> None:
    authority, _protected = await ownership.startup_authority_and_protection(dsn=None, instance_id="x", role="data-plane")
    assert authority is None and _protected is None
    assert ownership.process_authority() is None and not ownership.dispatch_refused()


async def test_startup_claims_authority_and_protects_its_own_generations_jobs() -> None:
    dsn = _ephemeral_url()
    from app.research.persistence.schema import ensure_schema

    cleanup = await asyncpg.connect(dsn)
    try:
        await ensure_schema(cleanup)
        await _clear_jobs(cleanup, "job-mine")
    finally:
        await cleanup.close()
    authority, _protected = await ownership.startup_authority_and_protection(
        dsn=dsn, instance_id="api-1", role="data-plane"
    )
    assert authority is not None and authority.boot_generation >= 1
    try:
        assert ownership.process_authority() is authority
        # The claimed generation protects its own dispatches from the sweep.
        from app.research.persistence.db import with_connection

        await with_connection(
            ownership.record_dispatch, authority, job_id="job-mine", kind="golden_search", record_id="study-9"
        )
        probe = await asyncpg.connect(dsn)
        try:
            assert "job-mine" in await ownership.protected_job_ids(probe)
        finally:
            await probe.close()
    finally:
        await authority.release()
        ownership.set_process_authority(None)
        cleanup = await asyncpg.connect(dsn)
        try:
            await _clear_jobs(cleanup, "job-mine")
        finally:
            await cleanup.close()
