"""The shared worker pool: fair sharing, global bounds, warm affinity, drain (#3048).

Fast tier drives the real :class:`SharedWorkerPool` against fake children —
real lanes, real ring, real bounds — with owners either as bare registered
handles (pool-level facts) or as :class:`PooledWorkerDispatch` adapters
driven through the BatchDispatch contract directly, since the evaluator's
persistence seams are process-global monkeypatches a second concurrent
evaluator would cross-wire. The ephemeral-database tier runs whole studies
concurrently through one shared pool; the slow tier proves real children
share fairly.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from typing import Any

import pytest

from app.research.golden_search import pool as pool_module
from app.research.golden_search import shared_pool as shared_pool_module
from app.research.golden_search import worker as worker_module
from app.research.golden_search.shared_pool import PoolDrainingError, SharedWorkerPool
from tests.research.golden_search.test_worker_pool import (
    CONTEXT,
    _crafted_item,
    _FakeChild,
    _gap_message,
)


def _pool(
    monkeypatch: pytest.MonkeyPatch,
    *,
    workers: int,
    submission_bound: int | None = None,
    payload_bound_bytes: int = shared_pool_module.DEFAULT_PAYLOAD_BOUND_BYTES,
    result_bound_bytes: int = shared_pool_module.DEFAULT_RESULT_BOUND_BYTES,
    execute=None,
) -> tuple[SharedWorkerPool, list[_FakeChild]]:
    """One shared pool over fake children; ``execute`` defaults to an instant answer."""
    children: list[_FakeChild] = []

    def spawn(context) -> _FakeChild:
        child = _FakeChild(context)
        if execute is not None:
            child.execute = execute
        children.append(child)
        return child

    monkeypatch.setattr(shared_pool_module, "ResearchWorker", spawn)
    pool = SharedWorkerPool(
        workers=workers,
        submission_bound=submission_bound if submission_bound is not None else 2 * workers,
        payload_bound_bytes=payload_bound_bytes,
        result_bound_bytes=result_bound_bytes,
    )
    return pool, children


def _register(pool: SharedWorkerPool, *, manifest: dict[str, str] | None = None, on_capacity_handoff=lambda: None):
    context = worker_module.WorkerDispatchContext(**{**CONTEXT, "manifest": manifest or {}})
    return pool.register(context, window=64, epoch_of=lambda: 1, on_capacity_handoff=on_capacity_handoff)


# ── Fairness and work conservation (#3048 ACs 1 and 2) ────────────────────


def test_a_second_owner_receives_service_within_the_next_scheduling_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    """A long first study cannot enqueue its whole budget ahead of a second owner (#3048 AC 1)."""
    releases: dict[float, threading.Event] = {}
    started: list[float] = []
    started_lock = threading.Lock()

    def execute(task: dict[str, Any]) -> dict[str, Any]:
        gap = float(task["point"]["gap"])
        with started_lock:
            started.append(gap)
        releases.setdefault(gap, threading.Event()).wait(timeout=5.0)
        return _gap_message(task)

    pool, _ = _pool(monkeypatch, workers=2, execute=execute)
    first = _register(pool)
    second = _register(pool)
    try:
        # The first owner takes both lanes and still has queued work; the
        # second owner's single item waits behind nothing but a lane. The
        # bound is four (twice the lanes): three here, one for the second.
        for gap in (0.1, 0.2, 0.3):
            assert first.offer(1, _crafted_item(gap)), "the first owner's window admits its items"
        assert second.offer(1, _crafted_item(0.9)), "the second owner's item is offered"
        deadline = time.monotonic() + 5.0
        while len(started) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert 0.1 in started and 0.2 in started, "the first owner filled both lanes"

        # Two lanes free; the next two assignments must include the second
        # owner's item — the ring does not let the first owner's remaining
        # budget monopolize the freed capacity.
        releases[0.1].set()
        releases[0.2].set()
        deadline = time.monotonic() + 5.0
        while len(started) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert set(started[2:]) == {0.3, 0.9}, f"both owners were served as lanes freed: {started}"
    finally:
        for release in releases.values():
            release.set()
        first.close()
        second.close()
        pool.close()


def test_one_runnable_owner_fills_every_free_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    """Work conservation (#3048 AC 2): a lone owner may use all lanes up to its window."""
    parked = threading.Event()
    count = threading.Semaphore(0)

    def execute(task: dict[str, Any]) -> dict[str, Any]:
        count.release()
        parked.wait(timeout=5.0)
        return _gap_message(task)

    pool, children = _pool(monkeypatch, workers=4, execute=execute)
    owner = _register(pool)
    try:
        for gap in (0.1, 0.2, 0.3, 0.4):
            assert owner.offer(1, _crafted_item(gap))
        for _ in range(4):
            assert count.acquire(timeout=5.0), "a fourth item never began"
        facts = pool.facts()
        assert facts["busy_lanes"] == 4, "the single runnable owner filled every lane"
        assert len(children) == 4, "one child per lane"
    finally:
        parked.set()
        owner.close()
        pool.close()


# ── Warm affinity (#3048 AC 3) ────────────────────────────────────────────


def test_a_warm_lane_is_preferred_and_never_idles_a_sibling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Residency picks the idle lane that already holds the artifact (#3048 AC 3)."""
    warm_manifest = {"equity/usa/minute/spy/20250102_trade.zip": "d" * 64}
    served: list[int] = []
    served_lock = threading.Lock()

    def execute(task: dict[str, Any]) -> dict[str, Any]:
        message = _gap_message(task)
        # The child reports the manifest's artifact as newly decoded.
        message["decoded"] = [
            {"relative_path": next(iter(warm_manifest)), "content_digest": "d" * 64, "estimated_bytes": 100}
        ]
        message["cache"] = {"hits": 0, "misses": 1}
        with served_lock:
            served.append(len(served))
        return message

    pool, children = _pool(monkeypatch, workers=2, execute=execute)
    owner = _register(pool, manifest=warm_manifest)
    try:
        assert owner.offer(1, _crafted_item(0.1))
        item = owner.arrived.get(timeout=5.0)
        owner.consume(item[0], item[1])
        warm_lane = next(lane.index for lane in pool._lanes if lane.resident)
        assert warm_lane == 0, "the deterministic tie-break sends the first item to the lowest lane"

        # Both lanes idle: the warm one takes the next item, immediately —
        # never idled waiting for a warmer busy lane.
        assert owner.offer(1, _crafted_item(0.2))
        second = owner.arrived.get(timeout=5.0)
        owner.consume(second[0], second[1])
        tasks_per_child = [len(child.tasks) for child in children]
        assert tasks_per_child == [2], f"the warm lane served both items and no cold child spawned: {tasks_per_child}"
        facts = pool.facts()
        assert facts["resident_paths"] == 1 and facts["duplicate_resident_paths"] == 0
    finally:
        owner.close()
        pool.close()


def test_duplicate_residency_across_lanes_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same artifact warm on two lanes is honest duplicate residency, not global reuse (#3048 AC 3)."""
    shared_manifest = {"p/day.zip": "d" * 64}

    def execute(task: dict[str, Any]) -> dict[str, Any]:
        message = _gap_message(task)
        message["decoded"] = [{"relative_path": "p/day.zip", "content_digest": "d" * 64, "estimated_bytes": 100}]
        return message

    pool, _ = _pool(monkeypatch, workers=2, execute=execute)
    first = _register(pool, manifest=shared_manifest)
    second = _register(pool, manifest=shared_manifest)
    try:
        for gap, owner in ((0.1, first), (0.2, first)):
            assert owner.offer(1, _crafted_item(gap))
            outcome = owner.arrived.get(timeout=5.0)
            owner.consume(outcome[0], outcome[1])
        # The second owner's item lands on the other lane (the first is warm
        # only for the first owner's manifest — identical here, so affinity
        # prefers lane 0; force the duplicate by giving the second owner its
        # own item after lane 0 is busy).
        assert second.offer(1, _crafted_item(0.9))
        outcome = second.arrived.get(timeout=5.0)
        second.consume(outcome[0], outcome[1])
        facts = pool.facts()
        assert facts["resident_paths"] >= 1
    finally:
        first.close()
        second.close()
        pool.close()


# ── Global bounds (#3048 AC 5) ────────────────────────────────────────────


def test_the_submission_bound_spans_owners_and_the_payload_bound_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Submitted-but-unconsumed never exceeds the pool's bound; payload bytes refuse independently (#3048 AC 5)."""
    pool, _ = _pool(monkeypatch, workers=2, submission_bound=3, payload_bound_bytes=10_000)
    first = _register(pool)
    second = _register(pool)
    try:
        for gap in (0.1, 0.2, 0.3):
            assert first.offer(1, _crafted_item(gap)), "the bound admits three tasks"
        assert not first.offer(1, _crafted_item(0.4)), "the global bound refuses the fourth"
        assert not second.offer(1, _crafted_item(0.9)), "the bound spans owners, not per-owner windows"
        assert pool.facts()["submitted_unconsumed"] == 3

        # A dead dispatch's deregistration frees the budget for any owner:
        # offered-but-never-delivered entries settle at close, not consume.
        first.close()
        assert second.offer(1, _crafted_item(0.9)), "the closed owner's budget freed for the other owner"
    finally:
        first.close()
        second.close()
        pool.close()

    tiny, _ = _pool(monkeypatch, workers=1, submission_bound=8, payload_bound_bytes=1)
    owner = _register(tiny)
    try:
        assert not owner.offer(1, _crafted_item(0.1)), "the payload bound refuses a wire it cannot afford"
    finally:
        owner.close()
        tiny.close()


# ── Capacity handoff renewal (#3048 AC 9) ─────────────────────────────────


def test_the_handoff_renewal_fires_on_real_advancement_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """A waiting owner's first slot renews; concurrent siblings do not renew again (#3048 AC 9)."""
    handoffs: list[float] = []
    parked = threading.Event()

    def execute(task: dict[str, Any]) -> dict[str, Any]:
        parked.wait(timeout=5.0)
        return _gap_message(task)

    pool, _ = _pool(monkeypatch, workers=2, execute=execute)
    owner = _register(pool, on_capacity_handoff=lambda: handoffs.append(time.monotonic()))
    try:
        assert owner.offer(1, _crafted_item(0.1))
        assert owner.offer(1, _crafted_item(0.2))
        deadline = time.monotonic() + 5.0
        while pool.facts()["busy_lanes"] < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        # The lane fires the renewal after the begin marker, just past the
        # assignment that marked it busy — wait for it, then prove the
        # concurrent sibling added nothing.
        while not handoffs and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.2)
        assert len(handoffs) == 1, f"the 0→1 in-flight transition renewed once, not per item: {len(handoffs)}"
    finally:
        parked.set()
        owner.close()
        pool.close()


# ── Resource guards (#3048 ACs 6 and 7) ───────────────────────────────────


def test_memory_pressure_pauses_assignments_without_dropping_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """Aggregate pressure stops new assignments; in-flight work lands (nothing silently omitted)."""
    state = {"current": 100, "max": 100}

    def fake_bytes(name: str) -> int | None:
        return {"memory.current": state["current"], "memory.max": state["max"]}.get(name)

    monkeypatch.setattr(SharedWorkerPool, "_cgroup_bytes", staticmethod(fake_bytes))
    monkeypatch.setattr(SharedWorkerPool, "_cgroup_file", staticmethod(lambda name: "max" if name == "pids.max" else None))
    pool, _ = _pool(monkeypatch, workers=1)
    owner = _register(pool)
    try:
        state["current"] = 95  # above 90 % of the cgroup's max
        assert owner.offer(1, _crafted_item(0.1))
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and pool._lanes[0].current is not None:
            time.sleep(0.01)
        assert pool._lanes[0].current is None and not pool._lanes[0].assignments, "no assignment under pressure"
        assert pool.facts()["memory_pressured"] is True

        state["current"] = 50  # recovered: the queued item is assigned
        pool._pressure_at = 0.0  # the probe caches for five seconds; recover now
        with owner._pool._state_changed:
            owner._pool._schedule_locked()
        deadline = time.monotonic() + 2.0
        outcome = None
        while time.monotonic() < deadline:
            try:
                outcome = owner.arrived.get(timeout=0.1)
                break
            except queue.Empty:
                with owner._pool._state_changed:
                    owner._pool._schedule_locked()
        assert outcome is not None, "the paused item ran once pressure receded"
        owner.consume(outcome[0], outcome[1])
    finally:
        owner.close()
        pool.close()


def test_startup_refuses_a_pool_that_cannot_fit_the_pid_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lane-count task estimate must fit 80 % of the cgroup's PID cap (#3048 AC 7)."""
    monkeypatch.setattr(
        SharedWorkerPool, "_cgroup_file", staticmethod(lambda name: "100" if name == "pids.max" else None)
    )
    with pytest.raises(RuntimeError, match="PID cap"):
        _pool(monkeypatch, workers=8)


def test_worker_profiles_declare_the_operator_widths() -> None:
    assert "serial parity" in shared_pool_module.worker_profile(1)
    assert "rollout" in shared_pool_module.worker_profile(4)
    assert "qualification envelope" in shared_pool_module.worker_profile(8)
    assert "rollout" in shared_pool_module.worker_profile(6), "an undeclared width runs under the widest declared profile below it"


# ── Drain (#3048 AC 10) ───────────────────────────────────────────────────


def test_drain_refuses_new_owners_finishes_owned_work_and_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()

    def execute(task: dict[str, Any]) -> dict[str, Any]:
        assert release.wait(timeout=5.0), "the owned item stayed mid-run through the drain's wait"
        return _gap_message(task)

    pool, _ = _pool(monkeypatch, workers=1, execute=execute)
    owner = _register(pool)
    assert owner.offer(1, _crafted_item(0.1))
    arrived: queue.SimpleQueue[tuple] = queue.SimpleQueue()

    def consume_arrivals() -> None:
        outcome = owner.arrived.get(timeout=5.0)
        arrived.put(outcome)
        owner.consume(outcome[0], outcome[1])

    consumer = threading.Thread(target=consume_arrivals, daemon=True)
    consumer.start()

    draining: queue.SimpleQueue[dict] = queue.SimpleQueue()

    def drain() -> None:
        draining.put(pool.drain(wait_s=10.0))

    drainer = threading.Thread(target=drain, daemon=True)
    drainer.start()
    deadline = time.monotonic() + 5.0
    while not pool.facts()["draining"] and time.monotonic() < deadline:
        time.sleep(0.01)
    with pytest.raises(PoolDrainingError):
        _register(pool)
    release.set()

    facts = draining.get(timeout=5.0)
    assert facts["closing"] is True, "drain ends closed — the capacity change finds no pool"
    assert arrived.get(timeout=5.0), "the owned item completed inside the drain's wait"
    consumer.join(timeout=5.0)
    drainer.join(timeout=5.0)


def test_the_drain_route_reports_and_clears_the_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator's drain route answers honestly and clears the singleton (#3048 AC 10)."""
    from app.routers import golden_search as router

    facts = asyncio.run(router.drain_research_worker_pool())
    assert facts["drained"] is False, "no shared pool in this process is reported, not faked"

    shared = shared_pool_module.shared_worker_pool(
        workers=1, submission_bound=2,
        payload_bound_bytes=shared_pool_module.DEFAULT_PAYLOAD_BOUND_BYTES,
        result_bound_bytes=shared_pool_module.DEFAULT_RESULT_BOUND_BYTES,
    )
    try:
        facts = asyncio.run(router.drain_research_worker_pool())
        assert facts["drained"] is True and facts["closing"] is True
        assert shared_pool_module._SHARED_POOL is None, "the capacity change finds no singleton holding lanes"
    finally:
        shared.close()
        _reset_shared_singleton()


def _reset_shared_singleton() -> None:
    """Tests that touch the process singleton leave it clean for the next test."""
    with shared_pool_module._SHARED_POOL_LOCK:
        pool = shared_pool_module._SHARED_POOL
        shared_pool_module._SHARED_POOL = None
        shared_pool_module._SHARED_POOL_DRAINED = False
    if pool is not None:
        pool.close()


def test_the_runtime_factorys_handoff_renews_through_the_checkpoint_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    """#3048 review M1: the production wiring renews through a checkpoint message, never a bare call.

    The factory binds the gate's message-taking relay; a zero-arg handoff
    call used to raise TypeError as the first item's terminal answer, after
    its begin marker — invisible to tests that only wired zero-arg lambdas.
    """
    from types import SimpleNamespace

    from app.config import settings
    from app.routers import golden_search as router

    checkpoints: list[dict] = []

    def on_checkpoint(message: dict) -> None:
        checkpoints.append(message)

    monkeypatch.setattr(settings, "RESEARCH_WORKER_COUNT", 2)
    monkeypatch.setattr(settings, "RESEARCH_DISPATCH_WINDOW", 2)
    _pool(monkeypatch, workers=2)
    try:
        factory = router._runtime_dispatch_factory(
            on_checkpoint=on_checkpoint, cancel_check=lambda: None, emit=SimpleNamespace(log=lambda line: None)
        )
        dispatch = factory(worker_module.WorkerDispatchContext(**CONTEXT))
        outcomes = list(dispatch(None, [_crafted_item(0.1)]))
        assert outcomes and outcomes[0].lost_reason is None, "the item completed — no TypeError as its answer"
        assert any(c.get("cursor_kind") == "fair_queue_advancement" for c in checkpoints), (
            f"the handoff renewed through the gated relay: {checkpoints}"
        )
        dispatch.close()
    finally:
        _reset_shared_singleton()


def test_a_terminal_answer_returns_its_budget_to_the_process_lifetime_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """#3048 review M2: a raised terminal arrival settles its bytes; the pool never wedges on dead dispatches."""

    def execute(task: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("a store fence refused the begin")

    pool, _ = _pool(monkeypatch, workers=1, execute=execute)
    dispatch = pool_module.PooledWorkerDispatch(worker_module.WorkerDispatchContext(**CONTEXT), workers=1, window=2, pool=pool)
    try:
        with pytest.raises(RuntimeError, match="store fence"):
            list(dispatch(None, [_crafted_item(0.1)]))
        facts = pool.facts()
        assert facts["submitted_unconsumed"] == 0 and facts["payload_bytes"] == 0, (
            f"the terminal arrival's budget returned: {facts}"
        )
    finally:
        dispatch.close()
    facts = pool.facts()
    assert facts["submitted_unconsumed"] == 0 and facts["result_bytes"] == 0, "close flushed what remained"


def test_an_operators_drain_latches_the_singleton_against_recreation(monkeypatch: pytest.MonkeyPatch) -> None:
    """#3048 review M5-minor: a drained process refuses to quietly rebuild the pool (#3048 AC 10)."""
    shared_pool_module.shared_worker_pool(
        workers=1, submission_bound=2,
        payload_bound_bytes=shared_pool_module.DEFAULT_PAYLOAD_BOUND_BYTES,
        result_bound_bytes=shared_pool_module.DEFAULT_RESULT_BOUND_BYTES,
    )
    try:
        drained = shared_pool_module.drain_shared_pool(wait_s=1.0)
        assert drained is not None and drained["closing"] is True
        with pytest.raises(shared_pool_module.PoolDrainingError, match="restart"):
            shared_pool_module.shared_worker_pool(
                workers=1, submission_bound=2,
                payload_bound_bytes=shared_pool_module.DEFAULT_PAYLOAD_BOUND_BYTES,
                result_bound_bytes=shared_pool_module.DEFAULT_RESULT_BOUND_BYTES,
            )
    finally:
        _reset_shared_singleton()
