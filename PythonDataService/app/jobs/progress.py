"""Redis-backed progress emission and cancellation for long-running jobs.

See ``app.jobs.__init__`` for the contract this module implements.

Design notes
------------
- We use a sync Redis client because the workloads we instrument
  (backtests, dataset generation, zip bundling) are CPU-bound and run on
  a worker thread spawned via ``asyncio.to_thread`` from the request
  handler. Mixing async Redis with a sync inner loop would force every
  ``emit`` to schedule onto the event loop — needless overhead.
- Stream entry IDs are Redis-generated (``XADD ... *``); we surface them
  back so the SSE layer can put them in the ``id:`` field. Reconnecting
  with ``Last-Event-ID`` becomes ``XRANGE job:{id}:events ({last_id} +``.
- Cancellation is cooperative: producers call ``check.should_cancel()``
  every N iterations. The DELETE endpoint sets ``cancel_requested=1`` on
  the state hash; the next check raises :class:`JobCancelled`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import redis

logger = logging.getLogger(__name__)


# 24 hours — long enough to retrieve a result after lunch, short enough
# that abandoned jobs don't pile up. Single-user research tool;
# revisit if multi-user lands.
JOB_TTL_SECONDS = 60 * 60 * 24

# How long a worker's lease outlives its last proof of progress. Liveness
# (#1938) is the lease, not the ``queued``/``running`` status: the worker
# slides it forward every time it emits an event or consults cancellation,
# so a worker that dies or hangs silently stops renewing and the job reads
# as not live within this window, while today's status fields would keep
# claiming ``running`` until the 24 h TTL. It must comfortably exceed the
# longest gap between two renewals of the quietest job — one backtest cell
# with no emit in between — so a legitimately slow cell never reads as dead.
# Renewal rides existing calls (no heartbeat thread): a heartbeat would keep
# proving liveness for a worker that has actually hung.
JOB_LEASE_TTL_SECONDS = 300

# Bound the events stream so a runaway emitter can't blow out memory.
# 50k events at ~200 bytes each = ~10 MB; plenty for a long backtest.
MAX_STREAM_LENGTH = 50_000

# How long any caller waits to connect to the job store, and then for any one
# command to answer, before giving up. Every Python call here is a short
# command (HSET, XADD, HGET, SMEMBERS, SET); the only long stream reads are
# the .NET jobs facade's, on its own client.
JOB_STORE_TIMEOUT_SECONDS = 5.0


def _redis_url() -> str:
    return os.environ.get("REDIS_URL", "redis://localhost:6379/0")


_pool: redis.ConnectionPool | None = None
_pool_lock = threading.Lock()


def get_redis() -> redis.Redis:
    """Return a process-wide Redis client. Pool is lazily initialized.

    Returning a pooled client (vs a connection-per-call) matters because
    producer threads XADD concurrently with the request handlers' reads;
    each wants its own connection without paying handshake cost on every
    call. Connecting and every command are bounded by
    ``JOB_STORE_TIMEOUT_SECONDS``: a Redis that drops packets, or accepts
    the connection and then stops answering, cannot hold a caller — least
    of all the startup sweep, which runs before the listener opens.
    """
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = redis.ConnectionPool.from_url(
                    _redis_url(),
                    decode_responses=True,
                    max_connections=32,
                    socket_connect_timeout=JOB_STORE_TIMEOUT_SECONDS,
                    socket_timeout=JOB_STORE_TIMEOUT_SECONDS,
                )
    return redis.Redis(connection_pool=_pool)


def _state_key(job_id: str) -> str:
    return f"job:{job_id}:state"


def _lease_key(job_id: str) -> str:
    return f"job:{job_id}:lease"


def _events_key(job_id: str) -> str:
    return f"job:{job_id}:events"


def _result_key(job_id: str) -> str:
    return f"job:{job_id}:result"


def _result_blob_key(job_id: str) -> str:
    return f"job:{job_id}:result-blob"


def _result_meta_key(job_id: str) -> str:
    return f"job:{job_id}:result-meta"


def _active_set_key() -> str:
    return "jobs:active"


def _bytes_redis() -> redis.Redis:
    """A second client that does NOT decode responses — for binary blobs.

    The default pool decodes everything as utf-8, which is correct for
    the JSON-shaped event/state schema but corrupts ZIP/PDF/Parquet
    bytes. We open a parallel single-shot connection in raw bytes mode
    when a job stores a binary result.
    """
    return redis.Redis.from_url(_redis_url(), decode_responses=False)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class JobCancelled(Exception):
    """Raised by :class:`CancellationCheck` when cancel was requested."""


def create_job(job_type: str, params: Mapping[str, Any]) -> str:
    """Create a new job record in Redis and return its id.

    Called by the HTTP handler that accepts the run request. The id is
    a UUIDv4 (we don't actually need v7 ordering — the stream entry IDs
    give us time-ordering for free).
    """
    job_id = str(uuid4())
    r = get_redis()
    state = {
        "id": job_id,
        "type": job_type,
        "status": "queued",
        "params": json.dumps(dict(params), default=str),
        "created_at": str(int(time.time() * 1000)),
        "cancel_requested": "0",
    }
    pipe = r.pipeline()
    pipe.hset(_state_key(job_id), mapping=state)
    pipe.expire(_state_key(job_id), JOB_TTL_SECONDS)
    pipe.sadd(_active_set_key(), job_id)
    pipe.execute()
    return job_id


def acquire_lease(job_id: str) -> None:
    """Mark a dispatched worker as holding the job: create ``job:{id}:lease``.

    Called on the request path by :func:`app.jobs.runner.run_in_thread` so a
    202 implies a held job. Liveness (#1938) is this key's existence, never
    the status fields: those keep saying ``running`` until a terminal event
    or the 24 h TTL, but the lease expires ``JOB_LEASE_TTL_SECONDS`` after
    the worker last proved progress.
    """
    get_redis().set(_lease_key(job_id), "1", ex=JOB_LEASE_TTL_SECONDS)


def renew_lease(job_id: str) -> None:
    """Slide the lease forward. A no-op once it has expired: a worker that
    lost its lease stays not-live even if it later resumes producing — its
    record honestly read as interrupted, and only a terminal event may close
    it again. Redis errors are logged, never raised into the worker."""
    try:
        get_redis().expire(_lease_key(job_id), JOB_LEASE_TTL_SECONDS)
    except redis.RedisError as exc:
        logger.warning("lease renewal failed for job %s: %s", job_id, exc)


def release_lease(job_id: str) -> None:
    """Drop the lease when the worker leaves (terminal event or thread exit).
    Best-effort: the status is terminal by then, so liveness is already
    false; this only spares Redis an expired-key wait."""
    try:
        get_redis().delete(_lease_key(job_id))
    except redis.RedisError as exc:
        logger.warning("lease release failed for job %s: %s", job_id, exc)


@dataclass
class CancellationCheck:
    """Cooperative cancellation polling.

    The inner loop calls :meth:`should_cancel` (or
    :meth:`raise_if_cancelled`) on every iteration. Redis is consulted at
    most once per ``check_every_n`` calls; intermediate calls return the
    cached result. The **first** call always consults Redis (#2463): a job
    that checks cancellation only a handful of times — at phase boundaries —
    would otherwise never read the flag at all, so its Cancel control was
    inert. Hot loops that genuinely check per bar opt into throttling with
    an explicit ``check_every_n``; the default (1) reads on every call.
    """

    job_id: str
    check_every_n: int = 1

    def __post_init__(self) -> None:
        self._calls = 0
        self._cached = False

    def should_cancel(self) -> bool:
        self._calls += 1
        if self._calls > 1 and self._calls % self.check_every_n != 0 and self._cached is False:
            return False
        try:
            r = get_redis()
            pipe = r.pipeline()
            pipe.hget(_state_key(self.job_id), "cancel_requested")
            # Same gated round trip doubles as the liveness heartbeat (#1938):
            # a loop that consults cancellation is making progress.
            pipe.expire(_lease_key(self.job_id), JOB_LEASE_TTL_SECONDS)
            val, _ = pipe.execute()
            self._cached = val == "1"
        except redis.RedisError as exc:
            # Don't mask the real work because Redis hiccupped — log and
            # treat as not-cancelled.
            logger.warning("cancel check failed for job %s: %s", self.job_id, exc)
        return self._cached

    def raise_if_cancelled(self) -> None:
        if self.should_cancel():
            raise JobCancelled(f"job {self.job_id} cancelled")


class ProgressEmitter:
    """Append events to ``job:{id}:events`` and update the state hash.

    Methods are intentionally narrow and named for the SSE event
    vocabulary the frontend renders. Each emit returns the Redis stream
    entry ID so callers can log it for debugging.
    """

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self._r = get_redis()

    # ----- low-level -----

    def _emit(self, event_type: str, payload: dict[str, Any]) -> str:
        body = json.dumps({"type": event_type, **payload}, default=str)
        entry_id = self._r.xadd(
            _events_key(self.job_id),
            {"event": body},
            maxlen=MAX_STREAM_LENGTH,
            approximate=True,
        )
        # Stream itself doesn't carry a TTL when first created — XADD
        # against a fresh key just creates it. Set the TTL on every emit
        # cheaply so it slides forward while the job is active. The emit is
        # also a liveness proof: renew the worker's lease on the same
        # round trips (#1938).
        pipe = self._r.pipeline()
        pipe.expire(_events_key(self.job_id), JOB_TTL_SECONDS)
        pipe.expire(_lease_key(self.job_id), JOB_LEASE_TTL_SECONDS)
        pipe.execute()
        return entry_id

    def emit_event(self, event_type: str, payload: dict[str, Any]) -> str:
        """Public escape hatch for domain-specific event types.

        Use sparingly. The standard verbs below cover most jobs; reach for
        ``emit_event`` only when the consumer needs richer payloads than
        ``progress(current, total)`` can carry — for example, the dataset
        bundler emits per-chunk and per-component status that the data-lab
        run-card renders as a chunk-list and component-checklist UI.
        """
        return self._emit(event_type, payload)

    def _patch_state(self, **fields: str) -> None:
        if not fields:
            return
        # Refresh the hash's 24 h TTL alongside the write (pipeline, one
        # round trip): EXPIRE is otherwise set only at create, so a late
        # patch after expiry would recreate a partial record with no TTL
        # at all (#1938).
        pipe = self._r.pipeline()
        pipe.hset(_state_key(self.job_id), mapping=fields)
        pipe.expire(_state_key(self.job_id), JOB_TTL_SECONDS)
        pipe.execute()

    # ----- public verbs -----

    def started(self) -> None:
        self._patch_state(status="running", started_at=str(int(time.time() * 1000)))
        self._emit("job.started", {})

    def phase(self, name: str) -> None:
        self._patch_state(phase=name)
        self._emit("job.phase", {"phase": name})

    def progress(self, current: int, total: int, *, unit: str = "bars", message: str | None = None) -> None:
        payload: dict[str, Any] = {"current": current, "total": total, "unit": unit}
        if message is not None:
            payload["message"] = message
        self._emit("job.progress", payload)

    def log(self, message: str, *, level: str = "info") -> None:
        self._emit("job.log", {"level": level, "message": message})

    def completed(self, result: Any) -> None:
        self._r.set(_result_key(self.job_id), json.dumps(result, default=str), ex=JOB_TTL_SECONDS)
        self._patch_state(
            status="completed",
            completed_at=str(int(time.time() * 1000)),
            result_kind="json",
        )
        # Public URL is served by the .NET layer at /api/jobs/{id}/result;
        # the frontend never hits Python directly.
        self._emit("job.completed", {"result_url": f"/api/jobs/{self.job_id}/result"})
        # Drop from active set — the events stream remains for late
        # subscribers, but the active list shouldn't carry this anymore.
        self._r.srem(_active_set_key(), self.job_id)

    def completed_blob(self, *, filename: str, content_type: str, body: bytes) -> None:
        """Store a binary result and emit ``job.completed``.

        The binary lives at ``job:{id}:result-blob`` (raw bytes — no JSON
        decoding) and the metadata at ``job:{id}:result-meta`` (filename,
        content_type, size_bytes). The .NET layer's
        ``GET /api/jobs/{id}/download`` reads both and streams the blob
        with a proper ``Content-Disposition`` header. This split keeps
        the JSON ``/result`` endpoint clean for value-bearing jobs and
        gives binary jobs a separate ``/download`` URL.
        """
        bytes_client = _bytes_redis()
        try:
            bytes_client.set(_result_blob_key(self.job_id), body, ex=JOB_TTL_SECONDS)
        finally:
            bytes_client.close()
        self._r.hset(
            _result_meta_key(self.job_id),
            mapping={
                "filename": filename,
                "content_type": content_type,
                "size_bytes": str(len(body)),
            },
        )
        self._r.expire(_result_meta_key(self.job_id), JOB_TTL_SECONDS)
        self._patch_state(
            status="completed",
            completed_at=str(int(time.time() * 1000)),
            result_kind="blob",
        )
        self._emit(
            "job.completed",
            {
                "download_url": f"/api/jobs/{self.job_id}/download",
                "filename": filename,
                "size_bytes": len(body),
            },
        )
        self._r.srem(_active_set_key(), self.job_id)

    def failed(self, code: str, message: str) -> None:
        self._patch_state(
            status="failed",
            error_code=code,
            error_message=message,
            completed_at=str(int(time.time() * 1000)),
        )
        self._emit("job.failed", {"code": code, "message": message})
        self._r.srem(_active_set_key(), self.job_id)

    def cancelled(self, reason: str = "user requested") -> None:
        self._patch_state(
            status="cancelled",
            completed_at=str(int(time.time() * 1000)),
        )
        self._emit("job.cancelled", {"reason": reason})
        self._r.srem(_active_set_key(), self.job_id)


ORPHANED_JOB_CODE = "DATA_SERVICE_RESTARTED"
ORPHANED_JOB_MESSAGE = "the data service restarted while this job was queued or running; its worker did not survive the restart"


def fail_jobs_without_a_worker() -> list[str]:
    """At startup, fail every job the active set still calls queued or running.

    Every job runs on a thread of this process (``app.jobs.runner.run_in_thread``),
    so before the listener opens, a job that is still ``queued`` or ``running``
    belonged to the previous process and has no worker left — a crash, an
    out-of-memory kill or a restart took it. Nothing else would ever close it:
    its record would read as live until the 24 h TTL, and the research record
    behind it (a grid search, a walk-forward study) would present as running
    and refuse Finish. An id whose state has expired, or that reached a terminal
    status without leaving the set, is only dropped from the set. Redis being
    unreachable, or silent after connecting, is logged and leaves everything
    alone; the service still boots, and every wait is bounded by the pool's
    connect and command timeouts.
    Returns the ids failed.
    """
    r = get_redis()
    failed: list[str] = []
    try:
        for job_id in sorted(r.smembers(_active_set_key())):
            if r.hget(_state_key(job_id), "status") in ("queued", "running"):
                ProgressEmitter(job_id).failed(code=ORPHANED_JOB_CODE, message=ORPHANED_JOB_MESSAGE)
                failed.append(job_id)
            else:
                r.srem(_active_set_key(), job_id)
            # The worker is gone either way: its lease must not outlive the
            # sweep, or the closed record would still look live until the
            # lease TTL (#1938).
            r.delete(_lease_key(job_id))
    except redis.RedisError as exc:
        logger.warning("could not close the jobs left by the previous process: %s", exc, exc_info=True)
    if failed:
        logger.warning("failed %d job(s) whose worker died with the previous process: %s", len(failed), ", ".join(failed))
    return failed
