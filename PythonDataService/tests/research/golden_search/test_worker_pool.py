"""The bounded research worker pool: overlap, order independence, and #3045's recovery under width (#3046).

Fast tier drives the real :class:`PooledWorkerDispatch` against fake
``ResearchWorker`` children — real lanes, real window bound, real straggler,
cancellation and loss paths, no processes. The ephemeral-database tier runs
whole studies through the pool with deterministic fake children (the serial
reference's own landscape) and proves the books, the search path and the
exam's detail evidence are unchanged by width, including a lane loss. The
slow tier proves two real spawned children genuinely overlap independent
evaluations.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import queue
import random
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from app.research.golden_search import pool as pool_module
from app.research.golden_search import service
from app.research.golden_search import shared_pool as shared_pool_module
from app.research.golden_search.evaluator import WorkerLost
from app.research.golden_search.selection import Metrics
from app.research.golden_search.worker import WorkerDispatchContext
from app.research.persistence import lifecycle
from tests._helpers.golden_search_study import (
    Driver,
    fake_detail,
    seed_lake,
    smooth_score,
    unique_symbol,
)
from tests.research.golden_search.test_evaluator import (
    _EngineSpy,
    _key_of,
    _metrics_by_gap,
    _point,
    _ReservationBridge,
    _seamed_evaluator,
)
from tests.research.golden_search.test_worker_loss_recovery import JAN

logger = logging.getLogger(__name__)

CONTEXT = dict(
    strategy_key="ema_crossover_signal",
    execution={"fill_mode": "next_bar_open", "commission_per_order": 0.0, "slippage_per_share": 0.0, "initial_cash": 100_000.0},
    manifest={},
    identity={"source_digest": "s" * 64, "environment_digest": "e" * 64, "digest_scheme": 2},
    job_id="job-pool-test",
    generation=1,
)


# ── A fake child for the real pool (fast tier) ────────────────────────────


class _FakeChild:
    """A ``ResearchWorker`` stand-in whose task execution is one configurable callable.

    Speaks exactly the parent-side protocol the pool uses: ``run_task``
    returns the child's result/failed message (or raises ``WorkerLost``),
    ``lost_reason`` reports a between-tasks death, ``cancel``/``close`` are
    bookkeeping.
    """

    def __init__(self, context: WorkerDispatchContext) -> None:
        self.context = context
        self.tasks: list[dict[str, Any]] = []
        self.closed = False
        self.lost: str | None = None
        self.execute: Callable[[dict[str, Any]], dict[str, Any]] = lambda task: _gap_message(task)
        self.relays: list[tuple[object, object]] = []

    def begin_task(self, *, cancel_check=None, on_checkpoint=None) -> None:
        # The shared lane points the child's cancellation poll and checkpoint
        # relay at each item's owner before it runs (#3048).
        self.relays.append((cancel_check, on_checkpoint))

    def run_task(self, task: dict[str, Any]) -> dict[str, Any]:
        self.tasks.append(task)
        return self.execute(task)

    def lost_reason(self) -> str | None:
        return self.lost

    def cancel(self) -> None:
        self.closed = True

    def close(self) -> None:
        self.closed = True


def _crafted_item(gap: float, *, key: str = "k" * 64, task_gen: int = 1):
    """A ready evaluation whose start marker is neutralized, for driving the dispatch contract directly.

    The evaluator's persistence seams are process-global monkeypatches, so
    a second owner on another thread exercises its dispatch — not a second
    evaluator — when a test needs concurrent owners.
    """
    from datetime import date as date_of

    from app.research.golden_search.evaluator import EvaluationRequest
    from app.research.golden_search.evaluator import ReadyEvaluation as _ReadyEvaluation

    class _Markerless(_ReadyEvaluation):
        def begin(self) -> None:
            pass

    return _Markerless(
        key=key,
        attempt=1,
        task_gen=task_gen,
        request=EvaluationRequest(
            point={"symbol": "SPY", "gap": gap},
            point_hash="p" * 64,
            window=JAN,
            warmup_from=date_of(2024, 12, 30),
            scenario=None,
            detail=False,
            stage="search",
            fold_index=None,
        ),
    )


def _gap_message(task: dict[str, Any]) -> dict[str, Any]:
    """The spy engine's landscape as a child result message: metrics keyed by the point's gap."""
    return {
        "type": "result",
        "key": task["key"],
        "metrics": _metrics_by_gap(float(task["point"]["gap"])).as_dict(),
        "detail": None,
        "curve": None,
    }


class _Intervals:
    """Per-item execution intervals, for overlap and window assertions."""

    def __init__(self) -> None:
        self.begun: dict[str, float] = {}
        self.ended: dict[str, float] = {}
        self._lock = threading.Lock()

    def start(self, key: str) -> None:
        with self._lock:
            self.begun[key] = time.monotonic()

    def finish(self, key: str) -> None:
        with self._lock:
            self.ended[key] = time.monotonic()

    def max_in_flight(self) -> int:
        points = sorted([(start, 1) for start in self.begun.values()] + [(end, -1) for end in self.ended.values()])
        active = peak = 0
        for _, delta in points:
            active += delta
            peak = max(peak, active)
        return peak

    def pairs_overlapping(self) -> int:
        count = 0
        for first in self.begun:
            for second in self.begun:
                if first < second and self.begun[first] < self.ended[second] and self.begun[second] < self.ended[first]:
                    count += 1
        return count


def _wire_pool(
    monkeypatch: pytest.MonkeyPatch,
    *,
    workers: int,
    window: int,
    execute: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    on_facts: Callable[[pool_module.DispatchFacts], None] = lambda facts: None,
    pool: shared_pool_module.SharedWorkerPool | None = None,
) -> tuple[pool_module.PooledWorkerDispatch, list[_FakeChild], _Intervals]:
    """Spawn the real pool over fake children; every child runs ``execute`` under interval tracking.

    ``pool`` binds this dispatch as a sibling owner on an existing shared
    pool (#3048) instead of building a private one.
    """
    children: list[_FakeChild] = []
    intervals = _Intervals()

    def spawn(context: WorkerDispatchContext) -> _FakeChild:
        child = _FakeChild(context)
        if execute is not None:
            child.execute = execute
        child.execute = _tracked(child.execute, intervals)
        children.append(child)
        return child

    def _tracked(inner: Callable[[dict[str, Any]], dict[str, Any]], tracked: _Intervals) -> Callable[[dict[str, Any]], dict[str, Any]]:
        def run(task: dict[str, Any]) -> dict[str, Any]:
            tracked.start(str(task["key"]))
            try:
                return inner(task)
            finally:
                tracked.finish(str(task["key"]))

        return run

    monkeypatch.setattr(shared_pool_module, "ResearchWorker", spawn)
    dispatch = pool_module.PooledWorkerDispatch(
        WorkerDispatchContext(**CONTEXT), workers=workers, window=window, on_facts=on_facts, pool=pool
    )
    return dispatch, children, intervals


# ── Fast tier ─────────────────────────────────────────────────────────────


def test_two_lanes_overlap_independent_evaluations_within_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    dispatch, children, intervals = _wire_pool(
        monkeypatch, workers=2, window=2, execute=lambda task: (time.sleep(0.05), _gap_message(task))[1]
    )
    try:
        engine, bridge = _EngineSpy(), _ReservationBridge(limit=10)
        evaluator = _seamed_evaluator(monkeypatch, engine, bridge)
        evaluator.dispatch = dispatch

        metrics = evaluator.evaluate([_point(gap / 10) for gap in range(1, 7)], window=JAN, stage="search")

        assert [m.net_profit for m in metrics] == [gap / 10 for gap in range(1, 7)], "input order, whatever arrived"
        assert len(children) == 2, "two actual lanes served the batch"
        # Overlap is read off the recorded intervals, not a wall-clock budget a loaded runner would break.
        assert intervals.pairs_overlapping() >= 1, "independent evaluations genuinely overlapped"
        assert intervals.max_in_flight() <= 2, "the window bounds begun-but-undelivered work"
        assert evaluator.new_runs == 6 and bridge.consumed == 6
    finally:
        dispatch.close()
    assert all(child.closed for child in children)


def test_randomized_completion_orders_reach_the_serial_winner_and_records(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research.golden_search.declarations import declaration_for
    from app.research.golden_search.protocol import GoldenSearchProtocol
    from app.research.golden_search.zoom import run_zoom
    from tests._helpers.golden_search_study import plan_request, registry_params

    symbol = unique_symbol()
    protocol = GoldenSearchProtocol.from_dict(plan_request(symbol, seed=registry_params(symbol)))
    declaration = declaration_for("ema_crossover_signal")

    def zoomed(evaluate) -> Any:
        return run_zoom(declaration=declaration, protocol=protocol, seed=protocol.seed, evaluate=evaluate)

    def evaluator_with(dispatch) -> Any:
        engine, bridge = _EngineSpy(), _ReservationBridge(limit=500)
        evaluator = _seamed_evaluator(monkeypatch, engine, bridge)
        evaluator.dispatch = dispatch
        return evaluator, bridge

    serial_evaluator, serial_bridge = evaluator_with(None)
    reference = zoomed(lambda points: serial_evaluator.evaluate(points, window=JAN, stage="search"))

    for seed in (1, 2, 3):
        jitter = random.Random(seed)

        def execute(task: dict[str, Any], jitter=jitter) -> dict[str, Any]:
            time.sleep(jitter.uniform(0.0, 0.03))
            return _gap_message(task)

        dispatch, _, _ = _wire_pool(monkeypatch, workers=3, window=3, execute=execute)
        try:
            pooled_evaluator, pooled_bridge = evaluator_with(dispatch)

            def pooled_evaluate(points: list[dict[str, Any]], evaluator=pooled_evaluator):
                return evaluator.evaluate(points, window=JAN, stage="search")

            shuffled = zoomed(pooled_evaluate)
        finally:
            dispatch.close()

        assert shuffled.winner == reference.winner and shuffled.stop_reason == reference.stop_reason
        assert shuffled.evaluated_hashes == reference.evaluated_hashes
        assert [round.as_dict() for round in shuffled.rounds] == [round.as_dict() for round in reference.rounds], (
            f"seed {seed}: the recorded path is the serial reference's"
        )
        assert pooled_bridge.reserved == serial_bridge.reserved, (
            f"seed {seed}: reservations stayed the exact affordable serial prefix"
        )


def test_a_hung_lane_is_fenced_while_its_sibling_keeps_delivering(monkeypatch: pytest.MonkeyPatch) -> None:
    sibling_progressed = threading.Event()

    def execute(task: dict[str, Any]) -> dict[str, Any]:
        gap = float(task["point"]["gap"])
        if gap == 0.5 and task["task_gen"] == 1:
            # Hung until a sibling's outcome actually flowed past it (#3046's
            # independent-timeout shape, the fence standing in for the child's
            # own silence bound); the replacement serves the re-admitted run.
            assert sibling_progressed.wait(timeout=5.0), "the sibling's outcomes flowed while this lane hung"
            raise WorkerLost("the hung child was fenced by its own silence bound")
        if gap == 0.3:
            sibling_progressed.set()
        return _gap_message(task)

    dispatch, _, _ = _wire_pool(monkeypatch, workers=2, window=2, execute=execute)
    try:
        bridge = _ReservationBridge(limit=10)
        evaluator = _seamed_evaluator(monkeypatch, _EngineSpy(), bridge)
        evaluator.dispatch = dispatch

        metrics = evaluator.evaluate([_point(0.1), _point(0.2), _point(0.5), _point(0.3)], window=JAN, stage="search")

        assert [m.net_profit for m in metrics] == [0.1, 0.2, 0.5, 0.3], "the batch completed through the replacement"
        assert bridge.rows[_key_of(evaluator, _point(0.5))]["retries"] == 1, "the hung run spent one retry"
        assert all(bridge.rows[_key_of(evaluator, _point(gap))]["retries"] == 0 for gap in (0.1, 0.2, 0.3))
    finally:
        dispatch.close()


def test_a_straggler_from_an_interrupted_dispatch_never_counts_or_records_twice(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A lane still in flight when the owner cancels: its old-generation outcome arrives late (#3066 review).

    Cancellation is what supersedes a dispatch now — one lane's loss no
    longer does, because the loss re-admits only its own item while healthy
    siblings keep delivering. The interrupted lane's twin still finishes
    first once its re-admitted replacement starts, so the production
    ordering — straggler before replacement — stays covered.
    """
    from app.jobs.progress import JobCancelled

    slow_started = threading.Event()
    release_slow = threading.Event()

    def execute(task: dict[str, Any]) -> dict[str, Any]:
        gap = float(task["point"]["gap"])
        if gap == 0.4 and task["task_gen"] == 1:
            slow_started.set()
            release_slow.wait(timeout=5.0)  # still running when the cancellation interrupts the batch
            return _gap_message(task)
        if gap == 0.4 and task["task_gen"] >= 2:
            release_slow.set()  # the re-admitted run frees its interrupted twin
            time.sleep(0.05)  # and finishes after it
        return _gap_message(task)

    dispatch, _, _ = _wire_pool(monkeypatch, workers=2, window=2, execute=execute)

    def cancelled() -> None:
        if slow_started.is_set():
            raise JobCancelled("cancelled by the owner")

    dispatch._context.cancel_check = cancelled
    try:
        bridge = _ReservationBridge(limit=10)
        evaluator = _seamed_evaluator(monkeypatch, _EngineSpy(), bridge)
        evaluator.dispatch = dispatch

        with pytest.raises(JobCancelled):
            evaluator.evaluate([_point(0.4), _point(0.1)], window=JAN, stage="search")

        dispatch._context.cancel_check = lambda: None
        metrics = evaluator.evaluate([_point(0.4), _point(0.1)], window=JAN, stage="search")

        assert [m.net_profit for m in metrics] == [0.4, 0.1], "no adapter-breach error: the straggler did not count"
        slow_key = _key_of(evaluator, _point(0.4))
        assert bridge.completed.count(slow_key) == 1, "the superseded generation's outcome was discarded, not recorded"
        assert bridge.rows[slow_key]["task_gen"] >= 2, "the durable row answered to the current generation"
    finally:
        release_slow.set()
        dispatch.close()

    discarded = [record for record in caplog.records if record.action.endswith("_discarded")]
    assert discarded, "the straggler's discard is explicit, not silent"


def test_cancellation_between_pooled_tasks_stops_only_this_owners_children(monkeypatch: pytest.MonkeyPatch) -> None:
    """#3046's prompt stop, narrowed to the shared pool (#3048): an interrupted
    dispatch stops the children running *its* items — a sibling owner's lanes
    are not its to stop — and its queued items die at take time instead."""
    from app.jobs.progress import JobCancelled

    armed = threading.Event()
    release_blocking = threading.Event()
    sibling_started = threading.Event()
    children: list[_FakeChild] = []
    sibling_gap = 0.3

    def spawn(context: WorkerDispatchContext) -> _FakeChild:
        child = _FakeChild(context)

        def execute(task: dict[str, Any]) -> dict[str, Any]:
            # One shared spawn serves every owner's lane: the sibling's item
            # parks its own child; the interrupted owner's items arm the
            # cancellation and park theirs.
            if float(task["point"]["gap"]) == sibling_gap:
                sibling_started.set()
            else:
                armed.set()
            assert release_blocking.wait(timeout=5.0), "a parked child stayed mid-run"
            return _gap_message(task)

        child.execute = execute
        children.append(child)
        return child

    monkeypatch.setattr(shared_pool_module, "ResearchWorker", spawn)
    pool = shared_pool_module.SharedWorkerPool(
        workers=3, submission_bound=6,
        payload_bound_bytes=shared_pool_module.DEFAULT_PAYLOAD_BOUND_BYTES,
        result_bound_bytes=shared_pool_module.DEFAULT_RESULT_BOUND_BYTES,
    )
    dispatch = pool_module.PooledWorkerDispatch(WorkerDispatchContext(**CONTEXT), workers=3, window=3, pool=pool)
    sibling = pool_module.PooledWorkerDispatch(
        WorkerDispatchContext(**{**CONTEXT, "job_id": "job-sibling"}), workers=3, window=3, pool=pool
    )
    sibling_done: queue.SimpleQueue[list[Any]] = queue.SimpleQueue()

    def run_sibling() -> None:
        # The sibling drives its dispatch's own contract — the evaluator's
        # persistence seams are process-global monkeypatches, so a second
        # evaluator on another thread would cross-wire the bridges.
        sibling_item = _crafted_item(sibling_gap)
        sibling_done.put(list(sibling(None, [sibling_item])))

    try:
        # The sibling takes its lane first, so the interrupted owner's items
        # land on the other two and the blast radius is observable per child.
        sibling_thread = threading.Thread(target=run_sibling, daemon=True)
        sibling_thread.start()
        assert sibling_started.wait(timeout=5.0), "the sibling owner genuinely served its own item meanwhile"

        def cancelled() -> None:
            if armed.is_set():
                raise JobCancelled("cancelled by the owner")

        dispatch._context.cancel_check = cancelled
        bridge = _ReservationBridge(limit=10)
        evaluator = _seamed_evaluator(monkeypatch, _EngineSpy(), bridge)
        evaluator.dispatch = dispatch
        with pytest.raises(JobCancelled):
            evaluator.evaluate([_point(0.1), _point(0.2), _point(0.4)], window=JAN, stage="search")
        stopped = [child for child in children if child.closed]
        assert stopped, "a child running this owner's item was told to stop"
        assert len(stopped) < len(children), "the sibling owner's child was left running"
        every = {_key_of(evaluator, _point(gap)) for gap in (0.1, 0.2, 0.4)}
        # Whatever arrived before the cancellation is durable; the rest released.
        assert set(bridge.released) == every - set(bridge.completed)
    finally:
        release_blocking.set()
        sibling_thread.join(timeout=5.0)
        dispatch.close()
        sibling.close()
    sibling_outcomes = sibling_done.get(timeout=5.0)
    assert [outcome.key for outcome in sibling_outcomes] and sibling_outcomes[0].lost_reason is None, (
        "the sibling's item completed on its own lane"
    )


def test_the_pool_publishes_its_scheduling_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    facts: list[pool_module.DispatchFacts] = []
    dispatch, _, _ = _wire_pool(
        monkeypatch, workers=2, window=2, execute=lambda task: (time.sleep(0.01), _gap_message(task))[1], on_facts=facts.append
    )
    try:
        engine, bridge = _EngineSpy(), _ReservationBridge(limit=10)
        evaluator = _seamed_evaluator(monkeypatch, engine, bridge)
        evaluator.dispatch = dispatch
        evaluator.evaluate([_point(0.1), _point(0.2)], window=JAN, stage="search")
    finally:
        dispatch.close()

    assert len(facts) >= 2, "one emission when the batch arrived, one when it ended"
    first, last = facts[0], facts[-1]
    assert (first.ready_width, first.workers, first.window) == (2, 2, 2)
    assert set(last.as_dict()) == {"ready_width", "workers", "window", "active", "adaptive_wait_ms"}


def test_a_loss_with_queued_work_recovers_without_lane_death(monkeypatch: pytest.MonkeyPatch) -> None:
    """A loss while the window holds un-started items: the batch lands, no lane dies, nothing re-runs free of charge (#3046)."""
    lost_once: set[str] = set()

    def execute(task: dict[str, Any]) -> dict[str, Any]:
        gap = float(task["point"]["gap"])
        if gap == 0.1 and task["task_gen"] == 1 and "lost" not in lost_once:
            lost_once.add("lost")
            time.sleep(0.02)  # let the window fill with not-yet-taken items first
            raise WorkerLost("a lane died with the window part-full")
        return _gap_message(task)

    dispatch, children, _ = _wire_pool(monkeypatch, workers=2, window=4, execute=execute)
    try:
        bridge = _ReservationBridge(limit=20)
        evaluator = _seamed_evaluator(monkeypatch, _EngineSpy(), bridge)
        evaluator.dispatch = dispatch

        metrics = evaluator.evaluate([_point(gap / 10) for gap in range(1, 7)], window=JAN, stage="search")

        assert [m.net_profit for m in metrics] == [gap / 10 for gap in range(1, 7)], "the batch landed"
        assert bridge.rows[_key_of(evaluator, _point(0.1))]["retries"] == 1, "only the lost in-flight run spent a retry"
        assert all(bridge.rows[_key_of(evaluator, _point(gap / 10))]["retries"] == 0 for gap in range(2, 7))
    finally:
        assert all(lane.is_alive() for lane in dispatch._pool._threads), "no lane died on the stale queue"
        started = time.monotonic()
        dispatch.close()
        assert time.monotonic() - started < 5.0, "close after an interrupted dispatch returns promptly"
    assert children


# ── Ephemeral-database tier: whole studies through the pool ───────────────


@pytest.fixture
def pool_symbol() -> str:
    return unique_symbol()


@pytest.fixture
def pool_lake(tmp_path: Path, pool_symbol: str) -> Path:
    return seed_lake(tmp_path, pool_symbol)


@pytest.fixture
def pool_driver(pool_lake: Path, monkeypatch: pytest.MonkeyPatch, conn: asyncpg.Connection) -> Driver:
    monkeypatch.setattr(lifecycle, "roots_for", lambda row: [pool_lake])
    return Driver(roots=[pool_lake])


def _landscape_message(task: dict[str, Any]) -> dict[str, Any]:
    """The serial FakeEngine's exact landscape as a child result message (#3046's equivalence witness)."""
    scenario = "base" if task["scenario"] is None else task["scenario"]["key"]
    window = (int(task["window_start_ms"]), int(task["window_end_ms"]))
    sharpe = smooth_score(task["point"], window, scenario)
    metrics = Metrics(
        status="completed",
        total_trades=40,
        net_profit=1_000.0 * sharpe,
        total_return_pct=sharpe / 100.0,
        sharpe_ratio=sharpe,
        max_drawdown_pct=0.1,
        win_rate=0.5,
    )
    detail = fake_detail(window, sharpe) if task["detail"] else None
    curve = [[ms, value] for ms, value in detail["daily_equity"]] if detail is not None else None
    return {"type": "result", "key": task["key"], "metrics": metrics.as_dict(), "detail": detail, "curve": curve}


def _landscape_pool(
    monkeypatch: pytest.MonkeyPatch, *, workers: int, window: int, lose_first: str | None = None
) -> pool_module.PooledWorkerDispatch:
    lost: set[str] = set()

    def execute(task: dict[str, Any]) -> dict[str, Any]:
        if lose_first is not None and task["task_gen"] == 1 and task["point"].get("gap") == lose_first and lose_first not in lost:
            lost.add(lose_first)
            raise WorkerLost("a pooled child died with the task in flight")
        return _landscape_message(task)

    def spawn(context: WorkerDispatchContext) -> _FakeChild:
        child = _FakeChild(context)
        child.execute = execute
        return child

    monkeypatch.setattr(shared_pool_module, "ResearchWorker", spawn)
    return pool_module.PooledWorkerDispatch(WorkerDispatchContext(**{**CONTEXT, "strategy_key": "ema_crossover_signal"}), workers=workers, window=window)


async def test_a_whole_study_through_the_pool_matches_the_serial_reference(conn, pool_driver: Driver, pool_symbol: str, monkeypatch) -> None:
    """Width changes neither the search nor the books: the pool's study lands where the serial FakeEngine's lands."""
    reference = await pool_driver.advance(await pool_driver.lock(pool_symbol), "continue")

    row = await pool_driver.lock(pool_symbol)
    outcome = await pool_driver.command(row, "continue")
    built: list[pool_module.PooledWorkerDispatch] = []

    def factory(context: WorkerDispatchContext) -> pool_module.PooledWorkerDispatch:
        dispatch = _landscape_pool(monkeypatch, workers=2, window=2)
        built.append(dispatch)
        return dispatch

    await pool_driver.run(outcome, dispatch_factory=factory)

    done = await service.get_row(row.id)
    assert done.state == reference.state == "awaiting_validation"
    assert done.consumed_evaluations == reference.consumed_evaluations
    assert done.results["search"]["procedure"] == reference.results["search"]["procedure"], "the winner and rounds are the serial reference's"
    assert done.results["search"]["counts"] == reference.results["search"]["counts"]
    assert done.results["recent"]["counts"] == reference.results["recent"]["counts"]
    assert await conn.fetchval("SELECT MAX(retries) FROM research_golden_search_evaluations WHERE study_id = $1", row.id) == 0

    # Lock-to-exam detail evidence through the pool: validation and the exam's
    # detail runs ride the same lanes.
    row = await service.get_row(row.id)
    row = await pool_driver.advance(row, "continue")
    row = await pool_driver.advance(row, "open_exam", {"acknowledge_final_test": True})
    fits = row.results["exam"]["fits"]
    assert fits and all(fit["metrics"]["status"] == "completed" for fit in fits)
    from app.research.golden_search.declarations import point_hash

    for fit in fits:
        record = await conn.fetchrow(
            """
            SELECT detail_json IS NOT NULL AS has_payload FROM research_golden_search_evaluations
             WHERE study_id = $1 AND point_hash = $2 AND detail AND scenario = 'base'
               AND window_start_ms = $3 AND window_end_ms = $4
            """,
            row.id,
            fit["point_hash"],
            row.protocol["final_start_ms"],
            row.protocol["final_end_ms"],
        )
        assert record is not None and record["has_payload"], f"fit {fit['key']}: the exam's detail evidence survived the pool"
    assert point_hash is not None


async def test_two_concurrent_studies_share_one_pool_and_match_the_serial_books(
    conn, pool_driver: Driver, pool_symbol: str, monkeypatch
) -> None:
    """#3048's core sharing evidence: two studies interleaved through one pool of lanes keep the serial schedule.

    Both studies land exactly where the serial FakeEngine reference lands —
    same winner, same rounds, same consumed count, no retry spent — with
    every dispatch an owner on one shared pool and capacity handoffs
    observed for both.
    """
    reference = await pool_driver.advance(await pool_driver.lock(pool_symbol), "continue")

    def spawn(context: WorkerDispatchContext) -> _FakeChild:
        child = _FakeChild(context)
        child.execute = lambda task: _landscape_message(task)
        return child

    monkeypatch.setattr(shared_pool_module, "ResearchWorker", spawn)
    shared = shared_pool_module.SharedWorkerPool(
        workers=2,
        submission_bound=4,
        payload_bound_bytes=shared_pool_module.DEFAULT_PAYLOAD_BOUND_BYTES,
        result_bound_bytes=shared_pool_module.DEFAULT_RESULT_BOUND_BYTES,
    )
    handoffs: list[str] = []
    built: list[pool_module.PooledWorkerDispatch] = []

    def factory(context: WorkerDispatchContext) -> pool_module.PooledWorkerDispatch:
        dispatch = pool_module.PooledWorkerDispatch(
            context, workers=2, window=2, pool=shared,
            on_capacity_handoff=lambda: handoffs.append(context.job_id),
        )
        built.append(dispatch)
        return dispatch

    rows = [await pool_driver.lock(pool_symbol), await pool_driver.lock(pool_symbol)]
    outcomes = [await pool_driver.command(row, "continue") for row in rows]
    try:
        await asyncio.gather(*(pool_driver.run(outcome, dispatch_factory=factory) for outcome in outcomes))
    finally:
        for dispatch in built:
            dispatch.close()
        shared.close()

    for row in rows:
        done = await service.get_row(row.id)
        assert done.state == reference.state == "awaiting_validation"
        assert done.consumed_evaluations == reference.consumed_evaluations
        assert done.results["search"]["procedure"] == reference.results["search"]["procedure"], (
            "interleaved sharing changed neither study's search"
        )
        assert done.results["search"]["counts"] == reference.results["search"]["counts"]
        assert done.results["recent"]["counts"] == reference.results["recent"]["counts"]
        assert await conn.fetchval(
            "SELECT MAX(retries) FROM research_golden_search_evaluations WHERE study_id = $1", row.id
        ) == 0, "interleaving never charged a retry"
    assert len(handoffs) >= 2, "both owners saw capacity handoffs"


async def test_a_lane_loss_under_width_keeps_the_books(conn, pool_driver: Driver, pool_symbol: str, monkeypatch) -> None:
    """#3045's recovery under #3046's width: a lost lane's study still matches the serial schedule."""
    reference = await pool_driver.advance(await pool_driver.lock(pool_symbol), "continue")

    row = await pool_driver.lock(pool_symbol)
    outcome = await pool_driver.command(row, "continue")
    seed_gap = float(row.protocol["seed"]["gap"])

    def factory(context: WorkerDispatchContext) -> pool_module.PooledWorkerDispatch:
        return _landscape_pool(monkeypatch, workers=2, window=2, lose_first=seed_gap)

    await pool_driver.run(outcome, dispatch_factory=factory)

    done = await service.get_row(row.id)
    assert done.state == reference.state
    assert done.consumed_evaluations == reference.consumed_evaluations
    assert done.results["search"]["counts"] == reference.results["search"]["counts"]
    assert await conn.fetchval("SELECT MAX(retries) FROM research_golden_search_evaluations WHERE study_id = $1", row.id) == 1, (
        "exactly the lost in-flight run spent a retry, once"
    )


# ── Slow tier: real children, real overlap ────────────────────────────────


def _close_writer_loop_catalog_pool() -> None:
    """Close the writer loop's lazily built catalog pool after a slow test.

    Real-children study persistence rides the shared writer loop, and that
    loop's first pooled call sizes its pool at the module ceiling. The
    daily tier runs one process, so a pool left open here would be the
    writer loop's first initializer ahead of #3047's envelope test later
    in that order — exactly the pool-at-startup state the lifespan's
    enforcement ordering exists to prevent.
    """
    from app.data_lake import catalog_client
    from app.utils.background_loop import run_on_background_loop

    closer = threading.Thread(
        target=lambda: run_on_background_loop(catalog_client.close_pool(), timeout=5.0), daemon=True
    )
    closer.start()
    closer.join(timeout=5.0)


@pytest.mark.slow
@pytest.mark.usefixtures("seeded_lake_catalog")
async def test_two_real_children_overlap_independent_evaluations(conn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two actual spawned workers overlap — not a synchronous function in a serial loop (#3046's first AC)."""
    from datetime import date as date_of
    from uuid import UUID

    from app.config import settings
    from app.data_lake.root_identity import init_empty_root
    from app.lean_sidecar.trading_calendar import expected_sessions
    from app.research.golden_search.evaluator import (
        EvaluationCapability,
        StudyEvaluator,
    )
    from app.research.golden_search.worker import ResearchWorker
    from app.utils.session_anchors import et_midnight_ms
    from tests._helpers.lake_fixture import seed_lake_minute_day

    symbol = "SPY"
    base = tmp_path / "pool-root"
    init_empty_root(base, UUID(int=3046))
    lake = base / "lake" / "polygon_split_adjusted"
    lake.mkdir(parents=True)
    manifest: dict[str, str] = {}
    for day in expected_sessions(date_of(2024, 12, 2), date_of(2025, 4, 30)):
        trade, _quote = seed_lake_minute_day(lake, symbol, day)
        manifest[trade.relative_to(lake).as_posix()] = hashlib.sha256(trade.read_bytes()).hexdigest()
        companion = trade.with_name(trade.name + ".adjustment.json")
        if companion.exists():
            manifest[companion.relative_to(lake).as_posix()] = hashlib.sha256(companion.read_bytes()).hexdigest()
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(base))

    monkeypatch.setenv("LEAN_DATA_WRITE_ROOT", str(base))

    study = "pool-real-" + unique_symbol().lower()
    await conn.execute(
        """
        INSERT INTO research_golden_search_studies (
            id, strategy_key, symbol, state, status, attempt, created_at_ms, updated_at_ms,
            protocol_json, protocol_hash, receipt_json, budget_cap
        ) VALUES ($1, 'sma_crossover', 'SPY', 'search_running', 'running', 1, 1, 1, '{}'::jsonb, 'h', '{}'::jsonb, 100)
        """,
        study,
    )

    window = (et_midnight_ms(date_of(2025, 1, 2)), et_midnight_ms(date_of(2025, 3, 3)))
    from app.research.sweep.identity import resolve_code_identity

    resolved = resolve_code_identity()
    identity = {"source_digest": resolved.source_digest, "environment_digest": resolved.environment_digest, "digest_scheme": resolved.digest_scheme}
    context = WorkerDispatchContext(
        strategy_key="sma_crossover",
        execution={"fill_mode": "next_bar_open", "commission_per_order": 0.0, "slippage_per_share": 0.0, "initial_cash": 50_000.0},
        manifest=manifest,
        identity=identity,
        job_id="job-pool-real",
        generation=1,
    )

    runs: list[tuple[int, float, float]] = []
    run_task = ResearchWorker.run_task

    def timed_run_task(self: ResearchWorker, task: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        started = time.monotonic()
        try:
            return run_task(self, task, **kwargs)
        finally:
            runs.append((id(self), started, time.monotonic()))

    monkeypatch.setattr(ResearchWorker, "run_task", timed_run_task)

    points = [
        {"symbol": "SPY", "short_window": short, "long_window": long, "resolution_minutes": 1}
        for short, long in ((2, 3), (2, 5), (2, 8), (2, 13), (3, 5), (3, 8), (3, 13), (5, 8))
    ]

    def evaluate_through(dispatch: Any, points: list[dict[str, Any]], digest: str) -> Any:
        evaluator = StudyEvaluator(
            study_id=study,
            attempt=1,
            strategy_key="sma_crossover",
            context_digest=digest,
            run_up_sessions=1,
            capability=EvaluationCapability(allowed=(window,)),
            budget_limit=100,
            execute=lambda request: None,
            dispatch=dispatch,
        )
        return evaluator.evaluate(points, window=window, stage="search")

    overlaps = 0
    widths: dict[int, float] = {}
    ran: dict[int, int] = {}
    try:
        for width in (1, 2, 4):
            runs.clear()
            dispatch = pool_module.PooledWorkerDispatch(context, workers=width, window=width)
            try:
                started = time.monotonic()
                metrics = await asyncio.to_thread(evaluate_through, dispatch, points, f"pool-probe-{width}")
                widths[width] = time.monotonic() - started
                assert all(m.status == "completed" for m in metrics), f"width {width}: every real evaluation completed ({[m.error for m in metrics]})"
            finally:
                dispatch.close()
            workers = {worker for worker, _, _ in runs}
            ran[width] = len(runs)
            assert len(workers) == min(width, len(points)), f"width {width}: {len(workers)} actual children served the batch"
            for first in runs:
                for second in runs:
                    if first[0] != second[0] and first[1] < second[2] and second[1] < first[2]:
                        overlaps += 1
            logger.info(
                "research pool measurement (#3046 evidence): width=%d elapsed=%.2fs tasks=%d",
                width,
                widths[width],
                len(runs),
            )

        assert overlaps >= 1, "two real children ran independent evaluations at the same time"
        # Transport sanity is the work done, not a wall-clock ratio: no width ran a point twice.
        assert ran == {width: len(points) for width in (1, 2, 4)}, "each width evaluated every point exactly once"
    finally:
        _close_writer_loop_catalog_pool()


# ── #3065/#3066 review regressions ─────────────────────────────────────────


def test_one_lanes_loss_charges_no_retry_to_its_in_flight_sibling(monkeypatch: pytest.MonkeyPatch) -> None:
    """#3066 review: a lane's loss re-admits only its own item.

    A whole-dispatch supersession would re-reserve the sibling too — its
    begun row spending retry allowance and its eventual result discarded as
    a straggler — timing a fabricated failure into the search.
    """
    sibling_started = threading.Event()

    def execute(task: dict[str, Any]) -> dict[str, Any]:
        gap = float(task["point"]["gap"])
        if gap == 0.1 and task["task_gen"] == 1:
            sibling_started.wait(timeout=5.0)  # the sibling is mid-run when this lane dies
            raise WorkerLost("a lane died while its sibling's task was in flight")
        if gap == 0.2:
            sibling_started.set()
            return _gap_message(task)  # finishes normally while the loss is being re-admitted
        return _gap_message(task)

    dispatch, _, _ = _wire_pool(monkeypatch, workers=2, window=2, execute=execute)
    try:
        bridge = _ReservationBridge(limit=10)
        evaluator = _seamed_evaluator(monkeypatch, _EngineSpy(), bridge)
        evaluator.dispatch = dispatch

        metrics = evaluator.evaluate([_point(0.1), _point(0.2)], window=JAN, stage="search")

        assert [m.net_profit for m in metrics] == [0.1, 0.2], "the batch completed: loss re-admitted, sibling recorded"
        lost_key, sibling_key = _key_of(evaluator, _point(0.1)), _key_of(evaluator, _point(0.2))
        assert bridge.rows[lost_key]["retries"] == 1, "only the lost run spent a retry"
        assert bridge.rows[sibling_key]["retries"] == 0, "the in-flight sibling was never re-reserved"
        assert sibling_key in bridge.completed, "the sibling's result was recorded, not discarded"
    finally:
        dispatch.close()


def test_the_window_bounds_submitted_work_through_delivery_not_queue_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    """#3066 review: begun-but-undurable work stays inside the window.

    The queue bound alone freed capacity the moment a lane dequeued, so
    fast lanes with slow persistence could begin nearly the whole batch
    while nothing durable had landed — the exact blast radius the window
    promised to bound.
    """

    class _WatchingBridge(_ReservationBridge):
        def __init__(self, limit: int) -> None:
            super().__init__(limit)
            self.spans: list[int] = []

        async def complete(self, conn, *args: Any, **kwargs: Any):
            time.sleep(0.02)  # the durable write is the slow thing
            outcome = await super().complete(conn, *args, **kwargs)
            self.spans.append(len(self.started) - len(self.completed))
            return outcome

    def execute(task: dict[str, Any]) -> dict[str, Any]:
        time.sleep(0.005)
        return _gap_message(task)

    dispatch, _children, _ = _wire_pool(monkeypatch, workers=2, window=2, execute=execute)
    try:
        engine, bridge = _EngineSpy(), _WatchingBridge(limit=20)
        evaluator = _seamed_evaluator(monkeypatch, engine, bridge)
        evaluator.dispatch = dispatch

        metrics = evaluator.evaluate([_point(gap / 10) for gap in range(1, 9)], window=JAN, stage="search")

        assert [m.net_profit for m in metrics] == [gap / 10 for gap in range(1, 9)], "the whole batch landed"
        assert bridge.spans and max(bridge.spans) <= 2, (
            f"begun-but-undurable work stayed inside the window (max span {max(bridge.spans)})"
        )
    finally:
        dispatch.close()


def test_spawn_failure_keeps_the_lanes_alive_and_bounds_by_the_allowance(monkeypatch: pytest.MonkeyPatch) -> None:
    """#3066 review: a child that never comes up is a loss, not a dead lane.

    Lanes exiting on spawn failure would strand the next dispatch on an
    arrival that never comes, instead of exhausting the bounded allowance.
    """

    def broken_spawn(context: WorkerDispatchContext) -> _FakeChild:
        raise WorkerLost("the research worker failed to start: no interpreter in the lane")

    monkeypatch.setattr(shared_pool_module, "ResearchWorker", broken_spawn)
    dispatch = pool_module.PooledWorkerDispatch(WorkerDispatchContext(**CONTEXT), workers=2, window=4)
    try:
        bridge = _ReservationBridge(limit=20)
        evaluator = _seamed_evaluator(monkeypatch, _EngineSpy(), bridge)
        evaluator.dispatch = dispatch

        with pytest.raises(WorkerLost, match="failed to start"):
            evaluator.evaluate([_point(0.1), _point(0.2), _point(0.3)], window=JAN, stage="search")

        assert all(lane.is_alive() for lane in dispatch._pool._threads), "no lane died on a spawn failure"
    finally:
        started = time.monotonic()
        dispatch.close()
        assert time.monotonic() - started < 5.0, "teardown never waits on a dead dispatch"


def test_a_child_spawning_as_the_pool_closes_is_killed_not_published(monkeypatch: pytest.MonkeyPatch) -> None:
    """#3066 review: startup is invisible to close()'s snapshot, so the lane owns the kill."""
    from datetime import date as date_of

    from app.research.golden_search.evaluator import EvaluationRequest, ReadyEvaluation

    children: list[_FakeChild] = []

    def spawn(context: WorkerDispatchContext) -> _FakeChild:
        child = _FakeChild(context)
        children.append(child)
        # The pool closes while this child is still starting: invisible to
        # close()'s snapshot, so the lane must own the kill.
        dispatch._pool._closing = True
        return child

    monkeypatch.setattr(shared_pool_module, "ResearchWorker", spawn)
    dispatch = pool_module.PooledWorkerDispatch(WorkerDispatchContext(**CONTEXT), workers=1, window=2)
    try:
        item = ReadyEvaluation(
            key="k" * 64,
            attempt=1,
            task_gen=1,
            request=EvaluationRequest(
                point={"symbol": "SPY", "gap": 0.1},
                point_hash="p" * 64,
                window=JAN,
                warmup_from=date_of(2024, 12, 30),
                scenario=None,
                detail=False,
                stage="search",
                fold_index=None,
            ),
        )
        entry = shared_pool_module._Entry(owner=dispatch._handle._state, epoch=dispatch._current_epoch(), item=item)
        dispatch._pool._run_assignment(dispatch._pool._lanes[0], entry)

        _key, _task_gen, arrival = dispatch._handle.arrived.get(timeout=1.0)
        from app.jobs.progress import JobCancelled as _JobCancelled
        assert isinstance(arrival, _JobCancelled), "the closing cancels the item home — a live owner unwinds, it does not re-admit forever"
        assert children[0].closed, "the runaway child was killed before it could begin"
        assert dispatch._handle.arrived.empty(), "exactly one answer, for exactly one item"
    finally:
        dispatch.close()
    assert dispatch._pool._lanes[0].worker is None, "the corpse was never published to the slot"


def test_a_delivered_cancellation_reaps_the_children_before_release_reconciliation(monkeypatch: pytest.MonkeyPatch) -> None:
    """#3065 review: a cancellation observed by the child stops every child before the release writes.

    Raising the cancellation in the evaluator's frame never runs the
    adapter's own handlers, so without the pre-reconciliation reap the
    children would sit through every stalled release write.
    """
    from app.jobs.progress import JobCancelled

    events: list[str] = []
    release_blocking = threading.Event()
    sibling_started = threading.Event()

    def spawn(context: WorkerDispatchContext) -> _FakeChild:
        child = _FakeChild(context)

        def execute(task: dict[str, Any]) -> dict[str, Any]:
            if float(task["point"]["gap"]) == 0.1:
                # The cancelling answer waits for its sibling to be provably
                # mid-run: on a slower runner the reap may otherwise find no
                # lane occupied yet (#3048 CI).
                assert sibling_started.wait(timeout=5.0), "the sibling began before the cancellation landed"
                return {"type": "failed", "key": task["key"], "error": "cancelled"}
            # A sibling item stays mid-run so the reap has a live child to stop.
            sibling_started.set()
            assert release_blocking.wait(timeout=5.0), "the mid-run child outlived its owner's cancellation"
            return {"type": "failed", "key": task["key"], "error": "cancelled"}

        child.execute = execute

        def cancel() -> None:
            child.closed = True
            events.append("child-stopped")

        child.cancel = cancel
        return child

    monkeypatch.setattr(shared_pool_module, "ResearchWorker", spawn)
    dispatch = pool_module.PooledWorkerDispatch(WorkerDispatchContext(**CONTEXT), workers=2, window=2)

    class _SlowReleaseBridge(_ReservationBridge):
        async def release(self, conn, *args: Any, **kwargs: Any):
            time.sleep(0.05)  # the durable reconciliation is the slow thing
            outcome = await super().release(conn, *args, **kwargs)
            events.append("released")
            return outcome

    bridge = _SlowReleaseBridge(limit=10)
    evaluator = _seamed_evaluator(monkeypatch, _EngineSpy(), bridge)
    evaluator.dispatch = dispatch
    try:
        with pytest.raises(JobCancelled):
            evaluator.evaluate([_point(0.1), _point(0.2), _point(0.3)], window=JAN, stage="search")
    finally:
        release_blocking.set()
        dispatch.close()

    assert "child-stopped" in events, "the children were told to stop"
    assert events.index("child-stopped") < events.index("released"), (
        f"the reap preceded the first release write: {events}"
    )


def test_the_pool_divides_the_shared_cache_budget_across_its_lanes() -> None:
    """#3066 review: eight lanes at the parity ceiling would consume the whole runtime envelope in caches alone."""
    from app.research.golden_search.worker import (
        WORKER_DECODED_CACHE_BYTES,
        WORKER_DECODED_CACHE_TOTAL_BYTES,
        pooled_cache_bytes,
    )

    assert pooled_cache_bytes(1) == WORKER_DECODED_CACHE_BYTES
    assert pooled_cache_bytes(4) == WORKER_DECODED_CACHE_BYTES
    assert pooled_cache_bytes(8) * 8 <= WORKER_DECODED_CACHE_TOTAL_BYTES

    context = WorkerDispatchContext(**CONTEXT)
    dispatch = pool_module.PooledWorkerDispatch(context, workers=8, window=8)
    dispatch.close()
    assert context.decoded_cache_bytes == WORKER_DECODED_CACHE_TOTAL_BYTES // 8, (
        "the shared budget, split across the configured width"
    )


def test_a_child_dead_between_tasks_is_replaced_before_the_next_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """#3065 review: the pool's slot is health-checked too, not only the serial adapter's."""
    order: list[tuple[str, str]] = []
    spawned = iter(["first", "second"])

    children: list[_FakeChild] = []

    def spawn(context: WorkerDispatchContext) -> _FakeChild:
        child = _FakeChild(context)
        child.name = next(spawned)
        order.append(("spawn", child.name))
        children.append(child)
        answer = child.run_task

        def run_task(task: dict[str, Any]) -> dict[str, Any]:
            message = answer(task)
            child.lost = "the research worker exited with code 1"  # dies right after its answer
            return message

        child.run_task = run_task
        return child

    monkeypatch.setattr(shared_pool_module, "ResearchWorker", spawn)
    dispatch = pool_module.PooledWorkerDispatch(WorkerDispatchContext(**CONTEXT), workers=1, window=2)
    try:
        engine, bridge = _EngineSpy(), _ReservationBridge(limit=10)
        evaluator = _seamed_evaluator(monkeypatch, engine, bridge)
        evaluator.dispatch = dispatch

        metrics = evaluator.evaluate([_point(0.1), _point(0.2)], window=JAN, stage="search")

        assert [m.net_profit for m in metrics] == [0.1, 0.2], "the batch completed through the replacement"
        assert [name for _, name in order] == ["first", "second"], "the corpse was replaced before the second marker"
        assert children[0].closed, "the health check closed the dead child"
        for gap in (0.1, 0.2):
            assert bridge.rows[_key_of(evaluator, _point(gap))]["retries"] == 0, "no allowance spent on an undispatched run"
    finally:
        dispatch.close()


@pytest.mark.slow
async def test_admission_verdicts_batch_with_measured_latency_across_widths(
    conn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3047's measurement tier: real children at 1/2/4/8 widths, real SQL round trips.

    The children's artifact reads ride the production relay — the process's
    admission batcher — over a lake whose receipts are real catalog rows, so
    every verdict is a genuine set-based SQL round trip. Per width this
    records the batcher's own facts (batch sizes, request versus SQL counts,
    round trips saved), per-batch query latency and per-evaluation write
    latency; those numbers are what gets published on #3047. Batching itself
    is asserted where it must exist: at full width, concurrent readers share
    round trips. The children — bare interpreters — hold no database
    connection of their own; the server sees at most the test harness beside
    the envelope's pools.
    """
    from datetime import date as date_of
    from statistics import median
    from uuid import UUID

    from app.config import settings
    from app.data_lake import catalog_client
    from app.data_lake.admission import start_admission_batcher, stop_admission_batcher
    from app.data_lake.root_identity import init_empty_root
    from app.lean_sidecar.trading_calendar import expected_sessions
    from app.research.golden_search import repository
    from app.research.golden_search.evaluator import EvaluationCapability, StudyEvaluator
    from app.utils.session_anchors import et_midnight_ms
    from tests._helpers.lake_fixture import seed_lake_minute_day

    symbol = "SPY"
    base = tmp_path / "admission-root"
    init_empty_root(base, UUID(int=3047))
    lake = base / "lake" / "polygon_split_adjusted"
    lake.mkdir(parents=True)
    manifest: dict[str, str] = {}
    for day in expected_sessions(date_of(2024, 12, 2), date_of(2025, 2, 28)):
        trade, _quote = seed_lake_minute_day(lake, symbol, day)
        manifest[trade.relative_to(lake).as_posix()] = hashlib.sha256(trade.read_bytes()).hexdigest()
        companion = trade.with_name(trade.name + ".adjustment.json")
        if companion.exists():
            manifest[companion.relative_to(lake).as_posix()] = hashlib.sha256(companion.read_bytes()).hexdigest()
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(base))
    monkeypatch.setenv("LEAN_DATA_WRITE_ROOT", str(base))

    # Real catalog rows, not the in-memory fixture: every admission verdict
    # below is a genuine SQL round trip, which is what the measurement needs.
    from app.data_lake.root_identity import read_marker

    root_id = read_marker(base).data_root_id
    for day in expected_sessions(date_of(2024, 12, 2), date_of(2025, 2, 28)):
        relative = f"equity/usa/minute/{symbol.lower()}/{day.strftime('%Y%m%d')}_trade.zip"
        payload = (lake / relative).read_bytes()
        await conn.execute(
            """
            INSERT INTO "DataLakeArtifacts" (
                "ArtifactKind", "Market", "Symbol", "TradingDate", "Resolution", "DataType",
                "Provider", "ProviderParams", "PriceAdjustmentMode", "DataContractHash",
                "FilePath", "Status", "LeaseOwner", "LeaseExpiresAtMs", "LeaseGeneration",
                "AttemptCount", "FetchedAtMs", "DataRootId", "FileSha256", "FileSizeBytes"
            ) VALUES (
                'time_series_bars', 'usa', $3, $4, 'minute', 'trade',
                'polygon', '{}'::jsonb, 'polygon_split_adjusted', 'dc', $5, 'complete',
                NULL, NULL, 1, 1, 1, $1, $2, $6
            )
            ON CONFLICT ("DataRootId", "Market", "Symbol", "TradingDate", "DataType",
                         "Provider", "PriceAdjustmentMode")
                WHERE "ArtifactKind" = 'time_series_bars' AND "Resolution" = 'minute'
            DO UPDATE SET "FileSha256" = $2, "FileSizeBytes" = $6, "Status" = 'complete'
            """,
            root_id, hashlib.sha256(payload).hexdigest(), symbol.upper(), day, relative, len(payload),
        )

    query_ms: list[float] = []
    inner_lookup = catalog_client.has_committed_file_receipts

    async def timed_lookup(requests):
        started = time.monotonic()
        try:
            return await inner_lookup(requests)
        finally:
            query_ms.append((time.monotonic() - started) * 1000.0)

    monkeypatch.setattr(catalog_client, "has_committed_file_receipts", timed_lookup)

    write_ms: list[float] = []
    inner_complete = repository.complete_evaluation

    async def timed_complete(conn_arg, *args, **kwargs):
        started = time.monotonic()
        try:
            return await inner_complete(conn_arg, *args, **kwargs)
        finally:
            write_ms.append((time.monotonic() - started) * 1000.0)

    monkeypatch.setattr(repository, "complete_evaluation", timed_complete)

    window = (et_midnight_ms(date_of(2025, 1, 2)), et_midnight_ms(date_of(2025, 2, 3)))
    from app.research.sweep.identity import resolve_code_identity

    resolved = resolve_code_identity()
    identity = {"source_digest": resolved.source_digest, "environment_digest": resolved.environment_digest, "digest_scheme": resolved.digest_scheme}
    context = WorkerDispatchContext(
        strategy_key="sma_crossover",
        execution={"fill_mode": "next_bar_open", "commission_per_order": 0.0, "slippage_per_share": 0.0, "initial_cash": 50_000.0},
        manifest=manifest,
        identity=identity,
        job_id="job-admission-measure",
        generation=1,
    )

    def percentile(samples: list[float], which: float) -> float:
        if not samples:
            return 0.0
        ordered = sorted(samples)
        return ordered[min(int(which * len(ordered)), len(ordered) - 1)]

    points = [
        {"symbol": symbol, "short_window": short, "long_window": long, "resolution_minutes": 1}
        for short, long in ((2, 3), (2, 5), (2, 8), (2, 13), (3, 5), (3, 8), (3, 13), (5, 8))
    ]

    batcher = start_admission_batcher()
    try:
        for width in (1, 2, 4, 8):
            study = f"admission-measure-{width}-" + unique_symbol().lower()
            await conn.execute(
                """
                INSERT INTO research_golden_search_studies (
                    id, strategy_key, symbol, state, status, attempt, created_at_ms, updated_at_ms,
                    protocol_json, protocol_hash, receipt_json, budget_cap
                ) VALUES ($1, 'sma_crossover', 'SPY', 'search_running', 'running', 1, 1, 1, '{}'::jsonb, 'h', '{}'::jsonb, 100)
                """,
                study,
            )

            def evaluate_through(dispatch: Any, digest: str, study_id: str = study) -> Any:
                evaluator = StudyEvaluator(
                    study_id=study_id,
                    attempt=1,
                    strategy_key="sma_crossover",
                    context_digest=digest,
                    run_up_sessions=1,
                    capability=EvaluationCapability(allowed=(window,)),
                    budget_limit=100,
                    execute=lambda request: None,
                    dispatch=dispatch,
                )
                return evaluator.evaluate(points, window=window, stage="search")

            before = batcher.facts()
            query_ms.clear()
            write_ms.clear()
            dispatch = pool_module.PooledWorkerDispatch(context, workers=width, window=width)
            try:
                started = time.monotonic()
                metrics = await asyncio.to_thread(evaluate_through, dispatch, f"admission-probe-{width}")
                elapsed = time.monotonic() - started
            finally:
                dispatch.close()
            after = batcher.facts()

            assert all(m.status == "completed" for m in metrics), f"width {width}: every evaluation completed"
            delta = {name: after[name] - before[name] if isinstance(after[name], int) else None for name in ("requests", "sql_lookups")}
            batch_delta = {
                size: after["batch_sizes"].get(size, 0) - before["batch_sizes"].get(size, 0)
                for size in set(after["batch_sizes"]) | set(before["batch_sizes"])
            }
            open_connections = await conn.fetchval("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")
            logger.info(
                "research admission measurement (#3047): width=%d elapsed=%.2fs requests=%d sql_lookups=%d "
                "query p50/p95=%.2f/%.2fms write p50/p95=%.2f/%.2fms batches=%s server_connections=%d",
                width, elapsed, delta["requests"], delta["sql_lookups"],
                median(query_ms) if query_ms else 0.0, percentile(query_ms, 0.95),
                median(write_ms) if write_ms else 0.0, percentile(write_ms, 0.95),
                batch_delta, open_connections,
            )
            assert open_connections <= 24, (
                f"width {width}: the children hold no database connections; {open_connections} were open"
            )
            if width == 8:
                assert delta["requests"] > delta["sql_lookups"] > 0, (
                    "concurrent readers shared round trips at full width "
                    f"({delta['requests']} requests, {delta['sql_lookups']} lookups)"
                )
    finally:
        stop_admission_batcher()
        _close_writer_loop_catalog_pool()


@pytest.mark.slow
@pytest.mark.usefixtures("seeded_lake_catalog")
async def test_two_studies_share_real_children_lanes_fairly(conn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#3048's sharing with real spawned children: two studies interleave on one pool's lanes.

    Both studies complete their batches with the serial answers, each study
    is served by a real child, the two studies' evaluations genuinely
    overlap on the shared lanes, and both owners saw capacity handoffs —
    evidence of fair sharing, not a serialized pass-through.
    """
    from datetime import date as date_of
    from uuid import UUID

    from app.config import settings
    from app.data_lake.root_identity import init_empty_root
    from app.lean_sidecar.trading_calendar import expected_sessions
    from app.research.golden_search.evaluator import EvaluationCapability, StudyEvaluator
    from app.research.sweep.identity import resolve_code_identity
    from app.utils.session_anchors import et_midnight_ms
    from tests._helpers.lake_fixture import seed_lake_minute_day

    symbol = "SPY"
    base = tmp_path / "shared-root"
    init_empty_root(base, UUID(int=3048))
    lake = base / "lake" / "polygon_split_adjusted"
    lake.mkdir(parents=True)
    manifest: dict[str, str] = {}
    for day in expected_sessions(date_of(2024, 12, 2), date_of(2025, 2, 28)):
        trade, _quote = seed_lake_minute_day(lake, symbol, day)
        manifest[trade.relative_to(lake).as_posix()] = hashlib.sha256(trade.read_bytes()).hexdigest()
        companion = trade.with_name(trade.name + ".adjustment.json")
        if companion.exists():
            manifest[companion.relative_to(lake).as_posix()] = hashlib.sha256(companion.read_bytes()).hexdigest()
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(base))
    monkeypatch.setenv("LEAN_DATA_WRITE_ROOT", str(base))

    studies = ["share-real-a-" + unique_symbol().lower(), "share-real-b-" + unique_symbol().lower()]
    for study in studies:
        await conn.execute(
            """
            INSERT INTO research_golden_search_studies (
                id, strategy_key, symbol, state, status, attempt, created_at_ms, updated_at_ms,
                protocol_json, protocol_hash, receipt_json, budget_cap
            ) VALUES ($1, 'sma_crossover', 'SPY', 'search_running', 'running', 1, 1, 1, '{}'::jsonb, 'h', '{}'::jsonb, 100)
            """,
            study,
        )

    window = (et_midnight_ms(date_of(2025, 1, 2)), et_midnight_ms(date_of(2025, 2, 3)))
    resolved = resolve_code_identity()
    identity = {"source_digest": resolved.source_digest, "environment_digest": resolved.environment_digest, "digest_scheme": resolved.digest_scheme}

    from app.research.golden_search.worker import ResearchWorker

    runs: list[tuple[str, int, float, float]] = []
    run_task = ResearchWorker.run_task

    def timed_run_task(self: ResearchWorker, task: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        started = time.monotonic()
        try:
            return run_task(self, task, **kwargs)
        finally:
            runs.append((str(task["key"])[:12], id(self), started, time.monotonic()))

    monkeypatch.setattr(ResearchWorker, "run_task", timed_run_task)

    handoffs: list[str] = []
    shared = shared_pool_module.SharedWorkerPool(
        workers=2,
        submission_bound=4,
        payload_bound_bytes=shared_pool_module.DEFAULT_PAYLOAD_BOUND_BYTES,
        result_bound_bytes=shared_pool_module.DEFAULT_RESULT_BOUND_BYTES,
    )
    dispatches: list[pool_module.PooledWorkerDispatch] = []
    evaluators: list[StudyEvaluator] = []
    for study, job in zip(studies, ("job-share-a", "job-share-b"), strict=True):
        context = WorkerDispatchContext(
            strategy_key="sma_crossover",
            execution={"fill_mode": "next_bar_open", "commission_per_order": 0.0, "slippage_per_share": 0.0, "initial_cash": 50_000.0},
            manifest=manifest,
            identity=identity,
            job_id=job,
            generation=1,
        )
        dispatches.append(
            pool_module.PooledWorkerDispatch(
                context, workers=2, window=2, pool=shared, on_capacity_handoff=lambda job=job: handoffs.append(job)
            )
        )
        evaluators.append(
            StudyEvaluator(
                study_id=study,
                attempt=1,
                strategy_key="sma_crossover",
                context_digest="shared-real",
                run_up_sessions=1,
                capability=EvaluationCapability(allowed=(window,)),
                budget_limit=100,
                execute=lambda request: None,
                dispatch=dispatches[-1],
            )
        )

    points = [
        {"symbol": "SPY", "short_window": short, "long_window": long, "resolution_minutes": 1}
        for short, long in ((2, 3), (2, 5), (3, 5), (3, 8))
    ]
    results: dict[str, list[Any]] = {}

    def evaluate(study: str, evaluator: StudyEvaluator) -> None:
        results[study] = evaluator.evaluate([dict(point) for point in points], window=window, stage="search")

    try:
        threads = [threading.Thread(target=evaluate, args=(study, evaluator), daemon=True) for study, evaluator in zip(studies, evaluators, strict=True)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=240.0)
    finally:
        for dispatch in dispatches:
            dispatch.close()
        shared.close()
        _close_writer_loop_catalog_pool()

    for study in studies:
        metrics = results[study]
        assert metrics and all(m.status == "completed" for m in metrics), f"{study}: every shared-pool evaluation completed"

    workers_used = {run[1] for run in runs}
    assert len(workers_used) >= 2, "real distinct children served the shared lanes"
    assert len(handoffs) >= 2, "both owners saw capacity handoffs while sharing"
    assert any(a[1] != b[1] and a[2] < b[3] and b[2] < a[3] for a in runs for b in runs), (
        "the studies' evaluations genuinely overlapped on the shared lanes"
    )
