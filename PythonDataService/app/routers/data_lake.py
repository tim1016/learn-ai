"""HTTP routes for the data lake.

POST /api/data-lake/ensure-data — invokes the in-process ensure_data() function.
POST /api/data-lake/backfill — submits a backfill as a job (#1836): reuses the
established job pattern (app.jobs.runner.run_in_thread + app.jobs.progress.
ProgressEmitter) so progress streams over the same Redis-backed SSE channel
every other job type uses. No new job infrastructure; the per-day loop over
ensure_data lives in app.data_lake.backfill.run_backfill.
Behind the DATA_LAKE_ENABLED feature flag; routes return 404 when the flag is off
(via main.py wiring, not this module).

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.3
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, Field

from app.data_lake.backfill import BackfillDayProgress, BackfillWaitProgress, run_backfill
from app.data_lake.ensure_data import ensure_data
from app.data_lake.types import DataAvailabilityResult, DataRunSpec
from app.jobs.progress import ProgressEmitter
from app.jobs.runner import run_in_thread

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/data-lake", tags=["data-lake"])


@router.post("/ensure-data", response_model=DataAvailabilityResult)
async def post_ensure_data(spec: DataRunSpec) -> DataAvailabilityResult:
    logger.info(
        "[STEP 1] /api/data-lake/ensure-data received: request_id=%s, symbols=%s",
        spec.request_id,
        spec.symbols,
    )
    return await ensure_data(spec)


class BackfillJobRequest(BaseModel):
    """Body of POST /api/data-lake/backfill.

    Wraps the existing DataRunSpec ensure contract as a job submission:
    the same snake_case fields and validation as /ensure-data's body
    (this router isn't behind the .NET-forwards-camelCase-verbatim
    convention app/routers/jobs.py uses — it's called the same direct way
    /ensure-data already is), plus a job_id the caller mints ahead of
    dispatch, per the established job pattern's convention of the caller
    minting the id before the worker starts writing progress against it.
    """

    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(..., min_length=1)
    spec: DataRunSpec


def _emit_day_progress(emit: ProgressEmitter, progress: BackfillDayProgress) -> None:
    """Surface one day's outcome as both a coarse progress tick and a
    structured domain event.

    Typed ArtifactFailure.reason codes (auth, entitlement, rate-limited,
    unknown symbol, ...) travel intact through the event payload and the
    log line — never flattened into prose — so an operator can tell an
    outage from a plan limitation straight off the SSE stream.
    """
    emit.progress(
        current=progress.day_index,
        total=progress.total_days,
        unit="days",
        message=(
            f"{progress.trading_date.isoformat()}: fetched {progress.fetched_count}, "
            f"reused {progress.reused_count}, failed {len(progress.failures)}"
        ),
    )
    emit.emit_event(
        "data_lake.backfill_day",
        {
            "trading_date": progress.trading_date.isoformat(),
            "day_index": progress.day_index,
            "total_days": progress.total_days,
            "days_remaining": progress.total_days - progress.day_index,
            "fetched_count": progress.fetched_count,
            "reused_count": progress.reused_count,
            "failures": [f.model_dump(mode="json") for f in progress.failures],
        },
    )
    for failure in progress.failures:
        detail_suffix = f" ({failure.detail})" if failure.detail else ""
        emit.log(
            f"{progress.trading_date.isoformat()} {failure.artifact_kind} failed: {failure.reason}{detail_suffix}",
            level="warning",
        )


def _emit_wait_progress(emit: ProgressEmitter, progress: BackfillWaitProgress) -> None:
    """Keep the SSE stream informative while a day coalesces on another
    worker's in-flight claim, instead of going silent for the wait.

    run_backfill already decides which polls are worth surfacing
    (backfill._WAIT_NOTIFY_EVERY, next to the poll interval it's derived
    from) — every on_wait callback that reaches this router is relayed
    verbatim, with no second throttling decision duplicated here.
    """
    emit.log(
        f"{progress.trading_date.isoformat()} {progress.symbol} {progress.data_type}: "
        f"waiting on another worker's in-flight fetch (attempt {progress.attempt})",
        level="info",
    )


@router.post("/backfill", status_code=status.HTTP_202_ACCEPTED)
async def start_backfill_job(req: BackfillJobRequest) -> dict:
    """Kick off a data-lake backfill in a worker thread. Returns 202.

    The work iterates the requested range's canonical NYSE sessions and
    calls the existing ensure_data() seam once per day
    (app.data_lake.backfill.run_backfill), emitting a job.progress tick and
    a data_lake.backfill_day event after each one.
    """
    spec = req.spec
    logger.info(
        "[STEP 1] /api/data-lake/backfill received: job_id=%s, request_id=%s, symbols=%s",
        req.job_id,
        spec.request_id,
        spec.symbols,
    )

    def work(emit: ProgressEmitter, cancel) -> dict:
        cancel.raise_if_cancelled()
        emit.phase("backfilling")
        emit.log(
            f"Backfilling {', '.join(spec.symbols)} from {spec.start_trading_date} to {spec.end_trading_date}"
        )

        def on_day_progress(progress: BackfillDayProgress) -> None:
            cancel.raise_if_cancelled()
            _emit_day_progress(emit, progress)

        def on_wait(progress: BackfillWaitProgress) -> None:
            cancel.raise_if_cancelled()
            _emit_wait_progress(emit, progress)

        async def _do() -> dict:
            result = await run_backfill(
                spec,
                on_day_progress=on_day_progress,
                on_wait=on_wait,
                cancel_check=cancel.raise_if_cancelled,
            )
            return result.model_dump(mode="json")

        # run_backfill is async (ensure_data awaits asyncpg + httpx calls);
        # run_in_thread executes work() on a plain worker thread, so this
        # thread gets its own event loop — matching the lean-engine-run job's
        # asyncio.run(_do()) pattern in app/routers/jobs.py.
        return asyncio.run(_do())

    run_in_thread(
        req.job_id,
        work,
        thread_name=f"lake-backfill-{req.job_id[:8]}",
        cancel_check_every_n=1,
    )
    return {"job_id": req.job_id, "status": "queued"}
