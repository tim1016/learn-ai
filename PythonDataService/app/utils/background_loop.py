"""One long-lived event loop for blocking callers that need an asyncpg pool.

Backtests and sweeps execute on worker threads with no loop of their own.
An asyncpg pool belongs to the loop that created it, and
``app.data_lake.catalog_client`` keys its pools by loop, so a fresh
``asyncio.run()`` per call would work but would pay for a brand-new pool of
real Postgres connections on every call and never close it. A process-wide
lock would serialize unrelated work. So: one loop for the whole process,
owned here, on which the worker-side pool is created once and concurrent
callers interleave. FastAPI's own loop owns a separate pool for request
handling; the two never share a connection.

Extracted from ``app.data_lake.run_materialization`` (which established the
pattern and its rationale) so Grid Search and Walk-Forward cell persistence
reuse the same loop instead of each starting another (PRD #1926 F15).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from typing import Any


class CallerStoppedWaitingError(RuntimeError):
    """``timeout`` elapsed while the coroutine was still running on the shared loop.

    Distinct from every ``TimeoutError`` a coroutine raises from inside
    itself — an asyncpg ``command_timeout``, say — which means the work
    *stopped*. Since Python 3.11 ``asyncio.TimeoutError`` is a builtin
    ``TimeoutError``, so those are indistinguishable from the wait's own
    timeout at the call site; this type is what tells the caller "the work is
    still going, you just stopped waiting for it" (#1977).
    """


_loop_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
THREAD_NAME = "background-loop"


def background_loop() -> asyncio.AbstractEventLoop:
    """Return the process-wide loop, starting its daemon thread on first use."""
    global _loop
    with _loop_lock:
        if _loop is None or _loop.is_closed():
            loop = asyncio.new_event_loop()
            thread = threading.Thread(target=loop.run_forever, name=THREAD_NAME, daemon=True)
            thread.start()
            _loop = loop
        return _loop


def run_on_background_loop[T](coroutine: Coroutine[Any, Any, T], *, timeout: float | None) -> T:
    """Run ``coroutine`` on the shared loop and block the calling thread for its result.

    Must be called from a thread that is not itself running an event loop —
    a caller with a loop should ``await`` instead. On timeout the coroutine is
    not cancelled: it keeps running on the shared loop to its own conclusion,
    which is the accepted trade-off for idempotent work whose result nobody is
    left waiting for. That case raises :class:`CallerStoppedWaitingError`;
    anything the coroutine itself raised propagates unchanged.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("run_on_background_loop was called from a running event loop; await the coroutine instead")
    future = asyncio.run_coroutine_threadsafe(coroutine, background_loop())
    try:
        return future.result(timeout=timeout)
    except TimeoutError:
        if future.done():
            # Either the coroutine raised a TimeoutError of its own, or it
            # finished between the wait expiring and this check. Both are real
            # outcomes; hand back the one that happened.
            return future.result()
        raise CallerStoppedWaitingError(
            f"still running on the {THREAD_NAME} after {timeout}s; the caller stopped waiting"
        ) from None
