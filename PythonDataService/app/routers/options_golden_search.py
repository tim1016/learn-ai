"""Options study commands, evidence reads and authorized research-job transport (#3332)."""
from __future__ import annotations

import asyncio
import contextlib
from typing import Annotated, Literal, Protocol

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import ValidationError

from app.jobs.progress import CancellationCheck, ProgressEmitter
from app.jobs.runner import run_in_thread
from app.research.options_golden_search import repository as repo
from app.research.options_golden_search import service, stages
from app.research.persistence.db import with_connection, writer_loop_call
from app.routers import research_records
from app.routers.research_store_unavailable import StoreUnreachableIsA503
from app.schemas.options_golden_search import (
    Command,
    Declarations,
    HistoryCaptureAccepted,
    HistoryCaptureJobRequest,
    JobAccepted,
    JobRequest,
    LockRequest,
    PlanRequest,
    Preflight,
    SessionResult,
    StudyDetail,
    StudySummary,
)
from app.security.data_plane_control import require_data_plane_control_secret
from app.utils.background_loop import CallerStoppedWaitingError
from app.utils.session_anchors import et_date_at_ms

router = APIRouter(route_class=StoreUnreachableIsA503)
jobs_router = APIRouter(route_class=StoreUnreachableIsA503)
StudyId = Annotated[str, Path(pattern=r"^os_[a-f0-9]{40}$")]


class HistoryCaptureService(Protocol):
    async def history_capture_scope(self, request: PlanRequest) -> tuple[str, tuple[int, ...]]: ...


async def get_service():
    return service


def refusal(exc: Exception) -> HTTPException:
    code = 409 if isinstance(exc,repo.Conflict) else 404 if isinstance(exc,LookupError) else 400
    return HTTPException(code,{"code":"OPTIONS_SEARCH_REFUSED","message":str(exc)})


@router.get("/declarations",response_model=Declarations)
async def declarations(setup_name: str = Query(min_length=1,max_length=80), api=Depends(get_service)) -> Declarations:
    try:
        return await api.declarations(setup_name)
    except (ValueError,LookupError) as exc:
        raise refusal(exc) from exc


@router.post("/preflight",response_model=Preflight)
async def preflight(body: PlanRequest,api=Depends(get_service)) -> Preflight:
    try:
        return (await api.prepare(body))[0]
    except (ValueError,LookupError) as exc:
        raise refusal(exc) from exc


@router.post("/studies",response_model=StudyDetail,status_code=201)
async def create(body: LockRequest,api=Depends(get_service)) -> StudyDetail:
    try:
        return await api.create(body)
    except (ValueError,LookupError) as exc:
        raise refusal(exc) from exc


@router.get("/studies",response_model=list[StudySummary])
async def history() -> list[StudySummary]:
    return [StudySummary.model_validate(row) for row in await with_connection(repo.summaries)]


@router.get("/studies/{study_id}",response_model=StudyDetail)
async def detail(study_id: StudyId,api=Depends(get_service)) -> StudyDetail:
    try:
        return await api.detail(study_id)
    except (ValueError,LookupError) as exc:
        raise refusal(exc) from exc


@router.post("/studies/{study_id}/commands",response_model=StudyDetail)
async def command(study_id: StudyId,body: Command,api=Depends(get_service)) -> StudyDetail:
    try:
        return await api.command(study_id,body)
    except (ValueError,LookupError) as exc:
        raise refusal(exc) from exc


@jobs_router.post("/options-golden-search",response_model=JobAccepted,status_code=202)
async def dispatch(body: JobRequest) -> JobAccepted:
    from app.services.research_runtime_client import (
        forward_options_search_job,
        is_research_runtime,
        research_forwarding_enabled,
    )
    if research_forwarding_enabled() and not is_research_runtime():
        return JobAccepted.model_validate(await forward_options_search_job(body.model_dump(mode="json")))
    await research_records.refuse_unauthorized_dispatch()
    try:
        bound = await writer_loop_call(with_connection(repo.bind,body.study_id,body.stage_token,body.job_id))
        await research_records.stamp_research_dispatch(job_id=body.job_id,kind="options_golden_search",record_id=body.study_id)
    except (repo.Conflict,LookupError,ValidationError) as exc:
        raise refusal(exc) from exc
    except (CallerStoppedWaitingError,HTTPException):
        with contextlib.suppress(CallerStoppedWaitingError):
            await writer_loop_call(with_connection(repo.release,body.study_id,body.job_id))
        raise
    if not bound:
        await asyncio.to_thread(research_records.require_live_redelivery,body.job_id,noun="Options study")
        return JobAccepted(job_id=body.job_id,study_id=body.study_id)
    runtime = is_research_runtime()

    def work(emit: ProgressEmitter,cancel: CancellationCheck) -> dict:
        from app.jobs.checkpoints import GatedLease
        from app.research.persistence import ownership
        authority = ownership.process_authority()
        lease = GatedLease(body.job_id,task_id="options-study",authority=authority) if runtime and authority is not None else None
        if runtime and lease is None:
            raise RuntimeError("The research runtime lost coordinator authority.")
        if lease:
            lease.phase("options-study-start")
        def checkpoint(done: int,total: int) -> None:
            emit.progress(done,total,unit="evaluations")
            if lease:
                lease.completion(done,total)
        result = stages.run(body.study_id,body.stage_token,body.job_id,
            cancel=lease.fenced_cancel if lease else cancel.raise_if_cancelled,checkpoint=checkpoint, on_child_checkpoint=lease.child_checkpoint if lease else None)
        return {"study_id":result["id"],"phase":result["phase"],"status":result["status"]}
    try:
        run_in_thread(body.job_id,work,thread_name=f"options-search-{body.job_id[:8]}",
            cancel_check_every_n=1,emit_renews_lease=not runtime)
    except Exception:
        await writer_loop_call(with_connection(repo.release,body.study_id,body.job_id))
        raise
    return JobAccepted(job_id=body.job_id,study_id=body.study_id)


@jobs_router.post("/options-golden-search-history",response_model=HistoryCaptureAccepted,status_code=202,
                  dependencies=[Depends(require_data_plane_control_secret)])
async def capture_history(body: HistoryCaptureJobRequest, api: HistoryCaptureService = Depends(get_service)) -> HistoryCaptureAccepted:
    try:
        underlying, sessions = await api.history_capture_scope(body.request)
    except (repo.Conflict, ValueError, LookupError) as exc:
        raise refusal(exc) from exc

    def work(emit: ProgressEmitter, cancel: CancellationCheck) -> dict:
        from app.options_backtest.chain_download import Outcome, ensure_chain_data
        from app.options_backtest.data_check import lake_reason_words

        async def capture() -> dict:
            emit.phase("capturing_history", friendly="Capturing underlying and listing history")
            fetched = 0
            for index, session_ms in enumerate(sessions, start=1):
                cancel.raise_if_cancelled()
                day = et_date_at_ms(session_ms)
                items = await ensure_chain_data(underlying, day, expiry=None, contracts=[])
                cancel.raise_if_cancelled()
                for item in items:
                    if item.outcome is Outcome.FAILED:
                        reason = (
                            "the underlying minute bars could not be fetched" if item.reason == "underlying_not_fetched" else
                            "the stored underlying bars could not be read" if item.reason == "underlying_unreadable" else
                            lake_reason_words(item.reason or "")
                        )
                        raise RuntimeError(f"{item.kind.value} history for {day.isoformat()} could not be captured: {reason}.")
                    if item.outcome is Outcome.FETCHED:
                        fetched += 1
                emit.progress(index, len(sessions), unit="sessions")
            return {"sessions_checked": len(sessions), "artifacts_fetched": fetched}

        return asyncio.run(capture())

    run_in_thread(body.job_id, work, thread_name=f"options-history-{body.job_id[:8]}")
    return HistoryCaptureAccepted(job_id=body.job_id)


@router.get("/studies/{study_id}/candidates/{candidate_hash}/sessions/{session_open_ms}",response_model=SessionResult)
async def session_evidence(study_id: StudyId,
    candidate_hash: Annotated[str,Path(pattern=r"^[a-f0-9]{64}$")],
    session_open_ms: Annotated[int,Path(ge=0,le=253402300799999)],
    window: Literal["development","final"]="development",api=Depends(get_service)) -> SessionResult:
    try:
        return SessionResult.model_validate(await api.read_session(study_id,candidate_hash,window,session_open_ms))
    except (ValueError,LookupError) as exc:
        raise refusal(exc) from exc
