"""Run a callable on a worker thread with progress instrumentation.

The pattern across all long jobs (backtest, dataset, polygon fetch, zip
bundling) is identical: the FastAPI handler returns 202 immediately, a
daemon thread does the work, and emits SSE-bound events to Redis. This
module hosts that pattern so each job type only writes the inner work
function.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Any

from app.jobs.progress import (
    CancellationCheck,
    JobCancelled,
    ProgressEmitter,
    acquire_lease,
    release_lease,
)

logger = logging.getLogger(__name__)


JobWork = Callable[[ProgressEmitter, CancellationCheck], Any]
"""A unit of job work. Receives an emitter and a cancellation check, and
returns one of:

- a JSON-serializable value → the runner calls
  :meth:`ProgressEmitter.completed` with it as the result.
- ``None`` → the work has already emitted its own terminal event
  (e.g. via :meth:`ProgressEmitter.completed_blob` for binary outputs);
  the runner skips the auto-complete step.

This keeps the simple ``return result`` ergonomic for JSON-bearing jobs
while letting binary jobs handle their own completion without producing
duplicate ``job.completed`` events.
"""


@dataclass
class PreparedWorker:
    """An exclusively held job whose durable binding may commit before its thread starts."""

    job_id: str
    emitter: ProgressEmitter
    cancel: CancellationCheck
    lease_token: str
    thread_name: str
    thread: threading.Thread | None = field(default=None, init=False)

    @property
    def started(self) -> bool:
        return self.thread is not None and self.thread.ident is not None

    def start(self, work: JobWork) -> threading.Thread:
        if self.thread is not None:
            raise RuntimeError("This prepared worker already attempted to start.")
        self.thread = threading.Thread(target=self._run, args=(work,), name=self.thread_name, daemon=True)
        self.thread.start()
        return self.thread

    def _run(self, work: JobWork) -> None:
        try:
            self.emitter.started()
            result = work(self.emitter, self.cancel)
            if result is not None:
                self.emitter.completed(result)
        except JobCancelled as exc:
            logger.info("job %s cancelled: %s", self.job_id, exc)
            self.emitter.cancelled(str(exc))
        except Exception as exc:
            logger.exception("job %s failed", self.job_id)
            self.emitter.failed(code=type(exc).__name__, message=str(exc))
        finally:
            release_lease(self.job_id, self.lease_token)


@contextmanager
def prepared_worker(
    job_id: str, *, cancel_check_every_n: int = 1, thread_name: str | None = None,
    emit_renews_lease: bool = True, progress_min_interval_s: float = 0.0,
) -> Iterator[PreparedWorker]:
    """Reserve a job once; release its own lease if its binding or thread cannot start.

    The worker owns release after an actual thread start, even if the caller
    loses that acknowledgement. A closed or previously started job cannot be
    reserved again; the caller may join a live redelivery through its record.
    """
    emitter = ProgressEmitter(job_id, renews_lease=emit_renews_lease, progress_min_interval_s=progress_min_interval_s)
    cancel = CancellationCheck(job_id, check_every_n=cancel_check_every_n)
    launch = PreparedWorker(job_id, emitter, cancel, acquire_lease(job_id), thread_name or f"job-{job_id[:8]}")
    try:
        yield launch
    finally:
        if not launch.started:
            release_lease(job_id, launch.lease_token)


async def _drain_worker_io(task: asyncio.Task[Any]) -> asyncio.CancelledError | None:
    """Remember cancellation while a lease operation finishes on its own thread."""
    cancelled = None
    while not task.done():
        try:
            await asyncio.wait([task])
        except asyncio.CancelledError as exc:
            cancelled = cancelled or exc
    return cancelled


@asynccontextmanager
async def async_prepared_worker(
    job_id: str, *, cancel_check_every_n: int = 1, thread_name: str | None = None,
    emit_renews_lease: bool = True, progress_min_interval_s: float = 0.0,
) -> AsyncIterator[PreparedWorker]:
    """The same reservation for an async coordinator; Redis never blocks its writer loop."""
    reservation = prepared_worker(job_id, cancel_check_every_n=cancel_check_every_n, thread_name=thread_name,
                                  emit_renews_lease=emit_renews_lease, progress_min_interval_s=progress_min_interval_s)
    acquiring = asyncio.create_task(asyncio.to_thread(reservation.__enter__))
    cancelled = await _drain_worker_io(acquiring)
    launch = acquiring.result()
    try:
        if cancelled is not None:
            raise cancelled
        yield launch
    except asyncio.CancelledError as exc:
        cancelled = cancelled or exc
    finally:
        # Repeated cancellation cannot release the authority's stamp lock
        # before an eventual claim is acquired and its own lease is released.
        releasing = asyncio.create_task(asyncio.to_thread(reservation.__exit__, None, None, None))
        cleanup_cancelled = await _drain_worker_io(releasing)
        releasing.result()
        if (cancelled := cancelled or cleanup_cancelled) is not None:
            raise cancelled


def run_in_thread(
    job_id: str,
    work: JobWork,
    *,
    # Default 1 (#2463): a job that checks cancellation only a handful of
    # times must actually read the flag, so throttling is opt-in for hot
    # loops that check per bar (feature research passes 100, signal engine 50).
    cancel_check_every_n: int = 1,
    thread_name: str | None = None,
    emit_renews_lease: bool = True,
    # Ordinary-progress coalescing (#3052): the pooled research jobs opt in
    # at one second, so worker checkpoint traffic cannot flood the stream.
    # Zero keeps the legacy serial contract — every tick is one event.
    progress_min_interval_s: float = 0.0,
) -> threading.Thread:
    """Spawn a daemon thread that runs ``work(emitter, cancel_check)``.

    Wraps the work in the canonical lifecycle:
      * acquires the job's liveness lease on the request path (#1938) — a
        202 implies a held job; the lease is renewed by the worker's own
        emits and cancellation checks and deleted in the ``finally`` below.
        ``emit_renews_lease=False`` is the checkpoint contract's opt-out
        (#3041/#3043): the research runtime's emits renew nothing, so only
        accepted execution checkpoints keep the lease alive
      * emits ``job.started`` before the work runs
      * emits ``job.completed`` with the returned value on success
      * emits ``job.cancelled`` if :class:`JobCancelled` was raised
      * emits ``job.failed`` on any other exception (with the type+msg)

    The handler returns the thread so the caller can keep a reference if
    it cares about test determinism; production callers fire-and-forget.
    """

    with prepared_worker(job_id, cancel_check_every_n=cancel_check_every_n, thread_name=thread_name,
                         emit_renews_lease=emit_renews_lease, progress_min_interval_s=progress_min_interval_s) as launch:
        return launch.start(work)
