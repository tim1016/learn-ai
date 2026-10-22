"""Archive health and explicit, authenticated manual job triggers."""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import Future

from fastapi import APIRouter, Depends, HTTPException, Request

from app.option_archive.planner import SessionOutcome
from app.option_archive.service import OptionArchiveService
from app.schemas.option_archive import (
    ArchiveJobName,
    ArchiveSessionCaptureRequest,
    ArchiveTriggerReceipt,
    OptionArchiveHealth,
)
from app.security.data_plane_control import require_data_plane_control_secret
from app.utils.background_loop import background_loop

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/option-archive", tags=["option-archive"])

# Asking Postgres for an advisory lock takes milliseconds; far longer means the catalog is unreachable.
LOCK_DECISION_TIMEOUT_S = 10.0


def get_archive_service(request: Request) -> OptionArchiveService:
    service = getattr(request.app.state, "option_archive_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Option archive status is unavailable; inspect the data service startup.")
    return service


@router.get("/status", response_model=OptionArchiveHealth)
async def archive_status(service: OptionArchiveService = Depends(get_archive_service)) -> OptionArchiveHealth:
    try:
        return await asyncio.to_thread(service.health)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Option archive receipts cannot be read; inspect the data lake.") from exc


async def _lock_granted(lock_decision: Future[bool], unchecked: str) -> bool:
    """Waits, bounded, for the background loop's answer to whether the job's lock was granted."""
    try:
        return await asyncio.wait_for(asyncio.wrap_future(lock_decision), LOCK_DECISION_TIMEOUT_S)
    except Exception as exc:
        # The service can take the decision just as the wait gives up. Once it has, cancelling fails and its
        # answer stands: a granted lock means the job started.
        if lock_decision.cancel():
            raise HTTPException(status_code=503, detail=unchecked) from exc
        try:
            return await asyncio.wrap_future(lock_decision)
        except Exception:
            raise HTTPException(status_code=503, detail=unchecked) from exc


@router.post("/trigger/{job}", response_model=ArchiveTriggerReceipt, status_code=202,
             dependencies=[Depends(require_data_plane_control_secret)])
async def trigger_archive_job(job: ArchiveJobName,
                              service: OptionArchiveService = Depends(get_archive_service)) -> ArchiveTriggerReceipt:
    if not service.config.ENABLED:
        raise HTTPException(status_code=409, detail="Option archive is disabled; review the runbook before enabling it.")
    # The request waits for the job lock's answer only. A repeated trigger joins
    # the running pass and gets its answer; the scheduler logs and records a failed run.
    lock_decision: Future[bool] = Future()
    asyncio.run_coroutine_threadsafe(service.scheduler.trigger(job, lock_decision), background_loop())
    unchecked = (f"Option archive could not confirm the {job} lock, so it cannot say whether the job started; "
                 "inspect the catalog database and the archive status before retrying.")
    if not await _lock_granted(lock_decision, unchecked):
        raise HTTPException(status_code=409, detail=f"Another holder has the {job} lock; retry the trigger when it ends.")
    return ArchiveTriggerReceipt(accepted=True, job=job)


@router.post("/capture-session", response_model=ArchiveTriggerReceipt, status_code=202,
             dependencies=[Depends(require_data_plane_control_secret)])
async def capture_archive_session(body: ArchiveSessionCaptureRequest,
                                  service: OptionArchiveService = Depends(get_archive_service)) -> ArchiveTriggerReceipt:
    if not service.config.ENABLED:
        raise HTTPException(status_code=409, detail="Option archive is disabled.")
    # Completion remains on the normal receipt/status path. Explicit retry
    # resets only this session's exhausted transient outcomes. The request waits
    # for the capture lock's answer only, so 202 means the recovery has started.
    lock_decision: Future[bool] = Future()
    future = asyncio.run_coroutine_threadsafe(service.recover_session(body.underlying, body.session_ms,
                                    retry_exhausted=body.retry_exhausted, lock_decision=lock_decision), background_loop())

    def report(done: Future[SessionOutcome | None]) -> None:
        try:
            done.result()
        except Exception:
            logger.exception("Manual session recovery failed", extra={
                "action": "manual_archive_session_failed", "underlying": body.underlying, "session_ms": body.session_ms})
    future.add_done_callback(report)
    unchecked = ("Option archive could not check the capture lock, so the recovery did not start; "
                 "inspect the catalog database and retry.")
    if not await _lock_granted(lock_decision, unchecked):
        raise HTTPException(status_code=409, detail="Another archive job holds the capture lock; retry the recovery when it ends.")
    return ArchiveTriggerReceipt(accepted=True, job="capture")
