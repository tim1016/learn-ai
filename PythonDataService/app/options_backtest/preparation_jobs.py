"""The research parent dispatches and observes an ordinary API preparation job."""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from app.broker.fleet.internal_http import build_internal_client, enforce_private_http_target
from app.config import settings
from app.jobs import facade
from app.jobs.checkpoints import GatedLease
from app.jobs.external_work import ExternalJobObserver
from app.jobs.progress import JobCancelled, ProgressEmitter
from app.options_backtest import preparation_binding, service
from app.options_backtest.frozen_inputs import FrozenRunInputs
from app.options_backtest.input_staging import job_root
from app.options_backtest.preparation import (
    PREPARATION_JOB_TYPE,
    PreparationCheckpoint,
    read_checkpoint,
    runtime_preparation_identity,
    verify_completed,
)
from app.options_backtest.saved_runs import RunFailure
from app.options_backtest.timings import PhaseTiming, RunTimings
from app.research.persistence.db import with_connection, writer_loop_call
from app.schemas.options_backtest_preparation import OptionsBacktestPreparationRequest
from app.schemas.options_backtest_runs import OptionsBacktestJobRequest
from app.security.data_plane_control import CONTROL_SECRET_HEADER
from app.utils.background_loop import CallerStoppedWaitingError

logger = logging.getLogger(__name__)

CANCEL_POLL_SECONDS = 0.2


async def _bind_fresh(run_id: int, req: OptionsBacktestJobRequest, lease: GatedLease,
                      identity: str, previous: str | None) -> str:
    lease.fenced_cancel()
    job_id = await asyncio.to_thread(facade.queue_internal_job, PREPARATION_JOB_TYPE, {"parent_job_id": req.job_id})
    try:
        return await writer_loop_call(with_connection(preparation_binding.bind_preparation, run_id, req.job_id,
            lease.generation, identity, job_id, previous=previous))
    except CallerStoppedWaitingError:
        # The write continues on the writer loop. Read behind its transaction
        # to reconcile this exact id; never queue an uncertain second child.
        current = await writer_loop_call(with_connection(preparation_binding.current_binding, run_id, req.job_id,
                                                          lease.generation, identity))
        if current != job_id:
            raise
        return job_id


async def prepare_over_api(req: OptionsBacktestJobRequest, *, run_id: int, emit: ProgressEmitter,
                           lease: GatedLease, timings: RunTimings) -> FrozenRunInputs:
    base = settings.OPTIONS_BACKTEST_DATA_SERVICE_URL.strip().rstrip("/")
    if not base:
        raise RuntimeError("OPTIONS_BACKTEST_DATA_SERVICE_URL is required; replay never acquires provider data")
    await asyncio.to_thread(enforce_private_http_target, base)
    identity = await asyncio.to_thread(runtime_preparation_identity, req)
    root = job_root(settings.OPTIONS_BACKTEST_INPUT_ROOT, req.job_id)
    closed: tuple[PhaseTiming, ...] = ()
    observer = ExternalJobObserver(emit, lease)
    job_id = await writer_loop_call(with_connection(preparation_binding.current_binding, run_id, req.job_id,
                                                    lease.generation, identity))
    after = "0-0"
    try:
        async with build_internal_client(timeout_s=5) as client:
            while True:
                lease.fenced_cancel()
                checkpoint = await asyncio.to_thread(read_checkpoint, root, identity)
                if checkpoint is not None:
                    closed = checkpoint.timings
                    if checkpoint.complete:
                        return await asyncio.to_thread(verify_completed, req, checkpoint)
                if time.monotonic() > observer.deadline:
                    raise TimeoutError("preparation stopped advancing beyond its bounded work deadline")
                if job_id is None:
                    job_id = await _bind_fresh(run_id, req, lease, identity, None)
                state = await facade.read_job_state(job_id)
                for entry_id, event in await facade.read_job_events(job_id, after):
                    lease.fenced_cancel()
                    observer.accept(job_id, entry_id, event)
                    after = entry_id
                status = state.get("status")
                if status == "failed" and state.get("error_code") == "DATA_SERVICE_RESTARTED":
                    # Only the API startup sweep authorizes a fresh child.
                    # Missing Redis state alone never admits a second fetch.
                    lease.external_wait_finished()
                    job_id = await _bind_fresh(run_id, req, lease, identity, job_id)
                    after = "0-0"
                    observer = ExternalJobObserver(emit, lease)
                    continue
                if status == "cancelled":
                    raise JobCancelled("input preparation was cancelled")
                if status == "failed":
                    raise service.RunFailedError(RunFailure(state.get("error_code", "PREPARATION_FAILED"),
                                                            state.get("error_message", "Input preparation failed.")))
                if status == "completed":
                    try:
                        result = PreparationCheckpoint.model_validate_json(await facade.read_result(job_id))
                    except facade.JobRecordNotFoundError:
                        # Result retention can expire between reading status
                        # and result. The verified complete checkpoint persists.
                        result = await asyncio.to_thread(read_checkpoint, root, identity)
                        if result is None or not result.complete:
                            raise
                    if result.identity != identity:
                        raise ValueError("preparation result binds another request, root or execution generation")
                    closed = result.timings
                    return await asyncio.to_thread(verify_completed, req, result)
                if status not in ("queued", "running"):
                    raise RuntimeError("the preparation job is absent or expired; its old id cannot execute again")
                if status == "queued" and not state.get("started_at"):
                    dispatch = OptionsBacktestPreparationRequest(job_id=job_id, run_id=run_id,
                        generation=lease.generation, identity=identity, parent=req)
                    try:
                        response = await client.post(f"{base}/api/jobs-internal/options-backtest/prepare",
                            json=dispatch.model_dump(mode="json"),
                            headers={CONTROL_SECRET_HEADER: settings.DATA_PLANE_CONTROL_SECRET})
                        if response.status_code not in (409, 502, 503, 504):
                            response.raise_for_status()
                        elif response.status_code == 409:
                            latest = await facade.read_job_state(job_id)
                            if latest.get("status") not in facade.TERMINAL_STATUSES:
                                response.raise_for_status()
                    except httpx.TransportError as exc:
                        # A lost dispatch reply observes the same canonical
                        # child id; neither the retry nor a poll renews parent.
                        logger.debug("preparation dispatch reply lost", extra={"job_id": job_id, "reason": str(exc)})
                await asyncio.sleep(CANCEL_POLL_SECONDS)
    finally:
        timings.adopt(closed)
        if job_id is not None:
            try:
                if (await facade.read_job_state(job_id)).get("status") in ("queued", "running"):
                    await facade.request_cancel(job_id)
            except facade.JobRecordNotFoundError:
                # Expired transport metadata is not a new worker authority.
                # The collector also checks the live parent binding itself.
                logger.debug("preparation transport expired before cancellation", extra={"job_id": job_id})
