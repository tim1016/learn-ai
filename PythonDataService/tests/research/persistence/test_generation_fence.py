"""Record writes and startup recovery share one database-scoped runtime authority (#3440)."""
from __future__ import annotations

from uuid import uuid4

import asyncpg
import pytest

from app.research.persistence import ownership
from app.research.persistence.schema import ensure_schema
from tests.research.conftest import _ephemeral_url
from tests.research.conftest import scratch_db as scratch_db

pytestmark = pytest.mark.slow


async def _owned_job(connection: asyncpg.Connection) -> None:
    await ensure_schema(connection)
    await connection.execute("INSERT INTO research_runtime_owners VALUES('coordinator','current','research',2,1)")
    await connection.execute("INSERT INTO research_job_owners VALUES('owned-job','options_backtest','7',NULL,'current',2,1)")


@pytest.mark.parametrize("mismatch", ["job", "kind", "record", "generation"])
async def test_another_job_or_record_cannot_write_under_a_live_generation(
    scratch_db: asyncpg.Connection, mismatch: str,
) -> None:
    await _owned_job(scratch_db)
    await scratch_db.execute("SELECT pg_advisory_lock($1,$2)", ownership.COORDINATOR_LOCK_CLASSID, ownership.COORDINATOR_LOCK_OBJID)
    assert await ownership.protected_job_ids(scratch_db) == {"owned-job"}
    async with scratch_db.transaction():
        with pytest.raises(ownership.GenerationFencedError):
            await ownership.lock_job_generation(
                scratch_db, "other-job" if mismatch == "job" else "owned-job",
                kind="golden_search" if mismatch == "kind" else "options_backtest",
                record_id="8" if mismatch == "record" else "7", generation=1 if mismatch == "generation" else 2,
            )


async def test_another_database_lock_protects_neither_recovery_nor_writes(scratch_db: asyncpg.Connection) -> None:
    await _owned_job(scratch_db)
    database = "generation_fence_" + uuid4().hex
    await scratch_db.execute(f'CREATE DATABASE "{database}"')
    try:
        other_database = await asyncpg.connect(_ephemeral_url(), database=database)
        try:
            await other_database.execute("SELECT pg_advisory_lock($1,$2)", ownership.COORDINATOR_LOCK_CLASSID, ownership.COORDINATOR_LOCK_OBJID)
            assert await ownership.protected_job_ids(scratch_db) == set()
            async with scratch_db.transaction():
                with pytest.raises(ownership.GenerationFencedError):
                    await ownership.lock_job_generation(scratch_db, "owned-job", kind="options_backtest", record_id="7", generation=2)
        finally:
            await other_database.close()
    finally:
        await scratch_db.execute(f'DROP DATABASE "{database}" WITH (FORCE)')


@pytest.mark.parametrize("change", ["runtime", "owner"])
async def test_a_fenced_write_holds_off_takeover_until_its_transaction_ends(
    scratch_db: asyncpg.Connection, change: str,
) -> None:
    await _owned_job(scratch_db)
    await scratch_db.execute("SELECT pg_advisory_lock($1,$2)", ownership.COORDINATOR_LOCK_CLASSID, ownership.COORDINATOR_LOCK_OBJID)
    database = await scratch_db.fetchval("SELECT current_database()")
    successor = await asyncpg.connect(_ephemeral_url(), database=database)
    update = ("UPDATE research_runtime_owners SET boot_generation=3" if change == "runtime" else
              "UPDATE research_job_owners SET boot_generation=3 WHERE job_id='owned-job'")
    try:
        async with scratch_db.transaction():
            await ownership.lock_job_generation(scratch_db, "owned-job", kind="options_backtest", record_id="7", generation=2)
            async with successor.transaction():
                await successor.execute("SET LOCAL lock_timeout='100ms'")
                with pytest.raises(asyncpg.LockNotAvailableError):
                    await successor.execute(update)
        await successor.execute(update)
        assert await ownership.protected_job_ids(scratch_db) == set()
    finally:
        await successor.close()


async def test_a_write_fence_cannot_be_used_without_a_transaction(scratch_db: asyncpg.Connection) -> None:
    with pytest.raises(ValueError, match="write transaction"):
        await ownership.lock_job_generation(scratch_db, "owned-job", kind="options_backtest", record_id="7", generation=2)
