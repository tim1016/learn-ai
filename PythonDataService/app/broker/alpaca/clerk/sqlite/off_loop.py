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

New module rather than a home in an existing one: the type is shared by
``exit_resolution``, ``order_evidence``, ``manual_order_cancellation``,
``exit_watchdog``, and ``reconcile``, whose only common dependencies are
dataclass/model modules where a callable seam would not belong.
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

type OffLoop[T] = Callable[[Callable[[], T]], Awaitable[T]]

_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="sqlite-clerk-off-loop")


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


__all__ = ["OffLoop", "off_loop_future", "run_inline", "to_thread"]
