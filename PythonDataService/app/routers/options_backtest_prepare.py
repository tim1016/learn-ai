"""Authenticated start/join of the API's canonical internal preparation job."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException

from app.jobs import facade
from app.jobs.progress import JobAlreadyStarted, JobLeaseOccupied
from app.jobs.runner import async_prepared_worker
from app.options_backtest import preparation_binding
from app.options_backtest.preparation import PREPARATION_JOB_TYPE, collect_preparation, preparation_identity
from app.research.persistence.db import with_connection, writer_loop_call
from app.research.persistence.ownership import GenerationFencedError
from app.routers.research_records import require_live_redelivery
from app.schemas.jobs_facade import JobAcceptedResponse
from app.schemas.options_backtest_preparation import OptionsBacktestPreparationRequest
from app.security.data_plane_control import require_data_plane_control_secret
from app.services.research_runtime_client import is_research_runtime

router = APIRouter()


@router.post("/options-backtest/prepare", status_code=202, response_model=JobAcceptedResponse,
             dependencies=[Depends(require_data_plane_control_secret)])
async def prepare(req: OptionsBacktestPreparationRequest) -> JobAcceptedResponse:
    if is_research_runtime():
        raise HTTPException(503, "Input acquisition belongs to the data service.")
    if await asyncio.to_thread(preparation_identity, req.parent) != req.identity:
        raise HTTPException(409, "Preparation binds another request, root or execution generation.")
    try:
        # Durable parent ownership precedes the canonical worker claim. An
        # expired old child id is never supplied a replacement lease.
        await writer_loop_call(with_connection(preparation_binding.require_binding, req.run_id, req.parent.job_id,
                                              req.generation, req.identity, req.job_id))
        await asyncio.to_thread(require_live_redelivery, req.parent.job_id, noun="Options Backtest")
        state = await facade.read_job_state(req.job_id)
        if state.get("type") != PREPARATION_JOB_TYPE or state.get("internal") != "1":
            raise HTTPException(409, "The preparation job is absent or expired; its old id cannot execute again.")
        async with async_prepared_worker(req.job_id, emit_renews_lease=False,
                                         thread_name=f"options-prepare-{req.job_id[:8]}") as worker:
            worker.cancel.renews_lease = False
            worker.start(lambda emit, cancel: collect_preparation(req, emit, cancel))
    except JobAlreadyStarted:
        await asyncio.to_thread(require_live_redelivery, req.job_id, noun="Options preparation")
    except JobLeaseOccupied as exc:
        raise HTTPException(503, str(exc)) from exc
    except GenerationFencedError as exc:
        raise HTTPException(409, str(exc)) from exc
    return JobAcceptedResponse(id=req.job_id, status="queued")
