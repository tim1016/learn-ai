"""One bounded pool of spawned workers shared fairly by every dispatch (#3048, PRD #3034 slice 13).

The lanes and their children are process-lifetime resources; a stage run
is an *owner* that registers, offers its already-authorized ready items,
and consumes their outcomes through the same
:class:`~app.research.golden_search.pool.PooledWorkerDispatch` contract the
per-dispatch pool served. Children are generic — the task wire carries
strategy, execution, manifest and sealed identity per task, and the child
checks the task's identity against the runtime's own — so a lane may serve
any owner whose code identity matches this process's, exactly as a fresh
per-stage child would.

Scheduling is work-conserving round-robin: a ring of runnable owners, one
item offered per turn, a single runnable owner filling every free lane. A
free lane is chosen by decoded-artifact residency — the warm hint children
report per task — with the lane index as the deterministic tie-break, and
an item never waits for a warmer busy lane while any idle one is free;
affinity is subordinate to the ring either way. Global bounds hold
whatever the owners' own windows permit: submitted-but-unconsumed tasks
never exceed the pool's submission bound (initially twice the lane count),
and payload and result bytes are bounded independently, the latter by
backpressure on delivery. Aggregate memory pressure — read from the
runtime's own cgroup, which already counts children, caches, imports and
the coordinator — pauses new assignments until it recedes; in-flight work
always completes or fails visibly, never silently.

Budget settlement follows the arrival: each delivered outcome carries the
bytes it must give back, and the owner settles them when its evaluator
consumes — or the pool settles at once when nobody ever will (a superseded
entry, a closed owner). Fencing is unchanged from the per-dispatch pool:
entries carry their owner's epoch (a superseded dispatch's leftovers die at
take time), arrivals carry the item's task generation, and one owner's
cancellation stops only the children running that owner's items.

Drain is the operator's capacity handoff: it stops new assignments, waits
bounded for owned work to complete, interrupts what cannot, reaps the
children, and leaves the pool closed so a restart at a new profile finds
no orphans.

Reference: #3048 / PRD #3034; ``app.research.golden_search.pool`` for the
owner-side adapter; ADR 0077 for the envelope the lane count fits inside.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.jobs.progress import JobCancelled
from app.research.golden_search.evaluator import (
    DeliveredOutcome,
    IdentityRefusalError,
    ReadyEvaluation,
    WorkerLost,
)
from app.research.golden_search.worker import (
    ResearchWorker,
    WorkerDispatchContext,
    delivered_outcome,
    task_wire,
)
from app.research.persistence.ownership import GenerationFencedError

logger = logging.getLogger(__name__)

# How long a lane waits for an assignment before re-polling shutdown.
_LANE_POLL_S = 0.5

# The initial global bound the issue sets: submitted-but-unconsumed tasks
# across every owner at most twice the lane count.
SUBMISSION_FACTOR = 2

# The independent byte bounds (#3048): task wires in flight, and delivered
# outcomes awaiting consumption. Generous by design — they are the fence
# that makes a runaway result shape visible as backpressure, not a tuner
# of normal traffic; manifests and detail curves are the big citizens.
DEFAULT_PAYLOAD_BOUND_BYTES = 256 * 1024**2
DEFAULT_RESULT_BOUND_BYTES = 256 * 1024**2

# Pressure is read from the cgroup no more often than this; the file read
# is cheap but the schedule path is hot.
_PRESSURE_PROBE_S = 5.0

# Conservative tasks (OS threads) a lane's child and its parent-side pump
# may hold, import spikes included; the startup fit check multiplies this
# by the lane count against 80 % of the cgroup's PID cap (#3048 AC 7).
_TASKS_PER_LANE = 8
_BASE_TASKS = 32

# The operator profiles (#3048): the declared widths, each inside ADR
# 0077's envelope. One is the serial parity runtime; four is the initial
# rollout; eight is the qualification envelope — eight CPUs, 16 GiB and
# the 512-PID cap the compose service pins. A configured width between
# profiles runs inside the same envelope under the widest declared
# profile at or below it — declared at startup, never silent.
WORKER_PROFILES: dict[int, str] = {
    1: "serial parity — one lane, the runtime #3044 shipped",
    2: "two lanes — the smallest sharing width",
    4: "four lanes — the initial rollout profile",
    8: "eight lanes — the qualification envelope (8 CPUs / 16 GiB / PID 512, ADR 0077)",
}


def worker_profile(count: int) -> str:
    """The declared profile a configured width runs under (#3048)."""
    widest = max((width for width in WORKER_PROFILES if width <= count), default=1)
    return WORKER_PROFILES[widest]


class PoolDrainingError(RuntimeError):
    """The shared pool is drained or draining and takes no new owners (#3048)."""


def _wire_size(task: dict[str, Any]) -> int:
    """The wire's serialized size — the honest payload bound needs the real bytes.

    The manifest dominates and is shared by the owner's every item, so the
    dumps cost is one per offer against a backtest per run.
    """
    try:
        return len(json.dumps(task, separators=(",", ":"), default=str))
    except (TypeError, ValueError):
        return 4096


@dataclass
class _Entry:
    """One owner's offered item inside the pool — submitted task state, never a candidate definition."""

    owner: _OwnerState
    epoch: int
    item: ReadyEvaluation
    payload_bytes: int = 0
    # Set when the scheduler hands a waiting owner its first slot; the lane
    # fires the owner's capacity-handoff renewal once the begin marker —
    # the durable transition underneath the handoff — is down (#3048).
    handoff_pending: bool = False


@dataclass
class _OwnerState:
    """The pool's view of one registered dispatch (#3048).

    ``pending`` holds offered-but-unassigned entries; the owner-side
    adapter keeps future candidate definitions to itself and tops this up
    only within its own window, so the pool never sees a study's whole
    budget as resident state. ``settled`` maps a delivered key to the bytes
    its consumption must give back.
    """

    context: WorkerDispatchContext
    window: int
    epoch_of: Callable[[], int]
    on_capacity_handoff: Callable[[], None]
    handle: OwnerHandle | None = None
    pending: deque[_Entry] = field(default_factory=deque)
    settled: dict[str, tuple[int, int]] = field(default_factory=dict)
    submitted: int = 0  # offered and not yet consumed, this owner
    in_flight: int = 0  # assigned to a lane and not yet delivered
    manifest_paths: frozenset[str] = field(default_factory=frozenset)
    closed: bool = False


@dataclass
class _Lane:
    """One lane: a thread owning one generic child, fed by the scheduler."""

    index: int
    assignments: deque[_Entry] = field(default_factory=deque)
    signal: threading.Condition = field(default_factory=threading.Condition)
    busy: bool = False
    worker: ResearchWorker | None = None
    current: _Entry | None = None
    # Warm hints: artifact path -> estimated decoded bytes. A hint can be
    # stale in both directions — a path absent here may be warm in the
    # child, and one present may since have been evicted — and either way
    # costs at most one re-decode: residency is a hint, never admission.
    resident: dict[str, int] = field(default_factory=dict)
    cache_snapshot: dict[str, int] = field(default_factory=dict)

    def residency_overlap(self, paths: frozenset[str]) -> int:
        return sum(1 for path in paths if path in self.resident)


class OwnerHandle:
    """The owner-side adapter's handle into the pool: offer, consume, deregister."""

    def __init__(self, pool: SharedWorkerPool, state: _OwnerState) -> None:
        self._pool = pool
        self._state = state
        # The arrival queue the adapter's generator drains. Unbounded by
        # count: the pool's result-byte bound is the real cap, enforced at
        # delivery with backpressure (#3048).
        self.arrived: queue.Queue[tuple[str, int, DeliveredOutcome | BaseException]] = queue.Queue()

    def offer(self, epoch: int, item: ReadyEvaluation) -> bool:
        """Offer one authorized item; ``False`` when a global bound is full.

        Called by the owner's own thread between consumptions, exactly
        where the per-dispatch pool topped up its window.
        """
        return self._pool._offer(self._state, epoch, item)

    def consume(self, key: str) -> None:
        """Settle one consumed outcome's bytes and wake the scheduler for the freed budget."""
        self._pool._consume(self._state, key)

    def interrupt_running(self) -> None:
        """Stop the children running this owner's items — the pre-reconciliation reap."""
        self._pool._cancel_owner_children(self._state)

    def close(self) -> None:
        """Deregister: queued entries die at take time, running items are interrupted."""
        self._pool._deregister(self._state)


class SharedWorkerPool:
    """The process-lifetime lanes every dispatch shares under one fair scheduler (#3048)."""

    def __init__(
        self,
        *,
        workers: int,
        submission_bound: int,
        payload_bound_bytes: int,
        result_bound_bytes: int,
        spawn: Callable[[WorkerDispatchContext], ResearchWorker] | None = None,
    ) -> None:
        if workers < 1:
            raise ValueError("a shared worker pool has at least one lane")
        self._spawn = spawn or ResearchWorker
        self._worker_count = workers
        self._submission_bound = submission_bound
        self._payload_bound_bytes = payload_bound_bytes
        self._result_bound_bytes = result_bound_bytes
        self._lanes = [_Lane(index=index) for index in range(workers)]
        self._state_changed = threading.Condition(threading.Lock())
        self._owners: list[_OwnerState] = []
        self._ring = 0
        self._submitted_total = 0
        self._payload_bytes = 0
        self._result_bytes = 0
        self._closing = False
        self._draining = False
        self._pressure_at = 0.0
        self._pressured = False
        self._refuse_pid_overrun()
        self._threads = [
            threading.Thread(target=self._lane_loop, args=(lane,), name=f"research-pool-{lane.index}", daemon=True)
            for lane in self._lanes
        ]
        for thread in self._threads:
            thread.start()

    # -- registration --------------------------------------------------------

    def register(
        self,
        context: WorkerDispatchContext,
        *,
        window: int,
        epoch_of: Callable[[], int],
        on_capacity_handoff: Callable[[], None] = lambda: None,
    ) -> OwnerHandle:
        """One dispatch joins the pool; its context wires each lane's per-task relay."""
        with self._state_changed:
            if self._closing:
                raise PoolDrainingError("the pool is closed")
            if self._draining:
                raise PoolDrainingError("the pool is draining")
            manifest = context.manifest if isinstance(context.manifest, dict) else {}
            state = _OwnerState(
                context=context,
                window=window,
                epoch_of=epoch_of,
                on_capacity_handoff=on_capacity_handoff,
                manifest_paths=frozenset(manifest.keys()),
            )
            handle = OwnerHandle(self, state)
            state.handle = handle
            self._owners.append(state)
            return handle

    # -- the owner-side seams ------------------------------------------------

    def _offer(self, owner: _OwnerState, epoch: int, item: ReadyEvaluation) -> bool:
        with self._state_changed:
            if self._closing or self._draining or owner.closed:
                return False
            if self._submitted_total >= self._submission_bound:
                return False
            entry = _Entry(owner=owner, epoch=epoch, item=item)
            entry.payload_bytes = _wire_size(entry_wire(owner, item))
            if self._payload_bytes + entry.payload_bytes > self._payload_bound_bytes:
                return False
            owner.pending.append(entry)
            owner.submitted += 1
            self._submitted_total += 1
            self._payload_bytes += entry.payload_bytes
            self._schedule_locked()
            return True

    def _consume(self, owner: _OwnerState, key: str) -> None:
        with self._state_changed:
            payload, result = owner.settled.pop(key, (0, 0))
            owner.submitted = max(owner.submitted - 1, 0)
            self._submitted_total = max(self._submitted_total - 1, 0)
            self._payload_bytes = max(self._payload_bytes - payload, 0)
            self._result_bytes = max(self._result_bytes - result, 0)
            self._state_changed.notify_all()
            self._schedule_locked()

    def _settle_locked(self, entry: _Entry) -> None:
        """Return an entry's budget when no consumption ever will — stale, closed or orphaned."""
        owner = entry.owner
        owner.submitted = max(owner.submitted - 1, 0)
        self._submitted_total = max(self._submitted_total - 1, 0)
        self._payload_bytes = max(self._payload_bytes - entry.payload_bytes, 0)
        self._state_changed.notify_all()

    def _deregister(self, owner: _OwnerState) -> None:
        with self._state_changed:
            owner.closed = True
            for entry in list(owner.pending):
                owner.pending.remove(entry)
                self._settle_locked(entry)
            self._owners = [existing for existing in self._owners if existing is not owner]
            if self._owners:
                self._ring = self._ring % len(self._owners)
        self._cancel_owner_children(owner)

    # -- the scheduler --------------------------------------------------------

    def _schedule_locked(self) -> None:
        """Assign pending entries to idle lanes: ring-fair, warm-affine, work-conserving.

        Runs under the pool lock at every state change — offer, consume,
        lane idle — so no scheduler thread exists to strand. Owners rotate
        per assignment; a lone runnable owner receives every free lane.
        """
        if self._closing or self._draining or self._memory_pressure_locked():
            return
        while True:
            if not any(not lane.busy for lane in self._lanes):
                return
            runnable = [owner for owner in self._owners if not owner.closed and owner.pending]
            if not runnable:
                return
            owner = runnable[self._ring % len(runnable)]
            if len(runnable) > 1:
                self._ring = (self._ring + 1) % len(runnable)
            entry = owner.pending.popleft()
            target = self._pick_lane_locked(entry)
            target.busy = True
            target.assignments.append(entry)
            with target.signal:
                target.signal.notify_all()
            owner.in_flight += 1
            if owner.in_flight == 1:
                # Capacity handoff: this owner just went from waiting to
                # running — the renewal evidence the PRD names for queued
                # jobs, fired by the lane once the begin marker is durable.
                entry.handoff_pending = True

    def _pick_lane_locked(self, entry: _Entry) -> _Lane:
        """The warmest idle lane for this entry, lowest index breaking ties (#3048).

        Called with at least one idle lane, considering only idle lanes: an
        item never waits for a warmer lane that is busy.
        """
        idle = [candidate for candidate in self._lanes if not candidate.busy]
        return max(
            idle,
            key=lambda candidate: (candidate.residency_overlap(entry.owner.manifest_paths), -candidate.index),
        )

    # -- the lanes ------------------------------------------------------------

    def _lane_loop(self, lane: _Lane) -> None:
        while True:
            with lane.signal:
                while not lane.assignments:
                    if self._closing:
                        return
                    lane.signal.wait(timeout=_LANE_POLL_S)
                entry = lane.assignments.popleft()
                closing_now = self._closing
            if closing_now:
                # Assigned but never begun: answer through the cancellation
                # path — a live owner's evaluator unwinds and releases
                # instead of re-admitting into a pool that is closing, or
                # waiting on an arrival that will never come (#3048).
                self._answer_closing(entry)
                continue
            try:
                self._run_assignment(lane, entry)
            except BaseException:
                # A lane bug must never vanish an item silently: the entry is
                # still owed, so it is answered as a loss the evaluator can
                # re-admit, the child closes with the failure, and the lane
                # stays alive for the next assignment — every lane dying here
                # would strand the pool's owners on arrivals that never come
                # (#3066 review, carried from the per-dispatch pool).
                logger.exception("a research pool lane failed; closing its worker")
                worker = lane.worker
                if worker is not None:
                    worker.close()
                    lane.worker = None
                self._deliver(lane, entry, WorkerLost(f"pool lane {lane.index} failed; its worker is closed"))
            with self._state_changed:
                lane.busy = False
                self._state_changed.notify_all()
                self._schedule_locked()

    def _answer_closing(self, entry: _Entry) -> None:
        """Settle an entry the closing pool will never run and cancel it home (#3048 AC 10).

        A drain interrupts owned work through the cancellation path — the
        evaluator unwinds, releases what it reserved, and the operator's
        capacity change leaves honest records, never a hung dispatch.
        """
        from app.jobs.progress import JobCancelled

        with self._state_changed:
            owner = entry.owner
            owner.in_flight = max(owner.in_flight - 1, 0)
            self._settle_locked(entry)
            closed = owner.closed
        handle = owner.handle
        if not closed and handle is not None:
            handle.arrived.put(
                (entry.item.key, entry.item.task_gen, JobCancelled("the research worker pool is closing; retry the dispatch"))
            )

    def _run_assignment(self, lane: _Lane, entry: _Entry) -> None:
        owner = entry.owner
        if owner.closed or entry.epoch != owner.epoch_of():
            # A superseded dispatch's leftover: nobody owes this run.
            with self._state_changed:
                owner.in_flight = max(owner.in_flight - 1, 0)
                self._settle_locked(entry)
            return
        worker = self._slot_worker(lane)
        if worker is None:
            if self._closing:
                self._answer_closing(entry)
                return
            try:
                worker = self._spawn(owner.context)
            except WorkerLost as exc:
                self._deliver(lane, entry, WorkerLost(f"the research worker failed to start: {exc}"))
                return
            except IdentityRefusalError as exc:
                # A refused identity is terminal for this owner, never a
                # retryable loss (#3065 review).
                self._deliver(lane, entry, exc)
                return
            with self._state_changed:
                runaway = self._closing
                if not runaway:
                    lane.worker = worker
            if runaway:
                # Startup is invisible to close()'s snapshot, so this lane
                # owns killing the child it just grew (#3066 review) — and
                # the cancellation is answered outside the pool lock, which
                # the guard above still holds.
                worker.close()
                self._answer_closing(entry)
                return
        lane.current = entry
        worker.begin_task(cancel_check=owner.context.cancel_check, on_checkpoint=owner.context.on_checkpoint)
        try:
            worker_task = entry_wire(owner, entry.item)
            entry.item.begin()
            if entry.handoff_pending:
                entry.handoff_pending = False
                owner.on_capacity_handoff()
            message = worker.run_task(worker_task)
        except WorkerLost:
            worker.close()
            with self._state_changed:
                if lane.worker is worker:
                    lane.worker = None
            self._deliver(lane, entry, WorkerLost("a pooled research worker died with a task in flight"))
            return
        except BaseException as exc:
            # A store fence (a refused begin, a superseded attempt) is an
            # honest terminal answer, not infrastructure loss: deliver it
            # and let the stage's handlers name it.
            if isinstance(exc, (JobCancelled, GenerationFencedError, IdentityRefusalError)):
                with self._state_changed:
                    if lane.worker is worker:
                        lane.worker = None
            self._deliver(lane, entry, exc)
            return
        finally:
            lane.current = None
        self._record_residency(lane, message)
        self._deliver(lane, entry, delivered_outcome(entry.item, message))

    def _slot_worker(self, lane: _Lane) -> ResearchWorker | None:
        """This lane's live child, or ``None`` once a between-tasks death is cleaned up (#3065 review)."""
        with self._state_changed:
            worker = lane.worker
            if worker is not None and worker.lost_reason() is not None:
                worker.close()
                lane.worker = None
                return None
            return worker

    def _record_residency(self, lane: _Lane, message: dict[str, Any]) -> None:
        """Fold a child's reported decode delta into this lane's warm hints (#3048)."""
        for artifact in message.get("decoded") or []:
            path = artifact.get("relative_path")
            if path:
                lane.resident[path] = int(artifact.get("estimated_bytes", 0))
        lane.cache_snapshot.update(
            {name: value for name, value in (message.get("cache") or {}).items() if isinstance(value, int)}
        )

    def _deliver(self, lane: _Lane, entry: _Entry, arrival: DeliveredOutcome | BaseException) -> None:
        """Deliver one outcome to its owner under the result-byte bound's backpressure.

        A closed owner has no consumer left, so the pool settles the
        entry's budget here instead of waiting for a consumption that will
        never come. A closing pool still delivers — the owner's generator
        is the waiter these losses answer — but skips the byte accounting:
        shutdown never blocks on a bound.
        """
        result_bytes = self._arrival_size(arrival)
        with self._state_changed:
            owner = entry.owner
            owner.in_flight = max(owner.in_flight - 1, 0)
            while not self._closing and self._result_bytes + result_bytes > self._result_bound_bytes:
                self._state_changed.wait(timeout=_LANE_POLL_S)
            if owner.closed:
                self._settle_locked(entry)
                return
            if not self._closing:
                self._result_bytes += result_bytes
                owner.settled[entry.item.key] = (entry.payload_bytes, result_bytes)
        handle = owner.handle
        if handle is not None:
            handle.arrived.put((entry.item.key, entry.item.task_gen, arrival))

    @staticmethod
    def _arrival_size(arrival: DeliveredOutcome | BaseException) -> int:
        try:
            if isinstance(arrival, DeliveredOutcome):
                return len(json.dumps(arrival.__dict__, default=str, separators=(",", ":")))
            return 1024
        except (TypeError, ValueError):
            return 4096

    # -- cancellation and teardown --------------------------------------------

    def _cancel_owner_children(self, owner: _OwnerState) -> None:
        """Stop only the children running this owner's items; its queued entries die at take time."""
        with self._state_changed:
            running = [lane for lane in self._lanes if lane.current is not None and lane.current.owner is owner]
        for lane in running:
            worker = lane.worker
            if worker is None:
                continue
            stopper = threading.Thread(target=worker.cancel, daemon=True)
            stopper.start()
            stopper.join(timeout=5.0)

    def drain(self, *, wait_s: float = 60.0) -> dict[str, Any]:
        """The operator's capacity handoff (#3048 AC 10): stop assignments, finish or interrupt, reap.

        New owners are refused with :class:`PoolDrainingError` from the
        moment draining begins. In-flight items get ``wait_s`` to complete;
        what cannot is left to its owner's own cancellation paths (the
        caller may cancel the owning jobs). The pool ends closed — the
        capacity change itself is a restart at the new profile.
        """
        with self._state_changed:
            if self._closing:
                return self.facts()
            self._draining = True
            deadline = time.monotonic() + wait_s
            while time.monotonic() < deadline:
                # Assigned-but-unpopped entries are in flight too: closing
                # ahead of the lane's wakeup would silently vanish them.
                if all(lane.current is None and not lane.assignments for lane in self._lanes):
                    break
                self._state_changed.wait(timeout=_LANE_POLL_S)
        self.close()
        return self.facts()

    def close(self) -> None:
        """Stop the pool: closing flag first, every child killed and reaped, lanes out within one poll.

        Entries still queued on an owner — offered, never assigned — are
        cancelled home the same way the lanes answer assigned-but-unbegun
        ones: a live owner unwinds through its cancellation path instead of
        waiting on arrivals a closed pool will never produce.
        """
        with self._state_changed:
            if self._closing:
                return
            self._closing = True
            self._state_changed.notify_all()
            workers = [lane.worker for lane in self._lanes if lane.worker is not None]
            for lane in self._lanes:
                lane.worker = None
            orphaned = [entry for owner in self._owners for entry in owner.pending]
            for owner in self._owners:
                owner.pending.clear()
            for entry in orphaned:
                self._settle_locked(entry)
        for entry in orphaned:
            handle = entry.owner.handle
            if not entry.owner.closed and handle is not None:
                from app.jobs.progress import JobCancelled

                handle.arrived.put(
                    (entry.item.key, entry.item.task_gen, JobCancelled("the research worker pool is closing; retry the dispatch"))
                )
        for lane in self._lanes:
            with lane.signal:
                lane.signal.notify_all()
        for worker in workers:
            worker.close()
        for thread in self._threads:
            thread.join(timeout=10.0)

    # -- evidence -------------------------------------------------------------

    def facts(self) -> dict[str, Any]:
        """The scheduling and resource facts the progress surface and qualification report (#3048)."""
        with self._state_changed:
            resident_counts: dict[str, int] = {}
            for lane in self._lanes:
                for path in lane.resident:
                    resident_counts[path] = resident_counts.get(path, 0) + 1
            duplicate = sum(count - 1 for count in resident_counts.values() if count > 1)
            return {
                "workers": self._worker_count,
                "owners": len(self._owners),
                "busy_lanes": sum(1 for lane in self._lanes if lane.busy),
                "queued": sum(len(owner.pending) for owner in self._owners),
                "submitted_unconsumed": self._submitted_total,
                "submission_bound": self._submission_bound,
                "payload_bytes": self._payload_bytes,
                "result_bytes": self._result_bytes,
                "resident_paths": len(resident_counts),
                "duplicate_resident_paths": duplicate,
                "memory_pressured": self._pressured,
                "draining": self._draining,
                "closing": self._closing,
            }

    # -- guards ---------------------------------------------------------------

    def _refuse_pid_overrun(self) -> None:
        """Refuse a lane count whose task estimate cannot fit 80 % of the cgroup's PID cap (#3048 AC 7)."""
        raw = self._cgroup_file("pids.max")
        try:
            limit = int((raw or "").strip())
        except ValueError:
            return  # "max" or absent: no positive evidence of a cap — deployment owns the envelope
        estimate = _BASE_TASKS + self._worker_count * _TASKS_PER_LANE
        if estimate > limit * 0.8:
            raise RuntimeError(
                f"the configured research pool ({self._worker_count} lanes, ~{estimate} tasks including imports) "
                f"cannot fit inside 80 % of the cgroup's {limit}-task PID cap; refusing to start"
            )

    def _memory_pressure_locked(self) -> bool:
        """Aggregate memory pressure from the runtime's own cgroup, probed at most every few seconds.

        The cgroup already counts everything the issue's aggregate must:
        children and their retained or pinned caches, active simulations,
        imports, buffers and the coordinator's own state. Pressure pauses
        new assignments — in-flight work completes or fails visibly, never
        silently omitted.
        """
        now = time.monotonic()
        if now - self._pressure_at < _PRESSURE_PROBE_S:
            return self._pressured
        self._pressure_at = now
        current = self._cgroup_bytes("memory.current")
        maximum = self._cgroup_bytes("memory.max")
        if current is None or maximum is None or maximum <= 0:
            self._pressured = False
            return False
        pressured = current > maximum * 0.9
        if pressured and not self._pressured:
            logger.warning(
                "research pool memory pressure: pausing new assignments",
                extra={"action": "research_pool_memory_pressure", "current_bytes": current, "max_bytes": maximum},
            )
        self._pressured = pressured
        return pressured

    @staticmethod
    def _cgroup_file(name: str) -> str | None:
        try:
            with open(f"/sys/fs/cgroup/{name}", encoding="ascii") as handle:
                return handle.read()
        except OSError:
            return None

    @classmethod
    def _cgroup_bytes(cls, name: str) -> int | None:
        raw = cls._cgroup_file(name)
        if raw is None:
            return None
        try:
            return int(raw.strip())
        except ValueError:
            return None


# The wire an owner's item carries to whichever lane runs it: the child is
# generic, so the wire — not the spawn — binds strategy, execution, manifest
# and sealed identity to the run (#3048).


def entry_wire(owner: _OwnerState, item: ReadyEvaluation) -> dict[str, Any]:
    context = owner.context
    return task_wire(item, strategy_key=context.strategy_key, execution=context.execution,
                     manifest=context.manifest, identity=context.identity)


# The process-lifetime singleton the runtime's dispatch factory binds to
# (#3048): one pool per process, created at the first factory build, closed
# by the lifespan (and by the operator's drain route).
_SHARED_POOL: SharedWorkerPool | None = None
_SHARED_POOL_LOCK = threading.Lock()


def shared_worker_pool(
    *,
    workers: int,
    submission_bound: int,
    payload_bound_bytes: int,
    result_bound_bytes: int,
) -> SharedWorkerPool:
    """The process's one shared pool; a second configuration is a capacity change, which requires drain."""
    global _SHARED_POOL
    with _SHARED_POOL_LOCK:
        if _SHARED_POOL is None:
            _SHARED_POOL = SharedWorkerPool(
                workers=workers,
                submission_bound=submission_bound,
                payload_bound_bytes=payload_bound_bytes,
                result_bound_bytes=result_bound_bytes,
            )
        elif _SHARED_POOL._worker_count != workers:
            raise RuntimeError(
                f"the shared research pool is already serving at {_SHARED_POOL._worker_count} lanes; "
                f"changing to {workers} is a capacity change — drain and restart (#3048)"
            )
        return _SHARED_POOL


def close_shared_pool() -> dict[str, Any] | None:
    """Close the process's shared pool at shutdown; returns its final facts when it existed."""
    global _SHARED_POOL
    with _SHARED_POOL_LOCK:
        pool, _SHARED_POOL = _SHARED_POOL, None
    if pool is None:
        return None
    facts = pool.facts()
    pool.close()
    return facts


def drain_shared_pool(*, wait_s: float = 60.0) -> dict[str, Any] | None:
    """The operator's drain of the process's shared pool (#3048 AC 10).

    Stops new assignments, waits bounded for owned work to complete,
    interrupts what cannot, reaps the children, and clears the singleton —
    the capacity change (a restart at the new profile) finds no orphans
    and no running pool.
    """
    global _SHARED_POOL
    with _SHARED_POOL_LOCK:
        pool, _SHARED_POOL = _SHARED_POOL, None
    if pool is None:
        return None
    return pool.drain(wait_s=wait_s)
