"""Grid Search HTTP boundary (PRD #1926 "HTTP contract").

``router`` — the research surface under ``/api/research/grid-search``:
preflight, history listing, detail, server-paged cells, and delete.
``jobs_router`` — ``POST /api/jobs-internal/grid-search``, dispatched by
the .NET jobs boundary with a minted ``job_id`` (a new search, or Finish of
an incomplete one via ``resume_search_id``). Every temporal value on the
wire is ``int64 ms UTC``.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any, Literal

from anyio import to_thread
from fastapi import APIRouter, HTTPException, Query, Response, status

from app.jobs.progress import ProgressEmitter
from app.jobs.runner import run_in_thread
from app.research.grid_search import repository as repo
from app.research.grid_search import service
from app.research.grid_search.db import connection
from app.research.grid_search.models import CellRow, SearchRow
from app.research.sweep.grid import LowHighStepRange, ParamRange, ValueListRange
from app.research.sweep.ranking import is_eligible
from app.schemas.grid_search import (
    GridSearchCellPageResponse,
    GridSearchCellResponse,
    GridSearchDetailResponse,
    GridSearchJobRequest,
    GridSearchPreflightResponse,
    GridSearchSpecRequest,
    GridSearchSummaryResponse,
    LowHighStepRangeRequest,
    RunUpPlanResponse,
    SearchOwnerResponse,
    ValueListRangeRequest,
)

router = APIRouter()
jobs_router = APIRouter()
logger = logging.getLogger(__name__)

CANCEL_ACK_TIMEOUT_SECONDS = 30.0


def range_from_request(spec: ValueListRangeRequest | LowHighStepRangeRequest) -> ParamRange:
    if isinstance(spec, ValueListRangeRequest):
        return ValueListRange(tuple(spec.values))
    return LowHighStepRange(low=spec.low, high=spec.high, step=spec.step)


def spec_from_request(body: GridSearchSpecRequest) -> service.GridSearchSpec:
    return service.GridSearchSpec(
        strategy_key=body.strategy_key,
        symbol=body.symbol.strip().upper(),
        param_ranges={name: range_from_request(spec) for name, spec in body.param_ranges.items()},
        start_ms=body.start_ms,
        end_ms=body.end_ms,
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


def _summary(row: SearchRow, *, live: bool | None, leader_params: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "id": row.id,
        "owner": SearchOwnerResponse(kind=row.owner.kind, owner_id=row.owner.owner_id, fold_index=row.owner.fold_index, phase=row.owner.phase),
        "strategy_key": row.strategy_key,
        "symbol": row.symbol,
        "status": service.presented_status(row, live=live),
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
        "leader_params": leader_params,
        "incomplete": row.incomplete,
        "uncommitted_changes": service.uncommitted_changes(row),
        "failure_reason": row.failure_reason,
    }


def _cell_response(cell: CellRow, *, leader_hash: str | None, measure: str, min_trades: int) -> GridSearchCellResponse:
    return GridSearchCellResponse(
        **{k: v for k, v in cell.as_dict().items() if k not in ("search_id",)},
        is_leader=cell.params_hash == leader_hash,
        eligible=is_eligible(cell, measure, min_trades=min_trades),  # type: ignore[arg-type]
    )


# ── Research surface ─────────────────────────────────────────────────────


@router.post("/preflight", response_model=GridSearchPreflightResponse)
async def preflight_grid_search(body: GridSearchSpecRequest) -> GridSearchPreflightResponse:
    """Validate, size, and plan a search without launching it."""
    try:
        pre = await to_thread.run_sync(partial(service.preflight, spec_from_request(body)))
    except service.GridSearchRefusal as exc:
        raise _refused(exc) from exc
    return GridSearchPreflightResponse(
        strategy_key=pre.spec.strategy_key,
        symbol=pre.spec.symbol,
        combinations=pre.combinations,
        total_backtests=pre.total_backtests,
        backtest_limit=service.MAX_TOTAL_BACKTESTS,
        estimated_seconds=pre.estimated_seconds,
        run_up=RunUpPlanResponse(
            data_start_ms=service.et_midnight_ms(pre.data_start),
            evaluation_start_ms=service.et_midnight_ms(pre.evaluation_start),
            evaluation_end_ms=service.et_midnight_ms(pre.evaluation_end) + 24 * 60 * 60 * 1000,
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
    async with connection() as conn:
        rows = await repo.list_searches(
            conn,
            strategy_key=strategy_key,
            symbol=symbol.strip().upper() if symbol else None,
            status=None if status_filter in (None, "interrupted") else status_filter,
            job_id=job_id,
            limit=limit,
        )
        summaries = []
        for row in rows:
            leader_params = None
            if row.leader_params_hash is not None:
                cells = await repo.list_all_cells(conn, row.id)
                leader_params = service.leader_params(row, cells)
            summaries.append(GridSearchSummaryResponse(**_summary(row, live=service.job_is_live(row.job_id), leader_params=leader_params)))
    if status_filter == "interrupted":
        return [summary for summary in summaries if summary.status == "interrupted"]
    return summaries


async def _load(search_id: str) -> tuple[SearchRow, list[CellRow]]:
    async with connection() as conn:
        row = await repo.get_search(conn, search_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"grid search {search_id} not found")
        cells = await repo.list_all_cells(conn, search_id)
    return row, cells


@router.get("/{search_id}", response_model=GridSearchDetailResponse)
async def get_grid_search(search_id: str) -> GridSearchDetailResponse:
    row, cells = await _load(search_id)
    live = service.job_is_live(row.job_id)
    refusal = await to_thread.run_sync(partial(service.resume_refusal, row, live=live))
    return GridSearchDetailResponse(
        **_summary(row, live=live, leader_params=service.leader_params(row, cells)),
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
    async with connection() as conn:
        row = await repo.get_search(conn, search_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"grid search {search_id} not found")
        try:
            page_result = await repo.list_cells(conn, search_id, sort_by=sort_by, direction=direction, page=page, page_size=page_size)
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
    async with connection() as conn:
        row = await repo.get_search(conn, search_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"grid search {search_id} not found")
    if row.status in ("queued", "running") and row.job_id and service.job_is_live(row.job_id):
        service.request_cancel(row.job_id)
        deadline = asyncio.get_running_loop().time() + CANCEL_ACK_TIMEOUT_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.25)
            async with connection() as conn:
                current = await repo.get_search(conn, search_id)
            if current is None or current.status not in ("queued", "running"):
                break
        else:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="the running search has not acknowledged cancellation yet; try again shortly",
            )
    async with connection() as conn:
        await repo.delete_search(conn, search_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Jobs boundary ────────────────────────────────────────────────────────


@jobs_router.post("/grid-search", status_code=status.HTTP_202_ACCEPTED)
async def start_grid_search_job(req: GridSearchJobRequest) -> dict[str, Any]:
    """Launch (or Finish) a search on a worker thread. Returns 202 once the record is durable."""
    if req.resume_search_id:
        async with connection() as conn:
            row = await repo.get_search(conn, req.resume_search_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"grid search {req.resume_search_id} not found")
        refusal = await to_thread.run_sync(partial(service.resume_refusal, row, live=service.job_is_live(row.job_id)))
        if refusal is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "NOT_RESUMABLE", "message": refusal})
        search_id = row.id
    else:
        try:
            record = await to_thread.run_sync(partial(service.prepare_launch, spec_from_request(req), job_id=req.job_id))
        except service.GridSearchRefusal as exc:
            raise _refused(exc) from exc
        created = await service.create(record)
        search_id = created.id

    def work(emit: ProgressEmitter, cancel) -> dict[str, Any]:
        outcome = service.execute(
            search_id,
            job_id=req.job_id,
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
