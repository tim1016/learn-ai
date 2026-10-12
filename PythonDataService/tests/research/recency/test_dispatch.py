"""Recency runs through the research worker runtime (#3051).

Fast tier: the wire and outcome translation, the child's half of one task
(the engine request rebuilt from the wire is the in-process executor's
request; the pieces cross and the coordinator's snapshot over them equals
the snapshot over the engine's own response), and both parent-side
adapters over fake children — order, loss re-offer, cancellation. No real
process or database here.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from app.jobs.progress import JobCancelled
from app.research.golden_search import shared_pool as shared_pool_module
from app.research.golden_search.evaluator import WorkerLost
from app.research.golden_search.worker import WorkerDispatchContext
from app.research.recency import dispatch as dispatch_module
from app.research.recency.dispatch import (
    RECENCY_WIRE_KIND,
    ReadyRun,
    RunOutcome,
    WorkerRunDispatch,
    pieces_to_wire,
    ready_run_builder,
    run_key,
    run_outcome,
    run_recency_run_task,
)
from app.research.sweep.grid import RunSpec, StrategyGridConfig, ValueListRange

IDENTITY = {"source_digest": "s" * 64, "environment_digest": "e" * 64, "digest_scheme": 2}
WINDOW = {"start_ms": 1735689600000, "end_ms": 1738368000000}
EXECUTION = {"fill_mode": "signal_bar_close", "commission_per_order": 0.0}


def _run(index: int) -> RunSpec:
    return RunSpec(symbol="SPY", strategy_key="sma_crossover", params={"short_window": float(index)}, params_hash=f"hash-{index}")


def _ready_of(run_spec: RunSpec, task_gen: int = 1) -> ReadyRun:
    return ReadyRun(
        key=run_key(run_spec.symbol, run_spec.strategy_key, run_spec.params_hash),
        attempt=1,
        task_gen=task_gen,
        run_spec=run_spec,
        window=WINDOW,
        execution=EXECUTION,
        identity=IDENTITY,
    )


def _index_of(key: str) -> int:
    return int(key.rsplit("|", 1)[1].split("-")[1])


def _pieces(index: int) -> dict[str, Any]:
    return {
        "data_policy": '{"adjusted": true, "symbol": "SPY"}',
        "trades": [
            {
                "entry_ms": 1735700000000,
                "exit_ms": 1735800000000,
                "pnl_pts": 1.5,
                "pnl_pct": 0.015,
                "quantity": 100 + index,
                "is_synthetic_exit": False,
                "signal_reason": "cross_up",
            }
        ],
        "study_id": None,
    }


def _result_message(key: str) -> dict[str, Any]:
    return {"type": "result", "key": key, "run": _pieces(_index_of(key))}


class _Out:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def write(self, message: dict[str, Any]) -> None:
        self.messages.append(dict(message))


class _Cache:
    def take_residency_delta(self) -> tuple[list, list]:
        return [], []


def _child_state(identity: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"identity": identity or IDENTITY, "completed": 0, "decoded_cache": _Cache()}


# ── The wire and its outcome translation ──────────────────────────────────


def test_the_run_key_is_the_composite_cell_identity() -> None:
    assert run_key("SPY", "sma", "abc") != run_key("QQQ", "sma", "abc"), "the same params across symbols are two runs"
    assert run_key("SPY", "sma", "abc") != run_key("SPY", "rsi", "abc"), "and across strategies"


def test_a_ready_run_wires_its_window_execution_and_identity() -> None:
    wire = _ready_of(_run(1)).as_wire()

    assert wire["kind"] == RECENCY_WIRE_KIND
    assert wire["key"] == "SPY|sma_crossover|hash-1"
    assert wire["attempt"] == 1 and wire["task_gen"] == 1
    assert wire["run"]["params_hash"] == "hash-1"
    assert wire["window"] == WINDOW, "the launch's ms window, never ISO strings"
    assert wire["execution"] == EXECUTION
    assert wire["identity"] == IDENTITY


def test_child_answers_translate_to_the_outcomes_the_runner_consumes() -> None:
    item = _ready_of(_run(1))
    cancelled = run_outcome(item, {"type": "failed", "key": item.key, "error": "cancelled"})
    assert cancelled.cancelled_by is not None and cancelled.pieces is None

    failed = run_outcome(item, {"type": "failed", "key": item.key, "error": "RuntimeError: no bars"})
    assert failed.error == "RuntimeError: no bars" and failed.pieces is None

    arrived = run_outcome(item, _result_message(item.key))
    assert arrived.pieces is not None and arrived.pieces["trades"][0]["quantity"] == 101


# ── The child's half of one task, and the snapshot parity it buys ─────────


def _fake_response(index: int) -> Any:
    from types import SimpleNamespace

    class _Policy:
        def model_dump_json(self) -> str:
            return '{"adjusted": true, "symbol": "SPY"}'

    return SimpleNamespace(
        success=True,
        error=None,
        data_policy=_Policy(),
        trades=[
            SimpleNamespace(
                entry_time=1735700000000,
                exit_time=1735800000000,
                pnl_pts=1.5,
                pnl_pct=0.015,
                quantity=100 + index,
                is_synthetic_exit=False,
                signal_reason="cross_up",
            )
        ],
        study_id=None,
    )


def test_the_child_builds_the_in_process_request_and_returns_the_snapshot_pieces(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research.recency import service as recency_service
    from app.services import engine_backtest_service

    seen: dict[str, Any] = {}

    def fake_engine(*, request: Any, **kwargs: Any) -> Any:
        seen["request"] = request
        seen["kwargs"] = kwargs
        return _fake_response(3)

    monkeypatch.setattr(engine_backtest_service, "execute_engine_backtest", fake_engine)
    out = _Out()
    run_recency_run_task(_ready_of(_run(3)).as_wire(), out=out, ask_admission=lambda path: b"", cancel_flag=threading.Event(), state=_child_state())

    expected = recency_service.engine_request_over(_run(3), window=WINDOW, execution=EXECUTION)
    assert seen["request"].model_dump() == expected.model_dump(), "the child's request is the in-process executor's request"

    result = next(message for message in out.messages if message["type"] == "result")
    assert result["run"]["trades"][0]["quantity"] == 103
    assert result["run"]["data_policy"] == '{"adjusted": true, "symbol": "SPY"}'
    phases = [(m["phase"], m["cursor_kind"], m["cursor"]) for m in out.messages if m["type"] == "checkpoint"]
    assert ("preparation", "processed_blocks", 0) in phases
    assert ("search", "durable_completions", 1) in phases


def test_an_unsuccessful_or_cancelled_run_delivers_never_computes(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from app.services import engine_backtest_service

    def failing_engine(*, request: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(success=False, error="engine refused the window", data_policy=None, trades=[], study_id=None)

    monkeypatch.setattr(engine_backtest_service, "execute_engine_backtest", failing_engine)
    out = _Out()
    run_recency_run_task(_ready_of(_run(1)).as_wire(), out=out, ask_admission=lambda path: b"", cancel_flag=threading.Event(), state=_child_state())
    failed = next(message for message in out.messages if message["type"] == "failed")
    assert "engine refused the window" in failed["error"]

    def cancelled_engine(*, request: Any, **kwargs: Any) -> Any:
        kwargs["while_waiting"]()
        raise AssertionError("a cancelled run never proceeds past its wait hook")

    monkeypatch.setattr(engine_backtest_service, "execute_engine_backtest", cancelled_engine)
    cancelled_out = _Out()
    flag = threading.Event()
    flag.set()
    run_recency_run_task(_ready_of(_run(1)).as_wire(), out=cancelled_out, ask_admission=lambda path: b"", cancel_flag=flag, state=_child_state())
    assert next(message for message in cancelled_out.messages if message["type"] == "failed")["error"] == "cancelled"


def test_the_coordinators_snapshot_over_the_pieces_equals_the_snapshot_over_the_response() -> None:
    """The parity receipt (#3051): pieces crossing the wire buy the identical persisted evidence."""
    from app.research.recency.dispatch import RunPieces
    from app.research.recency.runner import RecencyLaunchConfig, _snapshot_for_run

    config = RecencyLaunchConfig(
        launch_id="launch-1",
        strategies=[StrategyGridConfig(strategy_key="sma_crossover", param_ranges={"short_window": ValueListRange((3.0,))})],
        symbols=["SPY"],
        window_start_ms=WINDOW["start_ms"],
        window_end_ms=WINDOW["end_ms"],
    )
    response = _fake_response(3)
    over_response = _snapshot_for_run(_run(3), config, response, "version-7")
    over_pieces = _snapshot_for_run(_run(3), config, RunPieces(pieces_to_wire(response)), "version-7")

    assert over_pieces == over_response, "trades, fingerprints, P&L and Sharpe are identical over the wire"


# ── The serial adapter over one fake child ────────────────────────────────


class _FakeChild:
    def __init__(self, context: WorkerDispatchContext) -> None:
        self.context = context
        self.tasks: list[dict[str, Any]] = []
        self.closed = False
        self.cancelled = False
        self.behavior: Callable[[dict[str, Any]], dict[str, Any]] = lambda task: _result_message(str(task["key"]))

    def lost_reason(self) -> str | None:
        return None

    def run_task(self, task: dict[str, Any]) -> dict[str, Any]:
        self.tasks.append(task)
        return self.behavior(task)

    def cancel(self) -> None:
        self.cancelled = True
        self.closed = True

    def close(self) -> None:
        self.closed = True


def _serial_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    children: list[_FakeChild],
    behavior: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> WorkerRunDispatch:
    from app.research.golden_search import worker as worker_module

    def spawn(context: WorkerDispatchContext) -> _FakeChild:
        child = _FakeChild(context)
        if behavior is not None:
            child.behavior = behavior
        children.append(child)
        return child

    monkeypatch.setattr(worker_module, "ResearchWorker", spawn)
    context = WorkerDispatchContext(
        strategy_key="recency", execution={}, manifest={}, identity=IDENTITY,
        job_id="job-recency-test", generation=1, cancel_check=lambda: None,
    )
    return WorkerRunDispatch(context, _ready_of)


def test_the_serial_dispatch_runs_in_logical_order_over_the_composite_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    children: list[_FakeChild] = []
    dispatch = _serial_dispatch(monkeypatch, children)
    try:
        arrivals = list(dispatch([_run(index) for index in range(3)]))
    finally:
        dispatch.close()

    assert [run.params_hash for run, _pieces, _error in arrivals] == ["hash-0", "hash-1", "hash-2"]
    assert all(pieces is None or pieces["trades"][0]["quantity"] == 100 + index for index, (_r, pieces, _e) in enumerate(arrivals))
    assert len(children) == 1 and all(task["kind"] == RECENCY_WIRE_KIND for task in children[0].tasks)
    assert children[0].closed


def test_a_lost_child_re_offers_and_a_delivered_cancellation_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[int] = []

    def dying(task: dict[str, Any]) -> dict[str, Any]:
        attempts.append(task["task_gen"])
        raise WorkerLost("the child died")

    children: list[_FakeChild] = []
    dispatch = _serial_dispatch(monkeypatch, children, behavior=dying)
    try:
        with pytest.raises(WorkerLost, match="lost its worker"):
            list(dispatch([_run(0)]))
    finally:
        dispatch.close()
    assert attempts == [1, 2, 3]

    cancelling = lambda task: {"type": "failed", "key": task["key"], "error": "cancelled"}  # noqa: E731
    children2: list[_FakeChild] = []
    dispatch2 = _serial_dispatch(monkeypatch, children2, behavior=cancelling)
    try:
        with pytest.raises(JobCancelled):
            list(dispatch2([_run(0), _run(1)]))
    finally:
        dispatch2.close()
    assert children2[0].cancelled


# ── The pooled adapter over fake children on the real pool ────────────────


class _PoolFakeChild:
    def __init__(self, context: WorkerDispatchContext, execute: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self.context = context
        self.tasks: list[dict[str, Any]] = []
        self.closed = False
        self.cancelled = False
        self.execute = execute
        self.begun: dict[str, float] = {}
        self.ended: dict[str, float] = {}

    def begin_task(self, *, cancel_check: Any = None, on_checkpoint: Any = None) -> None:
        self.context.cancel_check = cancel_check or self.context.cancel_check
        self.context.on_checkpoint = on_checkpoint or self.context.on_checkpoint

    def run_task(self, task: dict[str, Any]) -> dict[str, Any]:
        self.tasks.append(task)
        key = str(task["key"])
        self.begun[key] = time.monotonic()
        try:
            return self.execute(task)
        finally:
            self.ended[key] = time.monotonic()

    def lost_reason(self) -> str | None:
        return None

    def cancel(self) -> None:
        self.cancelled = True
        self.closed = True

    def close(self) -> None:
        self.closed = True


def _pooled_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    workers: int,
    window: int,
    execute: Callable[[dict[str, Any]], dict[str, Any]],
    cancel_check: Callable[[], object] = lambda: None,
) -> tuple[dispatch_module.PooledRunDispatch, list[_PoolFakeChild]]:
    children: list[_PoolFakeChild] = []

    def spawn(context: WorkerDispatchContext) -> _PoolFakeChild:
        child = _PoolFakeChild(context, execute)
        children.append(child)
        return child

    monkeypatch.setattr(shared_pool_module, "ResearchWorker", spawn)
    context = WorkerDispatchContext(
        strategy_key="recency", execution={}, manifest={}, identity=IDENTITY,
        job_id="job-recency-test", generation=1, cancel_check=cancel_check,
    )
    dispatch = dispatch_module.PooledRunDispatch(context, _ready_of, workers=workers, window=window)
    return dispatch, children


def test_pooled_runs_overlap_and_every_arrival_reaches_the_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(task: dict[str, Any]) -> dict[str, Any]:
        time.sleep(0.05)
        return _result_message(str(task["key"]))

    dispatch, children = _pooled_dispatch(monkeypatch, workers=2, window=2, execute=slow)
    try:
        started = time.monotonic()
        arrivals = list(dispatch([_run(index) for index in range(4)]))
        elapsed = time.monotonic() - started
    finally:
        dispatch.close()

    assert sorted(run.params_hash for run, _p, _e in arrivals) == [f"hash-{index}" for index in range(4)]
    assert len(children) == 2
    overlap = any(
        children[0].begun[key] < children[1].ended[other] and children[1].begun[other] < children[0].ended[key]
        for key in children[0].begun
        for other in children[1].begun
    )
    assert overlap, "independent runs genuinely overlapped"
    assert elapsed < 4 * 0.05


def test_a_lost_lane_re_offers_only_its_run_under_an_advanced_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    def lossy(task: dict[str, Any]) -> dict[str, Any]:
        key = str(task["key"])
        if key.endswith("hash-1") and task["task_gen"] == 1:
            raise WorkerLost("the lane died with the run in flight")
        return _result_message(key)

    dispatch, children = _pooled_dispatch(monkeypatch, workers=2, window=2, execute=lossy)
    try:
        arrivals = list(dispatch([_run(index) for index in range(3)]))
    finally:
        dispatch.close()

    assert sorted(run.params_hash for run, _p, _e in arrivals) == ["hash-0", "hash-1", "hash-2"]
    re_offers = [task for child in children for task in child.tasks if str(task["key"]).endswith("hash-1")]
    assert sorted(task["task_gen"] for task in re_offers) == [1, 2]


def test_a_failed_run_arrives_as_its_error_and_never_aborts_the_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    def half_fails(task: dict[str, Any]) -> dict[str, Any]:
        key = str(task["key"])
        if key.endswith("hash-1"):
            return {"type": "failed", "key": key, "error": "ValueError: no bars for the window"}
        return _result_message(key)

    dispatch, _children = _pooled_dispatch(monkeypatch, workers=2, window=2, execute=half_fails)
    try:
        arrivals = list(dispatch([_run(index) for index in range(3)]))
    finally:
        dispatch.close()

    by_hash = {run.params_hash: (pieces, error) for run, pieces, error in arrivals}
    assert by_hash["hash-1"] == (None, "ValueError: no bars for the window"), "a failed run is per-run isolation"
    assert by_hash["hash-0"][0] is not None and by_hash["hash-2"][0] is not None


# ── The runner's dispatch mode ────────────────────────────────────────────


def test_run_recency_dispatch_mode_counts_skips_failures_and_snapshots_like_the_serial_loop() -> None:
    from app.research.recency.runner import RecencyLaunchConfig, run_recency

    config = RecencyLaunchConfig(
        launch_id="launch-1",
        strategies=[StrategyGridConfig(strategy_key="sma", param_ranges={"x": ValueListRange((1.0,))})],
        symbols=["SPY"],
        window_start_ms=WINDOW["start_ms"],
        window_end_ms=WINDOW["end_ms"],
    )
    persisted: list[Any] = []
    failed: list[str] = []
    progress: list[tuple[int, int]] = []

    config.strategies[0].param_ranges["x"] = ValueListRange((1.0, 2.0, 3.0))

    class _Permuting:
        def __call__(self, runs: Iterator[RunSpec]) -> Iterator[tuple[RunSpec, dict[str, Any] | None, str | None]]:
            pending = [(spec, _pieces(index), None) for index, spec in enumerate(runs)]
            pending[1] = (pending[1][0], None, "RuntimeError: no bars")
            yield from [pending[2], pending[0], pending[1]]  # arrival order is free

    summary = run_recency(
        config,
        persist_fn=persisted.append,
        strategy_code_version_fn=lambda strategy_key: "version-7",
        on_progress=lambda done, total: progress.append((done, total)),
        on_run_failed=lambda run_spec, message: failed.append(f"{run_spec.params_hash}: {message}"),
        dispatch=_Permuting(),
    )

    assert summary.expected_runs == 3
    assert summary.succeeded_runs == 2 and summary.failed_runs == 1 and summary.skipped_runs == 0
    assert len(persisted) == 2, "each succeeded arrival's snapshot persisted as it was consumed"
    assert len(failed) == 1 and failed[0].endswith("RuntimeError: no bars")
    assert progress[0] == (0, 3) and progress[-1] == (3, 3)
    assert all(snapshot.launch_id == "launch-1" for snapshot in persisted)


def test_run_recency_dispatch_mode_propagates_the_final_cancellation_poll() -> None:
    from app.research.recency.runner import RecencyLaunchConfig, run_recency

    config = RecencyLaunchConfig(
        launch_id="launch-2",
        strategies=[StrategyGridConfig(strategy_key="sma", param_ranges={"x": ValueListRange((1.0,))})],
        symbols=["SPY"],
        window_start_ms=0,
        window_end_ms=1,
    )
    polls = {"count": 0}

    def cancelling() -> None:
        polls["count"] += 1
        if polls["count"] >= 1:
            raise JobCancelled("cancelled after the last run")

    class _One:
        def __call__(self, runs: Iterator[RunSpec]) -> Iterator[tuple[RunSpec, dict[str, Any] | None, str | None]]:
            for spec in runs:
                yield spec, _pieces(0), None

    with pytest.raises(JobCancelled):
        run_recency(
            config,
            persist_fn=lambda snapshot: None,
            strategy_code_version_fn=lambda strategy_key: "v",
            cancel_check=cancelling,
            dispatch=_One(),
        )
    assert polls["count"] == 1, "the once-more poll after the last run observed the cancel (#1928)"


def test_run_recency_takes_exactly_one_executor() -> None:
    from app.research.recency.runner import RecencyLaunchConfig, run_recency

    config = RecencyLaunchConfig(
        launch_id="launch-3",
        strategies=[StrategyGridConfig(strategy_key="sma", param_ranges={})],
        symbols=["SPY"],
        window_start_ms=0,
        window_end_ms=1,
    )
    with pytest.raises(ValueError, match="exactly one executor"):
        run_recency(config, persist_fn=lambda snapshot: None, strategy_code_version_fn=lambda key: "v")
    with pytest.raises(ValueError, match="exactly one executor"):
        run_recency(
            config, persist_fn=lambda snapshot: None, strategy_code_version_fn=lambda key: "v",
            execute_backtest_fn=lambda run, config: None, dispatch=lambda runs: iter(()),
        )


# ── The runtime builder ───────────────────────────────────────────────────


def test_ready_run_builder_freezes_the_launch_parts() -> None:
    build = ready_run_builder(WINDOW, EXECUTION, IDENTITY, attempt=4)
    item = build(_run(0))

    assert item.attempt == 4 and item.task_gen == 1
    wired = item.as_wire()
    assert wired["attempt"] == 4 and wired["key"] == "SPY|sma_crossover|hash-0"
    assert wired["window"] == WINDOW and wired["execution"] == EXECUTION


def test_run_outcome_fields_name_their_meaning() -> None:
    outcome = RunOutcome(key="k", attempt=1, task_gen=1, pieces=None)
    assert outcome.error is None and outcome.cancelled_by is None and outcome.lost_reason is None
