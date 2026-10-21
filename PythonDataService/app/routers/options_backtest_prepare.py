"""Authenticated observers of durable preparation; sockets never own acquisition."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.config import settings
from app.options_backtest.input_staging import job_root
from app.options_backtest.preparation import (
    PreparationConflict,
    preparation_identity,
    read_preparation,
    start_preparation,
)
from app.schemas.options_backtest_runs import OptionsBacktestJobRequest
from app.security.data_plane_control import require_data_plane_control_secret
from app.services.research_runtime_client import is_research_runtime

router = APIRouter()


@router.post("/options-backtest/prepare", dependencies=[Depends(require_data_plane_control_secret)])
async def prepare(req: OptionsBacktestJobRequest) -> StreamingResponse:
    if is_research_runtime():
        raise HTTPException(503, "Input acquisition belongs to the data service.")
    identity = await asyncio.to_thread(preparation_identity, req)
    root = job_root(settings.OPTIONS_BACKTEST_INPUT_ROOT, req.job_id)
    try:
        await asyncio.to_thread(read_preparation, root, identity)
    except PreparationConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    collector = start_preparation(req, identity)

    async def stream() -> AsyncIterator[bytes]:
        sequence = -1
        while True:
            state = await asyncio.to_thread(read_preparation, root, identity)
            if state is not None and state["sequence"] != sequence:
                sequence = state["sequence"]
                yield json.dumps({"kind": "preparation", "state": state}, separators=(",", ":")).encode() + b"\n"
                if state["status"] != "collecting":
                    return
            if collector.done():
                # Surface failures before journal creation. A collector that
                # joined another owner completes without ending its observer.
                collector.result()
            await asyncio.sleep(0.1)

    return StreamingResponse(stream(), media_type="application/x-ndjson")
