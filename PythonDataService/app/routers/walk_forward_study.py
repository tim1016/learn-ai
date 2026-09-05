"""Walk-Forward Study HTTP boundary (PRD #1925 "HTTP contract").

``router`` — ``/api/research/walk-forward-studies``: preflight, history,
detail (folds and verdict), delete. ``jobs_router`` —
``POST /api/jobs-internal/walk-forward-study``, dispatched by the .NET jobs
boundary with a minted ``job_id`` (a new study, or Finish of an incomplete
one via ``resume_study_id``). Per-fold sweeps are read through the Grid
Search surface by their search ids.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any

from anyio import to_thread
from fastapi import APIRouter, HTTPException, Query, Response, status

from app.jobs.progress import ProgressEmitter
from app.jobs.runner import run_in_thread
from app.research.grid_search.engine_adapter import default_execute_cell
from app.research.persistence.db import connection
from app.research.walk_forward_study import repository as repo
from app.research.walk_forward_study import service
from app.research.walk_forward_study.models import StudyRow, StudySpec
from app.routers.grid_search import CANCEL_ACK_TIMEOUT_SECONDS, range_from_request
from app.schemas.walk_forward_study import (
    FoldPlanResponse,
    FoldResponse,
    VerdictResponse,
    WalkForwardStudyDetailResponse,
    WalkForwardStudyJobRequest,
    WalkForwardStudyPreflightResponse,
    WalkForwardStudySpecRequest,
    WalkForwardStudySummaryResponse,
)
from app.utils.session_anchors import et_midnight_ms

router = APIRouter()
jobs_router = APIRouter()
logger = logging.getLogger(__name__)


def spec_from_request(body: WalkForwardStudySpecRequest) -> StudySpec:
    return StudySpec(
        strategy_key=body.strategy_key,
        symbol=body.symbol.strip().upper(),
        param_ranges={name: range_from_request(spec) for name, spec in body.param_ranges.items()},
        start_ms=body.start_ms,
        end_ms=body.end_ms,
        training_months=body.training_months,
        test_months=body.test_months,
        resolution=body.resolution,
        fill_mode=body.fill_mode,
        commission_per_order=body.commission_per_order,
        slippage_per_share=body.slippage_per_share,
        initial_cash=body.initial_cash,
        measure=body.measure,
        min_trades=body.min_trades,
    )


def _refused(exc: service.GridSearchRefusal) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": exc.code, "message": str(exc)})


def _live(row: StudyRow) -> bool | None:
    return service.job_is_live(row.job_id) if row.status in ("queued", "running") else False


def _summary(row: StudyRow, *, live: bool | None) -> dict[str, Any]:
    return {
        "id": row.id,
        "strategy_key": row.strategy_key,
        "symbol": row.symbol,
        "status": service.presented_status(row, live=live),
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
        "uncommitted_changes": service.uncommitted_changes(row),
        "failure_reason": row.failure_reason,
    }


# ── Research surface ─────────────────────────────────────────────────────


@router.post("/preflight", response_model=WalkForwardStudyPreflightResponse)
async def preflight_walk_forward_study(body: WalkForwardStudySpecRequest) -> WalkForwardStudyPreflightResponse:
    """Validate the folds and the grid, size the workload, and plan the run-up without launching."""
    try:
        pre = await to_thread.run_sync(partial(service.preflight, spec_from_request(body)))
    except service.GridSearchRefusal as exc:
        raise _refused(exc) from exc
    return WalkForwardStudyPreflightResponse(
        strategy_key=pre.spec.strategy_key,
        symbol=pre.spec.symbol,
        combinations=pre.combinations,
        fold_count=len(pre.folds),
        total_backtests=pre.total_backtests,
        backtest_limit=service.sweeps.MAX_TOTAL_BACKTESTS,
        estimated_seconds=pre.estimated_seconds,
        required_samples=pre.required_samples,
        run_up_sessions=pre.run_up_sessions,
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
    async with connection() as conn:
        rows = await repo.list_studies(
            conn,
            strategy_key=strategy_key,
            symbol=symbol.strip().upper() if symbol else None,
            status=None if status_filter in (None, "interrupted") else status_filter,
            job_id=job_id,
            limit=limit,
        )
    summaries = [WalkForwardStudySummaryResponse(**_summary(row, live=_live(row))) for row in rows]
    if status_filter == "interrupted":
        return [summary for summary in summaries if summary.status == "interrupted"]
    return summaries


async def _load(study_id: str) -> StudyRow:
    async with connection() as conn:
        row = await repo.get_study(conn, study_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"walk-forward study {study_id} not found")
    return row


@router.get("/{study_id}", response_model=WalkForwardStudyDetailResponse)
async def get_walk_forward_study(study_id: str) -> WalkForwardStudyDetailResponse:
    row = await _load(study_id)
    live = _live(row)
    refusal = service.resume_refusal(row, live=live)
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
    if row.job_id and _live(row):
        service.request_cancel(row.job_id)
        deadline = asyncio.get_running_loop().time() + CANCEL_ACK_TIMEOUT_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.25)
            async with connection() as conn:
                current = await repo.get_study(conn, study_id)
            if current is None or current.status not in ("queued", "running"):
                break
        else:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="the running study has not acknowledged cancellation yet; try again shortly",
            )
    async with connection() as conn:
        await repo.delete_study(conn, study_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Jobs boundary ────────────────────────────────────────────────────────


@jobs_router.post("/walk-forward-study", status_code=status.HTTP_202_ACCEPTED)
async def start_walk_forward_study_job(req: WalkForwardStudyJobRequest) -> dict[str, Any]:
    """Launch (or Finish) a study on a worker thread. Returns 202 once the record is durable."""
    if req.resume_study_id:
        row = await _load(req.resume_study_id)
        refusal = await to_thread.run_sync(partial(service.resume_refusal, row, live=_live(row), verify_data=True))
        if refusal is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "NOT_RESUMABLE", "message": refusal})
        study_id = row.id
    else:
        try:
            record = await to_thread.run_sync(partial(service.prepare_launch, spec_from_request(req), job_id=req.job_id))
        except service.GridSearchRefusal as exc:
            raise _refused(exc) from exc
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
        return {
            "study_id": outcome.study_id,
            "status": outcome.status,
            "verdict": outcome.verdict.as_dict() if outcome.verdict else None,
        }

    run_in_thread(req.job_id, work, thread_name=f"walk-forward-study-{req.job_id[:8]}", cancel_check_every_n=1)
    return {"job_id": req.job_id, "study_id": study_id, "status": "queued"}
