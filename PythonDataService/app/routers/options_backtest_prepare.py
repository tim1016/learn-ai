"""Authenticated preparation stream; provider and catalog work stays in data service."""
from __future__ import annotations

import asyncio
import json
import queue
import threading
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.config import settings
from app.jobs.progress import CancellationCheck
from app.options_backtest import service
from app.options_backtest.frozen_inputs import freeze_run_inputs
from app.options_backtest.lake_data import LakeRunData
from app.options_backtest.timings import RunTimings
from app.schemas.options_backtest_runs import OptionsBacktestJobRequest
from app.security.data_plane_control import require_data_plane_control_secret
from app.services.research_runtime_client import is_research_runtime
from app.utils.timestamps import now_ms_utc

router = APIRouter()


@router.post("/options-backtest/prepare", dependencies=[Depends(require_data_plane_control_secret)])
async def prepare(req: OptionsBacktestJobRequest) -> StreamingResponse:
    if is_research_runtime():
        raise HTTPException(503, "Input acquisition belongs to the data service.")
    sessions = await asyncio.to_thread(service.sessions_of_range, req.start_ms, req.end_ms)
    frames: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=64)
    disconnected = threading.Event()
    cancelled = CancellationCheck(req.job_id, renews_lease=False)

    def stop() -> bool:
        return disconnected.is_set() or cancelled.should_cancel()

    def send(frame: dict[str, Any] | None) -> None:
        while not disconnected.is_set():
            try:
                frames.put(frame, timeout=0.2)
                return
            except queue.Full:
                if stop():
                    raise service.RunCancelled("preparation consumer stopped")
        raise service.RunCancelled("preparation consumer disconnected")

    def collect() -> None:
        ledger = RunTimings()
        hooks = service.RunHooks(on_phase=lambda phase: send({"kind": "phase", "phase": phase}),
            on_progress=lambda done, total: send({"kind": "progress", "done": done, "total": total}),
            on_log=lambda message: send({"kind": "log", "message": message}), should_stop=stop)
        try:
            service.prepare_run_data(req.setup, sessions, hooks, now_ms_utc, timings=ledger)
            hooks.on_phase("freezing_inputs")
            with ledger.measure("input_freeze"):
                frozen = freeze_run_inputs(req.setup, sessions, LakeRunData(req.setup.underlying),
                    settings.OPTIONS_BACKTEST_INPUT_ROOT, should_stop=stop,
                    on_session=lambda done, total, day: hooks.on_progress(done, total))
            send({"kind": "frozen", "inputs": frozen.model_dump(mode="json"), "timings": ledger.wire()})
        except Exception as exc:
            if not disconnected.is_set():
                failure = service.failure_of(exc)
                send({"kind": "failure", "code": failure.code, "reason": failure.reason})
        finally:
            if not disconnected.is_set():
                send(None)

    async def stream() -> AsyncIterator[bytes]:
        thread = threading.Thread(target=collect, name=f"options-prepare-{req.job_id[:8]}", daemon=True)
        thread.start()
        try:
            while True:
                frame = await asyncio.to_thread(frames.get)
                if frame is None:
                    return
                yield json.dumps(frame, separators=(",", ":")).encode() + b"\n"
        finally:
            disconnected.set()
    return StreamingResponse(stream(), media_type="application/x-ndjson")
