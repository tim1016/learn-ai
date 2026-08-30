"""HTTP routes for the data lake.

POST /api/data-lake/ensure-data — invokes the in-process ensure_data() function.
GET /api/data-lake/coverage, /artifacts/{id}, /storage-summary — read-only
projections of the catalog for the Observatory UI (issue #1835). Thin: no new
state, no writes.

POST /api/data-lake/backfill — submits a backfill as a job (#1836): reuses the
established job pattern (app.jobs.runner.run_in_thread + app.jobs.progress.
ProgressEmitter) so progress streams over the same Redis-backed SSE channel
every other job type uses. No new job infrastructure; the per-day loop over
ensure_data lives in app.data_lake.backfill.run_backfill.

GET /api/data-lake/backfill-defaults — the handful of DataRunSpec values a
browser cannot derive (the pinned LEAN image digest, the symbol/range caps).
Added for the Observatory UI (#1838) so its backfill form composes a valid
spec instead of asking an operator to hand-type a container digest.


Behind the DATA_LAKE_ENABLED feature flag; routes return 404 when the flag is off
(via main.py wiring, not this module).

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.3
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app.data_lake import catalog_client, root_identity
from app.data_lake.backfill import (
    BackfillDayProgress,
    BackfillResult,
    BackfillWaitProgress,
    EnsureFn,
    LeaseStatusFn,
    run_backfill,
)
from app.data_lake.catalog_client import MinuteBarLeaseStatus, select_minute_bar_lease_status
from app.data_lake.ensure_data import ensure_data, provider_for_data_type
from app.data_lake.types import (
    MAX_SYMBOL_LENGTH,
    MAX_TRADING_RANGE_DAYS,
    SYMBOL_RE,
    ArtifactDetail,
    ArtifactFailure,
    ArtifactIdentity,
    CoverageDay,
    CoverageResponse,
    DataAvailabilityResult,
    DataRunSpec,
    NonSessionRecord,
    PriceAdjustmentMode,
    StorageSummaryResponse,
    trading_date_at_ms,
    trading_range_span_days,
)
from app.jobs.progress import ProgressEmitter
from app.jobs.runner import run_in_thread
from app.lean_sidecar.config import PINNED_LEAN_IMAGE_DIGEST
from app.lean_sidecar.trading_calendar import session_open_ms_utc, session_windows_ms_utc

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/data-lake", tags=["data-lake"])


async def _ensure_catalog_pool() -> None:
    """FastAPI dependency: guarantee the asyncpg pool exists before a route
    touches the catalog.

    ensure_data() already calls catalog_client.init_pool() as its own first
    step, so POST /ensure-data has never needed this. The GET read routes
    never called it at all — in a fresh process (flag on, no prior POST),
    a GET hit connection()'s "asyncpg pool not initialized" RuntimeError as
    an unhandled 500. init_pool() is idempotent, so wiring it here is a
    no-op once ensure_data (or an earlier GET) has already initialized it.
    """
    await catalog_client.init_pool()


@router.post("/ensure-data", response_model=DataAvailabilityResult)
async def post_ensure_data(spec: DataRunSpec) -> DataAvailabilityResult:
    logger.info(
        "[STEP 1] /api/data-lake/ensure-data received: request_id=%s, symbols=%s",
        spec.request_id,
        spec.symbols,
    )
    return await ensure_data(spec)


@router.get("/coverage", response_model=CoverageResponse, dependencies=[Depends(_ensure_catalog_pool)])
async def get_coverage(
    symbol: str,
    start_trading_date_ms: int,
    end_trading_date_ms: int,
    market: Literal["usa"] = "usa",
    data_type: Literal["trade", "quote"] = "trade",
    price_adjustment_mode: PriceAdjustmentMode = "raw",
) -> CoverageResponse:
    """Per-day artifact status for a symbol, keyed by the canonical NYSE calendar.

    Days are the calendar's own sessions in the requested window — weekends
    and holidays are simply absent, never listed and never invented. A session
    with no matching catalog row is reported honestly as ``status="missing"``
    rather than omitted.

    The window arrives as two ``int64 ms UTC`` values, not ISO dates.
    ``.claude/rules/temporal-rigor.md`` allows exactly one wire format for a
    temporal value and a trading date is not an exception to it — it is a
    date-anchored value, carried as the millisecond instant of that session's
    open and resolved back through ``America/New_York``
    (:func:`trading_date_at_ms`). The response has always spoken ms
    (``CoverageDay.trading_date_ms``); before #1839 the request did not, which
    left one endpoint with a temporal format in each direction.

    ``provider`` is not a caller-supplied parameter: it's derived from
    ``data_type`` via ``provider_for_data_type`` (the same rule
    ``expand_required_artifacts`` uses to write these rows) — trade bars are
    Polygon's, quote bars are ``learn_ai_derived``. A fixed ``provider="polygon"``
    parameter made quote coverage unfindable, since quote rows are never
    catalogued under that provider.
    """
    if not SYMBOL_RE.match(symbol) or len(symbol) > MAX_SYMBOL_LENGTH:
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "invalid_symbol",
                "message": f"symbol must match {SYMBOL_RE.pattern} and be at most {MAX_SYMBOL_LENGTH} chars: {symbol!r}",
            },
        )
    try:
        start_trading_date = trading_date_at_ms(start_trading_date_ms)
        end_trading_date = trading_date_at_ms(end_trading_date_ms)
    except (OverflowError, OSError, ValueError) as exc:
        # A ms value outside the representable instant range. Typed, because
        # the alternative is a 500 on a caller sending seconds for ms -- the
        # single likeliest way to get this parameter wrong.
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "invalid_trading_date_ms",
                "message": (
                    f"trading-date window is not a representable ms-UTC instant "
                    f"({start_trading_date_ms}..{end_trading_date_ms}); values are "
                    f"milliseconds since the Unix epoch, anchored at the session open"
                ),
            },
        ) from exc
    if start_trading_date > end_trading_date:
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "invalid_range",
                "message": f"start_trading_date {start_trading_date} is after end_trading_date {end_trading_date}",
            },
        )
    # Same computation DataRunSpec's validator uses (types.py) — one shared
    # cap, one shared formula, so a write-window accepted by POST
    # /ensure-data can never be a read-window GET /coverage rejects.
    span_days = trading_range_span_days(start_trading_date, end_trading_date)
    if span_days > MAX_TRADING_RANGE_DAYS:
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "range_too_large",
                "message": f"range is {span_days} days; max is {MAX_TRADING_RANGE_DAYS}",
            },
        )

    logger.info(
        "[STEP 1] /api/data-lake/coverage requested: symbol=%s, start=%s, end=%s",
        symbol,
        start_trading_date,
        end_trading_date,
    )
    provider = provider_for_data_type(data_type)
    # Resolved once and passed explicitly to both the catalog query and the
    # response (issue #1876): coverage defaults to the service's configured
    # active root.
    data_root_id = root_identity.active_root_id()
    # One schedule build for the whole range, not one per day: session_date
    # and its 09:30 ET open both come off the same SessionWindow, so no
    # per-day session_open_ms_utc() call re-queries pandas_market_calendars.
    # A per-day query pattern here measured ~10s at the 5-year cap.
    windows = session_windows_ms_utc(start_trading_date, end_trading_date)
    rows_by_date = {
        row.trading_date: row
        for row in await catalog_client.select_artifact_coverage(
            market=market,
            symbol=symbol,
            data_type=data_type,
            provider=provider,
            price_adjustment_mode=price_adjustment_mode,
            start_trading_date=start_trading_date,
            end_trading_date=end_trading_date,
            data_root_id=data_root_id,
        )
    }
    days = []
    for window in windows:
        row = rows_by_date.get(window.session_date)
        days.append(
            CoverageDay(
                trading_date_ms=window.open_ms_utc,
                status=row.status if row is not None else "missing",
                artifact_id=row.artifact_id if row is not None else None,
            )
        )
    return CoverageResponse(
        market=market,
        symbol=symbol,
        data_type=data_type,
        resolution="minute",
        provider=provider,
        price_adjustment_mode=price_adjustment_mode,
        data_root_id=data_root_id,
        days=days,
    )


@router.get("/artifacts/{artifact_id}", response_model=ArtifactDetail, dependencies=[Depends(_ensure_catalog_pool)])
async def get_artifact_detail(artifact_id: int) -> ArtifactDetail:
    """Return the full receipt for one catalog row: hashes, size, provider params."""
    logger.info("[STEP 1] /api/data-lake/artifacts/%s requested", artifact_id)
    detail = await catalog_client.select_artifact_by_id(artifact_id)
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail={
                "reason": "artifact_not_found",
                "message": f"artifact {artifact_id} not found",
            },
        )
    return detail


@router.get("/storage-summary", response_model=StorageSummaryResponse, dependencies=[Depends(_ensure_catalog_pool)])
async def get_storage_summary(market: Literal["usa"] = "usa") -> StorageSummaryResponse:
    """Artifact counts/bytes by kind, plus each symbol's day-keyed coverage span.

    Defaults to the service's configured active root (issue #1876).
    """
    logger.info("[STEP 1] /api/data-lake/storage-summary requested: market=%s", market)
    data_root_id = root_identity.active_root_id()
    kinds, symbols = await asyncio.gather(
        catalog_client.select_storage_totals_by_kind(market, data_root_id=data_root_id),
        catalog_client.select_symbol_coverage_spans(market, data_root_id=data_root_id),
    )
    return StorageSummaryResponse(market=market, data_root_id=data_root_id, kinds=kinds, symbols=symbols)


class BackfillDefaults(BaseModel):
    """The parts of a ``DataRunSpec`` only the data plane knows (#1838).

    ``lean_image_digest`` is required by ``DataRunSpec`` and has no default;
    a browser has no way to derive the pinned LEAN image, and hand-typing a
    container digest into a form is not a receipt anybody could audit. The
    caps come from ``app.data_lake.types`` so the Observatory rejects an
    over-wide window in the form rather than by round-tripping a 422.

    ``lean_image_digest`` is ``None`` when the data plane has no pin
    configured — reported honestly rather than as an empty string, so the
    UI can say backfill is unavailable instead of submitting a spec that
    would fail deep inside Phase 0.
    """

    market: Literal["usa"] = "usa"
    lean_image_digest: str | None
    max_trading_range_days: int
    max_symbol_length: int


@router.get("/backfill-defaults", response_model=BackfillDefaults)
async def get_backfill_defaults(market: Literal["usa"] = "usa") -> BackfillDefaults:
    """Spec constants for a backfill form. Reads no catalog state."""
    return BackfillDefaults(
        market=market,
        lean_image_digest=PINNED_LEAN_IMAGE_DIGEST,
        max_trading_range_days=MAX_TRADING_RANGE_DAYS,
        max_symbol_length=MAX_SYMBOL_LENGTH,
    )


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

    ensure_data's asyncpg pool (app.data_lake.catalog_client) is keyed by
    the calling event loop — using it from a loop with no pool of its own
    raises "asyncpg pool not initialized" rather than silently reusing a
    foreign one. run_in_thread's worker thread has no loop of its own;
    work()'s asyncio.run(_do()) below spins up a brand-new, throwaway loop
    per job, which would pay for (and never close) a fresh asyncpg pool
    every single backfill job if left unbridged. Bridging every
    ensure_data() call through run_coroutine_threadsafe onto the loop
    captured here — before the worker thread starts — keeps every
    data-lake asyncpg operation on one consistent, already-pooled loop for
    the life of the process instead of churning through one pool per job.
    Only the pool-touching call is bridged; run_backfill's own
    orchestration and the emitter's synchronous Redis calls
    (progress/log/cancel-check) stay on the worker thread exactly as
    before, so this doesn't reintroduce blocking work onto the shared app
    loop.
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
