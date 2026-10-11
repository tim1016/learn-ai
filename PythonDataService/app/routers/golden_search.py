"""Golden Search HTTP boundary (#2696, ADR 0074).

``router`` — ``/api/research/golden-search``, mounted behind the data-plane
control guard (every unsafe method needs the control secret): capabilities,
defaults, preflight, the study record and its one command endpoint, the
runs with their curves and candidate detail. ``jobs_router`` —
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
import threading
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, Response, status

from app.config import settings
from app.jobs.progress import CancellationCheck, ProgressEmitter
from app.jobs.runner import run_in_thread
from app.research.golden_search import service
from app.research.golden_search.models import CandidateKey, GoldenSearchRefusal, RefusalKind, StudyRow
from app.research.golden_search.run_curves import MAX_CURVES, CurveSet, RunGroup, RunSort
from app.routers import research_records as records
from app.schemas.golden_search import (
    GoldenSearchCandidateDetail,
    GoldenSearchCapability,
    GoldenSearchCommandRequest,
    GoldenSearchCreateStudyRequest,
    GoldenSearchCurveSet,
    GoldenSearchDefaults,
    GoldenSearchJobAccepted,
    GoldenSearchJobRequest,
    GoldenSearchPlanCharts,
    GoldenSearchPreflight,
    GoldenSearchProtocolRequest,
    GoldenSearchRefusalBody,
    GoldenSearchRefusalDetail,
    GoldenSearchRunCurves,
    GoldenSearchRunsPage,
    GoldenSearchSearchCharts,
    GoldenSearchStudyDetail,
    GoldenSearchStudySummary,
    GoldenSearchTestOverTimeCharts,
)
from app.utils.session_anchors import MAX_TIMESTAMP_MS

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
    study = (
        (await _detail(exc.study)).model_dump(mode="json") if exc.kind == "conflict" and exc.study is not None else None
    )
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
    range_start_ms: int | None = Query(
        None, ge=0, le=MAX_TIMESTAMP_MS, description="The first day of a picked study range, as its ET midnight."
    ),
    range_end_ms: int | None = Query(
        None, ge=0, le=MAX_TIMESTAMP_MS, description="The ET midnight after the picked range's last day."
    ),
) -> GoldenSearchDefaults:
    """A complete starting plan whose intervals the server computes from the calendar and the lake, inside a picked range when one is given."""
    try:
        plan = await service.defaults(
            strategy_key,
            symbol,
            final_months=final_months,
            training_months=training_months,
            test_months=test_months,
            range_start_ms=range_start_ms,
            range_end_ms=range_end_ms,
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


@router.post(
    "/studies", status_code=status.HTTP_201_CREATED, response_model=GoldenSearchStudyDetail, responses=_REFUSALS
)
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


@router.delete(
    "/studies/{study_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response, responses=_REFUSALS
)
async def hide_study(study_id: StudyId) -> Response:
    """Hide a study from history; refused while a stage runs. Its rows, trials and exposures stay recorded."""
    try:
        await service.hide(study_id)
    except GoldenSearchRefusal as exc:
        raise await _refused(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/studies/{study_id}/runs", response_model=GoldenSearchRunsPage, responses=_REFUSALS)
async def list_runs(
    study_id: StudyId,
    group: RunGroup | None = Query(None, description="What asked for the runs."),
    fold_index: int | None = Query(None, ge=0),
    sort: RunSort = Query("recorded", description="The order: as recorded, or by a measure."),
    descending: bool = Query(False),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
) -> GoldenSearchRunsPage:
    """Every run of the study, grouped by what asked for it (#2930)."""
    try:
        result = await service.runs(
            study_id,
            group=group,
            fold_index=fold_index,
            sort=sort,
            descending=descending,
            page=page,
            page_size=page_size,
        )
    except GoldenSearchRefusal as exc:
        raise await _refused(exc) from exc
    return GoldenSearchRunsPage.model_validate(result)


@router.get("/studies/{study_id}/runs/curves", response_model=GoldenSearchRunCurves, responses=_REFUSALS)
async def get_run_curves(
    study_id: StudyId,
    key: Annotated[list[str], Query(description="The evaluation key of each run to draw.", max_length=MAX_CURVES)],
) -> GoldenSearchRunCurves:
    """The named runs' daily curves: cumulative return and fall from peak at each session close (#2930)."""
    try:
        result = await service.run_curves(study_id, key)
    except GoldenSearchRefusal as exc:
        raise await _refused(exc) from exc
    return GoldenSearchRunCurves.model_validate(result)


@router.get("/studies/{study_id}/charts/run-curves", response_model=GoldenSearchCurveSet, responses=_REFUSALS)
async def get_curve_set(
    study_id: StudyId,
    curve_set: Annotated[CurveSet, Query(alias="set", description="Which step's runs to draw.")],
    candidate: CandidateKey | None = Query(
        None, description="The candidate whose run, stresses and neighbors to draw; for the candidate set only."
    ),
) -> GoldenSearchCurveSet:
    """The runs one step draws together, each with its role and its daily curve (#2930)."""
    try:
        result = await service.curve_set(study_id, curve_set=curve_set, candidate_key=candidate)
    except GoldenSearchRefusal as exc:
        raise await _refused(exc) from exc
    return GoldenSearchCurveSet.model_validate(result)


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


@router.get(
    "/studies/{study_id}/charts/test-over-time", response_model=GoldenSearchTestOverTimeCharts, responses=_REFUSALS
)
async def get_test_over_time_charts(study_id: StudyId) -> GoldenSearchTestOverTimeCharts:
    """The Test over time step's charts: every fold's windows and results, the linked returns and each searched knob's drift."""
    try:
        result = await service.test_over_time_charts(study_id)
    except GoldenSearchRefusal as exc:
        raise await _refused(exc) from exc
    return GoldenSearchTestOverTimeCharts.model_validate(result)


@router.get(
    "/studies/{study_id}/candidates/{candidate_key}", response_model=GoldenSearchCandidateDetail, responses=_REFUSALS
)
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
    is refused (409); nothing runs without the matching token. When research
    execution is routed to the isolated runtime (#3043), this process
    forwards the dispatch and never binds or starts a thread: binding,
    ownership and execution happen once, in the runtime.
    """
    from app.services.research_runtime_client import (
        forward_golden_search_job,
        is_research_runtime,
        research_forwarding_enabled,
    )

    if research_forwarding_enabled() and not is_research_runtime():
        forwarded = await forward_golden_search_job(req.model_dump(mode="json"))
        return GoldenSearchJobAccepted(**forwarded)

    # Refused authority must never bind: a stamp refusal after the bind
    # strands the study on a job no worker will serve (#3042).
    await records.refuse_unauthorized_dispatch()
    try:
        bound = await service.bind_dispatch(req.study_id, stage_token=req.stage_token, job_id=req.job_id)
    except GoldenSearchRefusal as exc:
        raise await _refused(exc) from exc
    try:
        await records.stamp_research_dispatch(job_id=req.job_id, kind="golden_search", record_id=req.study_id)
    except HTTPException:
        # The binding is durable but this dispatch will not run: release it
        # (still queued, still this job id) so a retry binds instead of
        # becoming a redelivery that liveness refuses forever (#3042).
        await service.release_dispatch_binding(req.study_id, job_id=req.job_id)
        raise
    accepted = GoldenSearchJobAccepted(job_id=req.job_id, study_id=req.study_id, status="queued")
    if bound == "redelivery":
        # Never a second worker beside the first, and never a replay of a closed job under its old id.
        records.require_live_redelivery(req.job_id, noun=NOUN)
        return accepted

    runtime_executes = is_research_runtime()
    work = _gated_stage_work(req) if runtime_executes else _stage_work(req)

    run_in_thread(
        req.job_id,
        work,
        thread_name=f"golden-search-{req.job_id[:8]}",
        cancel_check_every_n=1,
        # In the runtime, emits renew nothing — only accepted checkpoints
        # keep the lease alive (#3041); the API's local mode keeps the
        # emit-and-check renewal ADR 0065 D1 makes authoritative there.
        emit_renews_lease=not runtime_executes,
    )
    logger.info(
        "golden search stage dispatched",
        extra={"action": "golden_search_stage_dispatched", "study_id": req.study_id, "job_id": req.job_id},
    )
    return accepted


def _stage_work(req: GoldenSearchJobRequest):
    """The local-mode worker: the stage run behind the runner's emit-and-cancel checks."""

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

    return work


def _gated_stage_work(req: GoldenSearchJobRequest):
    """The research runtime's worker: the stage run under the checkpoint lease contract (#3041, #3043).

    In the runtime, cancellation polls renew nothing — the lease renews only
    on accepted checkpoints (the stage's phase transitions and durable
    evaluation completions), each fenced by the coordinator's boot
    generation. This is the shape the serial proof in the study lifecycle
    suite pinned, now wired into the runtime that owns execution.
    """
    from app.jobs.checkpoints import CheckpointGate, ExecutionCheckpoint
    from app.jobs.progress import CancellationCheck
    from app.research.persistence import ownership
    from app.research.persistence.ownership import GenerationFencedError
    from app.utils.timestamps import now_ms_utc

    def work(emit: ProgressEmitter, _runner_cancel: CancellationCheck) -> dict[str, Any]:
        authority = ownership.process_authority()
        if authority is None:
            # The startup claim is fail-closed, so this is an authority lost
            # mid-run: the generation is dead and its lease must not renew.
            raise RuntimeError("research runtime worker without coordinator authority; the generation is fenced")
        generation = authority.boot_generation
        cancel = CancellationCheck(req.job_id, renews_lease=False)
        base_cancel = cancel.raise_if_cancelled

        def fenced_cancel() -> None:
            # Cancellation and the coordinator fence in one poll: once the
            # dedicated lock connection dies, a successor can already claim
            # the next generation, so this worker stops instead of renewing
            # a lease for a generation it no longer owns. Fired between
            # evaluations and on every stage-level poll; fencing inside a
            # single engine run waits for the child's advancing checkpoints
            # (#3045). A fenced generation is terminal — nothing may replace
            # the worker and continue — where a lost worker is retryable, so
            # this is not WorkerLost (#3045).
            base_cancel()
            if not authority.connected:
                raise GenerationFencedError("coordinator authority connection lost mid-run; the generation is fenced")

        seen: list[ExecutionCheckpoint] = []
        gate = CheckpointGate(req.job_id, generation, on_accepted=seen.append)

        def phase_checkpoint(phase: str) -> None:
            with gate_lock:
                gate.accept(
                    ExecutionCheckpoint(
                        generation=generation,
                        task_id="stage",
                        sequence=len(seen) + 1,
                        cursor=0,
                        cursor_kind="processed_blocks",
                        phase=phase,
                        occurred_at_ms=now_ms_utc(),
                        transition=True,
                    )
                )

        def completion_checkpoint(done: int, total: int) -> None:
            with gate_lock:
                gate.accept(
                    ExecutionCheckpoint(
                        generation=generation,
                        task_id="stage",
                        sequence=len(seen) + 1,
                        cursor=done,
                        cursor_kind="durable_completions",
                        phase="search",
                        occurred_at_ms=now_ms_utc(),
                        transition=True,
                    )
                )


        gate_lock = threading.Lock()

        def on_child_checkpoint(message: dict) -> None:
            # The child's real progress renews the lease through the same
            # gate: phase transitions and durable completions, generation-fenced.
            # Inline (never via the nested helpers): this runs on the pump
            # thread, and nesting two acquisitions of one non-reentrant lock
            # would deadlock the whole stage on the child's first checkpoint.
            with gate_lock:
                kind = str(message.get("cursor_kind", "processed_blocks"))
                gate.accept(
                    ExecutionCheckpoint(
                        generation=generation,
                        task_id="stage",
                        sequence=len(seen) + 1,
                        cursor=int(message.get("cursor", 0)),
                        cursor_kind=kind,
                        phase=str(message.get("phase", "search")),
                        occurred_at_ms=now_ms_utc(),
                        transition=True,
                    )
                )

        outcome = service.run_stage(
            req.study_id,
            stage_token=req.stage_token,
            job_id=req.job_id,
            cancel_check=fenced_cancel,
            on_phase=lambda phase: (emit.phase(phase), phase_checkpoint(phase)),
            on_progress=lambda done, total: (
                emit.progress(done, total, unit="evaluations"),
                completion_checkpoint(done, total),
            ),
            on_log=emit.log,
            dispatch_factory=_runtime_dispatch_factory(
                on_checkpoint=on_child_checkpoint, cancel_check=fenced_cancel, emit=emit
            ),
        )
        return outcome.as_dict()

    return work


def _runtime_dispatch_factory(*, on_checkpoint, cancel_check, emit) -> Callable[[Any], Any]:
    """The dispatch the runtime executes through: one worker, or the configured bounded pool (#3046).

    ``RESEARCH_WORKER_COUNT`` is the deployment's opt-in width — one is
    exactly the serial runtime #3044 shipped — and the pool's scheduling
    facts ride the existing log channel until their visual refinement.
    """
    from app.research.golden_search.worker import worker_dispatch_factory

    workers = settings.RESEARCH_WORKER_COUNT
    window = settings.RESEARCH_DISPATCH_WINDOW
    if workers < 2:
        return worker_dispatch_factory(on_checkpoint=on_checkpoint, cancel_check=cancel_check)
    from app.research.golden_search.pool import DispatchFacts, worker_pool_factory

    def on_facts(facts: DispatchFacts) -> None:
        emit.log(f"research worker pool: {facts.as_dict()}")

    return worker_pool_factory(
        on_checkpoint=on_checkpoint, cancel_check=cancel_check, on_facts=on_facts, workers=workers, window=window
    )
