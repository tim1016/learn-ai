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
from functools import partial
from typing import Any, Literal

from anyio import to_thread
from fastapi import APIRouter, HTTPException, Query, Response, status

from app.jobs.progress import CancellationCheck, ProgressEmitter
from app.jobs.runner import run_in_thread
from app.research.grid_search import repository as repo
from app.research.grid_search import service
from app.research.grid_search.engine_adapter import default_execute_cell
from app.research.grid_search.models import CellRow, SearchRow
from app.research.persistence import lifecycle
from app.research.persistence.db import with_connection
from app.research.sweep.ranking import is_eligible
from app.routers import research_records as records
from app.schemas.grid_search import (
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
) -> GridSearchCellPageResponse:
    row = await _load(search_id)
    try:
        page_result = await with_connection(repo.list_cells, search_id, sort_by=sort_by, direction=direction, page=page, page_size=page_size)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return GridSearchCellPageResponse(
        total=page_result.total,
        page=page_result.page,
        page_size=page_result.page_size,
        sort_by=sort_by,
        direction=direction,
        cells=[
            _cell_response(cell, leader_hash=row.leader_params_hash, measure=row.request["measure"], min_trades=int(row.request["min_trades"]))
            for cell in page_result.cells
        ],
    )


@router.delete("/{search_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_grid_search(search_id: str) -> Response:
    """Cancel a running search first and wait for the worker's acknowledgement, then remove it."""
    row = await _load(search_id)
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
    """Launch (or Finish) a search on a worker thread. Returns 202 once the record is durable."""
    if req.resume_search_id:
        row = await _load(req.resume_search_id)
        refusal = await to_thread.run_sync(partial(_resume_refusal, row, live=records.liveness(row), verify_data=True))
        if refusal is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "NOT_RESUMABLE", "message": refusal})
        search_id = row.id
    else:
        try:
            record = await to_thread.run_sync(partial(service.prepare_launch, to_grid_spec(req), job_id=req.job_id))
        except service.GridSearchRefusal as exc:
            raise records.refused(exc) from exc
        created = await service.create(record)
        search_id = created.id

    def work(emit: ProgressEmitter, cancel: CancellationCheck) -> dict[str, Any]:
        row, spec = service.load_search(search_id)
        outcome = service.execute(
            search_id,
            job_id=req.job_id,
            execute_cell=default_execute_cell(row, spec),
            cancel_check=cancel.raise_if_cancelled,
            on_phase=emit.phase,
            on_progress=lambda done, total: emit.progress(done, total, unit="backtests"),
            on_log=emit.log,
        )
        return {
            "search_id": outcome.search_id,
            "status": outcome.status,
            "leader_params_hash": outcome.leader_params_hash,
            "executed_cells": outcome.summary.executed_cells,
            "completed_cells": outcome.summary.completed_cells,
            "failed_cells": outcome.summary.failed_cells,
        }

    run_in_thread(req.job_id, work, thread_name=f"grid-search-{req.job_id[:8]}", cancel_check_every_n=1)
    return {"job_id": req.job_id, "search_id": search_id, "status": "queued"}
