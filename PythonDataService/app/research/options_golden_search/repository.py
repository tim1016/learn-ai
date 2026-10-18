"""Durable Options studies using the research writer, attempt fence and blob store (#3332).

Command receipts and final exposure are committed with their state transitions.
Evaluation reservations survive interruption; completion is conditional on the
same attempt. Money is stored in exact JSON strings, never stock float columns.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from app.research.persistence.db import connection
from app.research.persistence.fence import StaleAttemptError, lock_current_attempt
from app.utils.timestamps import now_ms_utc

TABLE = "research_options_search_studies"


class Conflict(ValueError):
    """A command or worker lost its authority to mutate the study."""


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def study(row: asyncpg.Record) -> dict[str, Any]:
    value = json.loads(row["document"])
    value.update(id=row["id"], revision=row["revision"], phase=row["phase"], status=row["status"],
                 attempt=row["attempt"], job_id=row["job_id"], updated_at_ms=row["updated_at_ms"])
    return value


async def get(conn: asyncpg.Connection, study_id: str, *, lock: bool = False) -> dict[str, Any]:
    row = await conn.fetchrow(f"SELECT * FROM {TABLE} WHERE id=$1" + (" FOR UPDATE" if lock else ""), study_id)
    if row is None:
        raise LookupError("Options search study was not found.")
    return study(row)


async def save(conn: asyncpg.Connection, value: dict[str, Any], *, bump: bool = True) -> dict[str, Any]:
    row = await conn.fetchrow(
        f"""UPDATE {TABLE} SET document=$2::jsonb, phase=$3, status=$4, job_id=$5,
        revision=revision+$6, updated_at_ms=$7 WHERE id=$1 RETURNING *""",
        value["id"], dumps(value), value["phase"], value["status"], value.get("job_id"), int(bump), now_ms_utc(),
    )
    if row is None:
        raise LookupError("Options search study was not found.")
    return study(row)


async def create(conn: asyncpg.Connection, value: dict[str, Any], key: str, request_hash: str) -> dict[str, Any]:
    async with conn.transaction():
        await conn.execute(
            f"""INSERT INTO {TABLE} (id,underlying,setup_name,phase,status,document,created_at_ms,updated_at_ms)
            VALUES ($1,$2,$3,'research','idle',$4::jsonb,$5,$5) ON CONFLICT (id) DO NOTHING""",
            value["id"], value["underlying"], value["setup_name"], dumps(value), now_ms_utc(),
        )
        result = await get(conn, value["id"], lock=True)
        prior = await command_receipt(conn, value["id"], key, request_hash)
        if prior is not None:
            return prior
        if result["protocol_hash"] != value["protocol_hash"]:
            raise Conflict("The lock key already names a different protocol.")
        await record_command(conn, value["id"], key, request_hash, result)
        return result


async def command_receipt(conn: asyncpg.Connection, study_id: str, key: str, request_hash: str) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        "SELECT request_hash,response FROM research_options_search_commands WHERE study_id=$1 AND command_id=$2",
        study_id, key,
    )
    if row is None:
        return None
    if row["request_hash"] != request_hash:
        raise Conflict("This command key already names a different request.")
    return json.loads(row["response"])


async def record_command(conn: asyncpg.Connection, study_id: str, key: str, request_hash: str, response: dict[str, Any]) -> None:
    await conn.execute(
        """INSERT INTO research_options_search_commands(study_id,command_id,request_hash,response,created_at_ms)
        VALUES ($1,$2,$3,$4::jsonb,$5)""", study_id, key, request_hash, dumps(response), now_ms_utc(),
    )


async def history(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    return [study(row) for row in await conn.fetch(f"SELECT * FROM {TABLE} ORDER BY created_at_ms DESC,id LIMIT 200")]


@asynccontextmanager
async def edit(study_id: str) -> AsyncIterator[tuple[asyncpg.Connection, dict[str, Any]]]:
    async with connection() as conn, conn.transaction():
        yield conn, await get(conn, study_id, lock=True)


async def fence(conn: asyncpg.Connection, study_id: str, attempt: int) -> dict[str, Any]:
    await lock_current_attempt(conn, table=TABLE, record_id=study_id, attempt=attempt)
    current = await get(conn, study_id)
    if current["status"] != "running":
        raise StaleAttemptError("Only the running Options attempt may persist work.")
    return current


async def bind(conn: asyncpg.Connection, study_id: str, token: str, job_id: str) -> bool:
    async with conn.transaction():
        value = await get(conn, study_id, lock=True)
        if value.get("stage_token") != token or value["status"] not in {"queued", "running"}:
            raise Conflict("This dispatch is not authorized by the current study command.")
        if value.get("job_id") is not None:
            if value["job_id"] != job_id:
                raise Conflict("Another job already owns this stage.")
            return False
        value["job_id"] = job_id
        await save(conn, value, bump=False)
        return True


async def release(conn: asyncpg.Connection, study_id: str, job_id: str) -> None:
    async with conn.transaction():
        value = await get(conn, study_id, lock=True)
        if value["status"] == "queued" and value.get("job_id") == job_id:
            value["job_id"] = None
            await save(conn, value, bump=False)


async def claim(conn: asyncpg.Connection, study_id: str, token: str, job_id: str) -> dict[str, Any]:
    async with conn.transaction():
        value = await get(conn, study_id, lock=True)
        if value["status"] != "queued" or value.get("stage_token") != token or value.get("job_id") != job_id:
            raise Conflict("The job no longer owns this stage.")
        # Each executable stage has its own elapsed bound; retries share it,
        # while owner review between stages consumes none of the next bound.
        if value.get("execution_deadline_ms") is None or value.get("execution_deadline_phase") != value["phase"]:
            value["execution_deadline_ms"] = now_ms_utc() + value["protocol"]["budget"]["max_elapsed_ms"]
            value["execution_deadline_phase"] = value["phase"]
            await save(conn,value,bump=False)
        row = await conn.fetchrow(
            f"UPDATE {TABLE} SET attempt=attempt+1,status='running' WHERE id=$1 RETURNING *", study_id,
        )
        return study(row)


async def reserve(conn: asyncpg.Connection, study_id: str, attempt: int, key: str, identity: dict[str, Any], *, cap: int) -> dict[str, Any] | None:
    """Reserve once before compute; a retry reuses its reservation and advances its visible attempt count."""
    async with conn.transaction():
        await fence(conn, study_id, attempt)
        prior = await conn.fetchrow("SELECT * FROM research_options_search_evaluations WHERE study_id=$1 AND key=$2", study_id, key)
        if prior is not None:
            if json.loads(prior["identity"]) != identity:
                raise Conflict("Evaluation identity does not match its reserved key.")
            if prior["result"] is not None:
                return json.loads(prior["result"])
            return None
        count = await conn.fetchval("SELECT count(*) FROM research_options_search_evaluations WHERE study_id=$1", study_id)
        if count >= cap:
            raise Conflict("The reserved evaluation bound is exhausted; scientific work cannot be truncated.")
        await conn.execute(
            """INSERT INTO research_options_search_evaluations(study_id,key,identity,execution_attempts)
            VALUES ($1,$2,$3::jsonb,0)""", study_id, key, dumps(identity),
        )
        return None


async def complete(conn: asyncpg.Connection, study_id: str, attempt: int, key: str, result: dict[str, Any], *, task_gen: int | None = None) -> None:
    async with conn.transaction():
        await fence(conn, study_id, attempt)
        if task_gen is not None:
            value = await get(conn, study_id)
            if value.get("execution_generations", {}).get(key, 1) != task_gen:
                raise Conflict("The evaluation delivery belongs to a superseded worker generation.")
        reserved = await conn.fetchrow("SELECT result FROM research_options_search_evaluations WHERE study_id=$1 AND key=$2", study_id, key)
        if reserved is None:
            raise Conflict("An evaluation must be reserved before completion.")
        prior = reserved["result"]
        if prior is not None and json.loads(prior) != result:
            raise Conflict("A completed evaluation cannot change its result.")
        await conn.execute(
            "UPDATE research_options_search_evaluations SET result=$3::jsonb WHERE study_id=$1 AND key=$2 AND result IS NULL",
            study_id, key, dumps(result),
        )


async def evaluations(conn: asyncpg.Connection, study_id: str) -> list[dict[str, Any]]:
    return [{"key": r["key"], "identity": json.loads(r["identity"]), "result": json.loads(r["result"]) if r["result"] else None,
             "execution_attempts": r["execution_attempts"]}
            for r in await conn.fetch("SELECT * FROM research_options_search_evaluations WHERE study_id=$1 ORDER BY key", study_id)]


async def trial(conn: asyncpg.Connection, study_id: str, attempt: int, record: dict[str, Any]) -> None:
    async with conn.transaction():
        await fence(conn, study_id, attempt)
        await conn.execute(
            "INSERT INTO research_options_search_trials(study_id,record,created_at_ms) VALUES ($1,$2::jsonb,$3)",
            study_id, dumps(record), now_ms_utc(),
        )


async def lock_exposure(conn: asyncpg.Connection, underlying: str) -> None:
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", "options-exposure:" + underlying)


async def exposure(conn: asyncpg.Connection, underlying: str, start_ms: int, end_ms: int, *, owner: str | None = None) -> str:
    reused = await conn.fetchval(
        """SELECT EXISTS(SELECT 1 FROM research_options_search_exposures
        WHERE underlying=$1 AND start_ms<$3 AND end_ms>$2 AND ($4::text IS NULL OR owner_id<>$4))""",
        underlying, start_ms, end_ms, owner,
    )
    if reused:
        return "previously_used"
    unknown = await conn.fetchval(
        """SELECT EXISTS(SELECT 1 FROM research_options_backtest_runs
        WHERE underlying=$1 AND first_session_open_ms<$3 AND last_session_open_ms>=$2)""", underlying, start_ms, end_ms,
    )
    return "history_unknown" if unknown else "not_opened"


async def reserve_exposure(conn: asyncpg.Connection, underlying: str, start_ms: int, end_ms: int, owner: str) -> str:
    await lock_exposure(conn, underlying)
    classification = await exposure(conn, underlying, start_ms, end_ms, owner=owner)
    await conn.execute(
        """INSERT INTO research_options_search_exposures(underlying,start_ms,end_ms,owner_id,created_at_ms)
        VALUES ($1,$2,$3,$4,$5) ON CONFLICT (owner_id,start_ms,end_ms) DO NOTHING""",
        underlying, start_ms, end_ms, owner, now_ms_utc(),
    )
    return classification


async def begin_execution(conn: asyncpg.Connection, study_id: str, attempt: int, key: str, *,
                          max_attempts: int, task_gen: int = 1) -> None:
    """Reserve a physical start before compute; no start can overdraw the locked attempt budget."""
    async with conn.transaction():
        value = await fence(conn, study_id, attempt)
        if value.get("execution_generations", {}).get(key, 1) != task_gen:
            raise Conflict("This worker generation no longer owns the evaluation.")
        current = await conn.fetchrow(
            "SELECT execution_attempts,result FROM research_options_search_evaluations WHERE study_id=$1 AND key=$2",
            study_id, key,
        )
        if current is None or current["result"] is not None:
            raise Conflict("Only a reserved incomplete evaluation may begin execution.")
        starts = await conn.fetchval(
            "SELECT coalesce(sum(execution_attempts),0) FROM research_options_search_evaluations WHERE study_id=$1", study_id,
        )
        if starts >= max_attempts:
            raise Conflict("The physical execution-attempt bound is exhausted; study is interrupted.")
        await conn.execute(
            "UPDATE research_options_search_evaluations SET execution_attempts=execution_attempts+1 WHERE study_id=$1 AND key=$2",
            study_id, key,
        )


async def advance_generation(conn: asyncpg.Connection, study_id: str, attempt: int, key: str, next_gen: int) -> None:
    """Commit a superseding task generation before any replacement can be dispatched."""
    async with conn.transaction():
        value = await fence(conn, study_id, attempt)
        generations = value.setdefault("execution_generations", {})
        if next_gen != generations.get(key, 1) + 1:
            raise Conflict("Worker generations must advance exactly once before a retry.")
        generations[key] = next_gen
        await save(conn, value, bump=False)


async def get_evaluation(conn: asyncpg.Connection, study_id: str, key: str) -> dict[str, Any]:
    row = await conn.fetchrow(
        "SELECT key,identity,result,execution_attempts FROM research_options_search_evaluations WHERE study_id=$1 AND key=$2",
        study_id, key,
    )
    if row is None:
        raise LookupError("Reserved Options evaluation was not found.")
    return {"key": row["key"], "identity": json.loads(row["identity"]),
            "result": json.loads(row["result"]) if row["result"] is not None else None,
            "execution_attempts": row["execution_attempts"]}


async def evaluation_summaries(conn: asyncpg.Connection, study_id: str, keys: list[str]) -> dict[str, dict[str, Any]]:
    rows = await conn.fetch(
        """SELECT key, result->'metrics' AS metrics, (result->>'complete')::boolean AS complete
        FROM research_options_search_evaluations WHERE study_id=$1 AND key=ANY($2::text[]) AND result IS NOT NULL""",
        study_id, keys,
    )
    return {row["key"]: {"metrics": json.loads(row["metrics"]), "complete": row["complete"]} for row in rows}


async def counters(conn: asyncpg.Connection, study_id: str) -> dict[str, int]:
    row = await conn.fetchrow(
        """SELECT count(*) AS reserved, count(result) AS completed,
        coalesce(sum(execution_attempts),0) AS execution_attempts,
        coalesce(sum((result->>'evidence_bytes')::bigint),0) AS evidence_bytes
        FROM research_options_search_evaluations WHERE study_id=$1""", study_id,
    )
    return {key: int(row[key]) for key in ("reserved", "completed", "execution_attempts", "evidence_bytes")}


async def summaries(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """History does not load candidate traces or evidence documents."""
    rows = await conn.fetch(f"SELECT id,setup_name,underlying,phase,status,updated_at_ms FROM {TABLE} ORDER BY created_at_ms DESC,id LIMIT 200")
    return [dict(row) for row in rows]
