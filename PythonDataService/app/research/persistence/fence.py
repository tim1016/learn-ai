"""The attempt fence every Python-owned research record shares (ADR 0055 §4).

A worker claims the next ``attempt`` generation atomically; every later write
row-locks the record inside its own transaction and refuses when the
generation moved on, when the record is complete (immutable), or when it is
gone. Grid Search and Walk-Forward Study records differ in their columns,
not in this contract, so it lives once here and each repository names its
table.
"""

from __future__ import annotations

import asyncpg

from app.utils.timestamps import now_ms_utc

CLAIMABLE_STATUSES: frozenset[str] = frozenset({"queued", "running", "failed", "cancelled"})


class StaleAttemptError(RuntimeError):
    """The writer's attempt generation is no longer current, or the record is complete or gone."""


class RecordNotFoundError(LookupError):
    pass


class RecordNotClaimableError(RuntimeError):
    pass


async def claim_attempt(conn: asyncpg.Connection, *, table: str, record_id: str, job_id: str | None, also_reset: str = "") -> int:
    """Atomically take the next attempt generation and mark the record running.

    ``table`` is a module-level table name; ``also_reset`` is a SQL fragment
    of extra assignments cleared on the new attempt (``", verdict_json = NULL"``).
    """
    async with conn.transaction():
        row = await conn.fetchrow(f"SELECT status FROM {table} WHERE id = $1 FOR UPDATE", record_id)
        if row is None:
            raise RecordNotFoundError(record_id)
        if row["status"] not in CLAIMABLE_STATUSES:
            raise RecordNotClaimableError(f"{record_id} is {row['status']} and cannot be claimed")
        attempt = await conn.fetchval(
            f"""
            UPDATE {table}
               SET attempt = attempt + 1, status = 'running', job_id = $2, updated_at_ms = $3,
                   finished_at_ms = NULL, failure_reason = NULL, incomplete = FALSE{also_reset}
             WHERE id = $1
            RETURNING attempt
            """,
            record_id,
            job_id,
            now_ms_utc(),
        )
        return int(attempt)


async def lock_current_attempt(conn: asyncpg.Connection, *, table: str, record_id: str, attempt: int) -> None:
    """Row-lock the record and refuse a writer that is stale, or a record that is complete."""
    row = await conn.fetchrow(f"SELECT attempt, status FROM {table} WHERE id = $1 FOR UPDATE", record_id)
    if row is None:
        raise StaleAttemptError(f"{record_id} no longer exists; attempt {attempt} may not write")
    if int(row["attempt"]) != attempt:
        raise StaleAttemptError(f"{record_id} is on attempt {row['attempt']}; attempt {attempt} may not write")
    if row["status"] == "completed":
        raise StaleAttemptError(f"{record_id} is complete and immutable; attempt {attempt} may not write")
