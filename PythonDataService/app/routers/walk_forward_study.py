"""Walk-Forward Study HTTP boundary (#1925 "HTTP contract").

``router`` — ``/api/research/walk-forward-studies``: preflight, history,
detail (folds and verdict), the folds' test-window curves, delete. ``jobs_router`` —
``POST /api/jobs-internal/walk-forward-study``, dispatched by the .NET jobs
boundary with a minted ``job_id`` (a new study, or Finish of an incomplete
one via ``resume_study_id``). Per-fold sweeps are read through the Grid
Search surface by their search ids.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from functools import partial
from typing import Any

from anyio import to_thread
from fastapi import APIRouter, HTTPException, Query, Response, status

from app.jobs.progress import CancellationCheck, ProgressEmitter
from app.jobs.runner import run_in_thread
from app.lean_sidecar.trading_calendar import session_windows_ms_utc
from app.research.grid_search import repository as sweep_repo
from app.research.grid_search.engine_adapter import default_execute_cell
from app.research.grid_search.service import MAX_TOTAL_BACKTESTS, GridSearchRefusal, window_dates
from app.research.persistence import lifecycle
from app.research.persistence.db import with_connection
from app.research.session_curves import curves_read
from app.research.walk_forward_study import repository as repo
from app.research.walk_forward_study import service
from app.research.walk_forward_study.models import FoldRecord, StudyRow, StudySpec
from app.routers import research_records as records
from app.schemas.walk_forward_study import (
    FoldPlanResponse,
    FoldResponse,
    VerdictResponse,
    WalkForwardFoldCurvesResponse,
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


def _test_window_closes(folds: Sequence[FoldRecord]) -> list[int]:
    """Every scheduled session close inside a fold's test window, from the canonical calendar."""
    if not folds:
        return []
    first, last = window_dates(min(fold.test_start_ms for fold in folds), max(fold.test_end_ms for fold in folds))
    closes = (window.close_ms_utc for window in session_windows_ms_utc(first, last))
    return [close for close in closes if any(fold.test_start_ms <= close < fold.test_end_ms for fold in folds)]


@router.get("/{study_id}/fold-curves", response_model=WalkForwardFoldCurvesResponse)
async def get_walk_forward_fold_curves(study_id: str) -> WalkForwardFoldCurvesResponse:
    """Each completed fold's winner over its test window: cumulative return and fall from peak at each session close (#2936).

    Every fold's test run starts from the study's starting capital, so each
    curve starts flat. The sessions are those of every fold's test window, so
    a fold with no curve — failed, not yet run, or recorded without one —
    stands on the axis as a gap and nothing is drawn across it.
    """
    row = await _load(study_id)
    # A completed fold's evidence is its winner's cell in the fold's test sweep.
    tested = {
        fold.fold_index: (fold.test_search_id, fold.winner_params_hash)
        for fold in row.folds
        if fold.status == "completed" and fold.test_search_id is not None and fold.winner_params_hash is not None
    }
    kept = await with_connection(sweep_repo.curves_of, list(tested.values()))
    return WalkForwardFoldCurvesResponse.model_validate(
        curves_read(
            list(tested),
            {index: kept[cell] for index, cell in tested.items() if cell in kept},
            StudySpec.from_request_dict(row.request).grid.initial_cash,
            key="fold_index",
            also=_test_window_closes(row.folds),
        )
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
    """Launch (or Finish) a study. Returns 202 once the record is durable and a worker holds it.

    In research mode the API forwards the dispatch and never prepares,
    stamps or starts a thread: the runtime prepares the study, and each
    fold's sweeps run their cells through the shared research workers
    under the checkpoint lease (#3050). Here — the local mode, or this
    process being the runtime — the study runs on a worker thread exactly
    as before.
    """
    from app.services.research_runtime_client import (
        forward_walk_forward_study_job,
        is_research_runtime,
        research_forwarding_enabled,
    )

    if research_forwarding_enabled() and not is_research_runtime():
        return await forward_walk_forward_study_job(req.model_dump(mode="json"))

    # Refused authority must never bind (#3042): the check runs before any
    # durable record exists.
    await records.refuse_unauthorized_dispatch()
    if req.resume_study_id:
        row = await _load(req.resume_study_id)
        if row.job_id == req.job_id and row.status in records.STORED_LIVE_STATUSES:
            # A redelivery of a live dispatch joins the first worker before
            # any resume refusal can call it unresumable — the attempt is
            # never claimed twice (#3050).
            records.require_live_redelivery(req.job_id, noun=NOUN)
            return {"job_id": req.job_id, "study_id": row.id, "status": "queued"}
        refusal = await to_thread.run_sync(partial(_resume_refusal, row, live=records.liveness(row), verify_data=True))
        if refusal is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "NOT_RESUMABLE", "message": refusal})
        study_id = row.id
        await records.stamp_research_dispatch(job_id=req.job_id, kind="walk_forward", record_id=study_id)
    else:
        # A fresh launch is idempotent under its job id: a study already
        # bound to it is a redelivery, never a second study (#3050).
        bound = await with_connection(repo.list_studies, job_id=req.job_id, limit=1)
        if bound:
            records.require_live_redelivery(req.job_id, noun=NOUN)
            return {"job_id": req.job_id, "study_id": bound[0].id, "status": "queued"}
        try:
            record = await to_thread.run_sync(partial(service.prepare_launch, to_study_spec(req), job_id=req.job_id))
        except GridSearchRefusal as exc:
            raise records.refused(exc) from exc
        study_id = record.id
        # The stamp precedes the durable row (#3042's lesson, grid's order):
        # a stamp refusal strands nothing, a create failure retries as a
        # fresh dispatch.
        await records.stamp_research_dispatch(job_id=req.job_id, kind="walk_forward", record_id=study_id)
        await service.create(record)

    runtime_executes = is_research_runtime()
    work = _gated_study_work(req, study_id) if runtime_executes else _local_study_work(req, study_id)
    run_in_thread(
        req.job_id,
        work,
        thread_name=f"walk-forward-study-{req.job_id[:8]}",
        cancel_check_every_n=1,
        # In the runtime, emits renew nothing — only accepted checkpoints
        # keep the lease alive (#3041).
        emit_renews_lease=not runtime_executes,
        progress_min_interval_s=1.0 if runtime_executes else 0.0,
    )
    return {"job_id": req.job_id, "study_id": study_id, "status": "queued"}


def _local_study_work(req: WalkForwardStudyJobRequest, study_id: str) -> Callable[[ProgressEmitter, CancellationCheck], dict[str, Any]]:
    """The local-mode worker: the study behind the runner's emit-and-cancel checks, cells in this process."""

    def work(emit: ProgressEmitter, cancel: CancellationCheck) -> dict[str, Any]:
        outcome = service.execute(
            study_id,
            job_id=req.job_id,
            cell_executor=partial(default_execute_cell, cancel_check=cancel.raise_if_cancelled),
            cancel_check=cancel.raise_if_cancelled,
            on_phase=emit.phase,
            on_progress=lambda done, total: emit.progress(done, total, unit="backtests"),
            on_log=emit.log,
        )
        return _outcome_response(outcome)

    return work


def _gated_study_work(req: WalkForwardStudyJobRequest, study_id: str) -> Callable[[ProgressEmitter, CancellationCheck], dict[str, Any]]:
    """The research runtime's study worker: the folds under the checkpoint lease contract (#3041, #3050).

    The folds stay strictly sequential — train, select, test, then the next
    fold — exactly as the local mode runs them; what the runtime changes is
    only where each sweep's ready cells compute. One dispatch per sweep
    joins the shared pool as its own owner, so a study never holds capacity
    its dependencies cannot use yet.
    """
    from app.jobs.checkpoints import GatedLease
    from app.research.grid_search.dispatch import runtime_cell_dispatch
    from app.research.persistence import ownership

    def work(emit: ProgressEmitter, _runner_cancel: CancellationCheck) -> dict[str, Any]:
        authority = ownership.process_authority()
        if authority is None:
            # The startup claim is fail-closed, so this is an authority lost
            # mid-run: the generation is dead and its lease must not renew.
            raise RuntimeError("research runtime worker without coordinator authority; the generation is fenced")
        lease = GatedLease(req.job_id, task_id="study", authority=authority)
        outcome = service.execute(
            study_id,
            job_id=req.job_id,
            cell_dispatch_factory=lambda row, spec, attempt, job_id: runtime_cell_dispatch(
                row,
                spec,
                attempt=attempt,
                job_id=job_id,
                on_checkpoint=lease.child_checkpoint,
                cancel_check=lease.fenced_cancel,
                on_log=emit.log,
                on_waiting=_waiting_emitter(emit),
            ),
            cancel_check=lease.fenced_cancel,
            on_phase=lambda phase: (emit.phase(phase), lease.phase(phase)),
            on_progress=lambda done, total: (emit.progress(done, total, unit="backtests"), lease.completion(done, total)),
            on_log=emit.log,
        )
        return _outcome_response(outcome)

    return work




def _waiting_emitter(emit):
    """The waiting state as a transition-gated event (#3052)."""
    last = {"waiting": object()}

    def emit_waiting(waiting, facts) -> None:
        if waiting != last["waiting"]:
            last["waiting"] = waiting
            emit.research_waiting(waiting, detail={k: facts.get(k) for k in ("workers", "busy_lanes", "active", "queued", "ready_width") if k in facts})

    return emit_waiting

def _outcome_response(outcome: service.StudyOutcome) -> dict[str, Any]:
    return {"study_id": outcome.study_id, "status": outcome.status, "verdict": outcome.verdict.as_dict() if outcome.verdict else None}
