"""The attempt fence every Python-owned research record shares (ADR 0055 §4).

A worker claims the next ``attempt`` generation atomically; every later write
row-locks the record inside its own transaction and refuses when the
generation moved on, when the record is complete (immutable), or when it is
gone. Grid Search, Walk-Forward Study and — since #1938 — the Recency Chart
launch differ in their column names and status vocabulary, not in this
contract, so it lives once here and each repository names its table and its
:class:`FenceColumns`.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from app.utils.timestamps import now_ms_utc

CLAIMABLE_STATUSES: frozenset[str] = frozenset({"queued", "running", "failed", "cancelled"})


@dataclass(frozen=True)
class FenceColumns:
    """The column names and status vocabulary a record answers the fence with.

    The defaults are the snake_case columns of ``research_grid_searches`` /
    ``research_walk_forward_studies``. ``updated_at_ms`` is ``None`` for a
    record that keeps no such column (``RecencyLaunches``), and the status
    names let a record speak its own case-vocabulary (``RUNNING`` /
    ``COMPLETED``).
    """

    record_id: str = "id"
    attempt: str = "attempt"
    status: str = "status"
    job_id: str = "job_id"
    updated_at_ms: str | None = "updated_at_ms"
    finished_at_ms: str = "finished_at_ms"
    failure_reason: str = "failure_reason"
    incomplete: str = "incomplete"
    claimable_statuses: frozenset[str] = CLAIMABLE_STATUSES
    running_status: str = "running"
    completed_status: str = "completed"


def _sql_name(name: str) -> str:
    """Quote a column name exactly when it is not lowercase — Postgres folds
    bare identifiers, and ``"RecencyLaunches"."Attempt"`` would silently
    resolve against ``attempt`` and find nothing."""
    return name if name.islower() else f'"{name}"'


class StaleAttemptError(RuntimeError):
    """The writer's attempt generation is no longer current, or the record is complete or gone."""


class RecordNotFoundError(LookupError):
    pass


class RecordNotClaimableError(RuntimeError):
    pass


async def claim_attempt(
    conn: asyncpg.Connection,
    *,
    table: str,
    record_id: str,
    job_id: str | None,
    also_reset: str = "",
    columns: FenceColumns = FenceColumns(),
) -> int:
    """Atomically take the next attempt generation and mark the record running.

    ``table`` is a module-level table name; ``also_reset`` is a SQL fragment
    of extra assignments cleared on the new attempt (``", verdict_json = NULL"``);
    ``columns`` names the record's fence columns and statuses.
    """
    c = columns
    updates = [
        f"{_sql_name(c.attempt)} = {_sql_name(c.attempt)} + 1",
        f"{_sql_name(c.status)} = '{c.running_status}'",
        f"{_sql_name(c.job_id)} = $2",
    ]
    params: list[object] = [record_id, job_id]
    if c.updated_at_ms is not None:
        params.append(now_ms_utc())
        updates.append(f"{_sql_name(c.updated_at_ms)} = ${len(params)}")
    updates.extend(
        [
            f"{_sql_name(c.finished_at_ms)} = NULL",
            f"{_sql_name(c.failure_reason)} = NULL",
            f"{_sql_name(c.incomplete)} = FALSE",
        ]
    )
    async with conn.transaction():
        row = await conn.fetchrow(
            f"SELECT {_sql_name(c.status)} FROM {table} WHERE {_sql_name(c.record_id)} = $1 FOR UPDATE", record_id
        )
        if row is None:
            raise RecordNotFoundError(record_id)
        if row[c.status] not in c.claimable_statuses:
            raise RecordNotClaimableError(f"{record_id} is {row[c.status]} and cannot be claimed")
        attempt = await conn.fetchval(
            f"""
            UPDATE {table}
               SET {', '.join(updates)}{also_reset}
             WHERE {_sql_name(c.record_id)} = $1
            RETURNING {_sql_name(c.attempt)}
            """,
            *params,
        )
        return int(attempt)


async def lock_current_attempt(
    conn: asyncpg.Connection,
    *,
    table: str,
    record_id: str,
    attempt: int,
    columns: FenceColumns = FenceColumns(),
) -> None:
    """Row-lock the record and refuse a writer that is stale, or a record that is complete."""
    c = columns
    row = await conn.fetchrow(
        f"SELECT {_sql_name(c.attempt)}, {_sql_name(c.status)} FROM {table} WHERE {_sql_name(c.record_id)} = $1 FOR UPDATE",
        record_id,
    )
    if row is None:
        raise StaleAttemptError(f"{record_id} no longer exists; attempt {attempt} may not write")
    if int(row[c.attempt]) != attempt:
        raise StaleAttemptError(f"{record_id} is on attempt {row[c.attempt]}; attempt {attempt} may not write")
    if row[c.status] == c.completed_status:
        raise StaleAttemptError(f"{record_id} is complete and immutable; attempt {attempt} may not write")
