"""Walk-Forward Study HTTP boundary (PRD #1925 "HTTP contract").

``router`` — ``/api/research/walk-forward-studies``: preflight, history,
detail (folds and verdict), delete. ``jobs_router`` —
``POST /api/jobs-internal/walk-forward-study``, dispatched by the .NET jobs
boundary with a minted ``job_id`` (a new study, or Finish of an incomplete
one via ``resume_study_id``). Per-fold sweeps are read through the Grid
Search surface by their search ids.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import Any

from anyio import to_thread
from fastapi import APIRouter, HTTPException, Query, Response, status

from app.jobs.progress import ProgressEmitter
from app.jobs.runner import run_in_thread
from app.research.grid_search.engine_adapter import default_execute_cell
from app.research.grid_search.service import MAX_TOTAL_BACKTESTS, GridSearchRefusal
from app.research.persistence import lifecycle
from app.research.persistence.db import with_connection
from app.research.walk_forward_study import repository as repo
from app.research.walk_forward_study import service
from app.research.walk_forward_study.models import StudyRow
from app.routers import research_records as records
from app.schemas.walk_forward_study import (
    FoldPlanResponse,
    FoldResponse,
    VerdictResponse,
    WalkForwardStudyDetailResponse,
    WalkForwardStudyJobRequest,
    WalkForwardStudyPreflightResponse,
    WalkForwardStudySpecRequest,
    WalkForwardStudySummaryResponse,
    to_study_spec,
)
from app.utils.session_anchors import et_midnight_ms

router = APIRouter()
jobs_router = APIRouter()
logger = logging.getLogger(__name__)
NOUN = "study"


def _summary(row: StudyRow, *, live: bool | None) -> dict[str, Any]:
    return {
        "id": row.id,
        "strategy_key": row.strategy_key,
        "symbol": row.symbol,
        "status": lifecycle.presented_status(row, live=live),
        "job_id": row.job_id,
        "created_at_ms": row.created_at_ms,
        "finished_at_ms": row.finished_at_ms,
        "window_start_ms": int(row.request["start_ms"]),
        "window_end_ms": int(row.request["end_ms"]),
        "training_months": int(row.request["training_months"]),
        "test_months": int(row.request["test_months"]),
        "measure": row.request["measure"],
        "min_trades": int(row.request["min_trades"]),
        "fold_count": len(row.folds),
        "completed_folds": sum(1 for fold in row.folds if fold.status == "completed"),
        "failed_folds": sum(1 for fold in row.folds if fold.status == "failed"),
        "expected_backtests": row.expected_backtests,
        "completed_backtests": row.completed_backtests,
        "verdict": VerdictResponse(**row.verdict) if row.verdict else None,
        "winner_changes": service.winner_changes(row.folds),
        "incomplete": row.incomplete,
        "uncommitted_changes": lifecycle.uncommitted_changes(row),
        "failure_reason": row.failure_reason,
    }


def _resume_refusal(row: StudyRow, *, live: bool | None, verify_data: bool = False) -> str | None:
    return lifecycle.resume_refusal(row, noun=NOUN, unit="fold", live=live, verify_data=verify_data)


# ── Research surface ─────────────────────────────────────────────────────


@router.post("/preflight", response_model=WalkForwardStudyPreflightResponse)
async def preflight_walk_forward_study(body: WalkForwardStudySpecRequest) -> WalkForwardStudyPreflightResponse:
    """Validate the folds and the grid, size the workload, and plan the run-up without launching."""
    try:
        pre = await to_thread.run_sync(partial(service.preflight, to_study_spec(body)))
    except GridSearchRefusal as exc:
        raise records.refused(exc) from exc
    return WalkForwardStudyPreflightResponse(
        strategy_key=pre.spec.grid.strategy_key,
        symbol=pre.spec.grid.symbol,
        combinations=pre.sweep.combinations,
        fold_count=len(pre.folds),
        total_backtests=pre.sweep.total_backtests,
        backtest_limit=MAX_TOTAL_BACKTESTS,
        estimated_seconds=pre.sweep.estimated_seconds,
        required_samples=pre.sweep.run_up.required_samples,
        run_up_sessions=pre.sweep.run_up.run_up_sessions,
        folds=[
            FoldPlanResponse(
                fold_index=fold.fold_index,
                train_start_ms=et_midnight_ms(fold.train_start),
                train_end_ms=et_midnight_ms(fold.train_end),
                test_start_ms=et_midnight_ms(fold.test_start),
                test_end_ms=et_midnight_ms(fold.test_end),
            )
            for fold in pre.folds
        ],
    )


@router.get("", response_model=list[WalkForwardStudySummaryResponse])
async def list_walk_forward_studies(
    strategy_key: str | None = Query(None),
    symbol: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    job_id: str | None = Query(None, description="The launch's job id, so a client can find the study it just started"),
    limit: int = Query(200, ge=1, le=1000),
) -> list[WalkForwardStudySummaryResponse]:
    statuses, fetch_limit = records.stored_status_query(status_filter, limit)
    rows = await with_connection(
        repo.list_studies,
        strategy_key=strategy_key,
        symbol=symbol.strip().upper() if symbol else None,
        statuses=statuses,
        job_id=job_id,
        limit=fetch_limit,
    )
    summaries = [WalkForwardStudySummaryResponse(**_summary(row, live=records.liveness(row))) for row in rows]
    return records.cut_to_presented(summaries, status_filter, limit)


async def _load(study_id: str) -> StudyRow:
    row = await with_connection(repo.get_study, study_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"walk-forward study {study_id} not found")
    return row


@router.get("/{study_id}", response_model=WalkForwardStudyDetailResponse)
async def get_walk_forward_study(study_id: str) -> WalkForwardStudyDetailResponse:
    row = await _load(study_id)
    live = records.liveness(row)
    refusal = _resume_refusal(row, live=live)
    return WalkForwardStudyDetailResponse(
        **_summary(row, live=live),
        request=row.request,
        receipt=row.receipt,
        folds=[FoldResponse(**fold.as_dict()) for fold in row.folds],
        resumable=refusal is None,
        resume_refusal=refusal,
    )


@router.delete("/{study_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_walk_forward_study(study_id: str) -> Response:
    """Cancel a running study first and wait for the worker's acknowledgement, then remove it and its sweeps."""
    row = await _load(study_id)
    if row.job_id and records.liveness_or_503(row, noun=NOUN):

        async def current_status() -> str | None:
            current = await with_connection(repo.get_study, study_id)
            return current.status if current else None

        await records.cancel_and_await_ack(row.job_id, current_status, noun=NOUN)
    await with_connection(repo.delete_study, study_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Jobs boundary ────────────────────────────────────────────────────────


@jobs_router.post("/walk-forward-study", status_code=status.HTTP_202_ACCEPTED)
async def start_walk_forward_study_job(req: WalkForwardStudyJobRequest) -> dict[str, Any]:
    """Launch (or Finish) a study on a worker thread. Returns 202 once the record is durable."""
    if req.resume_study_id:
        row = await _load(req.resume_study_id)
        refusal = await to_thread.run_sync(partial(_resume_refusal, row, live=records.liveness(row), verify_data=True))
        if refusal is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "NOT_RESUMABLE", "message": refusal})
        study_id = row.id
    else:
        try:
            record = await to_thread.run_sync(partial(service.prepare_launch, to_study_spec(req), job_id=req.job_id))
        except GridSearchRefusal as exc:
            raise records.refused(exc) from exc
        created = await service.create(record)
        study_id = created.id

    def work(emit: ProgressEmitter, cancel) -> dict[str, Any]:
        outcome = service.execute(
            study_id,
            job_id=req.job_id,
            cell_executor=default_execute_cell,
            cancel_check=cancel.raise_if_cancelled,
            on_phase=emit.phase,
            on_progress=lambda done, total: emit.progress(done, total, unit="backtests"),
            on_log=emit.log,
        )
        return {"study_id": outcome.study_id, "status": outcome.status, "verdict": outcome.verdict.as_dict() if outcome.verdict else None}

    run_in_thread(req.job_id, work, thread_name=f"walk-forward-study-{req.job_id[:8]}", cancel_check_every_n=1)
    return {"job_id": req.job_id, "study_id": study_id, "status": "queued"}
