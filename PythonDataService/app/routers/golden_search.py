"""Golden Search HTTP boundary (#2696, ADR 0074).

``router`` — ``/api/research/golden-search``, mounted behind the data-plane
control guard (every unsafe method needs the control secret): capabilities,
defaults, preflight, the study record and its one command endpoint, the
evaluation ledger and candidate detail. ``jobs_router`` —
``POST /api/jobs-internal/golden-search``, dispatched by the .NET jobs
boundary with a minted ``job_id``. It is unguarded like Grid Search's, but it
can only run a stage a guarded command already authorized: the request must
echo that command's ``stage_token``, and the study binds one job to it.

The router is transport only. The study service owns every rule; its
refusals map to HTTP by kind — invalid 400, not found 404, conflict 409
(with the study as it now stands), unavailable 503 — under one body,
``{"detail": {code, message, field, refusals, study}}``.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, Response, status

from app.jobs.progress import CancellationCheck, ProgressEmitter
from app.jobs.runner import run_in_thread
from app.research.golden_search import service
from app.research.golden_search.models import CandidateKey, GoldenSearchRefusal, RefusalKind, StudyRow
from app.routers import research_records as records
from app.schemas.golden_search import (
    GoldenSearchCandidateDetail,
    GoldenSearchCapability,
    GoldenSearchCommandRequest,
    GoldenSearchCreateStudyRequest,
    GoldenSearchDefaults,
    GoldenSearchEvaluationPage,
    GoldenSearchJobAccepted,
    GoldenSearchJobRequest,
    GoldenSearchPlanCharts,
    GoldenSearchPreflight,
    GoldenSearchProtocolRequest,
    GoldenSearchRefusalBody,
    GoldenSearchRefusalDetail,
    GoldenSearchSearchCharts,
    GoldenSearchStudyDetail,
    GoldenSearchStudySummary,
    GoldenSearchTestOverTimeCharts,
)

router = APIRouter()
jobs_router = APIRouter()
logger = logging.getLogger(__name__)
NOUN = "study"

_STATUS_BY_KIND: dict[RefusalKind, int] = {
    "invalid": status.HTTP_400_BAD_REQUEST,
    "not_found": status.HTTP_404_NOT_FOUND,
    "conflict": status.HTTP_409_CONFLICT,
    "unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
}
_REFUSALS: dict[int | str, dict[str, Any]] = {
    code: {"model": GoldenSearchRefusalBody} for code in sorted(set(_STATUS_BY_KIND.values()))
}
StudyId = Annotated[str, Path(min_length=1, max_length=64)]


async def _detail(row: StudyRow, *, dispatch: dict[str, Any] | None = None) -> GoldenSearchStudyDetail:
    return GoldenSearchStudyDetail.model_validate(await service.detail(row, dispatch=dispatch))


async def _refused(exc: GoldenSearchRefusal) -> HTTPException:
    """The refusal as its HTTP answer; a conflict about a study carries the study as it now stands."""
    study = (await _detail(exc.study)).model_dump(mode="json") if exc.kind == "conflict" and exc.study is not None else None
    detail = GoldenSearchRefusalDetail.model_validate(
        {"code": exc.code, "message": str(exc), "field": exc.field, "refusals": list(exc.refusals), "study": study}
    )
    return HTTPException(status_code=_STATUS_BY_KIND[exc.kind], detail=detail.model_dump(mode="json"))


# ── Plan ─────────────────────────────────────────────────────────────────


@router.get("/capabilities", response_model=list[GoldenSearchCapability])
async def get_capabilities() -> list[GoldenSearchCapability]:
    """Every registered strategy: its Golden Search knobs, fixed controls and constraints, or why it has none."""
    return [GoldenSearchCapability.model_validate(row) for row in service.capabilities()]


@router.get("/defaults", response_model=GoldenSearchDefaults, responses=_REFUSALS)
async def get_defaults(
    strategy_key: str = Query(min_length=1, max_length=128),
    symbol: str = Query(min_length=1, max_length=16),
    final_months: int = Query(service.DEFAULT_FINAL_MONTHS, ge=1, le=60),
    training_months: int = Query(service.DEFAULT_TRAINING_MONTHS, ge=1, le=120),
    test_months: int = Query(service.DEFAULT_TEST_MONTHS, ge=1, le=60),
) -> GoldenSearchDefaults:
    """A complete starting plan whose intervals the server computes from the calendar and the lake."""
    try:
        plan = await service.defaults(
            strategy_key, symbol, final_months=final_months, training_months=training_months, test_months=test_months
        )
    except GoldenSearchRefusal as exc:
        raise await _refused(exc) from exc
    return GoldenSearchDefaults.model_validate(plan)


@router.post("/preflight", response_model=GoldenSearchPreflight, responses=_REFUSALS)
async def preflight_plan(body: GoldenSearchProtocolRequest) -> GoldenSearchPreflight:
    """Review a plan without side effects. Every problem a well-formed plan can have is a refusal in a 200."""
    try:
        review = await service.preflight(body.as_plan())
    except GoldenSearchRefusal as exc:
        raise await _refused(exc) from exc
    return GoldenSearchPreflight.model_validate(review)


# ── Studies ──────────────────────────────────────────────────────────────


@router.post("/studies", status_code=status.HTTP_201_CREATED, response_model=GoldenSearchStudyDetail, responses=_REFUSALS)
async def lock_study(body: GoldenSearchCreateStudyRequest) -> GoldenSearchStudyDetail:
    """Lock a reviewed plan into a new study; the same key and plan return the study it already locked.

    With ``run_research`` the study also starts Search and runs on to Compare;
    the answer carries the dispatch the client starts through the jobs boundary.
    """
    try:
        if body.run_research:
            outcome = await service.lock_and_run(body.protocol.as_plan(), idempotency_key=body.idempotency_key)
            return await _detail(outcome.study, dispatch=outcome.dispatch)
        row = await service.lock_study(body.protocol.as_plan(), idempotency_key=body.idempotency_key)
    except GoldenSearchRefusal as exc:
        raise await _refused(exc) from exc
    return await _detail(row)


@router.get("/studies", response_model=list[GoldenSearchStudySummary])
async def list_studies(
    strategy_key: str | None = Query(None, max_length=128),
    symbol: str | None = Query(None, max_length=16),
    include_hidden: bool = Query(False),
    limit: int = Query(100, ge=1, le=1000),
) -> list[GoldenSearchStudySummary]:
    """History, newest first; hidden studies only when asked for."""
    rows = await service.summaries(strategy_key=strategy_key, symbol=symbol, include_hidden=include_hidden, limit=limit)
    return [GoldenSearchStudySummary.model_validate(row) for row in rows]


@router.get("/studies/{study_id}", response_model=GoldenSearchStudyDetail, responses=_REFUSALS)
async def get_study(study_id: StudyId) -> GoldenSearchStudyDetail:
    try:
        row = await service.get_row(study_id)
    except GoldenSearchRefusal as exc:
        raise await _refused(exc) from exc
    return await _detail(row)


@router.post("/studies/{study_id}/commands", response_model=GoldenSearchStudyDetail, responses=_REFUSALS)
async def run_command(body: GoldenSearchCommandRequest, study_id: StudyId) -> GoldenSearchStudyDetail:
    """Apply one lifecycle command under its expected revision.

    A command that authorizes a stage answers with its ``dispatch``; the
    client starts that stage through the jobs boundary. ``revise`` answers
    with the new study it created.
    """
    try:
        outcome = await service.run_command(
            study_id,
            command=body.command,
            expected_revision=body.expected_revision,
            idempotency_key=body.idempotency_key,
            payload=body.payload,
        )
    except GoldenSearchRefusal as exc:
        raise await _refused(exc) from exc
    return await _detail(outcome.study, dispatch=outcome.dispatch)


@router.delete("/studies/{study_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response, responses=_REFUSALS)
async def hide_study(study_id: StudyId) -> Response:
    """Hide a study from history; refused while a stage runs. Its rows, trials and exposures stay recorded."""
    try:
        await service.hide(study_id)
    except GoldenSearchRefusal as exc:
        raise await _refused(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/studies/{study_id}/evaluations", response_model=GoldenSearchEvaluationPage, responses=_REFUSALS)
async def list_evaluations(
    study_id: StudyId,
    stage: str | None = Query(None, max_length=32, description="The step that asked for the evaluation, e.g. search or validation."),
    fold_index: int | None = Query(None, ge=0),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
) -> GoldenSearchEvaluationPage:
    try:
        result = await service.evaluations(study_id, stage=stage, fold_index=fold_index, page=page, page_size=page_size)
    except GoldenSearchRefusal as exc:
        raise await _refused(exc) from exc
    return GoldenSearchEvaluationPage.model_validate(result)


@router.get("/studies/{study_id}/charts/plan", response_model=GoldenSearchPlanCharts, responses=_REFUSALS)
async def get_plan_charts(study_id: StudyId) -> GoldenSearchPlanCharts:
    """The Plan step's charts: the frozen windows, search space, workload, trade minimums and data coverage."""
    try:
        result = await service.plan_step_charts(study_id)
    except GoldenSearchRefusal as exc:
        raise await _refused(exc) from exc
    return GoldenSearchPlanCharts.model_validate(result)


@router.get("/studies/{study_id}/charts/search", response_model=GoldenSearchSearchCharts, responses=_REFUSALS)
async def get_search_charts(study_id: StudyId) -> GoldenSearchSearchCharts:
    """The Search step's charts: each procedure's replayed path, knob moves and profiles, and every point it scored."""
    try:
        result = await service.search_step_charts(study_id)
    except GoldenSearchRefusal as exc:
        raise await _refused(exc) from exc
    return GoldenSearchSearchCharts.model_validate(result)


@router.get("/studies/{study_id}/charts/test-over-time", response_model=GoldenSearchTestOverTimeCharts, responses=_REFUSALS)
async def get_test_over_time_charts(study_id: StudyId) -> GoldenSearchTestOverTimeCharts:
    """The Test over time step's charts: every fold's windows and results, the linked returns and each searched knob's drift."""
    try:
        result = await service.test_over_time_charts(study_id)
    except GoldenSearchRefusal as exc:
        raise await _refused(exc) from exc
    return GoldenSearchTestOverTimeCharts.model_validate(result)


@router.get("/studies/{study_id}/candidates/{candidate_key}", response_model=GoldenSearchCandidateDetail, responses=_REFUSALS)
async def get_candidate(candidate_key: CandidateKey, study_id: StudyId) -> GoldenSearchCandidateDetail:
    """A candidate's development detail run (equity, drawdown, months, trades), and its final-test run once scored."""
    try:
        result = await service.candidate(study_id, candidate_key)
    except GoldenSearchRefusal as exc:
        raise await _refused(exc) from exc
    return GoldenSearchCandidateDetail.model_validate(result)


# ── Jobs boundary ────────────────────────────────────────────────────────


@jobs_router.post("/golden-search", status_code=status.HTTP_202_ACCEPTED, response_model=GoldenSearchJobAccepted)
async def start_golden_search_job(req: GoldenSearchJobRequest) -> GoldenSearchJobAccepted:
    """Run the stage a guarded command authorized, on a worker thread. 202 once the job is bound to it.

    A token the study did not issue, or a stage another job already holds,
    is refused (409); nothing runs without the matching token.
    """
    try:
        bound = await service.bind_dispatch(req.study_id, stage_token=req.stage_token, job_id=req.job_id)
    except GoldenSearchRefusal as exc:
        raise await _refused(exc) from exc
    accepted = GoldenSearchJobAccepted(job_id=req.job_id, study_id=req.study_id, status="queued")
    if bound == "redelivery":
        # Never a second worker beside the first, and never a replay of a closed job under its old id.
        records.require_live_redelivery(req.job_id, noun=NOUN)
        return accepted

    def work(emit: ProgressEmitter, cancel: CancellationCheck) -> dict[str, Any]:
        outcome = service.run_stage(
            req.study_id,
            stage_token=req.stage_token,
            job_id=req.job_id,
            cancel_check=cancel.raise_if_cancelled,
            on_phase=emit.phase,
            on_progress=lambda done, total: emit.progress(done, total, unit="evaluations"),
            on_log=emit.log,
        )
        return outcome.as_dict()

    run_in_thread(req.job_id, work, thread_name=f"golden-search-{req.job_id[:8]}", cancel_check_every_n=1)
    logger.info(
        "golden search stage dispatched",
        extra={"action": "golden_search_stage_dispatched", "study_id": req.study_id, "job_id": req.job_id},
    )
    return accepted
