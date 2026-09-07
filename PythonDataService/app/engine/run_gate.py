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

**What this gate covers.** ``BacktestEngine.run`` holds it, so every engine
run is counted by construction rather than by remembering to route through one
function. It was first held only by
``routers.engine.execute_engine_backtest``, which covered five callers and
missed three — ``/api/spec-strategy/backtest``, ``/api/research-runs`` and
``/api/lean-sidecar/cross-reconcile`` each built an engine of their own, so a
run on any of them could still pair with a gated run and reach the footprint
that killed uvicorn (#1990).

``execute_engine_backtest`` keeps its own, wider hold. A run's memory is live
well beyond the simulation: the auto-fetch that precedes it, and the response
holding the equity curve while the row is written. Releasing at
``engine.run``'s boundary would let a second run allocate on top of the first's
retained half-gigabyte, which is the thing being prevented. So the two holds
nest, and the inner one passes through — see the re-entrancy note below.

**It queues; it does not refuse.** A sweep must run all of its cells, so a
refusal would turn a memory guard into failed work. The half-gigabyte belongs
to the run in flight, not to the ones waiting. The queue is unbounded, which is
the right trade for a single-operator research tool: the cost of a waiter is
its thread, and a bound would have to reject work that a human is watching.

**Re-entrancy, and the one rule a maintainer has to check.** The gate is an
``RLock``, so a thread that already holds it passes straight through: the
outer hold provides the exclusivity the inner acquire would ask for. That
ownership is per *thread*, which gives the rule:

    A thread holding the gate must never hand engine work to another thread
    and wait for it.

That second thread does not queue — it wedges, permanently and silently. It
blocks in ``RLock.acquire``, which is a C-level wait no ``asyncio.wait_for``
timeout can rescue, and its caller is holding the very lock it waits on. The
process does not error; it stops.

Note what the rule does *not* say. Reaching the engine through
``asyncio.to_thread`` is fine and three callers do exactly that
(``post_cross_reconcile``, ``run_spec_against_bars_and_persist``,
``run_shadow_trace_evaluation``) — they hop off the event loop *before* taking
the gate, and hold nothing while they wait. ``scripts/run_replay_proof.py``
reaches ``run_shadow_trace_evaluation`` the same way and is likewise safe. The
hazard is only the hop taken *while holding*, and today nothing does that: the
call is synchronous from the moment the gate is taken. Adding a ``to_thread``,
a ``run_in_thread`` or a background-loop submit anywhere inside a gated call
is the edit that breaks this.

One thing re-entrancy gives up: a genuine backtest inside a backtest — an
engine run started from within another engine run on the same thread — now
passes through, so two runs' memory would be live while the gate reports one.
Nothing constructs an engine from inside a strategy, and the guard that would
catch it is the hand-rolled owner tracking ``RLock`` replaced. Stated so the
trade is visible rather than discovered.

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
real work between cells, so the waiter wins in practice. And a queued caller holds a
worker thread for the whole wait. A sync ``/backtest`` request holds one of
anyio's 40 tokens; the three ``asyncio.to_thread`` callers hold one of the
loop's *default* executor, which is ``min(32, cpu + 4)`` — **six** on the
two-CPU container. Six concurrent cross-reconciles would occupy all of them
(one running, five queued) and stall every other ``to_thread`` route in the
process. ``/health`` is ``async def`` with no thread hop, so the container
stays up; still, if that ever bites, ``anyio.CapacityLimiter`` is the idiom
this repo already uses for it (``app/broker/alpaca/client.py``).

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

# Re-entrant, because the two holds nest: the router's spans auto-fetch and
# persistence, the engine's covers the run itself, and both are on one thread.
# ``RLock`` owns that per-thread bookkeeping — a hand-rolled ``threading.local``
# flag beside a semaphore said the same thing in fifteen more lines, and said
# it in a comment rather than in the primitive. A release by a thread that does
# not own it raises, as a double release did before.
_gate = threading.RLock()

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
    abandons the wait, and neither can leak the gate because the lock is not
    held while they run.

    A thread that already holds the gate re-acquires it without waiting, and
    reports nothing — the nesting of the router's hold and the engine's is the
    normal case, not a queue.
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


    if not _gate.acquire(blocking=False):
        logger.info("[ENGINE] Queued behind the backtest in flight (waiter=%s)", threading.current_thread().name)
        on_wait()
        waited_from = time.monotonic()
        while not _gate.acquire(timeout=WAIT_POLL_SECONDS):
            while_waiting()
        logger.info("[ENGINE] Waited %.1fs for the backtest in flight", time.monotonic() - waited_from)

    try:
        yield
    finally:
        _gate.release()
