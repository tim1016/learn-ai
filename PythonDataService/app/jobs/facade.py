"""The public jobs facade: mint a job, record it in Redis, hand it to its worker route, and serve its stream.

``POST /api/jobs/{type}`` is how the browser starts every long job. This module
owns the type-agnostic half: it mints the job id, writes the initial
``job:{id}:state`` record (see :mod:`app.jobs.progress` for the shared Redis
schema), and dispatches the original body plus ``job_id`` to the type's
internal route. Python owns each type's schema and math; the facade never
reads a field of the body except to redact it from the persisted copy.

Dispatch is in-process. The internal route is called through
``httpx.ASGITransport`` against the running FastAPI app, so it passes the same
middleware and dependencies (trusted hosts, the data-plane control secret, the
research-runtime forwarding) a socket call would, without a loopback hop.

The Redis client in :mod:`app.jobs.progress` is synchronous; every command
here runs through ``asyncio.to_thread`` so a slow store never stalls the event
loop, and each keeps that client's ``JOB_STORE_TIMEOUT_SECONDS`` bound.
Event streams are therefore polled with non-blocking reads, as the worker side
already expects.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings
from app.jobs import progress
from app.security.data_plane_control import CONTROL_SECRET_HEADER
from app.utils.background_loop import run_on_background_loop

logger = logging.getLogger(__name__)

# Public job type -> the internal route that runs it. Add a row when a new job
# kind appears; state, events, cancel and result are type-agnostic. The
# data-lake backfill worker lives on the data-lake router rather than under
# /api/jobs-internal, and accepts the same caller-minted ``job_id``.
JOB_TYPE_ROUTES: Mapping[str, str] = {
    "dataset-zip": "/api/jobs-internal/dataset-zip",
    "engine_backtest": "/api/jobs-internal/engine-backtest",
    "lean_engine_run": "/api/jobs-internal/lean-engine-run",
    "cross_sectional": "/api/jobs-internal/cross-sectional",
    "feature_research": "/api/jobs-internal/feature-research",
    "signal_engine": "/api/jobs-internal/signal-engine",
    "recency_chart": "/api/jobs-internal/recency-chart",
    "grid_search": "/api/jobs-internal/grid-search",
    "walk_forward_study": "/api/jobs-internal/walk-forward-study",
    "golden_search": "/api/jobs-internal/golden-search",
    "options_backtest": "/api/jobs-internal/options-backtest",
    "options_golden_search": "/api/jobs-internal/options-golden-search",
    "options_golden_search_history": "/api/jobs-internal/options-golden-search-history",
    "data_lake_backfill": "/api/data-lake/backfill",
}

# The job types whose route writes the data plane and so demands the control secret.
CONTROL_SECRET_JOB_TYPES = frozenset({"data_lake_backfill", "options_golden_search_history"})

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

# The dispatch only has to get the worker started (the job store carries the run itself).
DISPATCH_TIMEOUT_SECONDS = 60.0
# A worker thread waits this long for the facade to mint and dispatch a job.
LAUNCH_TIMEOUT_SECONDS = DISPATCH_TIMEOUT_SECONDS + progress.JOB_STORE_TIMEOUT_SECONDS * 2
SSE_POLL_INTERVAL_SECONDS = 0.5
SSE_READ_BATCH = 64

REJECTED_CODE = "PythonRejected"
_REDACTED_PARAMETER = "algorithm_source"


class JobRecordNotFoundError(LookupError):
    """The job, its result or its blob is absent or expired; the message is the public 404 text."""


@dataclass(frozen=True)
class JobStartOutcome:
    """What a start attempt came to.

    ``status_code`` is 202 when the internal route accepted, the route's own
    status when it refused, and 502 when it could not be reached or raised.
    ``detail`` is the refusal body text or the transport error, None on success.
    """

    job_id: str
    accepted: bool
    status_code: int
    detail: str | None = None


@dataclass(frozen=True)
class JobBlob:
    filename: str
    content_type: str
    body: bytes


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------


def redact_sensitive_parameters(value: Any) -> Any:
    """A copy of ``value`` with every ``algorithm_source`` key removed, at any depth.

    The shared job-state projection must not retain source code that belongs
    only to the submitting browser, the worker's dispatch and the audit
    workspace. The dispatched body is untouched: the worker still receives the
    complete request.
    """
    if isinstance(value, dict):
        return {key: redact_sensitive_parameters(item) for key, item in value.items() if key != _REDACTED_PARAMETER}
    if isinstance(value, list):
        return [redact_sensitive_parameters(item) for item in value]
    return value


def control_secret_headers(job_type: str) -> dict[str, str]:
    """The control-secret header this type's dispatch carries, empty when it needs none.

    Empty too when the secret is unconfigured, so an operator running without
    one gets the same "unconfigured" refusal from the route that a direct
    caller would.
    """
    if job_type not in CONTROL_SECRET_JOB_TYPES:
        return {}
    secret = settings.DATA_PLANE_CONTROL_SECRET
    return {CONTROL_SECRET_HEADER: secret} if secret else {}


def _internal_base_url() -> str:
    """A base URL whose host the app's TrustedHostMiddleware admits (a wildcard entry cannot be dialed)."""
    concrete = [host for host in settings.get_trusted_hosts() if "*" not in host]
    return f"http://{concrete[0] if concrete else 'localhost'}"


def _record_queued(job_id: str, job_type: str, body: Mapping[str, Any]) -> None:
    key = progress.state_key(job_id)
    pipe = progress.get_redis().pipeline()
    pipe.hset(
        key,
        mapping={
            "id": job_id,
            "type": job_type,
            "status": "queued",
            "params": json.dumps(redact_sensitive_parameters(body), separators=(",", ":")),
            "created_at": str(int(time.time() * 1000)),
            "cancel_requested": "0",
        },
    )
    pipe.expire(key, progress.JOB_TTL_SECONDS)
    pipe.sadd(progress.active_set_key(), job_id)
    pipe.execute()


def _record_failed(job_id: str, code: str, message: str) -> None:
    store = progress.get_redis()
    store.hset(
        progress.state_key(job_id),
        mapping={"status": "failed", "error_code": code, "error_message": message},
    )
    store.srem(progress.active_set_key(), job_id)


async def _dispatch(app: Any, job_type: str, body: Mapping[str, Any]) -> httpx.Response:
    # ``content=`` rather than ``json=``: httpx cannot UTF-8 encode a lone
    # surrogate a client may have sent, but ASCII-escaped JSON carries it.
    payload = json.dumps(body).encode("ascii")
    headers = {"Content-Type": "application/json", **control_secret_headers(job_type)}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=_internal_base_url()) as client:
        async with asyncio.timeout(DISPATCH_TIMEOUT_SECONDS):
            return await client.post(JOB_TYPE_ROUTES[job_type], content=payload, headers=headers)


async def start_job(app: Any, job_type: str, body: Mapping[str, Any]) -> JobStartOutcome:
    """Mint a job id, record it queued, and dispatch ``body`` plus ``job_id`` to the type's internal route.

    ``job_type`` must be a key of :data:`JOB_TYPE_ROUTES`. A refused or failed
    dispatch marks the job failed in Redis (and drops it from the active set)
    before returning, so no record is left claiming ``queued``.
    """
    if job_type not in JOB_TYPE_ROUTES:
        raise ValueError(f"unsupported job type {job_type!r}")
    job_id = str(uuid.uuid4())
    await asyncio.to_thread(_record_queued, job_id, job_type, body)
    try:
        response = await _dispatch(app, job_type, {**body, "job_id": job_id})
    except Exception as exc:
        logger.exception("[Jobs] failed to dispatch job %s to its worker route", job_id)
        message = str(exc) or type(exc).__name__
        await asyncio.to_thread(_record_failed, job_id, type(exc).__name__, message)
        return JobStartOutcome(job_id=job_id, accepted=False, status_code=502, detail=message)
    if not response.is_success:
        logger.error("[Jobs] worker route rejected start of %s: %s %s", job_id, response.status_code, response.text)
        await asyncio.to_thread(_record_failed, job_id, REJECTED_CODE, response.text)
        return JobStartOutcome(job_id=job_id, accepted=False, status_code=response.status_code, detail=response.text)
    return JobStartOutcome(job_id=job_id, accepted=True, status_code=202)


def launch_job_from_worker_thread(job_type: str, body: Mapping[str, Any]) -> JobStartOutcome:
    """Start a job from a thread with no event loop of its own (a backtest worker), blocking for the outcome.

    The start runs on the shared background loop, the one worker threads
    already use for their own async work. The ASGI app is resolved here and
    not at import: ``app.main`` imports every router, this module's among them.
    """
    from app.main import app

    return run_on_background_loop(start_job(app, job_type, body), timeout=LAUNCH_TIMEOUT_SECONDS)


# ---------------------------------------------------------------------------
# Events (SSE)
# ---------------------------------------------------------------------------


def _read_range(job_id: str, start: str) -> list[tuple[str, dict[str, str]]]:
    return progress.get_redis().xrange(progress.events_key(job_id), min=start, max="+")


def _read_after(job_id: str, last_id: str) -> list[tuple[str, dict[str, str]]]:
    streams = progress.get_redis().xread({progress.events_key(job_id): last_id}, count=SSE_READ_BATCH)
    return streams[0][1] if streams else []


def _read_status(job_id: str) -> str | None:
    return progress.get_redis().hget(progress.state_key(job_id), "status")


def _sse_frame(entry_id: str, fields: Mapping[str, str]) -> str | None:
    event = fields.get("event")
    if event is None:
        return None
    return f"id: {entry_id}\ndata: {event}\n\n"


async def stream_job_events(job_id: str, last_event_id: str | None) -> AsyncIterator[str]:
    """The job's events as SSE frames, resuming after ``last_event_id`` (a Redis stream entry id).

    Replays the history, then tails the stream until the job reaches a
    terminal status and the stream is drained. The ends are the caller going
    away (the generator is cancelled) or that terminal drain. A job id the
    store does not know tails until the caller leaves, as the status is never
    terminal.
    """
    last_id = last_event_id or "0-0"
    # "0-0" means everything, which an exclusive range would skip the first entry of.
    start = "-" if last_id == "0-0" else f"({last_id}"
    for entry_id, fields in await asyncio.to_thread(_read_range, job_id, start):
        frame = _sse_frame(entry_id, fields)
        if frame is not None:
            yield frame
        last_id = entry_id

    while True:
        entries = await asyncio.to_thread(_read_after, job_id, last_id)
        if entries:
            for entry_id, fields in entries:
                frame = _sse_frame(entry_id, fields)
                if frame is not None:
                    yield frame
                last_id = entry_id
            continue
        if await asyncio.to_thread(_read_status, job_id) in TERMINAL_STATUSES:
            # One more drain: the final emit can land between the empty read and the status flip.
            for entry_id, fields in await asyncio.to_thread(_read_range, job_id, f"({last_id}"):
                frame = _sse_frame(entry_id, fields)
                if frame is not None:
                    yield frame
            return
        await asyncio.sleep(SSE_POLL_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Result, download, cancel, list
# ---------------------------------------------------------------------------


def _read_result(job_id: str) -> str | None:
    return progress.get_redis().get(progress.result_key(job_id))


async def read_result(job_id: str) -> str:
    """The raw JSON text a JSON-bearing job stored as its result."""
    raw = await asyncio.to_thread(_read_result, job_id)
    if raw is None:
        raise JobRecordNotFoundError("result not found or expired")
    return raw


def _read_blob(job_id: str) -> JobBlob:
    meta = progress.get_redis().hgetall(progress.result_meta_key(job_id))
    if not meta:
        raise JobRecordNotFoundError("blob result not found or expired")
    body = progress.read_result_blob(job_id)
    if body is None:
        raise JobRecordNotFoundError("blob payload missing (meta present)")
    return JobBlob(
        filename=meta.get("filename", f"job-{job_id}"),
        content_type=meta.get("content_type", "application/octet-stream"),
        body=body,
    )


async def read_download(job_id: str) -> JobBlob:
    """The binary a blob-bearing job stored (a ZIP, a PDF), with its recorded name and type."""
    return await asyncio.to_thread(_read_blob, job_id)


def _request_cancel(job_id: str) -> None:
    store = progress.get_redis()
    key = progress.state_key(job_id)
    if not store.exists(key):
        raise JobRecordNotFoundError("job not found")
    # Cooperative: the worker polls this flag and writes its own terminal state.
    store.hset(key, "cancel_requested", "1")


async def request_cancel(job_id: str) -> None:
    """Ask the job's worker to stop; it acknowledges by finishing its own record."""
    await asyncio.to_thread(_request_cancel, job_id)


def _read_active_states() -> list[dict[str, str]]:
    store = progress.get_redis()
    active_key = progress.active_set_key()
    states: list[dict[str, str]] = []
    for job_id in sorted(store.smembers(active_key)):
        state = store.hgetall(progress.state_key(job_id))
        if not state:
            # The state expired but the id is still in the active set: clean up.
            store.srem(active_key, job_id)
            continue
        states.append(state)
    return states


async def list_active_jobs() -> list[dict[str, str]]:
    """The state record of every queued or running job."""
    return await asyncio.to_thread(_read_active_states)
