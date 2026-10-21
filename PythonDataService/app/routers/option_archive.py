"""Archive health and explicit, authenticated manual job triggers."""
from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request

from app.option_archive.service import OptionArchiveService
from app.schemas.option_archive import ArchiveSessionCaptureRequest, ArchiveTriggerReceipt, OptionArchiveHealth
from app.security.data_plane_control import require_data_plane_control_secret
from app.utils.background_loop import background_loop

router = APIRouter(prefix="/api/option-archive", tags=["option-archive"])


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


@router.post("/trigger/{job}", response_model=ArchiveTriggerReceipt, status_code=202,
             dependencies=[Depends(require_data_plane_control_secret)])
async def trigger_archive_job(job: Literal["capture", "snapshot", "backup", "restore"],
                              service: OptionArchiveService = Depends(get_archive_service)) -> ArchiveTriggerReceipt:
    if not service.config.ENABLED:
        raise HTTPException(status_code=409, detail="Option archive is disabled; review the runbook before enabling it.")
    future = asyncio.run_coroutine_threadsafe(service.scheduler.trigger(job), background_loop())
    # Keep the request bounded. Job ownership and the scheduler's running map
    # make a repeated trigger join the same physical pass.
    def report(done: object) -> None:
        import logging
        try:
            future.result()
        except Exception:
            logging.getLogger(__name__).exception("Manual archive job failed", extra={"action": "manual_archive_job_failed", "job": job})
    future.add_done_callback(report)
    return ArchiveTriggerReceipt(accepted=True, job=job)


@router.post("/capture-session", response_model=ArchiveTriggerReceipt, status_code=202,
             dependencies=[Depends(require_data_plane_control_secret)])
async def capture_archive_session(body: ArchiveSessionCaptureRequest,
                                  service: OptionArchiveService = Depends(get_archive_service)) -> ArchiveTriggerReceipt:
    if not service.config.ENABLED:
        raise HTTPException(status_code=409, detail="Option archive is disabled.")
    from app.lean_sidecar.trading_calendar import session_open_ms_utc
    from app.utils.session_anchors import et_date_at_ms
    if session_open_ms_utc(et_date_at_ms(body.session_ms)) != body.session_ms:
        raise HTTPException(status_code=422, detail="session_ms must be the canonical session open")
    # Completion remains on the normal receipt/status path. Explicit retry
    # resets only this session's exhausted transient outcomes.
    asyncio.run_coroutine_threadsafe(service.capture_session(body.underlying, body.session_ms,
                                    retry_exhausted=body.retry_exhausted), background_loop())
    return ArchiveTriggerReceipt(accepted=True, job="capture")
