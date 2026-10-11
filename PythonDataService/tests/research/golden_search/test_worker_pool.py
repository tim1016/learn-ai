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
import logging
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
    ``cancel``/``close`` are bookkeeping.
    """

    def __init__(self, context: WorkerDispatchContext) -> None:
        self.context = context
        self.tasks: list[dict[str, Any]] = []
        self.closed = False
        self.execute: Callable[[dict[str, Any]], dict[str, Any]] = lambda task: _gap_message(task)

    def run_task(self, task: dict[str, Any]) -> dict[str, Any]:
        self.tasks.append(task)
        return self.execute(task)

    def cancel(self) -> None:
        self.closed = True

    def close(self) -> None:
        self.closed = True


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
) -> tuple[pool_module.PooledWorkerDispatch, list[_FakeChild], _Intervals]:
    """Spawn the real pool over fake children; every child runs ``execute`` under interval tracking."""
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

    monkeypatch.setattr(pool_module, "ResearchWorker", spawn)
    dispatch = pool_module.PooledWorkerDispatch(
        WorkerDispatchContext(**CONTEXT), workers=workers, window=window, on_facts=on_facts
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

        started = time.monotonic()
        metrics = evaluator.evaluate([_point(gap / 10) for gap in range(1, 7)], window=JAN, stage="search")
        elapsed = time.monotonic() - started

        assert [m.net_profit for m in metrics] == [gap / 10 for gap in range(1, 7)], "input order, whatever arrived"
        assert len(children) == 2, "two actual lanes served the batch"
        assert intervals.pairs_overlapping() >= 1, "independent evaluations genuinely overlapped"
        assert intervals.max_in_flight() <= 2, "the window bounds begun-but-undelivered work"
        assert elapsed < 6 * 0.05, f"overlap shortened the batch ({elapsed:.2f}s for six 50ms items)"
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


def test_a_straggler_from_a_superseded_dispatch_never_counts_or_records_twice(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A lane still in flight when a sibling's loss interrupts the batch: its old-generation outcome arrives late."""
    slow_started = threading.Event()
    release_slow = threading.Event()

    def execute(task: dict[str, Any]) -> dict[str, Any]:
        gap = float(task["point"]["gap"])
        if gap == 0.4 and task["task_gen"] == 1:
            slow_started.set()
            release_slow.wait(timeout=5.0)  # still running when the loss interrupts the batch
            # Released by the fresh run's start: the twin has a head start, so it
            # finishes FIRST — the production ordering, not the kind one.
            return _gap_message(task)
        if gap == 0.4 and task["task_gen"] >= 2:
            release_slow.set()  # the re-admitted run frees its superseded twin
            time.sleep(0.05)  # and finishes after it
        if gap == 0.1 and task["task_gen"] == 1:
            assert slow_started.wait(timeout=5.0), "the slow lane's run had begun before the loss"
            raise WorkerLost("a lane died while another's task was still in flight")
        return _gap_message(task)

    dispatch, _, _ = _wire_pool(monkeypatch, workers=2, window=2, execute=execute)
    try:
        bridge = _ReservationBridge(limit=10)
        evaluator = _seamed_evaluator(monkeypatch, _EngineSpy(), bridge)
        evaluator.dispatch = dispatch

        metrics = evaluator.evaluate([_point(0.1), _point(0.4), _point(0.2)], window=JAN, stage="search")

        assert [m.net_profit for m in metrics] == [0.1, 0.4, 0.2], "no adapter-breach error: the straggler did not count"
        slow_key = _key_of(evaluator, _point(0.4))
        assert bridge.completed.count(slow_key) == 1, "the superseded generation's outcome was discarded, not recorded"
        assert bridge.rows[slow_key]["task_gen"] >= 2, "the durable row answered to the current generation"
    finally:
        release_slow.set()
        dispatch.close()

    discarded = [record for record in caplog.records if record.action.endswith("_discarded")]
    assert discarded, "the straggler's discard is explicit, not silent"


def test_cancellation_between_pooled_tasks_stops_every_child(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.jobs.progress import JobCancelled

    armed = threading.Event()

    def execute(task: dict[str, Any]) -> dict[str, Any]:
        message = _gap_message(task)
        armed.set()  # cancellation arrives once execution has begun, not before dispatch
        return message

    dispatch, children, _ = _wire_pool(monkeypatch, workers=2, window=2, execute=execute)

    def cancelled() -> None:
        if armed.is_set():
            raise JobCancelled("cancelled by the owner")

    dispatch._context.cancel_check = cancelled
    try:
        bridge = _ReservationBridge(limit=10)
        evaluator = _seamed_evaluator(monkeypatch, _EngineSpy(), bridge)
        evaluator.dispatch = dispatch
        with pytest.raises(JobCancelled):
            evaluator.evaluate([_point(0.1), _point(0.2), _point(0.3)], window=JAN, stage="search")
        assert any(child.closed for child in children), "the children were told to stop"
        every = {_key_of(evaluator, _point(gap)) for gap in (0.1, 0.2, 0.3)}
        # Whatever arrived before the cancellation is durable; the rest released.
        assert set(bridge.released) == every - set(bridge.completed)
    finally:
        dispatch.close()


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


def test_a_loss_with_queued_stale_work_recovers_without_lane_death(monkeypatch: pytest.MonkeyPatch) -> None:
    """A loss while the window holds un-started items: the epoch discards them, no lane dies, the batch lands (#3046)."""
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
        assert all(lane.is_alive() for lane in dispatch._lanes), "no lane died on the stale queue"
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

    monkeypatch.setattr(pool_module, "ResearchWorker", spawn)
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


@pytest.mark.slow
@pytest.mark.usefixtures("seeded_lake_catalog")
async def test_two_real_children_overlap_independent_evaluations(conn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two actual spawned workers overlap — not a synchronous function in a serial loop (#3046's first AC)."""
    import hashlib
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
    assert widths[4] < widths[1] * 1.5, "width never made the corpus dramatically slower (transport sanity)"
