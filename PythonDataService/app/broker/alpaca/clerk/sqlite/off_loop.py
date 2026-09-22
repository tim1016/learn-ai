"""Off-loop execution seam for the SQLite Clerk's synchronous repository spine.

The reconciliation sweep's pass is read/decode heavy, and every repository
call it made used to run on the event loop (#1993). Two hop shapes exist, and
they are not interchangeable:

- Folds that must serialize in the intake domain hop through
  ``ReentrantAsyncLock.off_loop`` — the fence's one sanctioned yield.
- The claim-guarded resolution machines (exit, order submission, manual
  cancellation) never held intake — their exclusion is the operation claim
  plus the repository write lock — so their sync runs hop on a plain worker
  thread, the same seam ``reconcile._reconcile_effect``'s own reads already
  used before #1993.

New module rather than a home in an existing one: the type is shared by
``exit_resolution``, ``order_evidence``, ``manual_order_cancellation``,
``exit_watchdog``, and ``reconcile``, whose only common dependencies are
dataclass/model modules where a callable seam would not belong.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

type OffLoop[T] = Callable[[Callable[[], T]], Awaitable[T]]


async def run_inline[T](operation: Callable[[], T]) -> T:
    """Execute the spine unit on the caller's thread (pre-#1993 behavior)."""
    return operation()


def to_thread[T](operation: Callable[[], T]) -> Awaitable[T]:
    """Execute the spine unit on a worker thread without holding intake."""
    return asyncio.to_thread(operation)


__all__ = ["OffLoop", "run_inline", "to_thread"]
