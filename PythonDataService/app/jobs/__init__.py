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
