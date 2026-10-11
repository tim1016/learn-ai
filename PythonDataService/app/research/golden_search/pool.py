"""A bounded pool of spawned workers executes one ready batch concurrently (#3046, PRD #3034 slice 11).

Only the currently ready candidates spread across lanes — the procedure
boundaries stay sequential exactly as before: a Zoom round's choice, a fold's
train/test order, an exam's authorization. The pool is the ``BatchDispatch``
seam's second production adapter: every lane owns one
:class:`~app.research.golden_search.worker.ResearchWorker` for the pool's
life (its decoded cache reused across batches, sized by the shared cache
budget), begins an item (``ReadyEvaluation.begin``) immediately before
submitting the task, and delivers outcomes as they arrive — arrival order
is free, the evaluator consumes and persists each outcome before pulling
the next, and logical order never sees the pool.

Accounting is #3045's, unchanged: a window item begun but undelivered has a
start marker down, so a crash or loss spends retry allowance only for runs
that may have begun. The window is a permit count, not a queue length: a
permit is held from submission until the evaluator consumes the item's
outcome, so begun-but-undurable work can never exceed ``window`` however
fast the lanes run (#3066 review). A lane's loss is delivered as that
item's outcome — the evaluator re-admits only the lost item while healthy
siblings keep delivering — never as a whole-dispatch supersession that
would charge their begun rows retries (#3066 review). Cross-dispatch state
is still fenced twice: every queued entry carries the dispatch epoch that
interrupted it forward (a lane discards a superseded entry without
beginning it), and every arrival carries the item's task generation (an
in-flight twin from a superseded dispatch may finish first, and its outcome
is discarded against the generation, never consuming the re-admitted item's
slot). Workers are replaced only after the lost child is killed and
reaped; a lane's hung child times out on its own two clocks while its
siblings keep delivering.

Reference: #3046 / PRD #3034; ``app.research.golden_search.worker`` for the
one-worker runtime this generalizes; ADR 0077 for the runtime envelope the
count is bounded by.
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
    IdentityRefusalError,
    ReadyEvaluation,
    WorkerLost,
)
from app.research.golden_search.selection import Metrics
from app.research.golden_search.worker import (
    ResearchWorker,
    WorkerDispatchContext,
    delivered_outcome,
    pooled_cache_bytes,
    task_wire,
)
from app.research.persistence.ownership import GenerationFencedError

logger = logging.getLogger(__name__)

# How long a lane waits for work before re-polling shutdown; teardown never
# waits longer than this past its kill of the children.
_LANE_POLL_S = 0.5


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
    """The BatchDispatch a stage's evaluator runs through on a bounded worker pool (#3046).

    ``workers`` lanes, each owning one spawned child for the pool's life; at
    most ``window`` items submitted-but-unconsumed at any moment — the finite
    permit bound that keeps a whole-budget batch from ever becoming resident
    execution, however fast the lanes finish or slowly the evaluator
    persists (#3066 review). A lost worker is one item's loss, delivered as
    that item's outcome: the losing lane closes and forgets its child (a
    replacement spawns only when work next reaches that lane), the evaluator
    re-admits only that key under an advanced generation, and any twin still
    executing elsewhere lands as a generation-discarded straggler.
    """

    def __init__(
        self,
        context: WorkerDispatchContext,
        *,
        workers: int,
        window: int,
        on_facts: Callable[[DispatchFacts], None] = lambda facts: None,
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
        # The work bound is the pool's finite pending-task bound (#3046): a
        # whole-budget batch never becomes resident futures, only not-yet-
        # submitted items plus at most this many submitted-but-unconsumed.
        # The queue carries the entries; the semaphore below carries the
        # bound, because a lane dequeuing frees queue capacity long before
        # the evaluator consumes the outcome (#3066 review).
        self._work: queue.Queue[Any] = queue.Queue(maxsize=self._window)
        self._permits = threading.BoundedSemaphore(self._window)
        self._arrived: queue.Queue[tuple[str, int, DeliveredOutcome | BaseException]] = queue.Queue()
        self._slots: list[ResearchWorker | None] = [None] * workers
        self._slot_lock = threading.Lock()
        # The dispatch epoch fences cross-dispatch queue state: an interrupted
        # dispatch's queued entries are discarded at take time, never begun.
        self._epoch = 0
        self._epoch_lock = threading.Lock()
        self._closing = threading.Event()
        self._lanes = [
            threading.Thread(target=self._lane, args=(index,), name=f"research-pool-{index}", daemon=True)
            for index in range(workers)
        ]
        for lane in self._lanes:
            lane.start()

    # -- the lanes ----------------------------------------------------------

    def _lane(self, index: int) -> None:
        while True:
            try:
                entry = self._work.get(timeout=_LANE_POLL_S)
            except queue.Empty:
                if self._closing.is_set():
                    return
                continue
            if self._closing.is_set():
                with self._slot_lock:
                    worker, self._slots[index] = self._slots[index], None
                if worker is not None:
                    worker.close()
                return
            epoch, item = entry
            if epoch != self._current_epoch():
                # A superseded dispatch's leftover: nobody owes this run anymore.
                continue
            try:
                self._run_one(index, item)
            except BaseException:
                # A lane bug must never vanish an item silently: the item stays
                # owed (the evaluator's adapter-breach guard names it), the
                # child closes with the failure, and the lane stays alive for
                # the next entry — every lane dying here would strand the next
                # dispatch on an arrival that never comes (#3066 review).
                logger.exception("a research pool lane failed; closing its worker")
                with self._slot_lock:
                    worker, self._slots[index] = self._slots[index], None
                if worker is not None:
                    worker.close()
                self._arrived.put((item.key, item.task_gen, WorkerLost(f"pool lane {index} failed; its worker is closed")))

    def _current_epoch(self) -> int:
        with self._epoch_lock:
            return self._epoch

    def _slot_worker(self, index: int) -> ResearchWorker | None:
        """This lane's live child, or ``None`` once a between-tasks death is cleaned up (#3065 review)."""
        with self._slot_lock:
            worker = self._slots[index]
        if worker is not None and worker.lost_reason() is not None:
            worker.close()
            with self._slot_lock:
                if self._slots[index] is worker:
                    self._slots[index] = None
            return None
        return worker

    def _run_one(self, index: int, item: ReadyEvaluation) -> None:
        """Run one item on this lane's owned child, replacing the child only after a loss is cleaned up."""
        worker = self._slot_worker(index)
        if worker is None:
            if self._closing.is_set():
                self._arrived.put((item.key, item.task_gen, WorkerLost("the research pool is closing")))
                return
            try:
                worker = ResearchWorker(self._context)
            except WorkerLost as exc:
                # A child that never came up is this item's loss — reported,
                # not a dead lane: work reaching this lane again tries again,
                # and the evaluator's allowance bounds the tries (#3066 review).
                self._arrived.put((item.key, item.task_gen, WorkerLost(f"the research worker failed to start: {exc}")))
                return
            except IdentityRefusalError as exc:
                # A refused identity is terminal for the runtime, never a
                # retryable loss (#3065 review).
                self._arrived.put((item.key, item.task_gen, exc))
                return
            with self._slot_lock:
                if self._closing.is_set():
                    # The pool closed while this child was starting (#3066
                    # review): startup is invisible to close()'s snapshot, so
                    # the lane owns killing it before anything begins.
                    runaway = True
                else:
                    self._slots[index] = worker
                    runaway = False
            if runaway:
                worker.close()
                self._arrived.put((item.key, item.task_gen, WorkerLost("the research pool is closing")))
                return
        try:
            worker_task = task_wire(
                item,
                strategy_key=self._context.strategy_key,
                execution=self._context.execution,
                manifest=self._context.manifest,
                identity=self._context.identity,
            )
            item.begin()
            message = worker.run_task(worker_task)
        except WorkerLost:
            # Cleanup before replacement (#3045): the child is killed and
            # reaped here; work reaching this lane again spawns a fresh one.
            worker.close()
            with self._slot_lock:
                if self._slots[index] is worker:
                    self._slots[index] = None
            self._arrived.put((item.key, item.task_gen, WorkerLost("a pooled research worker died with a task in flight")))
            return
        except BaseException as exc:
            # A store fence (a refused begin, a superseded attempt) is an honest
            # terminal answer, not infrastructure loss: deliver it and let the
            # stage's handlers name it. run_task already killed and reaped its
            # child on the process-control paths; forget the slot so the lane
            # replaces it instead of reusing the corpse (#3065 review).
            if isinstance(exc, (JobCancelled, GenerationFencedError, IdentityRefusalError)):
                with self._slot_lock:
                    if self._slots[index] is worker:
                        self._slots[index] = None
            self._arrived.put((item.key, item.task_gen, exc))
            return
        self._arrived.put((item.key, item.task_gen, delivered_outcome(item, message)))

    # -- the dispatch -------------------------------------------------------

    def __call__(self, run, batch: Sequence[ReadyEvaluation]):  # BatchDispatch signature; run unused by design
        owed = {item.key: item for item in batch}
        with self._epoch_lock:
            self._epoch += 1
            epoch = self._epoch
        submitted: set[str] = set()
        consumed: set[str] = set()
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
                    key, task_gen, arrival = self._arrived.get(timeout=0.2)
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
                    continue
                del owed[key]
                if isinstance(arrival, WorkerLost):
                    # One lane's loss is that item's loss, not the dispatch's
                    # (#3066 review): delivered as an outcome so the evaluator
                    # re-admits only this key while its healthy siblings keep
                    # delivering — a supersession would spend their begun rows'
                    # retry allowance and discard their results as stragglers,
                    # timing a fabricated failure into the search.
                    yield DeliveredOutcome(
                        key=key,
                        attempt=item.attempt,
                        task_gen=task_gen,
                        result=EvaluationResult(metrics=Metrics.failed("worker lost")),
                        lost_reason=str(arrival),
                    )
                    consumed.add(key)
                    self._permits.release()
                    self._feed(owed, submitted, epoch)
                    continue
                if isinstance(arrival, BaseException):
                    # This dispatch is over: whatever it still had queued dies at
                    # take time, so a lane freed during the recovery cannot begin a
                    # run nobody owes anymore. The process-control errors stop the
                    # healthy siblings too, now, not at stage teardown (#3065
                    # review); every other terminal answer leaves them to it.
                    if isinstance(arrival, (JobCancelled, GenerationFencedError, IdentityRefusalError)):
                        self._interrupt(epoch)
                    else:
                        with self._epoch_lock:
                            if self._epoch == epoch:
                                self._epoch += 1
                    raise arrival
                yield arrival
                # The evaluator resumed — the outcome it took is durable — so
                # the window opens by one for the next submission.
                consumed.add(key)
                self._permits.release()
                self._feed(owed, submitted, epoch)
            # The batch is delivered; the next dispatch opens a fresh epoch, so
            # anything still queued from this one dies at take time.
        finally:
            for _ in range(len(submitted) - len(consumed)):
                self._permits.release()
            self._emit_facts(len(batch), adaptive_wait_ms)

    def _feed(self, owed: Mapping[str, ReadyEvaluation], submitted: set[str], epoch: int) -> None:
        """Top the dispatch up to the window with the batch's remaining items, in logical order.

        A permit is taken per submission and returned only when the evaluator
        consumes that item's outcome, so submitted-but-unconsumed — queued,
        in flight or arrived — never exceeds the window (#3066 review).
        """
        for key, item in owed.items():
            if key in submitted:
                continue
            if not self._permits.acquire(blocking=False):
                return
            try:
                self._work.put_nowait((epoch, item))
            except queue.Full:
                self._permits.release()
                return
            submitted.add(key)

    def _interrupt(self, epoch: int) -> None:
        """Invalidate everything this dispatch still owns, then stop its children (#3045, #3046)."""
        with self._epoch_lock:
            if self._epoch == epoch:
                self._epoch += 1
        self._cancel_children()

    def cancel(self) -> None:
        """The evaluator's pre-reconciliation reap (#3065 review): invalidate everything, stop the children.

        Called when a cancellation or fence was raised outside this adapter's
        own handlers — delivered as an outcome, most often — so the children
        never wait out the evaluator's durable release writes.
        """
        with self._epoch_lock:
            self._epoch += 1
        self._cancel_children()

    def _active_count(self) -> int:
        """Lanes holding a child plus items queued or arrived and not yet consumed (#3046's active-worker fact)."""
        with self._slot_lock:
            held = sum(worker is not None for worker in self._slots)
        return held + self._work.qsize() + self._arrived.qsize()

    def _emit_facts(self, ready_width: int, adaptive_wait_ms: int) -> None:
        self._on_facts(
            DispatchFacts(
                ready_width=ready_width,
                workers=self._worker_count,
                window=self._window,
                active=self._active_count(),
                adaptive_wait_ms=adaptive_wait_ms,
            )
        )

    def _cancel_children(self) -> None:
        """Stop every child promptly, in parallel — the cancel flag first, then kill and reap."""
        with self._slot_lock:
            workers = [worker for worker in self._slots if worker is not None]
        stopping = [threading.Thread(target=worker.cancel) for worker in workers]
        for stopper in stopping:
            stopper.start()
        for stopper in stopping:
            stopper.join(timeout=5.0)

    def close(self) -> None:
        """Stop the pool: the closing flag first, every child killed and reaped, lanes out within one poll."""
        self._closing.set()
        with self._slot_lock:
            workers = [worker for worker in self._slots if worker is not None]
            self._slots = [None] * self._worker_count
        for worker in workers:
            worker.close()
        for lane in self._lanes:
            lane.join(timeout=10.0)


def worker_pool_factory(
    *,
    on_checkpoint: Callable[[Mapping[str, Any]], None],
    cancel_check: Callable[[], object] = lambda: None,
    on_facts: Callable[[DispatchFacts], None] = lambda facts: None,
    workers: int,
    window: int,
) -> Callable[[WorkerDispatchContext], PooledWorkerDispatch]:
    """The pool-side factory a stage run hands its context to (#3046).

    The stage knows the row's sealed context; the runtime knows the pool's
    bounds. This closes the gap the way ``worker_dispatch_factory`` does for
    one worker, attaching the checkpoint relay and the cancellation poll.
    """

    def build(context: WorkerDispatchContext) -> PooledWorkerDispatch:
        context.cancel_check = cancel_check
        context.on_checkpoint = on_checkpoint
        return PooledWorkerDispatch(context, workers=workers, window=window, on_facts=on_facts)

    return build
