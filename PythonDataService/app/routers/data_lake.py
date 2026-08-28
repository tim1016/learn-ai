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
from typing import Any

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, Field

from app.data_lake.backfill import (
    BackfillDayProgress,
    BackfillResult,
    BackfillWaitProgress,
    EnsureFn,
    LeaseStatusFn,
    run_backfill,
)
from app.data_lake.catalog_client import MinuteBarLeaseStatus, select_minute_bar_lease_status
from app.data_lake.ensure_data import ensure_data
from app.data_lake.types import ArtifactFailure, ArtifactIdentity, DataAvailabilityResult, DataRunSpec, NonSessionRecord
from app.jobs.progress import ProgressEmitter
from app.jobs.runner import run_in_thread
from app.lean_sidecar.trading_calendar import session_open_ms_utc

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


# ---------------------------------------------------------------------------
# Wire serialization — temporal-rigor.md: every wire temporal value is
# int64 ms UTC, never an ISO date string. ArtifactFailure/NonSessionRecord
# stay plain `date`-typed Pydantic models (they're a shared contract
# /ensure-data's own response also uses, out of scope here); the
# conversion happens only at this router's own SSE/job-result boundary.
# "Trading date" is anchored at the session open (09:30 ET), per
# temporal-rigor.md's date-anchored-value rule.
# ---------------------------------------------------------------------------


def _failure_to_wire(failure: ArtifactFailure) -> dict[str, Any]:
    payload = failure.model_dump(mode="json", exclude={"trading_date"})
    payload["trading_date_ms"] = session_open_ms_utc(failure.trading_date) if failure.trading_date is not None else None
    return payload


def _non_session_to_wire(record: NonSessionRecord) -> dict[str, Any]:
    payload = record.model_dump(mode="json", exclude={"trading_date"})
    payload["trading_date_ms"] = session_open_ms_utc(record.trading_date)
    return payload


def _backfill_result_to_wire(result: BackfillResult) -> dict[str, Any]:
    payload = result.model_dump(
        mode="json",
        exclude={"start_trading_date", "end_trading_date", "failures", "skipped_non_sessions"},
    )
    payload["start_trading_date_ms"] = session_open_ms_utc(result.start_trading_date)
    payload["end_trading_date_ms"] = session_open_ms_utc(result.end_trading_date)
    payload["failures"] = [_failure_to_wire(f) for f in result.failures]
    payload["skipped_non_sessions"] = [_non_session_to_wire(r) for r in result.skipped_non_sessions]
    return payload


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
            "trading_date_ms": session_open_ms_utc(progress.trading_date),
            "day_index": progress.day_index,
            "total_days": progress.total_days,
            "days_remaining": progress.total_days - progress.day_index,
            "fetched_count": progress.fetched_count,
            "reused_count": progress.reused_count,
            "failures": [_failure_to_wire(f) for f in progress.failures],
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
    verbatim, with no second throttling decision duplicated here. This is
    a free-text log line, not a structured wire field, so the trading
    date renders as prose (matching the "Backfilling ... from ... to ..."
    line above) rather than needing the ms-UTC treatment.
    """
    emit.log(
        f"{progress.trading_date.isoformat()} {progress.symbol} {progress.data_type}: "
        f"waiting on another worker's in-flight fetch (attempt {progress.attempt})",
        level="info",
    )


def _bridge_ensure_fn(loop: asyncio.AbstractEventLoop) -> EnsureFn:
    """Route every ensure_data() call back onto the loop this HTTP
    request is already running on.

    ensure_data's asyncpg pool (app.data_lake.catalog_client) is a
    process-global bound to whichever event loop first awaits
    catalog_client.init_pool() — using it from a different loop raises
    asyncpg's cross-loop error. run_in_thread's worker thread has no loop
    of its own; work()'s asyncio.run(_do()) below spins up a brand-new,
    throwaway loop per job, which IS a different loop the moment the pool
    was already initialized elsewhere — a prior /ensure-data call on this
    request's own loop (the common case, since /ensure-data is a plain
    async handler that always runs there), or a prior backfill job's
    now-closed one. Bridging every ensure_data() call through
    run_coroutine_threadsafe onto the loop captured here — before the
    worker thread starts — keeps every data-lake asyncpg operation on one
    consistent loop for the life of the process. Only the pool-touching
    call is bridged; run_backfill's own orchestration and the emitter's
    synchronous Redis calls (progress/log/cancel-check) stay on the
    worker thread exactly as before, so this doesn't reintroduce blocking
    work onto the shared app loop.
    """

    async def _ensure_on_request_loop(day_spec: DataRunSpec) -> DataAvailabilityResult:
        future = asyncio.run_coroutine_threadsafe(ensure_data(day_spec), loop)
        return await asyncio.wrap_future(future)

    return _ensure_on_request_loop


def _bridge_status_fn(loop: asyncio.AbstractEventLoop) -> LeaseStatusFn:
    """Same bridge as _bridge_ensure_fn, for the lease-status poll — it
    reads the same pool-backed catalog table and must run on the same
    loop the pool is bound to."""

    async def _status_on_request_loop(identity: ArtifactIdentity) -> MinuteBarLeaseStatus | None:
        future = asyncio.run_coroutine_threadsafe(select_minute_bar_lease_status(identity), loop)
        return await asyncio.wrap_future(future)

    return _status_on_request_loop


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
    # Captured on this request's own running loop (the FastAPI app loop),
    # before the worker thread starts — see _bridge_ensure_fn.
    loop = asyncio.get_running_loop()

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
                ensure_fn=_bridge_ensure_fn(loop),
                status_fn=_bridge_status_fn(loop),
            )
            return _backfill_result_to_wire(result)

        # run_backfill's own orchestration (the per-day loop, progress/log
        # emission, cancellation checks) runs entirely on this worker
        # thread's own throwaway loop, matching the lean-engine-run job's
        # asyncio.run(_do()) pattern in app/routers/jobs.py — only the
        # ensure_data()/catalog calls bridged above cross back onto the
        # request's loop.
        return asyncio.run(_do())

    run_in_thread(
        req.job_id,
        work,
        thread_name=f"lake-backfill-{req.job_id[:8]}",
        cancel_check_every_n=1,
    )
    return {"job_id": req.job_id, "status": "queued"}
