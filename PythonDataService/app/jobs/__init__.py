"""Job orchestration: progress emission, cancellation, Redis-backed state.

The contract:

  * Every long-running operation that wants to surface progress accepts a
    :class:`ProgressEmitter` and (optionally) a :class:`CancellationCheck`.
  * Events are appended to a Redis Stream at ``job:{id}:events`` whose
    entry IDs are reused as SSE ``id:`` field, so reconnecting clients
    can resume cleanly via ``Last-Event-ID``.
  * Job state (status, params, cancel flag) lives in a Redis hash at
    ``job:{id}:state`` with a 24h TTL; the result of a successful job is
    stored at ``job:{id}:result`` (also 24h TTL).

Terminal events (completed, failed, cancelled) close the stream from the
producer side; consumers detect terminal status via the ``status`` field
on the state hash.

Every job runs on a thread of the service process. At startup, before the
listener opens, the service fails whatever the active set still calls
queued or running (:func:`app.jobs.progress.fail_jobs_without_a_worker`):
those jobs lost their worker with the previous process, and nothing else
would ever close their records.
"""

from app.jobs.progress import (
    CancellationCheck,
    JobCancelled,
    ProgressEmitter,
    create_job,
    get_redis,
)

__all__ = [
    "CancellationCheck",
    "JobCancelled",
    "ProgressEmitter",
    "create_job",
    "get_redis",
]
