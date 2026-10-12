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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.research.golden_search.evaluator import (
    DeliveredOutcome,
    EvaluationResult,
    ReadyEvaluation,
    WorkerLost,
)
from app.research.golden_search.selection import Metrics
from app.research.golden_search.shared_pool import (
    PooledOwnerDispatch,
    SharedWorkerPool,
)
from app.research.golden_search.worker import WorkerDispatchContext

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


class PooledWorkerDispatch(PooledOwnerDispatch):
    """The BatchDispatch a stage's evaluator runs through as one owner on a worker pool (#3046, #3048).

    The dispatch registers with its pool for its whole life and drives one
    generator per ready batch: it offers up to ``window``
    submitted-but-unconsumed items — the finite permit bound that keeps a
    whole-budget batch from ever becoming resident execution, however fast
    the lanes finish or slowly the evaluator persists (#3066 review) — and
    yields each outcome as it arrives. On a shared pool the same window is
    this owner's share of the pool's global submission bound; a lost worker
    is one item's loss, delivered as that item's outcome for the evaluator
    to re-admit under an advanced generation. The window arithmetic, the
    epoch fence, the cancellation poll and the budget settlement live in
    the shared owner base (#3049): this subclass is exactly the evaluator's
    per-arrival policy.
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
        super().__init__(
            context, workers=workers, window=window, pool=pool,
            on_capacity_handoff=on_capacity_handoff, on_facts=on_facts,
        )
        self._worker_count = workers

    def _facts_payload(self, ready_width: int, adaptive_wait_ms: int) -> DispatchFacts:
        return DispatchFacts(
            ready_width=ready_width,
            workers=self._worker_count,
            window=self._window,
            active=self._pool.facts()["busy_lanes"],
            adaptive_wait_ms=adaptive_wait_ms,
        )

    # -- the dispatch -------------------------------------------------------

    def __call__(self, run, batch: Sequence[ReadyEvaluation]):  # BatchDispatch signature; run unused by design
        owed = {item.key: item for item in batch}
        epoch, state = self._begin(len(batch))
        try:
            for item, arrival in self._pump(owed, state, epoch):
                if isinstance(arrival, WorkerLost):
                    # One lane's loss is that item's loss, not the dispatch's
                    # (#3066 review): delivered as an outcome so the evaluator
                    # re-admits only this key while its healthy siblings keep
                    # delivering — a supersession would spend their begun rows'
                    # retry allowance and discard their results as stragglers,
                    # timing a fabricated failure into the search.
                    yield DeliveredOutcome(
                        key=item.key,
                        attempt=item.attempt,
                        task_gen=item.task_gen,
                        result=EvaluationResult(metrics=Metrics.failed("worker lost")),
                        lost_reason=str(arrival),
                    )
                else:
                    # The evaluator's own answers arrive pre-decoded; a
                    # cancelled run is one of them (``cancelled_by``), and the
                    # evaluator — not this adapter — releases its reservation.
                    yield arrival
                # The evaluator resumed — the outcome it took is durable — so
                # the window opens by one for the next submission.
                self._feed(owed, state, epoch)
            # The batch is delivered; the next dispatch opens a fresh epoch, so
            # anything still queued from this one dies at take time.
        finally:
            self._emit_facts(len(batch), state.adaptive_wait_ms)


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
