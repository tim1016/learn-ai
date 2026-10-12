"""Grid Search HTTP boundary (PRD #1926 "HTTP contract").

``router`` — the research surface under ``/api/research/grid-search``:
preflight, history listing, detail, server-paged cells, and delete.
``jobs_router`` — ``POST /api/jobs-internal/grid-search``, dispatched by
the .NET jobs boundary with a minted ``job_id`` (a new search, or Finish of
an incomplete one via ``resume_search_id``). Every temporal value on the
wire is ``int64 ms UTC``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial
from typing import Annotated, Any, Literal

from anyio import to_thread
from fastapi import APIRouter, HTTPException, Query, Response, status

from app.jobs.progress import CancellationCheck, ProgressEmitter
from app.jobs.runner import run_in_thread
from app.research.grid_search import repository as repo
from app.research.grid_search import service
from app.research.grid_search.engine_adapter import default_execute_cell
from app.research.grid_search.models import CellRow, GridSearchSpec, SearchRow
from app.research.persistence import lifecycle
from app.research.persistence.db import with_connection
from app.research.session_curves import curves_read
from app.research.sweep.ranking import is_eligible
from app.routers import research_records as records
from app.schemas.grid_search import (
    GridSearchCellCurvesResponse,
    GridSearchCellPageResponse,
    GridSearchCellResponse,
    GridSearchDetailResponse,
    GridSearchJobRequest,
    GridSearchPreflightResponse,
    GridSearchSpecRequest,
    GridSearchSummaryResponse,
    RunUpPlanResponse,
    SearchOwnerResponse,
    to_grid_spec,
)
from app.utils.session_anchors import et_day_end_ms, et_midnight_ms

router = APIRouter()
jobs_router = APIRouter()
logger = logging.getLogger(__name__)
NOUN = "search"
#: The most curves one read returns: an overlay past this is unreadable.
MAX_CURVES = 200


def _summary(row: SearchRow, *, live: bool | None) -> dict[str, Any]:
    return {
        "id": row.id,
        "owner": SearchOwnerResponse(kind=row.owner.kind, owner_id=row.owner.owner_id, fold_index=row.owner.fold_index, phase=row.owner.phase),
        "strategy_key": row.strategy_key,
        "symbol": row.symbol,
        "status": lifecycle.presented_status(row, live=live),
        "job_id": row.job_id,
        "created_at_ms": row.created_at_ms,
        "finished_at_ms": row.finished_at_ms,
        "window_start_ms": int(row.request["start_ms"]),
        "window_end_ms": int(row.request["end_ms"]),
        "measure": row.request["measure"],
        "min_trades": int(row.request["min_trades"]),
        "expected_cells": row.expected_cells,
        "completed_cells": row.completed_cells,
        "failed_cells": row.failed_cells,
        "leader_params_hash": row.leader_params_hash,
        "leader_params": row.leader_params,
        "incomplete": row.incomplete,
        "uncommitted_changes": lifecycle.uncommitted_changes(row),
        "failure_reason": row.failure_reason,
    }


def _cell_response(cell: CellRow, *, leader_hash: str | None, measure: str, min_trades: int) -> GridSearchCellResponse:
    return GridSearchCellResponse(
        **{k: v for k, v in cell.as_dict().items() if k not in ("search_id",)},
        is_leader=cell.params_hash == leader_hash,
        eligible=is_eligible(cell, measure, min_trades=min_trades),
    )


def _resume_refusal(row: SearchRow, *, live: bool | None, verify_data: bool = False) -> str | None:
    return lifecycle.resume_refusal(row, noun=NOUN, unit="cell", live=live, verify_data=verify_data)


# ── Research surface ─────────────────────────────────────────────────────


@router.post("/preflight", response_model=GridSearchPreflightResponse)
async def preflight_grid_search(body: GridSearchSpecRequest) -> GridSearchPreflightResponse:
    """Validate, size, and plan a search without launching it."""
    try:
        pre = await to_thread.run_sync(partial(service.preflight, to_grid_spec(body)))
    except service.GridSearchRefusal as exc:
        raise records.refused(exc) from exc
    return GridSearchPreflightResponse(
        strategy_key=pre.spec.strategy_key,
        symbol=pre.spec.symbol,
        combinations=pre.combinations,
        total_backtests=pre.total_backtests,
        backtest_limit=service.MAX_TOTAL_BACKTESTS,
        estimated_seconds=pre.estimated_seconds,
        run_up=RunUpPlanResponse(
            data_start_ms=et_midnight_ms(pre.data_start),
            evaluation_start_ms=et_midnight_ms(pre.evaluation_start),
            evaluation_end_ms=et_day_end_ms(pre.evaluation_end),
            required_samples=pre.run_up.required_samples,
            bar_span_ms=pre.run_up.bar_span_ms,
            run_up_sessions=pre.run_up.run_up_sessions,
            carved_from_range=pre.run_up.carved_from_range,
        ),
        expected_sessions=pre.expected_sessions,
    )


@router.get("", response_model=list[GridSearchSummaryResponse])
async def list_grid_searches(
    strategy_key: str | None = Query(None),
    symbol: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    job_id: str | None = Query(None, description="The launch's job id, so a client can find the search it just started"),
    limit: int = Query(200, ge=1, le=1000),
) -> list[GridSearchSummaryResponse]:
    """History: user-launched searches only, newest first. Walk-forward-owned sweeps never appear."""
    statuses, fetch_limit = records.stored_status_query(status_filter, limit)
    rows = await with_connection(
        repo.list_searches,
        strategy_key=strategy_key,
        symbol=symbol.strip().upper() if symbol else None,
        statuses=statuses,
        job_id=job_id,
        limit=fetch_limit,
    )
    summaries = [GridSearchSummaryResponse(**_summary(row, live=records.liveness(row))) for row in rows]
    return records.cut_to_presented(summaries, status_filter, limit)


async def _load(search_id: str) -> SearchRow:
    row = await with_connection(repo.get_search, search_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"grid search {search_id} not found")
    return row


def _refuse_if_owned(row: SearchRow) -> None:
    """A study's fold sweep is that study's evidence: it is finished, deleted and resumed with the study, never here."""
    if row.owner.kind != "user":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "OWNED_BY_STUDY", "message": f"grid search {row.id} belongs to walk-forward study {row.owner.owner_id}; manage it through the study"},
        )


@router.get("/{search_id}", response_model=GridSearchDetailResponse)
async def get_grid_search(search_id: str) -> GridSearchDetailResponse:
    row = await _load(search_id)
    live = records.liveness(row)
    # Status, tree state and code identity only; the data-snapshot re-hash is
    # reserved for the Finish request, where it decides something.
    refusal = _resume_refusal(row, live=live)
    return GridSearchDetailResponse(
        **_summary(row, live=live),
        request=row.request,
        receipt=row.receipt,
        resumable=refusal is None,
        resume_refusal=refusal,
    )


@router.get("/{search_id}/cells", response_model=GridSearchCellPageResponse)
async def list_grid_search_cells(
    search_id: str,
    sort_by: str = Query("sharpe_ratio"),
    direction: Literal["asc", "desc"] = Query("desc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    pin_leader: bool = Query(False, description="Put the leader's cell first whatever the sort (a study-owned sweep's evidence row)"),
) -> GridSearchCellPageResponse:
    row = await _load(search_id)
    try:
        page_result = await with_connection(
            repo.list_cells,
            search_id,
            sort_by=sort_by,
            direction=direction,
            page=page,
            page_size=page_size,
            pin_params_hash=row.leader_params_hash if pin_leader else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return GridSearchCellPageResponse(
        total=page_result.total,
        page=page_result.page,
        page_size=page_result.page_size,
        sort_by=sort_by,
        direction=direction,
        without_curve=page_result.without_curve,
        cells=[
            _cell_response(cell, leader_hash=row.leader_params_hash, measure=row.request["measure"], min_trades=int(row.request["min_trades"]))
            for cell in page_result.cells
        ],
    )


@router.get("/{search_id}/cells/curves", response_model=GridSearchCellCurvesResponse)
async def get_grid_search_cell_curves(
    search_id: str,
    params_hash: Annotated[list[str], Query(description="The params hash of each cell to draw.", max_length=MAX_CURVES)],
) -> GridSearchCellCurvesResponse:
    """The named cells' daily curves: cumulative return and fall from peak at each session close (#2935)."""
    row = await _load(search_id)
    asked = list(dict.fromkeys(params_hash))
    stored = await with_connection(repo.cell_curves, search_id, asked)
    return GridSearchCellCurvesResponse.model_validate(curves_read(asked, stored, GridSearchSpec.from_request_dict(row.request).initial_cash, key="params_hash"))


@router.delete("/{search_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_grid_search(search_id: str) -> Response:
    """Cancel a running search first and wait for the worker's acknowledgement, then remove it."""
    row = await _load(search_id)
    _refuse_if_owned(row)
    if row.job_id and records.liveness_or_503(row, noun=NOUN):

        async def current_status() -> str | None:
            current = await with_connection(repo.get_search, search_id)
            return current.status if current else None

        await records.cancel_and_await_ack(row.job_id, current_status, noun=NOUN)
    await with_connection(repo.delete_search, search_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Jobs boundary ────────────────────────────────────────────────────────


@jobs_router.post("/grid-search", status_code=status.HTTP_202_ACCEPTED)
async def start_grid_search_job(req: GridSearchJobRequest) -> dict[str, Any]:
    """Launch (or Finish) a search. Returns 202 once the record is durable and a worker holds it.

    In research mode the API forwards the dispatch and never binds, stamps
    or starts a thread: the runtime prepares the immutable receipt and
    materialization, claims the attempt and runs the cells (#3049). Here —
    the local mode, or this process being the runtime — the search runs on
    a worker thread exactly as before, through spawned research workers in
    the runtime.
    """
    from app.services.research_runtime_client import (
        forward_grid_search_job,
        is_research_runtime,
        research_forwarding_enabled,
    )

    if research_forwarding_enabled() and not is_research_runtime():
        return await forward_grid_search_job(req.model_dump(mode="json"))

    # Refused authority must never bind (#3042): the check runs before any
    # durable record exists.
    await records.refuse_unauthorized_dispatch()
    if req.resume_search_id:
        row = await _load(req.resume_search_id)
        _refuse_if_owned(row)
        if row.job_id == req.job_id and row.status in records.STORED_LIVE_STATUSES:
            # A redelivery of a live dispatch joins the first worker before
            # any resume refusal can call it unresumable — the attempt is
            # never claimed twice (#3049).
            records.require_live_redelivery(req.job_id, noun=NOUN)
            return {"job_id": req.job_id, "search_id": row.id, "status": "queued"}
        refusal = await to_thread.run_sync(partial(_resume_refusal, row, live=records.liveness(row), verify_data=True))
        if refusal is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "NOT_RESUMABLE", "message": refusal})
        search_id = row.id
        await records.stamp_research_dispatch(job_id=req.job_id, kind="grid_search", record_id=search_id)
    else:
        # A fresh launch is idempotent under its job id: a search already
        # bound to it is a redelivery, never a second search (#3049).
        bound = await with_connection(repo.list_searches, job_id=req.job_id, limit=1)
        if bound:
            records.require_live_redelivery(req.job_id, noun=NOUN)
            return {"job_id": req.job_id, "search_id": bound[0].id, "status": "queued"}
        try:
            record = await to_thread.run_sync(partial(service.prepare_launch, to_grid_spec(req), job_id=req.job_id))
        except service.GridSearchRefusal as exc:
            raise records.refused(exc) from exc
        search_id = record.id
        # The stamp precedes the durable row: a stamp refusal strands
        # nothing, and a create failure leaves only a stamped job whose
        # search was never born — a retry as a fresh dispatch, not a dead
        # redelivery (#3042's lesson, applied to grid's launch order).
        await records.stamp_research_dispatch(job_id=req.job_id, kind="grid_search", record_id=search_id)
        await service.create(record)

    runtime_executes = is_research_runtime()
    work = _gated_grid_work(req, search_id) if runtime_executes else _local_grid_work(req, search_id)
    run_in_thread(
        req.job_id,
        work,
        thread_name=f"grid-search-{req.job_id[:8]}",
        cancel_check_every_n=1,
        # In the runtime, emits renew nothing — only accepted checkpoints
        # keep the lease alive (#3041); the API's local mode keeps the
        # emit-and-check renewal ADR 0065 D1 makes authoritative there.
        emit_renews_lease=not runtime_executes,
        progress_min_interval_s=1.0 if runtime_executes else 0.0,
    )
    return {"job_id": req.job_id, "search_id": search_id, "status": "queued"}


def _local_grid_work(req: GridSearchJobRequest, search_id: str) -> Callable[[ProgressEmitter, CancellationCheck], dict[str, Any]]:
    """The local-mode worker: the search behind the runner's emit-and-cancel checks, cells in this process."""

    def work(emit: ProgressEmitter, cancel: CancellationCheck) -> dict[str, Any]:
        row, spec = service.load_search(search_id)
        outcome = service.execute(
            search_id,
            job_id=req.job_id,
            execute_cell=default_execute_cell(row, spec, cancel_check=cancel.raise_if_cancelled),
            cancel_check=cancel.raise_if_cancelled,
            on_phase=emit.phase,
            on_progress=lambda done, total: emit.progress(done, total, unit="backtests"),
            on_log=emit.log,
        )
        return _outcome_response(outcome)

    return work


def _gated_grid_work(req: GridSearchJobRequest, search_id: str) -> Callable[[ProgressEmitter, CancellationCheck], dict[str, Any]]:
    """The research runtime's grid worker: the search under the checkpoint lease contract (#3041, #3049).

    In the runtime, cancellation polls renew nothing — the lease renews only
    on accepted checkpoints (the run's phase transitions, each durably
    persisted cell and the children's own progress), each fenced by the
    coordinator's boot generation. Cells execute through spawned research
    workers — one child at the serial width, the shared pool wider — and
    persistence, ranking and the terminal status stay in the coordinator.
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
        lease = GatedLease(req.job_id, task_id="search", authority=authority)
        outcome = service.execute(
            search_id,
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

def _outcome_response(outcome: service.ExecutionOutcome) -> dict[str, Any]:
    return {
        "search_id": outcome.search_id,
        "status": outcome.status,
        "leader_params_hash": outcome.leader_params_hash,
        "executed_cells": outcome.summary.executed_cells,
        "completed_cells": outcome.summary.completed_cells,
        "failed_cells": outcome.summary.failed_cells,
    }
