"""asyncpg repository for walk-forward studies (PRD #1925; ADR 0055).

Same shape and fence as the Grid Search repository: a study row is durable
at launch, ``attempt`` is claimed atomically, and every fold update and
terminal transition checks the generation inside its transaction. A study's
per-fold sweeps are ordinary ``research_grid_searches`` rows owned by it;
deleting the study deletes them.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import asyncpg

from app.research.grid_search.repository import StaleAttemptError
from app.research.walk_forward_study.models import FoldRecord, NewStudy, StudyRow, StudyStatus
from app.utils.timestamps import now_ms_utc

CLAIMABLE_STATUSES: frozenset[str] = frozenset({"queued", "running", "failed", "cancelled"})


class StudyNotFoundError(LookupError):
    pass


class StudyNotClaimableError(RuntimeError):
    pass


_COLUMNS = """
    id, strategy_key, symbol, status, attempt, job_id, created_at_ms, updated_at_ms, finished_at_ms,
    request_json::text AS request_json, receipt_json::text AS receipt_json, folds_json::text AS folds_json,
    verdict_json::text AS verdict_json, expected_backtests, completed_backtests, incomplete, failure_reason
"""


def _row(row: asyncpg.Record) -> StudyRow:
    return StudyRow(
        id=row["id"],
        strategy_key=row["strategy_key"],
        symbol=row["symbol"],
        status=row["status"],
        attempt=row["attempt"],
        job_id=row["job_id"],
        created_at_ms=row["created_at_ms"],
        updated_at_ms=row["updated_at_ms"],
        finished_at_ms=row["finished_at_ms"],
        request=json.loads(row["request_json"]),
        receipt=json.loads(row["receipt_json"]),
        folds=[FoldRecord.from_dict(item) for item in json.loads(row["folds_json"])],
        verdict=json.loads(row["verdict_json"]) if row["verdict_json"] else None,
        expected_backtests=row["expected_backtests"],
        completed_backtests=row["completed_backtests"],
        incomplete=row["incomplete"],
        failure_reason=row["failure_reason"],
    )


async def create_study(conn: asyncpg.Connection, study: NewStudy) -> StudyRow:
    now = now_ms_utc()
    await conn.execute(
        """
        INSERT INTO research_walk_forward_studies (
            id, strategy_key, symbol, status, attempt, job_id, created_at_ms, updated_at_ms,
            request_json, receipt_json, folds_json, expected_backtests
        ) VALUES ($1, $2, $3, 'queued', 0, $4, $5, $5, $6::jsonb, $7::jsonb, $8::jsonb, $9)
        """,
        study.id,
        study.strategy_key,
        study.symbol,
        study.job_id,
        now,
        json.dumps(study.request, sort_keys=True),
        json.dumps(study.receipt, sort_keys=True),
        json.dumps([fold.as_dict() for fold in study.folds]),
        study.expected_backtests,
    )
    row = await get_study(conn, study.id)
    assert row is not None
    return row


async def get_study(conn: asyncpg.Connection, study_id: str) -> StudyRow | None:
    row = await conn.fetchrow(f"SELECT {_COLUMNS} FROM research_walk_forward_studies WHERE id = $1", study_id)
    return _row(row) if row is not None else None


async def list_studies(
    conn: asyncpg.Connection,
    *,
    strategy_key: str | None = None,
    symbol: str | None = None,
    status: str | None = None,
    job_id: str | None = None,
    limit: int = 200,
) -> list[StudyRow]:
    rows = await conn.fetch(
        f"""
        SELECT {_COLUMNS} FROM research_walk_forward_studies
        WHERE ($1::text IS NULL OR strategy_key = $1)
          AND ($2::text IS NULL OR symbol = $2)
          AND ($3::text IS NULL OR status = $3)
          AND ($4::text IS NULL OR job_id = $4)
        ORDER BY created_at_ms DESC, id DESC
        LIMIT $5
        """,
        strategy_key,
        symbol,
        status,
        job_id,
        limit,
    )
    return [_row(row) for row in rows]


async def claim_attempt(conn: asyncpg.Connection, study_id: str, *, job_id: str | None) -> int:
    async with conn.transaction():
        row = await conn.fetchrow("SELECT status FROM research_walk_forward_studies WHERE id = $1 FOR UPDATE", study_id)
        if row is None:
            raise StudyNotFoundError(study_id)
        if row["status"] not in CLAIMABLE_STATUSES:
            raise StudyNotClaimableError(f"study {study_id} is {row['status']} and cannot be claimed")
        attempt = await conn.fetchval(
            """
            UPDATE research_walk_forward_studies
               SET attempt = attempt + 1, status = 'running', job_id = $2, updated_at_ms = $3,
                   finished_at_ms = NULL, failure_reason = NULL, incomplete = FALSE, verdict_json = NULL
             WHERE id = $1
            RETURNING attempt
            """,
            study_id,
            job_id,
            now_ms_utc(),
        )
        return int(attempt)


async def _lock(conn: asyncpg.Connection, study_id: str, attempt: int) -> None:
    row = await conn.fetchrow("SELECT attempt, status FROM research_walk_forward_studies WHERE id = $1 FOR UPDATE", study_id)
    if row is None:
        raise StaleAttemptError(f"study {study_id} no longer exists; attempt {attempt} may not write")
    if int(row["attempt"]) != attempt:
        raise StaleAttemptError(f"study {study_id} is on attempt {row['attempt']}; attempt {attempt} may not write")
    if row["status"] == "completed":
        raise StaleAttemptError(f"study {study_id} is complete and immutable; attempt {attempt} may not write")


async def update_folds(conn: asyncpg.Connection, study_id: str, attempt: int, folds: Sequence[FoldRecord], *, completed_backtests: int) -> None:
    async with conn.transaction():
        await _lock(conn, study_id, attempt)
        await conn.execute(
            """
            UPDATE research_walk_forward_studies
               SET folds_json = $2::jsonb, completed_backtests = $3, updated_at_ms = $4
             WHERE id = $1
            """,
            study_id,
            json.dumps([fold.as_dict() for fold in folds]),
            completed_backtests,
            now_ms_utc(),
        )


async def finish_study(
    conn: asyncpg.Connection,
    study_id: str,
    attempt: int,
    *,
    status: StudyStatus,
    verdict: dict | None,
    incomplete: bool,
    failure_reason: str | None,
) -> None:
    async with conn.transaction():
        await _lock(conn, study_id, attempt)
        await conn.execute(
            """
            UPDATE research_walk_forward_studies
               SET status = $2, verdict_json = $3::jsonb, incomplete = $4, failure_reason = $5,
                   finished_at_ms = $6, updated_at_ms = $6
             WHERE id = $1
            """,
            study_id,
            status,
            json.dumps(verdict, sort_keys=True) if verdict is not None else None,
            incomplete,
            failure_reason,
            now_ms_utc(),
        )


async def delete_study(conn: asyncpg.Connection, study_id: str) -> bool:
    """Remove the study and every sweep it owns."""
    async with conn.transaction():
        await conn.execute(
            "DELETE FROM research_grid_searches WHERE owner_kind = 'walk_forward' AND owner_id = $1",
            study_id,
        )
        result = await conn.execute("DELETE FROM research_walk_forward_studies WHERE id = $1", study_id)
    return result.endswith(" 1")
