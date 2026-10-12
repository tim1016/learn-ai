"""A stage's evaluator runs through the shared pool as one owner (#3046, #3048; PRD #3034 slices 11 and 13).

Only the currently ready candidates spread across lanes — the procedure
boundaries stay sequential exactly as before: a Zoom round's choice, a fold's
train/test order, an exam's authorization. Every dispatch is an *owner* on a
:class:`~app.research.golden_search.shared_pool.SharedWorkerPool`: it keeps
its future candidate definitions to itself, offers already-authorized items
within its own window, and consumes outcomes as they arrive — arrival order
is free, the evaluator persists each outcome before pulling the next, and
logical order never sees the pool. Constructed without a pool it owns a
private one (tests, a lone local dispatch); the research runtime's factory
binds every dispatch to the process's one shared pool, where round-robin
fairness, the global submission bound and warm-lane affinity live (#3048).

Accounting is #3045's, unchanged: a window item begun but undelivered has a
start marker down, so a crash or loss spends retry allowance only for runs
that may have begun. The window is a permit count, not a queue length: a
permit is held from submission until the evaluator consumes the item's
outcome, so begun-but-undurable work can never exceed ``window`` however
fast the lanes run (#3066 review). A lane's loss is delivered as that
item's outcome — the evaluator re-admits only the lost item while healthy
siblings keep delivering — never as a whole-dispatch supersession that
would charge their begun rows retries (#3066 review). Cross-dispatch state
is still fenced twice: every offered entry carries the dispatch epoch that
interrupted it forward (a lane discards a superseded entry without
beginning it), and every arrival carries the item's task generation (an
in-flight twin from a superseded dispatch may finish first, and its outcome
is discarded against the generation, never consuming the re-admitted item's
slot). Workers are replaced only after the lost child is killed and
reaped; a lane's hung child times out on its own two clocks while its
siblings keep delivering.

Reference: #3046 / #3048 / PRD #3034; ``app.research.golden_search.worker``
for the one-worker runtime this generalizes; ADR 0077 for the runtime
envelope the count is bounded by.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.jobs.progress import JobCancelled
from app.research.golden_search.evaluator import (
    DeliveredOutcome,
    EvaluationResult,
    ReadyEvaluation,
    WorkerLost,
)
from app.research.golden_search.selection import Metrics
from app.research.golden_search.shared_pool import (
    DEFAULT_PAYLOAD_BOUND_BYTES,
    DEFAULT_RESULT_BOUND_BYTES,
    SUBMISSION_FACTOR,
    OwnerHandle,
    SharedWorkerPool,
)
from app.research.golden_search.worker import (
    WorkerDispatchContext,
    pooled_cache_bytes,
)
from app.research.persistence.ownership import GenerationFencedError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DispatchFacts:
    """What one dispatch through the pool observed (#3046): the progress contract's scheduling facts."""

    ready_width: int
    workers: int
    window: int
    active: int
    adaptive_wait_ms: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready_width": self.ready_width,
            "workers": self.workers,
            "window": self.window,
            "active": self.active,
            "adaptive_wait_ms": self.adaptive_wait_ms,
        }


class PooledWorkerDispatch:
    """The BatchDispatch a stage's evaluator runs through as one owner on a worker pool (#3046, #3048).

    The dispatch registers with its pool for its whole life and drives one
    generator per ready batch: it offers up to ``window``
    submitted-but-unconsumed items — the finite permit bound that keeps a
    whole-budget batch from ever becoming resident execution, however fast
    the lanes finish or slowly the evaluator persists (#3066 review) — and
    yields each outcome as it arrives. On a shared pool the same window is
    this owner's share of the pool's global submission bound; a lost worker
    is one item's loss, delivered as that item's outcome for the evaluator
    to re-admit under an advanced generation.
    """

    def __init__(
        self,
        context: WorkerDispatchContext,
        *,
        workers: int,
        window: int,
        on_facts: Callable[[DispatchFacts], None] = lambda facts: None,
        pool: SharedWorkerPool | None = None,
        on_capacity_handoff: Callable[[], None] = lambda: None,
    ) -> None:
        if workers < 1:
            raise ValueError("a worker pool has at least one lane")
        self._context = context
        self._worker_count = workers
        self._window = max(window, workers)
        self._on_facts = on_facts
        # Every child's decoded cache shares one budget inside the runtime
        # envelope: eight lanes at the serial parity ceiling would consume it
        # in caches alone (#3066 review).
        context.decoded_cache_bytes = pooled_cache_bytes(workers)
        self._owns_pool = pool is None
        self._pool = pool or SharedWorkerPool(
            workers=workers,
            # A private pool admits its one owner's whole window; the shared
            # pool the runtime builds carries the issue's initial bound.
            submission_bound=max(SUBMISSION_FACTOR * workers, self._window),
            payload_bound_bytes=DEFAULT_PAYLOAD_BOUND_BYTES,
            result_bound_bytes=DEFAULT_RESULT_BOUND_BYTES,
        )
        self._epoch = 0
        self._epoch_lock = threading.Lock()
        # The owner-side window arithmetic (offered minus consumed this
        # batch) — only the evaluator's own thread touches it.
        self._outstanding = 0
        self._handle: OwnerHandle | None = self._pool.register(
            context,
            window=self._window,
            epoch_of=self._current_epoch,
            on_capacity_handoff=on_capacity_handoff,
        )

    def _current_epoch(self) -> int:
        with self._epoch_lock:
            return self._epoch

    # -- the dispatch -------------------------------------------------------

    def __call__(self, run, batch: Sequence[ReadyEvaluation]):  # BatchDispatch signature; run unused by design
        owed = {item.key: item for item in batch}
        with self._epoch_lock:
            self._epoch += 1
            epoch = self._epoch
        submitted: set[str] = set()
        consumed: set[str] = set()
        self._outstanding = 0
        adaptive_wait_ms = 0
        self._emit_facts(len(batch), adaptive_wait_ms)
        try:
            self._feed(owed, submitted, epoch)
            while owed:
                try:
                    cancelled = self._context.cancel_check()
                except (JobCancelled, GenerationFencedError):
                    # The production polls raise (#3041): stopping the children and
                    # the epoch is this side's job on every cancellation path,
                    # never only the returning one (#3045's prompt-slot rule).
                    self._interrupt(epoch)
                    raise
                if cancelled:
                    self._interrupt(epoch)
                    raise JobCancelled("cancelled between pooled research worker tasks")
                waited_at = time.monotonic()
                try:
                    key, task_gen, arrival = self._handle.arrived.get(timeout=0.2)
                except queue.Empty:
                    # Every lane busy or the window full: the pool's adaptive wait (#3046).
                    adaptive_wait_ms += int((time.monotonic() - waited_at) * 1000)
                    continue
                item = owed.get(key)
                if item is None or item.task_gen != task_gen:
                    # A straggler from a superseded dispatch — most often an
                    # in-flight twin that finished before its re-admitted
                    # replacement. The evaluator's task-generation fence owns the
                    # durable refusal; the pool keeps the owed slot for the
                    # current generation's outcome (#3045, #3046).
                    logger.warning(
                        "a pooled worker delivered an outcome the current batch no longer owes",
                        extra={"action": "golden_search_pool_straggler_discarded", "evaluation_key": key},
                    )
                    self._handle.consume(key)
                    continue
                del owed[key]
                if isinstance(arrival, WorkerLost):
                    # One lane's loss is that item's loss, not the dispatch's
                    # (#3066 review): delivered as an outcome so the evaluator
                    # re-admits only this key while its healthy siblings keep
                    # delivering — a supersession would spend their begun rows'
                    # retry allowance and discard their results as stragglers,
                    # timing a fabricated failure into the search.
                    self._handle.consume(key)
                    self._outstanding -= 1
                    consumed.add(key)
                    yield DeliveredOutcome(
                        key=key,
                        attempt=item.attempt,
                        task_gen=task_gen,
                        result=EvaluationResult(metrics=Metrics.failed("worker lost")),
                        lost_reason=str(arrival),
                    )
                    self._feed(owed, submitted, epoch)
                    continue
                if isinstance(arrival, BaseException):
                    # This dispatch is over: whatever it still had queued dies at
                    # take time, so a lane freed during the recovery cannot begin a
                    # run nobody owes anymore. The process-control errors stop the
                    # healthy siblings too, now, not at stage teardown (#3065
                    # review); every other terminal answer leaves them to it.
                    if isinstance(arrival, (JobCancelled, GenerationFencedError)):
                        self._interrupt(epoch)
                    else:
                        with self._epoch_lock:
                            if self._epoch == epoch:
                                self._epoch += 1
                    raise arrival
                self._handle.consume(key)
                self._outstanding -= 1
                consumed.add(key)
                yield arrival
                # The evaluator resumed — the outcome it took is durable — so
                # the window opens by one for the next submission.
                self._feed(owed, submitted, epoch)
            # The batch is delivered; the next dispatch opens a fresh epoch, so
            # anything still queued from this one dies at take time.
        finally:
            self._emit_facts(len(batch), adaptive_wait_ms)

    def _feed(self, owed: Mapping[str, ReadyEvaluation], submitted: set[str], epoch: int) -> None:
        """Top the dispatch up to the window with the batch's remaining items, in logical order.

        The owner's window is its own permit count — offered minus consumed —
        and the pool's global bounds refuse an offer the whole pool cannot
        afford; either way this loop stops and retries on the next
        consumption, never blocking the evaluator.
        """
        handle = self._handle
        if handle is None:
            return
        for key, item in owed.items():
            if key in submitted:
                continue
            if self._outstanding >= self._window:
                return
            if not handle.offer(epoch, item):
                return
            submitted.add(key)
            self._outstanding += 1

    def _interrupt(self, epoch: int) -> None:
        """Invalidate everything this dispatch still owns, then stop its children (#3045, #3046).

        On a shared pool "its children" means exactly the lanes running
        this owner's items — a sibling owner's lanes are not this
        dispatch's to stop (#3048).
        """
        with self._epoch_lock:
            if self._epoch == epoch:
                self._epoch += 1
        if self._handle is not None:
            self._handle.interrupt_running()

    def cancel(self) -> None:
        """The evaluator's pre-reconciliation reap (#3065 review): invalidate everything, stop the children.

        Called when a cancellation or fence was raised outside this adapter's
        own handlers — delivered as an outcome, most often — so the children
        never wait out the evaluator's durable release writes.
        """
        with self._epoch_lock:
            self._epoch += 1
        if self._handle is not None:
            self._handle.interrupt_running()

    def _emit_facts(self, ready_width: int, adaptive_wait_ms: int) -> None:
        self._on_facts(
            DispatchFacts(
                ready_width=ready_width,
                workers=self._worker_count,
                window=self._window,
                active=self._pool.facts()["busy_lanes"],
                adaptive_wait_ms=adaptive_wait_ms,
            )
        )

    def close(self) -> None:
        """Leave the pool: queued entries die at take time, running items are interrupted, a private pool stops."""
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        if self._owns_pool:
            self._pool.close()


def worker_pool_factory(
    *,
    on_checkpoint: Callable[[Mapping[str, Any]], None],
    cancel_check: Callable[[], object] = lambda: None,
    on_facts: Callable[[DispatchFacts], None] = lambda facts: None,
    workers: int,
    window: int,
    pool: SharedWorkerPool | None = None,
    on_capacity_handoff: Callable[[], None] = lambda: None,
) -> Callable[[WorkerDispatchContext], PooledWorkerDispatch]:
    """The pool-side factory a stage run hands its context to (#3046, #3048).

    The stage knows the row's sealed context; the runtime knows the pool's
    bounds. This closes the gap the way ``worker_dispatch_factory`` does for
    one worker, attaching the checkpoint relay and the cancellation poll —
    and, when the runtime passes its shared pool, binding this dispatch to
    the process's one set of lanes instead of a private one.
    """

    def build(context: WorkerDispatchContext) -> PooledWorkerDispatch:
        context.cancel_check = cancel_check
        context.on_checkpoint = on_checkpoint
        return PooledWorkerDispatch(
            context, workers=workers, window=window, on_facts=on_facts,
            pool=pool, on_capacity_handoff=on_capacity_handoff,
        )

    return build
