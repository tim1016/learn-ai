"""Off-loop execution seam for the SQLite Clerk's synchronous repository spine.

The reconciliation sweep's pass is read/decode heavy, and every repository
call it made used to run on the event loop (#1993). Two hop shapes exist, and
they are not interchangeable:

- Folds that must serialize in the intake domain hop through
  ``ReentrantAsyncLock.off_loop`` — the fence's one sanctioned yield, which
  submits through :func:`off_loop_future` here.
- The claim-guarded resolution machines (exit, order submission, manual
  cancellation) never held intake — their exclusion is the operation claim
  plus the repository write lock — so their sync runs hop on a plain worker
  thread via :func:`to_thread`, the same seam
  ``reconcile._reconcile_effect``'s own reads already used before #1993.

Both shapes submit to one process-wide pool rather than ``asyncio.to_thread``
per call. The caller's context is copied into the worker so a fold still sees
the fence's dynamic scope (a guarded broker port would reject contact from
inside a fold), and — decisively for the test budget — the pool's threads are
created once per process instead of once per event loop: pytest runs a fresh
loop per test, and a default executor per loop churned threads through every
one of them.

Cancellation discipline for both shapes lives here too. A cancelled await
cannot stop a worker already inside a fold, so whoever owns the exclusion the
fold runs under — the intake fence, or an operation claim — must hold that
exclusion until the worker finishes (:func:`finish_abandoned_hop` is that
bounded wait). :func:`claim_scoped` wraps a runner so the claim release in a
resolver's ``finally`` can never interleave with its own abandoned worker's
writes, and :func:`run_drained` runs that release itself off the loop.

Known limit, accepted: the bounded wait releases the exclusion but cannot
kill the worker — Python threads are not cancellable, and the pool's threads
are joined at interpreter exit. A fold wedged past the bound therefore keeps
its pool thread (and can delay a clean process exit) until the process is
restarted; the fence already poisons itself at that point, and the restart
its error demands is what reclaims the thread. Making a wedged store call
abandonable is a repository-level concern (sqlite ``interrupt()``/busy
timeouts), not a seam-level one.

New module rather than a home in an existing one: the type is shared by
``exit_resolution``, ``order_evidence``, ``manual_order_cancellation``,
``exit_watchdog``, and ``reconcile``, whose only common dependencies are
dataclass/model modules where a callable seam would not belong.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

logger = logging.getLogger(__name__)

type OffLoop[T] = Callable[[Callable[[], T]], Awaitable[T]]

_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="sqlite-clerk-off-loop")

ABANDONED_WORKER_TIMEOUT_S = 30.0


async def run_inline[T](operation: Callable[[], T]) -> T:
    """Execute the spine unit on the caller's thread (pre-#1993 behavior)."""
    return operation()


def to_thread[T](operation: Callable[[], T]) -> Awaitable[T]:
    """Execute the spine unit on the shared worker pool without holding intake."""
    return asyncio.get_running_loop().run_in_executor(
        _EXECUTOR, contextvars.copy_context().run, operation
    )


def off_loop_future[T](
    operation: Callable[..., T],
    *args: object,
    **kwargs: object,
) -> asyncio.Future[Any]:
    """Fence-facing submit: one unit on the shared pool as a loop future.

    The future keeps running to its own conclusion if its awaiter is
    cancelled — exactly the property ``ReentrantAsyncLock.off_loop``'s
    abandoned-hop handling depends on.
    """
    loop = asyncio.get_running_loop()
    context = contextvars.copy_context()

    def run_unit() -> T:
        return context.run(operation, *args, **kwargs)

    return loop.run_in_executor(_EXECUTOR, run_unit)


async def finish_abandoned_hop(
    hop: asyncio.Future[Any],
    *,
    timeout_s: float,
    log_outcome: Callable[[BaseException | None], None],
) -> str:
    """Let a worker whose caller was cancelled reach its outcome, bounded.

    Returns ``"completed"`` once the worker finished (its exception, if any,
    is handed to ``log_outcome``), ``"timeout"`` when the bound elapsed with
    the worker still running, and ``"recancelled"`` when a second
    cancellation interrupted the wait. For the latter two the worker keeps
    running on the loop and its eventual outcome is still routed to
    ``log_outcome`` via a done-callback, so a failure is logged rather than
    dropped as an unretrieved task exception. The caller decides what an
    unfinished worker means for its exclusion — the fence poisons itself
    (:class:`ReentrantAsyncLock`), a claim scope can only log.
    """
    try:
        await asyncio.wait_for(asyncio.shield(hop), timeout=timeout_s)
    except asyncio.CancelledError:
        hop.add_done_callback(_outcome_retriever(log_outcome))
        return "recancelled"
    except TimeoutError:
        hop.add_done_callback(_outcome_retriever(log_outcome))
        return "timeout"
    except Exception as error:
        log_outcome(error)
        return "completed"
    log_outcome(None)
    return "completed"


def _outcome_retriever(
    log_outcome: Callable[[BaseException | None], None],
) -> Callable[[asyncio.Future[Any]], None]:
    def retrieve(hop: asyncio.Future[Any]) -> None:
        if hop.cancelled():
            log_outcome(None)
            return
        log_outcome(hop.exception())

    return retrieve


def claim_scoped(
    run: OffLoop,
    *,
    abandoned_timeout_s: float = ABANDONED_WORKER_TIMEOUT_S,
) -> OffLoop:
    """Wrap a runner so a cancelled await waits out its in-flight worker.

    The claim-scoped twin of the intake fence's abandoned-hop rule (#1993):
    a resolver's ``finally: release_operation_claim`` runs in the cancelled
    task's unwind, and releasing while the abandoned worker may still be
    folding lets the next claimant interleave with writes it cannot see.
    The wait is bounded by ``abandoned_timeout_s``; a worker that outlives
    it is logged CRITICAL and the claim is released anyway — unlike the
    fence, a claim scope has no way to refuse the next claimant, so the
    record is the best fail-loud available.
    """

    async def guarded[T](operation: Callable[[], T]) -> T:
        hop = asyncio.ensure_future(run(operation))
        try:
            return await asyncio.shield(hop)
        except asyncio.CancelledError:
            status = await finish_abandoned_hop(
                hop,
                timeout_s=abandoned_timeout_s,
                log_outcome=_log_claim_scoped_outcome,
            )
            if status != "completed":
                logger.critical(
                    "SQLite Clerk claim-scoped fold still running after its caller "
                    "was cancelled; releasing the claim with the worker unfinished",
                    extra={
                        "off_loop_event": "claim_scoped_abandoned_timeout",
                        "abandoned_timeout_s": abandoned_timeout_s,
                    },
                )
            raise

    return guarded


async def run_drained[T](run: OffLoop, operation: Callable[[], T]) -> T:
    """Run one unit through ``run``, drained if the caller is cancelled.

    For the release tail of a claim scope: the release must leave the
    event-loop thread (it takes the repository write lock), and a
    cancellation landing on its await must not abandon it mid-release.
    """
    return await claim_scoped(run)(operation)


def _log_claim_scoped_outcome(error: BaseException | None) -> None:
    if error is None:
        return
    logger.error(
        "SQLite Clerk claim-scoped fold errored after its caller was cancelled",
        extra={"off_loop_event": "claim_scoped_abandoned_error"},
        exc_info=error,
    )


__all__ = [
    "ABANDONED_WORKER_TIMEOUT_S",
    "OffLoop",
    "claim_scoped",
    "finish_abandoned_hop",
    "off_loop_future",
    "run_drained",
    "run_inline",
    "to_thread",
]

