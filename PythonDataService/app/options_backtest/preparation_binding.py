"""The current API preparation job belongs to one live, fenced run (#3440)."""
from __future__ import annotations

import asyncpg

from app.research.persistence.ownership import GenerationFencedError, lock_job_generation


async def current_binding(conn: asyncpg.Connection, run_id: int, parent_job_id: str, generation: int,
                          identity: str) -> str | None:
    async with conn.transaction():
        row = await _lock_parent(conn, run_id, parent_job_id, generation)
        if row["preparation_identity"] not in (None, identity):
            raise ValueError("preparation binds another request, root, or execution generation")
        return row["preparation_job_id"]


async def bind_preparation(conn: asyncpg.Connection, run_id: int, parent_job_id: str, generation: int,
                           identity: str, job_id: str, *, previous: str | None) -> str:
    """Replace ``previous`` with a fresh queued id; a lost acknowledgement is read back with ``current_binding``."""
    async with conn.transaction():
        row = await _lock_parent(conn, run_id, parent_job_id, generation)
        if row["preparation_identity"] not in (None, identity):
            raise ValueError("preparation binds another request, root, or execution generation")
        if row["preparation_job_id"] != previous:
            raise ValueError("the current preparation job changed before replacement")
        await conn.execute("UPDATE research_options_backtest_runs SET preparation_job_id=$2, preparation_identity=$3 WHERE id=$1",
                           run_id, job_id, identity)
        return job_id


async def require_binding(conn: asyncpg.Connection, run_id: int, parent_job_id: str, generation: int,
                          identity: str, job_id: str) -> None:
    async with conn.transaction():
        row = await _lock_parent(conn, run_id, parent_job_id, generation)
        if row["preparation_identity"] != identity or row["preparation_job_id"] != job_id:
            raise GenerationFencedError("preparation no longer belongs to the current run")


async def _lock_parent(conn: asyncpg.Connection, run_id: int, parent_job_id: str,
                       generation: int) -> asyncpg.Record:
    await lock_job_generation(conn, parent_job_id, kind="options_backtest", record_id=str(run_id), generation=generation)
    row = await conn.fetchrow("SELECT job_id,status,preparation_job_id,preparation_identity "
                              "FROM research_options_backtest_runs WHERE id=$1 FOR UPDATE", run_id)
    if row is None or row["job_id"] != parent_job_id or row["status"] != "running":
        raise GenerationFencedError("preparation requires its live parent run")
    return row
