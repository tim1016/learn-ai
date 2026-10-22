"""The research parent dispatches and observes an ordinary API preparation job."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import httpx

from app.broker.fleet.internal_http import build_internal_client, enforce_private_http_target
from app.config import settings
from app.jobs import facade
from app.jobs.checkpoints import GatedLease
from app.jobs.external_work import ExternalJobObserver
from app.jobs.progress import MAX_STREAM_LENGTH, ORPHANED_JOB_CODE, JobCancelled, ProgressEmitter
from app.options_backtest import preparation_binding, service
from app.options_backtest.frozen_inputs import FrozenRunInputs
from app.options_backtest.input_staging import job_root
from app.options_backtest.preparation import (
    PREPARATION_JOB_TYPE,
    read_checkpoint,
    runtime_preparation_identity,
    verify_completed,
)
from app.options_backtest.saved_runs import RunFailure
from app.options_backtest.timings import PhaseTiming, RunTimings
from app.research.persistence.db import with_connection, writer_loop_call
from app.research.persistence.ownership import GenerationFencedError
from app.schemas.options_backtest_preparation import OptionsBacktestPreparationRequest
from app.schemas.options_backtest_runs import OptionsBacktestJobRequest
from app.security.data_plane_control import CONTROL_SECRET_HEADER

logger = logging.getLogger(__name__)

CANCEL_POLL_SECONDS = 0.2


class _ClosedTimings:
    """Keep each child's latest cumulative spans; a restart keeps those of the children before it."""

    def __init__(self) -> None:
        self._past: tuple[PhaseTiming, ...] = ()
        self._current: tuple[PhaseTiming, ...] = ()

    @property
    def rows(self) -> tuple[PhaseTiming, ...]:
        return self._past + self._current

    def accept(self, rows: tuple[PhaseTiming, ...]) -> None:
        shared = min(len(rows), len(self._current))
        if rows[:shared] != self._current[:shared]:
            logger.warning("preparation timing snapshot changed its closed prefix")
        elif len(rows) > len(self._current):
            self._current = rows

    def restarted(self) -> None:
        self._past, self._current = self.rows, ()

    def event(self, event: dict, identity: str) -> None:
        if event.get("type") == "job.observations" and event.get("identity") == identity:
            try:
                self.accept(tuple(PhaseTiming.model_validate(row) for row in event["phase_timings"]))
            except (KeyError, TypeError, ValueError):
                logger.warning("preparation timing observation could not be read", exc_info=True)


async def _drain_observations(job_id: str, after: str, closed: _ClosedTimings, identity: str) -> str:
    """A bounded metadata-only drain, allowing headroom for approximate stream trimming."""
    remaining = 2 * MAX_STREAM_LENGTH
    while remaining:
        batch = await facade.read_job_events(job_id, after)
        if not batch:
            return after
        for entry_id, event in batch[:remaining]:
            closed.event(event, identity)
            after = entry_id
        remaining -= min(len(batch), remaining)
    logger.warning("preparation observation drain reached the retained stream bound", extra={"job_id": job_id})
    return after


async def _completed_inputs(req: OptionsBacktestJobRequest, root: Path, identity: str,
                            closed: _ClosedTimings) -> FrozenRunInputs | None:
    """The verified inputs of a complete checkpoint file, which also carries its writer's closed spans."""
    checkpoint = await asyncio.to_thread(read_checkpoint, root, identity)
    if checkpoint is None or not checkpoint.complete:
        return None
    closed.accept(checkpoint.timings)
    return await asyncio.to_thread(verify_completed, req, checkpoint)


async def _close_unbound_job(job_id: str) -> None:
    try:
        # No worker exists to acknowledge the ordinary cooperative cancel flag.
        await asyncio.to_thread(lambda: ProgressEmitter(job_id, renews_lease=False).cancelled(
            "preparation binding did not commit"))
    except Exception:
        logger.warning("unbound preparation job could not be closed", exc_info=True, extra={"job_id": job_id})


async def _bind_fresh(run_id: int, req: OptionsBacktestJobRequest, lease: GatedLease,
                      identity: str, previous: str | None) -> str:
    lease.fenced_cancel()
    job_id = await asyncio.to_thread(facade.queue_internal_job, PREPARATION_JOB_TYPE, {"parent_job_id": req.job_id})
    try:
        return await writer_loop_call(with_connection(preparation_binding.bind_preparation, run_id, req.job_id,
            lease.generation, identity, job_id, previous=previous))
    except (ValueError, GenerationFencedError):
        # These failures precede the binding write and roll back its transaction.
        await _close_unbound_job(job_id)
        raise
    except Exception as failure:
        # A wait or commit acknowledgement can fail after the binding commits.
        # Reconcile the exact id before closing any possibly owned work.
        try:
            current = await writer_loop_call(with_connection(preparation_binding.current_binding, run_id, req.job_id,
                                                              lease.generation, identity))
        except Exception as reconciliation_failure:
            logger.warning("preparation binding could not be reconciled; its job remains queued", exc_info=True,
                           extra={"job_id": job_id})
            raise failure from reconciliation_failure
        if current == job_id:
            return job_id
        await _close_unbound_job(job_id)
        raise



async def prepare_over_api(req: OptionsBacktestJobRequest, *, run_id: int, emit: ProgressEmitter,
                           lease: GatedLease, timings: RunTimings) -> FrozenRunInputs:
    base = settings.OPTIONS_BACKTEST_DATA_SERVICE_URL.strip().rstrip("/")
    if not base:
        raise RuntimeError("OPTIONS_BACKTEST_DATA_SERVICE_URL is required; replay never acquires provider data")
    await asyncio.to_thread(enforce_private_http_target, base)
    identity = await asyncio.to_thread(runtime_preparation_identity, req)
    root = job_root(settings.OPTIONS_BACKTEST_INPUT_ROOT, req.job_id)
    closed = _ClosedTimings()
    observer = ExternalJobObserver(emit, lease)
    job_id: str | None = None
    after = "0-0"
    try:
        async with build_internal_client(timeout_s=5) as client:
            while True:
                lease.fenced_cancel()
                if (frozen := await _completed_inputs(req, root, identity, closed)) is not None:
                    return frozen
                if time.monotonic() > observer.deadline:
                    raise TimeoutError("preparation stopped advancing beyond its bounded work deadline")
                if job_id is None:
                    job_id = await _bind_fresh(run_id, req, lease, identity, None)
                state = await facade.read_job_state(job_id)
                for entry_id, event in await facade.read_job_events(job_id, after):
                    lease.fenced_cancel()
                    closed.event(event, identity)
                    observer.accept(job_id, entry_id, event)
                    after = entry_id
                status = state.get("status")
                if status in facade.TERMINAL_STATUSES:
                    after = await _drain_observations(job_id, after, closed, identity)
                if status == "failed" and state.get("error_code") == ORPHANED_JOB_CODE:
                    # Only the API startup sweep authorizes a fresh child.
                    # Missing Redis state alone never admits a second fetch.
                    if (frozen := await _completed_inputs(req, root, identity, closed)) is not None:
                        return frozen
                    lease.external_wait_finished()
                    job_id = await _bind_fresh(run_id, req, lease, identity, job_id)
                    closed.restarted()
                    after = "0-0"
                    observer = ExternalJobObserver(emit, lease)
                    continue
                if status == "cancelled":
                    raise JobCancelled("input preparation was cancelled")
                if status == "failed":
                    raise service.RunFailedError(RunFailure(state.get("error_code", "PREPARATION_FAILED"),
                                                            state.get("error_message", "Input preparation failed.")))
                if status == "completed":
                    # The child writes its complete checkpoint before it completes.
                    if (frozen := await _completed_inputs(req, root, identity, closed)) is None:
                        raise RuntimeError("the preparation job completed without a complete freeze checkpoint")
                    return frozen
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
                        else:
                            logger.debug("preparation dispatch not accepted yet",
                                         extra={"job_id": job_id, "status_code": response.status_code})
                    except httpx.TransportError as exc:
                        # A lost dispatch reply observes the same canonical
                        # child id; neither the retry nor a poll renews parent.
                        logger.debug("preparation dispatch reply lost", extra={"job_id": job_id, "reason": str(exc)})
                await asyncio.sleep(CANCEL_POLL_SECONDS)
    finally:
        if job_id is not None:
            try:
                # A terminal/cancellation may race the last ordinary batch.
                # Timing observations provide no lease or liveness evidence.
                await _drain_observations(job_id, after, closed, identity)
            except Exception:
                logger.warning("final preparation observations could not be read", exc_info=True,
                               extra={"job_id": job_id})
        timings.adopt(closed.rows)
        if job_id is not None:
            try:
                if (await facade.read_job_state(job_id)).get("status") in ("queued", "running"):
                    await facade.request_cancel(job_id)
            except facade.JobRecordNotFoundError:
                # Expired transport metadata is not a new worker authority.
                # The collector also checks the live parent binding itself.
                logger.debug("preparation transport expired before cancellation", extra={"job_id": job_id})
