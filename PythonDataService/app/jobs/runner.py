"""Run a callable on a worker thread with progress instrumentation.

The pattern across all long jobs (backtest, dataset, polygon fetch, zip
bundling) is identical: the FastAPI handler returns 202 immediately, a
daemon thread does the work, and emits SSE-bound events to Redis. This
module hosts that pattern so each job type only writes the inner work
function.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from app.jobs.progress import CancellationCheck, JobCancelled, ProgressEmitter

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


def run_in_thread(
    job_id: str,
    work: JobWork,
    *,
    cancel_check_every_n: int = 1000,
    thread_name: str | None = None,
) -> threading.Thread:
    """Spawn a daemon thread that runs ``work(emitter, cancel_check)``.

    Wraps the work in the canonical lifecycle:
      * emits ``job.started`` before the work runs
      * emits ``job.completed`` with the returned value on success
      * emits ``job.cancelled`` if :class:`JobCancelled` was raised
      * emits ``job.failed`` on any other exception (with the type+msg)

    The handler returns the thread so the caller can keep a reference if
    it cares about test determinism; production callers fire-and-forget.
    """

    emitter = ProgressEmitter(job_id)
    cancel = CancellationCheck(job_id, check_every_n=cancel_check_every_n)

    def _runner() -> None:
        try:
            emitter.started()
            result = work(emitter, cancel)
            if result is not None:
                emitter.completed(result)
            # else: work has already emitted its own terminal event
            # (typically completed_blob for binary outputs).
        except JobCancelled as exc:
            logger.info("job %s cancelled: %s", job_id, exc)
            emitter.cancelled(str(exc))
        except Exception as exc:
            # Terminal sink — every uncaught error becomes job.failed so
            # the SSE consumer always sees a final event. Re-raising
            # would leave the stream open until TTL.
            logger.exception("job %s failed", job_id)
            emitter.failed(code=type(exc).__name__, message=str(exc))

    name = thread_name or f"job-{job_id[:8]}"
    thread = threading.Thread(target=_runner, name=name, daemon=True)
    thread.start()
    return thread
