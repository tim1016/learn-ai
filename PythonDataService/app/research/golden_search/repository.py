"""asyncpg repository for Golden Search studies, their evaluations, trials, commands and exposures (#2696, ADR 0074).

Every function takes a connection from the calling loop's pool
(``app.research.persistence.db``). Two concurrency contracts meet here:

* **Commands** row-lock the study, check its ``revision`` and record the
  command under its idempotency key in the same transaction, so a retried
  request returns the original outcome and a stale one changes nothing.
* **Stage writes** run under the shared attempt fence
  (``app.research.persistence.fence``): a worker whose attempt was
  superseded cannot write an evaluation, consume budget or move the study.

The trial and exposure ledgers are append-only at rest (V11 triggers);
nothing here updates or deletes them.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import asyncpg

from app.research.golden_search.models import (
    CommandRecord,
    EvaluationPage,
    EvaluationRecord,
    NewStudy,
    StudyRow,
)
from app.research.golden_search.selection import Metrics
from app.research.persistence import fence
from app.utils.timestamps import now_ms_utc

STUDIES = "research_golden_search_studies"

_STUDY_COLUMNS = """
    id, parent_study_id, strategy_key, symbol, state, revision, status, attempt, job_id, pending_stage,
    stage_token, created_at_ms, updated_at_ms, finished_at_ms, protocol_json::text AS protocol_json,
    protocol_hash, receipt_json::text AS receipt_json, results_json::text AS results_json, candidate_key,
    exam_locked, decision_json::text AS decision_json, budget_cap, consumed_evaluations, cache_hits,
    invalid_points, incomplete, failure_reason, hidden
"""
_EVALUATION_COLUMNS = """
    study_id, evaluation_key, point_hash, point_json::text AS point_json, window_start_ms, window_end_ms,
    scenario, detail, stage, fold_index, status, attempt, retries, total_trades, net_profit, total_return_pct,
    sharpe_ratio, max_drawdown_pct, win_rate, error, detail_json::text AS detail_json, created_at_ms,
    completed_at_ms
"""
# Plain columns a transition may set; JSON columns are handled by name below.
_UPDATABLE_COLUMNS = frozenset(
    {
        "state",
        "status",
        "pending_stage",
        "stage_token",
        "job_id",
        "candidate_key",
        "exam_locked",
        "failure_reason",
        "incomplete",
        "finished_at_ms",
        "invalid_points",
    }
)


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, allow_nan=False)


def _study(row: asyncpg.Record) -> StudyRow:
    return StudyRow(
        id=row["id"],
        parent_study_id=row["parent_study_id"],
        strategy_key=row["strategy_key"],
        symbol=row["symbol"],
        state=row["state"],
        revision=row["revision"],
        status=row["status"],
        attempt=row["attempt"],
        job_id=row["job_id"],
        pending_stage=row["pending_stage"],
        stage_token=row["stage_token"],
        created_at_ms=row["created_at_ms"],
        updated_at_ms=row["updated_at_ms"],
        finished_at_ms=row["finished_at_ms"],
        protocol=json.loads(row["protocol_json"]),
        protocol_hash=row["protocol_hash"],
        receipt=json.loads(row["receipt_json"]),
        results=json.loads(row["results_json"]),
        candidate_key=row["candidate_key"],
        exam_locked=row["exam_locked"],
        decision=json.loads(row["decision_json"]) if row["decision_json"] is not None else None,
        budget_cap=row["budget_cap"],
        consumed_evaluations=row["consumed_evaluations"],
        cache_hits=row["cache_hits"],
        invalid_points=row["invalid_points"],
        incomplete=row["incomplete"],
        failure_reason=row["failure_reason"],
        hidden=row["hidden"],
    )


def _evaluation(row: asyncpg.Record) -> EvaluationRecord:
    return EvaluationRecord(
        study_id=row["study_id"],
        evaluation_key=row["evaluation_key"],
        point_hash=row["point_hash"],
        point=json.loads(row["point_json"]),
        window_start_ms=row["window_start_ms"],
        window_end_ms=row["window_end_ms"],
        scenario=row["scenario"],
        detail=row["detail"],
        stage=row["stage"],
        fold_index=row["fold_index"],
        status=row["status"],
        attempt=row["attempt"],
        retries=row["retries"],
        total_trades=row["total_trades"],
        net_profit=row["net_profit"],
        total_return_pct=row["total_return_pct"],
        sharpe_ratio=row["sharpe_ratio"],
        max_drawdown_pct=row["max_drawdown_pct"],
        win_rate=row["win_rate"],
        error=row["error"],
        detail_json=json.loads(row["detail_json"]) if row["detail_json"] is not None else None,
        created_at_ms=row["created_at_ms"],
        completed_at_ms=row["completed_at_ms"],
    )


def metrics_of(record: EvaluationRecord) -> Metrics:
    """What a recorded evaluation contributes to selection; a pending row has no result yet."""
    if record.status == "pending":
        raise ValueError(f"evaluation {record.evaluation_key} has no result yet")
    return Metrics(
        status=record.status,
        total_trades=int(record.total_trades or 0),
        net_profit=record.net_profit,
        total_return_pct=record.total_return_pct,
        sharpe_ratio=record.sharpe_ratio,
        max_drawdown_pct=record.max_drawdown_pct,
        win_rate=record.win_rate,
        error=record.error,
    )


# ── Studies ──────────────────────────────────────────────────────────────


async def insert_study(conn: asyncpg.Connection, new: NewStudy) -> tuple[StudyRow, CommandRecord]:
    """Write a locked study and its lock command, or return the ones its idempotency key already wrote."""
    now = now_ms_utc()
    async with conn.transaction():
        inserted = await conn.fetchval(
            """
            INSERT INTO research_golden_search_studies (
                id, parent_study_id, strategy_key, symbol, state, revision, status, attempt,
                created_at_ms, updated_at_ms, protocol_json, protocol_hash, receipt_json, budget_cap
            ) VALUES ($1, $2, $3, $4, 'locked', 0, 'idle', 0, $5, $5, $6::jsonb, $7, $8::jsonb, $9)
            ON CONFLICT (id) DO NOTHING
            RETURNING id
            """,
            new.id,
            new.parent_study_id,
            new.strategy_key,
            new.symbol,
            now,
            _dumps(new.protocol),
            new.protocol_hash,
            _dumps(new.receipt),
            new.budget_cap,
        )
        if inserted is not None:
            await record_command(
                conn,
                study_id=new.id,
                idempotency_key=new.idempotency_key,
                command="lock",
                request_sha256=new.request_sha256,
                response={"study_id": new.id},
            )
    row = await get_study(conn, new.id)
    command = await get_command(conn, new.id, new.idempotency_key)
    if row is None or command is None:
        raise LookupError(f"study {new.id} was neither written nor found")
    return row, command


async def get_study(conn: asyncpg.Connection, study_id: str) -> StudyRow | None:
    row = await conn.fetchrow(f"SELECT {_STUDY_COLUMNS} FROM {STUDIES} WHERE id = $1", study_id)
    return _study(row) if row is not None else None


async def lock_study(conn: asyncpg.Connection, study_id: str) -> StudyRow | None:
    """The study row-locked for the caller's transaction."""
    row = await conn.fetchrow(f"SELECT {_STUDY_COLUMNS} FROM {STUDIES} WHERE id = $1 FOR UPDATE", study_id)
    return _study(row) if row is not None else None


async def list_studies(
    conn: asyncpg.Connection,
    *,
    strategy_key: str | None = None,
    symbol: str | None = None,
    include_hidden: bool = False,
    limit: int = 100,
) -> list[StudyRow]:
    """Newest first."""
    rows = await conn.fetch(
        f"""
        SELECT {_STUDY_COLUMNS} FROM {STUDIES}
         WHERE ($1::text IS NULL OR strategy_key = $1)
           AND ($2::text IS NULL OR symbol = $2)
           AND ($3 OR NOT hidden)
         ORDER BY created_at_ms DESC, id DESC
         LIMIT $4
        """,
        strategy_key,
        symbol,
        include_hidden,
        limit,
    )
    return [_study(row) for row in rows]


async def update_study(
    conn: asyncpg.Connection,
    study_id: str,
    *,
    changes: Mapping[str, Any] | None = None,
    decision: Mapping[str, Any] | None = None,
    results_patch: Mapping[str, Any] | None = None,
    bump_revision: bool = True,
) -> StudyRow:
    """Apply one transition: named columns, a replaced decision, top-level results keys merged in."""
    params: list[Any] = [study_id, now_ms_utc()]
    assignments = ["updated_at_ms = $2"]
    for name, value in (changes or {}).items():
        if name not in _UPDATABLE_COLUMNS:
            raise ValueError(f"{name!r} is not a column a study transition may set")
        params.append(value)
        assignments.append(f"{name} = ${len(params)}")
    if decision is not None:
        params.append(_dumps(dict(decision)))
        assignments.append(f"decision_json = ${len(params)}::jsonb")
    if results_patch:
        params.append(_dumps(dict(results_patch)))
        assignments.append(f"results_json = results_json || ${len(params)}::jsonb")
    if bump_revision:
        assignments.append("revision = revision + 1")
    row = await conn.fetchrow(
        f"UPDATE {STUDIES} SET {', '.join(assignments)} WHERE id = $1 RETURNING {_STUDY_COLUMNS}",
        *params,
    )
    if row is None:
        raise LookupError(f"study {study_id} not found")
    return _study(row)


async def update_study_fenced(
    conn: asyncpg.Connection,
    study_id: str,
    attempt: int,
    *,
    changes: Mapping[str, Any] | None = None,
    results_patch: Mapping[str, Any] | None = None,
    bump_revision: bool = False,
) -> StudyRow:
    """A stage worker's write: refused unless ``attempt`` is still the study's current, unfinished attempt."""
    async with conn.transaction():
        await fence.lock_current_attempt(conn, table=STUDIES, record_id=study_id, attempt=attempt)
        return await update_study(conn, study_id, changes=changes, results_patch=results_patch, bump_revision=bump_revision)


async def save_checkpoint(conn: asyncpg.Connection, study_id: str, attempt: int, checkpoint: Mapping[str, Any]) -> None:
    """Persist the approval checkpoint inside the study's decision, under the attempt fence."""
    async with conn.transaction():
        await fence.lock_current_attempt(conn, table=STUDIES, record_id=study_id, attempt=attempt)
        await conn.execute(
            f"""
            UPDATE {STUDIES}
               SET decision_json = jsonb_set(COALESCE(decision_json, '{{}}'::jsonb), '{{checkpoint}}', $2::jsonb),
                   updated_at_ms = $3
             WHERE id = $1
            """,
            study_id,
            _dumps(dict(checkpoint)),
            now_ms_utc(),
        )


async def hide_study(conn: asyncpg.Connection, study_id: str) -> bool:
    result = await conn.execute(f"UPDATE {STUDIES} SET hidden = TRUE, updated_at_ms = $2 WHERE id = $1", study_id, now_ms_utc())
    return result.endswith(" 1")


# ── Dispatch and the attempt fence ───────────────────────────────────────

DispatchOutcome = Literal["bound", "redelivery", "mismatch", "not_pending", "taken", "not_found"]


async def bind_dispatch(conn: asyncpg.Connection, study_id: str, *, stage_token: str, job_id: str) -> DispatchOutcome:
    """Bind the jobs boundary's job id to the stage a guarded command authorized — once."""
    async with conn.transaction():
        row = await conn.fetchrow(
            f"SELECT status, pending_stage, stage_token, job_id FROM {STUDIES} WHERE id = $1 FOR UPDATE", study_id
        )
        if row is None:
            return "not_found"
        if row["pending_stage"] is None or row["stage_token"] is None or row["stage_token"] != stage_token:
            return "mismatch"
        if row["job_id"] == job_id:
            return "redelivery"
        if row["status"] != "queued":
            return "not_pending"
        if row["job_id"] is not None:
            return "taken"
        await conn.execute(f"UPDATE {STUDIES} SET job_id = $2, updated_at_ms = $3 WHERE id = $1", study_id, job_id, now_ms_utc())
        return "bound"


class StageClaimError(RuntimeError):
    """The stage token or job does not match what the study authorized."""


async def claim_stage(conn: asyncpg.Connection, study_id: str, *, stage_token: str, job_id: str) -> tuple[StudyRow, int]:
    """Verify the authorized stage and take the next attempt generation for it."""
    async with conn.transaction():
        row = await lock_study(conn, study_id)
        if row is None:
            raise fence.RecordNotFoundError(study_id)
        if row.pending_stage is None or row.stage_token != stage_token:
            raise StageClaimError(f"study {study_id} has no stage authorized under this token")
        if row.job_id not in (None, job_id):
            raise StageClaimError(f"study {study_id}'s stage is bound to another job")
        attempt = await fence.claim_attempt(conn, table=STUDIES, record_id=study_id, job_id=job_id)
        claimed = await get_study(conn, study_id)
        assert claimed is not None
        return claimed, attempt


# ── Commands and trials ──────────────────────────────────────────────────


async def get_command(conn: asyncpg.Connection, study_id: str, idempotency_key: str) -> CommandRecord | None:
    row = await conn.fetchrow(
        """
        SELECT study_id, idempotency_key, command, request_sha256, response_json::text AS response_json, created_at_ms
          FROM research_golden_search_commands WHERE study_id = $1 AND idempotency_key = $2
        """,
        study_id,
        idempotency_key,
    )
    if row is None:
        return None
    return CommandRecord(
        study_id=row["study_id"],
        idempotency_key=row["idempotency_key"],
        command=row["command"],
        request_sha256=row["request_sha256"],
        response=json.loads(row["response_json"]),
        created_at_ms=row["created_at_ms"],
    )


async def record_command(
    conn: asyncpg.Connection,
    *,
    study_id: str,
    idempotency_key: str,
    command: str,
    request_sha256: str,
    response: Mapping[str, Any],
) -> None:
    await conn.execute(
        """
        INSERT INTO research_golden_search_commands (study_id, idempotency_key, command, request_sha256, response_json, created_at_ms)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6)
        """,
        study_id,
        idempotency_key,
        command,
        request_sha256,
        _dumps(dict(response)),
        now_ms_utc(),
    )


async def insert_trial(
    conn: asyncpg.Connection,
    study_id: str,
    *,
    stage: str,
    kind: str,
    payload: Mapping[str, Any],
    fold_index: int | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO research_golden_search_trials (study_id, stage, fold_index, kind, payload_json, created_at_ms)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6)
        """,
        study_id,
        stage,
        fold_index,
        kind,
        _dumps(dict(payload)),
        now_ms_utc(),
    )


async def insert_trial_fenced(
    conn: asyncpg.Connection,
    study_id: str,
    attempt: int,
    *,
    stage: str,
    kind: str,
    payload: Mapping[str, Any],
    fold_index: int | None = None,
) -> None:
    async with conn.transaction():
        await fence.lock_current_attempt(conn, table=STUDIES, record_id=study_id, attempt=attempt)
        await insert_trial(conn, study_id, stage=stage, kind=kind, payload={**payload, "attempt": attempt}, fold_index=fold_index)


async def insert_trials_fenced(conn: asyncpg.Connection, study_id: str, attempt: int, trials: Sequence[Mapping[str, Any]]) -> None:
    """Append a procedure's path (``{stage, fold_index, kind, payload}`` each) in one fenced transaction."""
    async with conn.transaction():
        await fence.lock_current_attempt(conn, table=STUDIES, record_id=study_id, attempt=attempt)
        for trial in trials:
            await insert_trial(
                conn,
                study_id,
                stage=trial["stage"],
                fold_index=trial.get("fold_index"),
                kind=trial["kind"],
                payload={**trial["payload"], "attempt": attempt},
            )


async def list_trials(conn: asyncpg.Connection, study_id: str, *, kind: str | None = None) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT id, stage, fold_index, kind, payload_json::text AS payload_json, created_at_ms
          FROM research_golden_search_trials
         WHERE study_id = $1 AND ($2::text IS NULL OR kind = $2)
         ORDER BY id
        """,
        study_id,
        kind,
    )
    return [
        {
            "id": row["id"],
            "stage": row["stage"],
            "fold_index": row["fold_index"],
            "kind": row["kind"],
            "payload": json.loads(row["payload_json"]),
            "created_at_ms": row["created_at_ms"],
        }
        for row in rows
    ]


# ── Exposure ledger ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExposureOverlaps:
    ledger: int
    outside: int


async def exposure_overlaps(
    conn: asyncpg.Connection, *, symbol: str, start_ms: int, end_ms: int, exclude_study_id: str | None = None
) -> ExposureOverlaps:
    """Recorded use of ``[start_ms, end_ms)`` for ``symbol``: final tests opened, and other research that read it.

    Ledger overlaps count studies, from any strategy or revision. Outside
    activity is research that was never instrumented for exposure: Grid
    Search and Walk-Forward windows (half-open), saved backtests (start and
    end dates inclusive) and other studies' evaluations.
    """
    row = await conn.fetchrow(
        """
        SELECT
          (SELECT COUNT(DISTINCT e.study_id) FROM research_golden_search_exposures e
            WHERE e.symbol = $1 AND e.interval_start_ms < $3 AND e.interval_end_ms > $2) AS ledger,
          (SELECT COUNT(*) FROM research_grid_searches g
            WHERE upper(g.symbol) = $1
              AND (g.request_json ->> 'start_ms')::bigint < $3 AND (g.request_json ->> 'end_ms')::bigint > $2)
          + (SELECT COUNT(*) FROM research_walk_forward_studies w
            WHERE upper(w.symbol) = $1
              AND (w.request_json ->> 'start_ms')::bigint < $3 AND (w.request_json ->> 'end_ms')::bigint > $2)
          + (SELECT COUNT(*) FROM research_backtest_runs r
            WHERE upper(r.symbol) = $1 AND r.start_ms < $3 AND r.end_ms >= $2)
          + (SELECT COUNT(DISTINCT v.study_id) FROM research_golden_search_evaluations v
               JOIN research_golden_search_studies s ON s.id = v.study_id
            WHERE s.symbol = $1 AND ($4::text IS NULL OR v.study_id <> $4)
              AND v.window_start_ms < $3 AND v.window_end_ms > $2) AS outside
        """,
        symbol,
        start_ms,
        end_ms,
        exclude_study_id,
    )
    assert row is not None
    return ExposureOverlaps(ledger=int(row["ledger"]), outside=int(row["outside"]))


async def lock_exposure(conn: asyncpg.Connection, symbol: str) -> None:
    """Serialize exposure claims on one symbol for the rest of the caller's transaction."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext('golden_exposure:' || $1))", symbol)


async def insert_exposure(
    conn: asyncpg.Connection,
    *,
    symbol: str,
    start_ms: int,
    end_ms: int,
    study_id: str,
    strategy_key: str,
    kind: Literal["reserved", "result"],
    state_at_reservation: str,
    claim: str,
    candidate_point_hash: str,
    payload: Mapping[str, Any],
) -> None:
    await conn.execute(
        """
        INSERT INTO research_golden_search_exposures (
            symbol, interval_start_ms, interval_end_ms, study_id, strategy_key, kind,
            state_at_reservation, claim, candidate_point_hash, payload_json, created_at_ms
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11)
        """,
        symbol,
        start_ms,
        end_ms,
        study_id,
        strategy_key,
        kind,
        state_at_reservation,
        claim,
        candidate_point_hash,
        _dumps(dict(payload)),
        now_ms_utc(),
    )


async def finish_exam(
    conn: asyncpg.Connection,
    study_id: str,
    attempt: int,
    *,
    changes: Mapping[str, Any],
    results_patch: Mapping[str, Any],
    exposure: Mapping[str, Any],
) -> StudyRow:
    """The exam's outcome joins the exposure ledger and the study in one fenced transaction; a replay appends no second result."""
    async with conn.transaction():
        await fence.lock_current_attempt(conn, table=STUDIES, record_id=study_id, attempt=attempt)
        recorded = await conn.fetchval(
            "SELECT 1 FROM research_golden_search_exposures WHERE study_id = $1 AND kind = 'result'", study_id
        )
        if recorded is None:
            await insert_exposure(
                conn,
                symbol=exposure["symbol"],
                start_ms=exposure["start_ms"],
                end_ms=exposure["end_ms"],
                study_id=study_id,
                strategy_key=exposure["strategy_key"],
                kind="result",
                state_at_reservation=exposure["state_at_reservation"],
                claim=exposure["claim"],
                candidate_point_hash=exposure["candidate_point_hash"],
                payload=exposure["payload"],
            )
        return await update_study(conn, study_id, changes=changes, results_patch=results_patch)


async def list_exposures(conn: asyncpg.Connection, *, study_id: str | None = None, symbol: str | None = None) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT id, symbol, interval_start_ms, interval_end_ms, study_id, strategy_key, kind, state_at_reservation,
               claim, candidate_point_hash, payload_json::text AS payload_json, created_at_ms
          FROM research_golden_search_exposures
         WHERE ($1::text IS NULL OR study_id = $1) AND ($2::text IS NULL OR symbol = $2)
         ORDER BY id
        """,
        study_id,
        symbol,
    )
    return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]


# ── Evaluations and the budget ledger ────────────────────────────────────


@dataclass(frozen=True)
class NewEvaluation:
    evaluation_key: str
    point_hash: str
    point: dict[str, Any]
    window_start_ms: int
    window_end_ms: int
    scenario: str
    detail: bool
    stage: str
    fold_index: int | None


@dataclass(frozen=True)
class Reservation:
    """``run``: dispatch the engine; ``cached``: ``record`` already holds the result; ``exhausted``: no budget."""

    kind: Literal["run", "cached", "exhausted"]
    record: EvaluationRecord | None = None
    # 1 when ``cached`` is a true cache hit; 0 when a retry ran out and the row was just recorded failed.
    counted_as_cache_hit: int = 0


RETRY_EXHAUSTED = "retry allowance exhausted"
# Marks a pending row whose run was cancelled, so a later attempt re-runs it without spending a retry.
CANCELLED_BEFORE_RESULT = "cancelled before a result"


async def reserve_evaluation(
    conn: asyncpg.Connection,
    study_id: str,
    attempt: int,
    new: NewEvaluation,
    *,
    limit: int,
    retry_allowance: int,
) -> Reservation:
    """In one fenced transaction: answer from the cache, re-admit a crashed attempt's row, or reserve budget and insert."""
    now = now_ms_utc()
    async with conn.transaction():
        await fence.lock_current_attempt(conn, table=STUDIES, record_id=study_id, attempt=attempt)
        consumed = await conn.fetchval(f"SELECT consumed_evaluations FROM {STUDIES} WHERE id = $1", study_id)
        row = await conn.fetchrow(
            f"""
            SELECT {_EVALUATION_COLUMNS} FROM research_golden_search_evaluations
             WHERE study_id = $1 AND evaluation_key = $2 FOR UPDATE
            """,
            study_id,
            new.evaluation_key,
        )
        if row is None:
            if consumed + 1 > limit:
                return Reservation(kind="exhausted")
            await conn.execute(
                """
                INSERT INTO research_golden_search_evaluations (
                    study_id, evaluation_key, point_hash, point_json, window_start_ms, window_end_ms, scenario, detail,
                    stage, fold_index, status, attempt, created_at_ms
                ) VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9, $10, 'pending', $11, $12)
                """,
                study_id,
                new.evaluation_key,
                new.point_hash,
                _dumps(new.point),
                new.window_start_ms,
                new.window_end_ms,
                new.scenario,
                new.detail,
                new.stage,
                new.fold_index,
                attempt,
                now,
            )
            await conn.execute(
                f"UPDATE {STUDIES} SET consumed_evaluations = consumed_evaluations + 1, updated_at_ms = $2 WHERE id = $1",
                study_id,
                now,
            )
            return Reservation(kind="run")
        record = _evaluation(row)
        if record.status != "pending":
            await conn.execute(f"UPDATE {STUDIES} SET cache_hits = cache_hits + 1 WHERE id = $1", study_id)
            await insert_trial(
                conn,
                study_id,
                stage=new.stage,
                fold_index=new.fold_index,
                kind="cache_hit",
                payload={"evaluation_key": new.evaluation_key, "point_hash": new.point_hash, "attempt": attempt},
            )
            return Reservation(kind="cached", record=record, counted_as_cache_hit=1)
        if record.error == CANCELLED_BEFORE_RESULT:
            # A cancel is not a crash: the allowance bounds runs that died, not runs the owner stopped.
            await conn.execute(
                """
                UPDATE research_golden_search_evaluations SET attempt = $3, error = NULL
                 WHERE study_id = $1 AND evaluation_key = $2
                """,
                study_id,
                new.evaluation_key,
                attempt,
            )
            return Reservation(kind="run")
        if record.retries >= retry_allowance:
            failed = await conn.fetchrow(
                f"""
                UPDATE research_golden_search_evaluations
                   SET status = 'failed', error = $3, attempt = $4, completed_at_ms = $5
                 WHERE study_id = $1 AND evaluation_key = $2
                RETURNING {_EVALUATION_COLUMNS}
                """,
                study_id,
                new.evaluation_key,
                RETRY_EXHAUSTED,
                attempt,
                now,
            )
            assert failed is not None
            await insert_trial(
                conn,
                study_id,
                stage=new.stage,
                fold_index=new.fold_index,
                kind="evaluated",
                payload={"evaluation_key": new.evaluation_key, "point_hash": new.point_hash, "status": "failed", "error": RETRY_EXHAUSTED, "attempt": attempt},
            )
            return Reservation(kind="cached", record=_evaluation(failed))
        await conn.execute(
            """
            UPDATE research_golden_search_evaluations SET retries = retries + 1, attempt = $3
             WHERE study_id = $1 AND evaluation_key = $2
            """,
            study_id,
            new.evaluation_key,
            attempt,
        )
        return Reservation(kind="run")


async def release_evaluation(conn: asyncpg.Connection, study_id: str, attempt: int, evaluation_key: str) -> None:
    """Leave a cancelled run's row pending, marked so the next attempt re-runs it without spending a retry."""
    async with conn.transaction():
        await fence.lock_current_attempt(conn, table=STUDIES, record_id=study_id, attempt=attempt)
        await conn.execute(
            """
            UPDATE research_golden_search_evaluations SET error = $4
             WHERE study_id = $1 AND evaluation_key = $2 AND status = 'pending' AND attempt = $3
            """,
            study_id,
            evaluation_key,
            attempt,
            CANCELLED_BEFORE_RESULT,
        )


def _finite(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) else None


async def complete_evaluation(
    conn: asyncpg.Connection,
    study_id: str,
    attempt: int,
    evaluation_key: str,
    *,
    metrics: Metrics,
    detail: Mapping[str, Any] | None,
) -> None:
    """Record a pending evaluation's result under the attempt fence, with its trial."""
    async with conn.transaction():
        await fence.lock_current_attempt(conn, table=STUDIES, record_id=study_id, attempt=attempt)
        row = await conn.fetchrow(
            """
            UPDATE research_golden_search_evaluations
               SET status = $4, total_trades = $5, net_profit = $6, total_return_pct = $7, sharpe_ratio = $8,
                   max_drawdown_pct = $9, win_rate = $10, error = $11, detail_json = $12::jsonb, completed_at_ms = $13
             WHERE study_id = $1 AND evaluation_key = $2 AND status = 'pending' AND attempt = $3
            RETURNING point_hash, stage, fold_index, scenario, detail
            """,
            study_id,
            evaluation_key,
            attempt,
            metrics.status,
            metrics.total_trades,
            _finite(metrics.net_profit),
            _finite(metrics.total_return_pct),
            _finite(metrics.sharpe_ratio),
            _finite(metrics.max_drawdown_pct),
            _finite(metrics.win_rate),
            metrics.error,
            _dumps(dict(detail)) if detail is not None else None,
            now_ms_utc(),
        )
        if row is None:
            raise fence.StaleAttemptError(f"evaluation {evaluation_key} is not pending under attempt {attempt}")
        await insert_trial(
            conn,
            study_id,
            stage=row["stage"],
            fold_index=row["fold_index"],
            kind="evaluated",
            payload={
                "evaluation_key": evaluation_key,
                "point_hash": row["point_hash"],
                "scenario": row["scenario"],
                "detail": row["detail"],
                "status": metrics.status,
                "attempt": attempt,
            },
        )


async def consume_budget(
    conn: asyncpg.Connection, study_id: str, attempt: int, count: int, *, limit: int, step: str, once_key: str
) -> bool:
    """Atomically take ``count`` units for work run outside the evaluator (the proof); ``False`` when it does not fit.

    Each ``once_key`` is drawn at most once per study, whichever attempt drew
    it: a worker that died between the draw and its checkpoint resumes
    without drawing again, and the repeat reports ``True``.
    """
    if count < 1:
        raise ValueError("consume at least one evaluation")
    async with conn.transaction():
        await fence.lock_current_attempt(conn, table=STUDIES, record_id=study_id, attempt=attempt)
        drawn = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM research_golden_search_trials
                 WHERE study_id = $1 AND kind = 'approval'
                   AND payload_json->>'event' = 'consumed' AND payload_json->>'key' = $2
            )
            """,
            study_id,
            once_key,
        )
        if drawn:
            return True
        consumed = await conn.fetchval(f"SELECT consumed_evaluations FROM {STUDIES} WHERE id = $1", study_id)
        if consumed + count > limit:
            return False
        await conn.execute(
            f"UPDATE {STUDIES} SET consumed_evaluations = consumed_evaluations + $2, updated_at_ms = $3 WHERE id = $1",
            study_id,
            count,
            now_ms_utc(),
        )
        await insert_trial(
            conn, study_id, stage=step, kind="approval", payload={"event": "consumed", "key": once_key, "evaluations": count, "attempt": attempt}
        )
        return True


async def get_evaluation(conn: asyncpg.Connection, study_id: str, evaluation_key: str) -> EvaluationRecord | None:
    row = await conn.fetchrow(
        f"SELECT {_EVALUATION_COLUMNS} FROM research_golden_search_evaluations WHERE study_id = $1 AND evaluation_key = $2",
        study_id,
        evaluation_key,
    )
    return _evaluation(row) if row is not None else None


async def find_detail_evaluation(
    conn: asyncpg.Connection, study_id: str, *, point_hash: str, window_start_ms: int, window_end_ms: int
) -> EvaluationRecord | None:
    """The completed base-scenario detail run of one point over one window."""
    row = await conn.fetchrow(
        f"""
        SELECT {_EVALUATION_COLUMNS} FROM research_golden_search_evaluations
         WHERE study_id = $1 AND point_hash = $2 AND window_start_ms = $3 AND window_end_ms = $4
           AND scenario = 'base' AND detail AND status <> 'pending'
         ORDER BY completed_at_ms DESC NULLS LAST, evaluation_key
         LIMIT 1
        """,
        study_id,
        point_hash,
        window_start_ms,
        window_end_ms,
    )
    return _evaluation(row) if row is not None else None


async def count_recorded_evaluations(conn: asyncpg.Connection, study_id: str, steps: Sequence[str]) -> int:
    """Evaluations with a recorded result for the named steps — a running stage's progress."""
    value = await conn.fetchval(
        """
        SELECT COUNT(*) FROM research_golden_search_evaluations
         WHERE study_id = $1 AND stage = ANY($2::text[]) AND status <> 'pending'
        """,
        study_id,
        list(steps),
    )
    return int(value or 0)


async def count_procedure_evaluations(
    conn: asyncpg.Connection, study_id: str, *, step: str, fold_index: int | None, window_start_ms: int, window_end_ms: int
) -> int:
    """Distinct points a procedure had the engine score over its window, a batch the budget cut short included."""
    value = await conn.fetchval(
        """
        SELECT COUNT(*) FROM research_golden_search_evaluations
         WHERE study_id = $1 AND stage = $2 AND fold_index IS NOT DISTINCT FROM $3
           AND window_start_ms = $4 AND window_end_ms = $5 AND scenario = 'base' AND NOT detail AND status <> 'pending'
        """,
        study_id,
        step,
        fold_index,
        window_start_ms,
        window_end_ms,
    )
    return int(value or 0)


async def consumed_outside_evaluator(conn: asyncpg.Connection, study_id: str, step: str) -> int:
    value = await conn.fetchval(
        """
        SELECT COALESCE(SUM((payload_json ->> 'evaluations')::int), 0) FROM research_golden_search_trials
         WHERE study_id = $1 AND kind = 'approval' AND stage = $2 AND payload_json ->> 'event' = 'consumed'
        """,
        study_id,
        step,
    )
    return int(value or 0)


async def list_evaluations(
    conn: asyncpg.Connection,
    study_id: str,
    *,
    stage: str | None = None,
    fold_index: int | None = None,
    page: int = 1,
    page_size: int = 50,
) -> EvaluationPage:
    if page < 1 or not 1 <= page_size <= 500:
        raise ValueError("page must be positive and page_size between 1 and 500")
    where = "study_id = $1 AND ($2::text IS NULL OR stage = $2) AND ($3::int IS NULL OR fold_index = $3)"
    total = await conn.fetchval(f"SELECT COUNT(*) FROM research_golden_search_evaluations WHERE {where}", study_id, stage, fold_index)
    rows = await conn.fetch(
        f"""
        SELECT {_EVALUATION_COLUMNS} FROM research_golden_search_evaluations
         WHERE {where}
         ORDER BY created_at_ms, evaluation_key
         LIMIT $4 OFFSET $5
        """,
        study_id,
        stage,
        fold_index,
        page_size,
        (page - 1) * page_size,
    )
    return EvaluationPage(total=int(total or 0), page=page, page_size=page_size, rows=tuple(_evaluation(row) for row in rows))
