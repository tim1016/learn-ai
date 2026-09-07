"""One engine backtest in flight per process (#1957).

PR #1944 measured one full-history minute-resolution cell at ~480 MB of
resident memory above the idle service, against the data-service container's
2 GiB cgroup, and serialized cells *within* a sweep on that basis
(``app/research/sweep/execution.py``). Nothing serialized the paths that reach
the engine from outside a sweep: two Strategy Lab tabs, or a Lab run started
while a sweep is executing a cell, ran concurrently with no cross-job
accounting, and three at once would repeat the failure #1944 fixed — the
kernel kills uvicorn, the worker threads go with it, and the jobs behind them
read ``running`` until their record expires.

**What this gate covers, exactly.** It is held by
``routers.engine.execute_engine_backtest``, and so by its five callers: the
sync ``POST /api/engine/backtest`` endpoint, the Strategy Lab job worker, Grid
Search and Walk-Forward cells, and the Recency runner. It is *not* the only
way to reach ``BacktestEngine``. Three production paths still construct and run
the engine without passing through here:

* ``routers/spec_strategy.py`` (``POST /api/spec-strategy/backtest``)
* ``research/runs/runner.py`` (``POST /api/research-runs``)
* ``lean_sidecar/cross_runner.py`` (``POST /api/lean-sidecar/cross-reconcile``)

so a run on one of those, concurrent with a gated run, still reaches #1957's
footprint. Closing that gap means moving the gate down into
``BacktestEngine.run`` — the seam every engine run really does pass through —
which first requires those three callers to stop running the engine on the
event loop (the last two do today, which is a separate pre-existing bug). That
is tracked in #1990; do not read this module as proof the invariant is closed.

**It queues; it does not refuse.** A sweep must run all of its cells, so a
refusal would turn a memory guard into failed work. The half-gigabyte belongs
to the run in flight, not to the ones waiting. The queue is unbounded, which is
the right trade for a single-operator research tool: the cost of a waiter is
its thread, and a bound would have to reject work that a human is watching.

A waiter is never silent and never uninterruptible:

* ``on_wait`` fires once, before blocking, so a caller with a progress channel
  can say so (the Strategy Lab worker reports the ``waiting_for_engine``
  phase). Callers without one still leave a log line, emitted before the wait
  rather than after it, so a stalled service has evidence while it is stalled.
* ``while_waiting`` fires about once a second for as long as the wait lasts,
  which is where a caller puts its cancellation check. Without it a queued job
  would sit on an uncancellable ``acquire()`` and the operator's Cancel button
  would do nothing until the run ahead finished.

Two costs worth naming. The wait is not FIFO: CPython wakes a waiter, but a
thread calling ``acquire(blocking=False)`` in the window before that waiter
retakes the condition lock takes the slot first, so a sweep releasing and
re-acquiring per cell can in principle barge past a queued Lab run. It does
real work between cells, so the waiter wins in practice. And a queued sync
``/backtest`` request holds one of anyio's 40 threadpool tokens for the whole
wait; enough of them would starve the service's other sync routes. The
interactive path is the jobs worker, which has its own thread, so exposure is
low — but it is a real edge of "queue rather than refuse".

The whole call is gated, rather than just the engine run. At the front, the
auto-fetch immediately precedes the reader in the same call, so gating from the
start only means the queue forms a little earlier. At the back, the run's
memory is still live through persistence — the response holds the equity curve
and the result still references the bars — so releasing early would let a
second run allocate its half-gigabyte on top of the first's, which is the thing
being prevented. The bounded parity-companion dispatch rides along with it.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# Bounded, so a double release raises instead of quietly raising the permit
# count to two and letting exactly the concurrency this module exists to
# prevent through.
_gate = threading.BoundedSemaphore(1)
_held_by_this_thread = threading.local()

# How often a waiter surfaces to run ``while_waiting``. Short enough that a
# cancel feels immediate, long enough to be free next to a run measured in
# minutes.
WAIT_POLL_SECONDS = 1.0


@contextmanager
def one_backtest_in_flight(
    *,
    on_wait: Callable[[], None] = lambda: None,
    while_waiting: Callable[[], None] = lambda: None,
) -> Iterator[None]:
    """Hold the process-wide engine gate for the duration of one backtest.

    ``on_wait`` fires once, before blocking, only when the gate is already
    held — so a caller that gets straight through pays nothing and reports
    nothing. ``while_waiting`` then fires roughly every
    :data:`WAIT_POLL_SECONDS` until the gate is taken; raising from either
    abandons the wait, and neither can leak the gate because the semaphore is
    not held while they run.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        # A blocking acquire on the app loop would freeze every route,
        # ``/health`` included, and the container healthcheck would restart
        # the service — the same operator-visible outcome as the OOM this
        # gate prevents. Run the backtest in a thread instead.
        raise RuntimeError("one_backtest_in_flight was entered from a running event loop; run the backtest in a thread")

    if getattr(_held_by_this_thread, "held", False):
        # Re-entering would block on a semaphore this thread already holds and
        # hang forever. Nothing nests today; this makes it a loud failure if
        # something starts to. It only catches nesting that stays on one
        # thread — a nested run reached through ``asyncio.to_thread`` or the
        # background loop lands elsewhere and would deadlock silently.
        raise RuntimeError("the engine gate is already held by this thread; backtests must not nest")

    if not _gate.acquire(blocking=False):
        logger.info("[ENGINE] Queued behind the backtest in flight (waiter=%s)", threading.current_thread().name)
        on_wait()
        waited_from = time.monotonic()
        while not _gate.acquire(timeout=WAIT_POLL_SECONDS):
            while_waiting()
        logger.info("[ENGINE] Waited %.1fs for the backtest in flight", time.monotonic() - waited_from)

    _held_by_this_thread.held = True
    try:
        yield
    finally:
        _held_by_this_thread.held = False
        _gate.release()
