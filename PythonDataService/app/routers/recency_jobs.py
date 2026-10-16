"""The Recency Chart's jobs boundary, on its own dispatch surface (#3051; #1927, ADR 0065).

Extracted from ``app/routers/jobs.py`` when Recency joined the isolated
research runtime: the API's jobs mount and the runtime's internal mount
both carry this router, so a research-mode dispatch forwards here and a
runtime-mode dispatch executes — the same route, the same wire, the same
durable-launch contract. The route's request models moved with it; the
recency read surface stays in ``app/routers/recency.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import Field, ValidationError

from app.jobs.progress import CancellationCheck, ProgressEmitter
from app.jobs.runner import run_in_thread
from app.research.persistence import lifecycle
from app.research.recency import service as recency_service
from app.research.sweep.grid import LowHighStepRange, StrategyGridConfig, ValueListRange
from app.schemas.http_errors import ErrorDetailResponse
from app.utils.session_anchors import MAX_TIMESTAMP_MS

logger = logging.getLogger(__name__)

router = APIRouter()

from app.routers.jobs import _CamelCaseModel  # noqa: E402  (the jobs boundary's shared camelCase base)


class ValueListRangeRequest(_CamelCaseModel):
    type: Literal["value_list"] = "value_list"
    values: list[float] = Field(min_length=1)


class LowHighStepRangeRequest(_CamelCaseModel):
    type: Literal["low_high_step"] = "low_high_step"
    low: float
    high: float
    step: float


ParamRangeRequest = Annotated[
    ValueListRangeRequest | LowHighStepRangeRequest,
    Field(discriminator="type"),
]


class StrategyGridConfigRequest(_CamelCaseModel):
    strategy_key: str = Field(..., min_length=1)
    param_ranges: dict[str, ParamRangeRequest] = Field(default_factory=dict)


class RecencyChartSpecRequest(_CamelCaseModel):
    """One Recency launch's grid and window, as sent at launch and stored in the durable row.

    Each parameter's range is either an explicit value list or an
    inclusive low/high/step sweep — the discriminated
    ``type`` field lets one dict carry either shape per parameter.
    """

    strategies: list[StrategyGridConfigRequest] = Field(min_length=1)
    symbols: list[str] = Field(min_length=1)
    window_start_ms: int = Field(le=MAX_TIMESTAMP_MS)
    window_end_ms: int = Field(le=MAX_TIMESTAMP_MS)
    data_policy: str = "polygon-adjusted-regular-minute"
    fill_mode: str = "signal_bar_close"
    commission_per_order: float = 0.0


class RecencyChartJobRequest(RecencyChartSpecRequest):
    """Body of POST /api/jobs-internal/recency-chart.

    On a resume (#1938, ``resume_launch_id`` set) the spec fields are
    ignored and the stored configuration governs — a client resends the
    stored request it read from the launch, as Grid Search's Finish does.
    """

    job_id: str = Field(..., min_length=1)
    resume_launch_id: str | None = None


def _range_request_to_grid_range(req: ValueListRangeRequest | LowHighStepRangeRequest) -> ValueListRange | LowHighStepRange:
    if isinstance(req, ValueListRangeRequest):
        return ValueListRange(tuple(req.values))
    return LowHighStepRange(low=req.low, high=req.high, step=req.step)


def _grid_configs(req: RecencyChartSpecRequest) -> list[StrategyGridConfig]:
    return [
        StrategyGridConfig(strategy_key=s.strategy_key, param_ranges={name: _range_request_to_grid_range(r) for name, r in s.param_ranges.items()})
        for s in req.strategies
    ]


@router.post(
    "/recency-chart",
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorDetailResponse,
            "description": "A resume named a launch that does not exist.",
        },
        status.HTTP_409_CONFLICT: {
            "model": ErrorDetailResponse,
            "description": "A redelivered job_id whose configuration differs or whose job is no longer running, or a resume of a launch that may not be resumed.",
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorDetailResponse,
            "description": "The job store cannot say whether a redelivered job is still running.",
        },
    },
)
async def start_recency_chart_job(req: RecencyChartJobRequest) -> dict:
    """Validate, make the launch durable, and run the Recency Chart sweep. Returns 202.

    In research mode the API forwards the dispatch and never creates,
    stamps or starts a thread (#3051): the runtime does, and runs the
    launch's independent runs through the shared research workers. Here —
    the local mode, or this process being the runtime — the launch runs on
    a worker thread, in this process at the local mode.

    A malformed or oversized grid (D11) is refused before anything is
    written; the launch row exists before the worker starts (D20); a
    redelivered ``job_id`` is acknowledged without a second worker while
    the first still holds the job, and refused (409) once that job is
    closed or if the configuration differs. A resume (#1938) instead names
    an existing launch: the new job id is bound to the durable record by
    the worker's claim, the recorded cells are skipped, and the spec
    fields are ignored — the stored configuration governs.
    Everything after the HTTP boundary is ``app.research.recency.service``.
    """
    from app.services.research_runtime_client import (
        forward_recency_chart_job,
        is_research_runtime,
        research_forwarding_enabled,
    )

    if research_forwarding_enabled() and not is_research_runtime():
        return await forward_recency_chart_job(req.model_dump(mode="json"))

    from app.routers import research_records as records

    await records.refuse_unauthorized_dispatch()
    if req.resume_launch_id is not None:
        launch_row = await recency_service.load_launch(req.resume_launch_id)
        if launch_row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "RECENCY_LAUNCH_NOT_FOUND", "message": f"RecencyLaunch {req.resume_launch_id} not found"},
            )
        # A redelivery of a live dispatch joins the first worker before any
        # resume refusal can call it unresumable — the attempt is never
        # claimed twice (#3051).
        if launch_row.job_id == req.job_id and launch_row.status == "RUNNING":
            records.require_live_redelivery(req.job_id, noun="Recency launch")
            return {"job_id": req.job_id, "launch_id": launch_row.launch_id, "status": "queued"}
        # Ask Redis only when the answer can change the refusal (a RUNNING launch).
        live = lifecycle.job_is_live(launch_row.job_id) if launch_row.status == "RUNNING" else False
        refusal = recency_service.resume_refusal(launch_row, live=live)
        if refusal is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "NOT_RESUMABLE", "message": refusal})
        try:
            stored = RecencyChartSpecRequest.model_validate_json(launch_row.config_json)
        except ValidationError as exc:
            # The stored row no longer parses (a request bound tightened since
            # the launch, e.g. the ADR 0022 (g) ceiling); it cannot run as recorded.
            reasons = "; ".join(f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors())
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "NOT_RESUMABLE", "message": f"the stored launch configuration no longer validates: {reasons}"},
            ) from exc
        try:
            launch = recency_service.validate_launch(
                launch_id=launch_row.launch_id,
                strategies=_grid_configs(stored),
                symbols=stored.symbols,
                window_start_ms=stored.window_start_ms,
                window_end_ms=stored.window_end_ms,
                data_policy=stored.data_policy,
                fill_mode=stored.fill_mode,
                commission_per_order=stored.commission_per_order,
            )
        except recency_service.RecencyLaunchRejected as exc:
            # The stored configuration no longer validates (the grid rules
            # tightened since the launch); it cannot run as recorded.
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "NOT_RESUMABLE", "message": str(exc)}) from exc
        # A resume stamps like a fresh launch (#3042): the ownership row is
        # what a restart's recovery sweep reads to fence this generation.
        await records.stamp_research_dispatch(job_id=req.job_id, kind="recency", record_id=launch.config.launch_id)
    else:
        strategies = _grid_configs(req)
        try:
            # A new launch runs, stores and fingerprints the canonical name
            # (#2599). The resume above never does this: it keeps the stored
            # spelling, the one its recorded cells were fingerprinted under.
            req = req.model_copy(update={"fill_mode": recency_service.canonical_fill_mode(req.fill_mode)})
            launch = recency_service.validate_launch(
                launch_id=req.job_id,
                strategies=strategies,
                symbols=req.symbols,
                window_start_ms=req.window_start_ms,
                window_end_ms=req.window_end_ms,
                data_policy=req.data_policy,
                fill_mode=req.fill_mode,
                commission_per_order=req.commission_per_order,
            )
        except recency_service.RecencyLaunchRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # The stamp precedes the durable row (#3042's lesson, grid's order):
        # a stamp refusal strands nothing, a create failure retries as a
        # fresh dispatch.
        await records.stamp_research_dispatch(job_id=req.job_id, kind="recency", record_id=launch.config.launch_id)
        try:
            created = await recency_service.create_launch(launch, request=req.model_dump(mode="json", exclude={"job_id"}))
        except recency_service.RecencyLaunchConflict as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        if not created:
            # A redelivery: never a second thread beside the first worker, and never a replay of a closed
            # job through its old transport record (that is a resume, accepted via ``resume_launch_id``).
            records.require_live_redelivery(req.job_id, noun="Recency launch")
            return {"job_id": req.job_id, "launch_id": req.job_id, "status": "queued"}

    runtime_executes = is_research_runtime()
    work = _gated_launch_work(req, launch) if runtime_executes else _local_launch_work(launch)
    run_in_thread(
        req.job_id,
        work,
        thread_name=f"recency-{req.job_id[:8]}",
        cancel_check_every_n=1,
        # In the runtime, emits renew nothing — only accepted checkpoints
        # keep the lease alive (#3041).
        emit_renews_lease=not runtime_executes,
        progress_min_interval_s=1.0 if runtime_executes else 0.0,
    )
    return {"job_id": req.job_id, "launch_id": launch.config.launch_id, "status": "queued"}




def _waiting_emitter(emit):
    """The waiting state as a transition-gated event (#3052)."""
    last = {"waiting": object()}

    def emit_waiting(waiting, facts) -> None:
        if waiting != last["waiting"]:
            last["waiting"] = waiting
            emit.research_waiting(waiting, detail={k: facts.get(k) for k in ("workers", "busy_lanes", "active", "queued", "ready_width") if k in facts})

    return emit_waiting

def _local_launch_work(launch: Any) -> Callable[[ProgressEmitter, CancellationCheck], dict]:
    """The local-mode worker: the launch behind the runner's emit-and-cancel checks, runs in this process."""

    def work(emit: ProgressEmitter, cancel: CancellationCheck) -> dict:
        return recency_service.run_launch(launch.config, emit=emit, cancel=cancel)

    return work


def _gated_launch_work(req: RecencyChartJobRequest, launch: Any) -> Callable[[ProgressEmitter, CancellationCheck], dict]:
    """The research runtime's launch worker: the runs under the checkpoint lease contract (#3041, #3051).

    Snapshots, trade/evidence deduplication and the terminal accounting
    stay in the coordinator under the launch attempt fence; what the
    runtime changes is only where the independent runs compute — one
    dispatch per launch on the shared pool (or one spawned child at the
    serial width).
    """
    from app.jobs.checkpoints import GatedLease
    from app.research.persistence import ownership

    def work(emit: ProgressEmitter, _runner_cancel: CancellationCheck) -> dict:
        authority = ownership.process_authority()
        if authority is None:
            # The startup claim is fail-closed, so this is an authority lost
            # mid-run: the generation is dead and its lease must not renew.
            raise RuntimeError("research runtime worker without coordinator authority; the generation is fenced")
        lease = GatedLease(req.job_id, task_id="launch", authority=authority)

        def build_dispatch(config, attempt):
            from app.research.recency.dispatch import runtime_run_dispatch

            return runtime_run_dispatch(
                {"start_ms": config.window_start_ms, "end_ms": config.window_end_ms},
                {"fill_mode": config.fill_mode, "commission_per_order": config.commission_per_order},
                attempt=attempt,
                job_id=req.job_id,
                on_checkpoint=lease.child_checkpoint,
                cancel_check=lease.fenced_cancel,
                on_log=emit.log,
                on_waiting=_waiting_emitter(emit),
            )

        return recency_service.run_launch(
            launch.config,
            emit=emit,
            cancel=CancellationCheck(req.job_id, renews_lease=False),
            run_dispatch_factory=build_dispatch,
            on_phase=lambda phase: (emit.phase(phase), lease.phase(phase)),
            on_progress=lambda done, total: (emit.progress(done, total, unit="runs"), lease.completion(done, total)),
        )

    return work
