"""Grid Search cells through the research worker runtime (#3049).

Fast tier: the wire and outcome translation, the child's half of one task
(the engine request rebuilt from the wire is the in-process adapter's
request, the projection is grid's own), and both parent-side adapters over
fake children — the serial dispatch's order and loss retry, the pooled
dispatch's overlap, re-offer and cancellation. No real process or database
here; the slow tier's real-children coverage rides the golden worker pool
suite's harness.
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
from app.research.grid_search import dispatch as dispatch_module
from app.research.grid_search.dispatch import (
    GRID_WIRE_KIND,
    CellOutcome,
    ReadyCell,
    WorkerCellDispatch,
    cell_outcome,
    cell_to_wire,
    ready_cell_builder,
    run_grid_cell_task,
)
from app.research.grid_search.models import CellResult, GridSearchSpec, SearchOwner, SearchRow
from app.research.sweep.grid import RunSpec

# The real-child test's reads are admitted against a committed fixture
# catalog, never bypassed — the same contract the golden worker suite runs
# under.
pytestmark = pytest.mark.usefixtures("seeded_lake_catalog")

IDENTITY = {"source_digest": "s" * 64, "environment_digest": "e" * 64, "digest_scheme": 2}
INTERVAL = {"data_start_ms": 1735689600000, "evaluation_start_ms": 1736294400000, "evaluation_end_ms": 1737244800000}
EXECUTION = {
    "strategy_key": "sma_crossover",
    "symbol": "SPY",
    "fill_mode": "signal_bar_close",
    "commission_per_order": 0.0,
    "slippage_per_share": 0.0,
    "initial_cash": 100_000.0,
    "resolution": "minute",
}
MANIFEST = {"SPY/2025/01/2025-01-06.zip": "digest"}


def _candidate(index: int) -> RunSpec:
    return RunSpec(symbol="SPY", strategy_key="sma_crossover", params={"short_window": float(index)}, params_hash=f"hash-{index}")


def _ready_of(candidate: RunSpec, task_gen: int = 1) -> ReadyCell:
    return ReadyCell(
        key=candidate.params_hash,
        attempt=1,
        task_gen=task_gen,
        candidate=candidate,
        interval=INTERVAL,
        execution=EXECUTION,
        manifest=MANIFEST,
        identity=IDENTITY,
    )


def _ready(index: int, task_gen: int = 1) -> ReadyCell:
    return _ready_of(_candidate(index), task_gen)


def _index_of(key: str) -> int:
    return int(key.split("-")[1])


def _result_message(key: str) -> dict[str, Any]:
    index = _index_of(key)
    return {
        "type": "result",
        "key": key,
        "cell": cell_to_wire(
            CellResult(
                params_hash=key,
                params={"short_window": float(index)},
                status="completed",
                total_trades=3,
                net_profit=100.0 + index,
                total_return_pct=0.1,
                sharpe_ratio=1.5,
                max_drawdown_pct=5.0,
                win_rate=0.6,
                bars_consumed=100,
                daily_equity=[(1736294400000, 100_000.0), (1736380800000, 100_100.0)],
            )
        ),
    }


class _Out:
    """The child's writer as a test sees it: collected messages."""

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


def test_a_ready_cell_wires_its_own_identity_interval_and_execution() -> None:
    wire = _ready(1).as_wire()

    assert wire["kind"] == GRID_WIRE_KIND
    assert wire["key"] == "hash-1" and wire["attempt"] == 1 and wire["task_gen"] == 1
    assert wire["candidate"]["params_hash"] == "hash-1"
    assert wire["interval"] == INTERVAL, "the receipt's ms interval, never ISO strings"
    assert wire["execution"]["strategy_key"] == "sma_crossover"
    assert wire["identity"] == IDENTITY


def test_child_answers_translate_to_the_outcomes_the_runner_consumes() -> None:
    item = _ready(1)
    failed = cell_outcome(item, {"type": "failed", "key": "hash-1", "error": "ValueError: no bars"})
    assert failed.cell is not None and failed.cell.status == "failed" and failed.cell.error == "ValueError: no bars"
    assert failed.cell.params_hash == "hash-1", "a failed cell keeps its candidate's identity"

    arrived = cell_outcome(item, _result_message("hash-1"))
    assert arrived.cell is not None and arrived.cell.status == "completed"
    assert arrived.cell.daily_equity == [(1736294400000, 100_000.0), (1736380800000, 100_100.0)], (
        "the curve crosses as pairs, not JSON arrays"
    )
    assert cell_outcome(item, {"type": "result", "key": "hash-1", "cell": cell_to_wire(arrived.cell)}).cell == arrived.cell


# ── The child's half of one task ──────────────────────────────────────────


def test_the_child_builds_the_in_process_request_and_projects_the_grid_cell(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from app.research.grid_search import engine_adapter
    from app.services import engine_backtest_service

    seen: dict[str, Any] = {}

    def fake_engine(*, request: Any, **kwargs: Any) -> Any:
        seen["request"] = request
        seen["kwargs"] = kwargs
        return SimpleNamespace(
            success=True,
            error=None,
            statistics={
                "net_profit": 250.0,
                "net_profit_pct": 0.25,
                "sharpe_ratio": 2.0,
                "max_drawdown_pct": 4.0,
                "win_rate": 0.5,
            },
            total_trades=7,
            net_profit=250.0,
            bars_consumed=512,
            equity_day_ends=[],
        )

    monkeypatch.setattr(engine_backtest_service, "execute_engine_backtest", fake_engine)
    out = _Out()
    run_grid_cell_task(_ready(3).as_wire(), out=out, ask_admission=lambda path: b"", cancel_flag=threading.Event(), state=_child_state())

    expected = engine_adapter.engine_request_over(_candidate(3), interval=INTERVAL, execution=EXECUTION)
    assert seen["request"].model_dump() == expected.model_dump(), "the child's request is the in-process adapter's request"
    assert seen["kwargs"]["data_manifest"] == MANIFEST

    result = next(message for message in out.messages if message["type"] == "result")
    assert result["key"] == "hash-3"
    assert result["cell"]["total_trades"] == 7 and result["cell"]["sharpe_ratio"] == 2.0
    phases = [
        (message["phase"], message["cursor_kind"], message["cursor"])
        for message in out.messages
        if message["type"] == "checkpoint"
    ]
    assert ("preparation", "processed_blocks", 0) in phases
    assert ("search", "durable_completions", 1) in phases, "a completed cell reports the durable cursor"


def test_a_refused_identity_never_computes() -> None:
    refused_out = _Out()
    run_grid_cell_task(
        _ready(1).as_wire(),
        out=refused_out,
        ask_admission=lambda path: b"",
        cancel_flag=threading.Event(),
        state=_child_state(identity={**IDENTITY, "source_digest": "0" * 64}),
    )
    assert any(message["type"] == "identity_refused" for message in refused_out.messages)
    assert not any(message["type"] == "result" for message in refused_out.messages)

# ── The serial adapter over one fake child ────────────────────────────────


class _FakeChild:
    """A ``ResearchWorker`` stand-in speaking the parent-side protocol."""

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
) -> WorkerCellDispatch:
    def spawn(context: WorkerDispatchContext) -> _FakeChild:
        child = _FakeChild(context)
        if behavior is not None:
            child.behavior = behavior
        children.append(child)
        return child

    # The shared serial base resolves the spawn in the worker module's own
    # namespace — the base is the lifecycle, the subclass the policy (#3049).
    from app.research.golden_search import worker as worker_module

    monkeypatch.setattr(worker_module, "ResearchWorker", spawn)
    context = WorkerDispatchContext(
        strategy_key="sma_crossover",
        execution={},
        manifest=MANIFEST,
        identity=IDENTITY,
        job_id="job-grid-test",
        generation=1,
        cancel_check=lambda: None,
    )
    return WorkerCellDispatch(context, _ready_of)


def test_the_serial_dispatch_runs_cells_through_one_child_in_logical_order(monkeypatch: pytest.MonkeyPatch) -> None:
    children: list[_FakeChild] = []
    dispatch = _serial_dispatch(monkeypatch, children)
    try:
        pairs = list(dispatch([_candidate(index) for index in range(3)]))
    finally:
        dispatch.close()

    assert [spec.params_hash for spec, _ in pairs] == ["hash-0", "hash-1", "hash-2"], "logical order, one at a time"
    assert [cell.net_profit for _, cell in pairs] == [100.0, 101.0, 102.0]
    assert len(children) == 1 and len(children[0].tasks) == 3
    assert all(task["kind"] == GRID_WIRE_KIND for task in children[0].tasks), "the grid wire, not the golden one"
    assert children[0].closed


def test_a_lost_child_re_offers_the_cell_and_a_loss_past_the_allowance_stops_the_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    children: list[_FakeChild] = []
    attempts: list[int] = []

    def dying(task: dict[str, Any]) -> dict[str, Any]:
        attempts.append(task["task_gen"])
        raise WorkerLost("the child died")

    dispatch = _serial_dispatch(monkeypatch, children, behavior=dying)
    try:
        with pytest.raises(WorkerLost, match="lost its worker"):
            list(dispatch([_candidate(0)]))
    finally:
        dispatch.close()
    assert attempts == [1, 2, 3], "one re-offer per advanced generation, then the honest stop"


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
) -> tuple[dispatch_module.PooledCellDispatch, list[_PoolFakeChild]]:
    children: list[_PoolFakeChild] = []

    def spawn(context: WorkerDispatchContext) -> _PoolFakeChild:
        child = _PoolFakeChild(context, execute)
        children.append(child)
        return child

    monkeypatch.setattr(shared_pool_module, "ResearchWorker", spawn)
    context = WorkerDispatchContext(
        strategy_key="sma_crossover",
        execution={},
        manifest=MANIFEST,
        identity=IDENTITY,
        job_id="job-grid-test",
        generation=1,
        cancel_check=cancel_check,
    )
    dispatch = dispatch_module.PooledCellDispatch(context, _ready_of, workers=workers, window=window)
    return dispatch, children


def test_pooled_cells_overlap_and_every_arrival_reaches_the_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(task: dict[str, Any]) -> dict[str, Any]:
        time.sleep(0.05)
        return _result_message(str(task["key"]))

    dispatch, children = _pooled_dispatch(monkeypatch, workers=2, window=2, execute=slow)
    try:
        started = time.monotonic()
        pairs = list(dispatch([_candidate(index) for index in range(4)]))
        elapsed = time.monotonic() - started
    finally:
        dispatch.close()

    assert sorted(spec.params_hash for spec, _ in pairs) == [f"hash-{index}" for index in range(4)], (
        "the whole logical set, whatever order it arrived in"
    )
    assert len(children) == 2, "two actual lanes served the search"
    overlap = any(
        children[0].begun[key] < children[1].ended[other] and children[1].begun[other] < children[0].ended[key]
        for key in children[0].begun
        for other in children[1].begun
    )
    assert overlap, "independent cells genuinely overlapped"
    assert elapsed < 4 * 0.05, f"overlap shortened the search ({elapsed:.2f}s for four 50ms cells)"
    assert all(child.closed for child in children)


def test_a_lost_lane_re_offers_only_its_cell_under_an_advanced_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    def lossy(task: dict[str, Any]) -> dict[str, Any]:
        key = str(task["key"])
        if key == "hash-1" and task["task_gen"] == 1:
            raise WorkerLost("the lane died with the cell in flight")
        return _result_message(key)

    dispatch, children = _pooled_dispatch(monkeypatch, workers=2, window=2, execute=lossy)
    try:
        pairs = list(dispatch([_candidate(index) for index in range(3)]))
    finally:
        dispatch.close()

    assert sorted(spec.params_hash for spec, _ in pairs) == ["hash-0", "hash-1", "hash-2"], (
        "the lost cell came back; its siblings never left"
    )
    re_offers = [task for child in children for task in child.tasks if task["key"] == "hash-1"]
    assert sorted(task["task_gen"] for task in re_offers) == [1, 2], "the re-offer carries the advanced generation"


def test_a_loss_past_the_allowance_fails_the_search_never_records_the_cell(monkeypatch: pytest.MonkeyPatch) -> None:
    def always_lost(task: dict[str, Any]) -> dict[str, Any]:
        raise WorkerLost("the lane keeps dying")

    dispatch, _children = _pooled_dispatch(monkeypatch, workers=1, window=1, execute=always_lost)
    try:
        with pytest.raises(WorkerLost, match="lost its worker"):
            list(dispatch([_candidate(0)]))
    finally:
        dispatch.close()


def test_cancellation_interrupts_the_running_lanes_after_a_delivered_cell_stands(monkeypatch: pytest.MonkeyPatch) -> None:
    arrived = threading.Event()
    release = threading.Event()

    def cancelling() -> None:
        if arrived.is_set():
            raise JobCancelled("cancelled between pooled grid cell tasks")

    def first_then_blocked(task: dict[str, Any]) -> dict[str, Any]:
        key = str(task["key"])
        if key == "hash-0":
            return _result_message(key)
        release.wait(timeout=10.0)
        return _result_message(key)

    dispatch, children = _pooled_dispatch(monkeypatch, workers=2, window=2, execute=first_then_blocked, cancel_check=cancelling)

    def second_cell_running() -> bool:
        return any("hash-1" in child.begun for child in children)

    try:
        pairs: list[tuple[RunSpec, CellResult]] = []
        with pytest.raises(JobCancelled):
            for pair in dispatch([_candidate(index) for index in range(3)]):
                pairs.append(pair)
                # The interrupt must land while a lane is genuinely running
                # the next cell, never on a still-queued entry.
                deadline = time.monotonic() + 5.0
                while not second_cell_running() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert second_cell_running(), "the second cell began on a lane before the cancel armed"
                arrived.set()
        assert pairs, "the cell that arrived before the cancel stands"

        interrupted = [child for child in children if child.cancelled]
        assert interrupted, "the lanes still running this owner's cells were interrupted"
    finally:
        release.set()
        dispatch.close()


# ── The runner's dispatch mode ────────────────────────────────────────────


def test_run_grid_counts_and_persists_dispatched_arrivals_like_the_serial_loop() -> None:
    from app.research.grid_search.runner import run_grid

    persisted: list[CellResult] = []
    failed: list[str] = []
    progress: list[tuple[int, int]] = []

    class _Permuting:
        def __call__(self, cells: Iterator[RunSpec]) -> Iterator[tuple[RunSpec, CellResult]]:
            pending = [(spec, self._result(spec)) for spec in cells]
            yield from [pending[2], pending[0], pending[1]]  # arrival order is free

        @staticmethod
        def _result(spec: RunSpec) -> CellResult:
            broke = spec.params_hash == "hash-1"
            return CellResult(
                params_hash=spec.params_hash,
                params=dict(spec.params),
                status="failed" if broke else "completed",
                total_trades=1,
                error="engine failed" if broke else None,
            )

        def close(self) -> None:
            pass

    summary = run_grid(
        [_candidate(index) for index in range(4)],
        expected_cells=5,
        persist=persisted.append,
        on_progress=lambda done, total: progress.append((done, total)),
        on_cell_failed=lambda spec, message: failed.append(f"{spec.params_hash}: {message}"),
        skip_params_hashes=frozenset({"hash-3"}),
        dispatch=_Permuting(),
    )

    assert summary.executed_cells == 3 and summary.completed_cells == 2 and summary.failed_cells == 1
    assert len(persisted) == 3, "each arrival is persisted as it is consumed"
    assert failed == ["hash-1: engine failed"], "a failed arrival reports like a failed cell"
    assert progress[0] == (1, 5), "the skip set counts as done from the first tick"
    assert progress[-1] == (4, 5)


def test_run_grid_propagates_a_dispatch_cancellation_with_nothing_partial_persisted() -> None:
    from app.research.grid_search.runner import run_grid

    persisted: list[CellResult] = []

    class _Cancelling:
        def __call__(self, cells: Iterator[RunSpec]) -> Iterator[tuple[RunSpec, CellResult]]:
            raise JobCancelled("cancelled mid-search")

        def close(self) -> None:
            pass

    with pytest.raises(JobCancelled):
        run_grid([_candidate(0), _candidate(1)], expected_cells=2, persist=persisted.append, dispatch=_Cancelling())
    assert persisted == []


def test_run_grid_takes_exactly_one_executor() -> None:
    from app.research.grid_search.runner import run_grid

    class _Unused:
        def __call__(self, cells: Iterator[RunSpec]) -> Iterator[tuple[RunSpec, CellResult]]:
            yield from ()

        def close(self) -> None:
            pass

    with pytest.raises(ValueError, match="exactly one executor"):
        run_grid([], expected_cells=0, persist=lambda cell: None)
    with pytest.raises(ValueError, match="exactly one executor"):
        run_grid([], expected_cells=0, persist=lambda cell: None, execute_cell=lambda spec: None, dispatch=_Unused())


# ── The runtime builder ───────────────────────────────────────────────────


def _search_row() -> SearchRow:
    return SearchRow(
        id="search-1",
        owner=SearchOwner(),
        strategy_key="sma_crossover",
        symbol="SPY",
        status="queued",
        attempt=0,
        job_id="job-1",
        created_at_ms=0,
        updated_at_ms=0,
        finished_at_ms=None,
        request={},
        receipt={
            "interval_table": {**INTERVAL, "warmup_policy": "uniform_run_up"},
            "data_snapshot": {"artifacts": MANIFEST},
            "code_identity": IDENTITY,
        },
        expected_cells=2,
        completed_cells=0,
        failed_cells=0,
        leader_params_hash=None,
        leader_params=None,
        incomplete=False,
        failure_reason=None,
    )


def test_ready_cell_builder_freezes_the_receipt_parts_into_each_item() -> None:
    spec = GridSearchSpec(strategy_key="sma_crossover", symbol="SPY", param_ranges={}, start_ms=0, end_ms=1)
    build = ready_cell_builder(_search_row(), spec, attempt=3)

    item = build(_candidate(0))

    assert item.attempt == 3 and item.task_gen == 1
    assert item.interval == {key: INTERVAL[key] for key in ("data_start_ms", "evaluation_start_ms", "evaluation_end_ms")}
    assert item.execution["strategy_key"] == "sma_crossover" and item.identity == IDENTITY
    wired = item.as_wire()
    assert wired["attempt"] == 3 and wired["candidate"]["params_hash"] == "hash-0"


def test_cell_outcome_fields_name_their_meaning() -> None:
    outcome = CellOutcome(key="k", attempt=1, task_gen=1, cell=None)
    assert outcome.lost_reason is None


# ── A real spawned child (slow: real process, real engine) ────────────────


@pytest.mark.slow
def test_a_spawned_worker_returns_the_in_process_cell_exactly(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The dispatched cell is the locally executed cell, number for number (#3049)."""
    from datetime import date

    from app.config import settings
    from app.research.grid_search.engine_adapter import default_execute_cell
    from app.research.grid_search.models import SearchOwner
    from app.research.sweep.grid import ValueListRange
    from app.research.sweep.identity import resolve_code_identity
    from tests.research.golden_search.test_worker import _reference_lake

    write_root, _lake, manifest = _reference_lake(tmp_path)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))  # this process's settings object
    monkeypatch.setenv("LEAN_DATA_WRITE_ROOT", str(write_root))  # the child reads env, not the object

    from app.utils.session_anchors import et_midnight_ms

    start_ms, end_ms = et_midnight_ms(date(2025, 1, 2)), et_midnight_ms(date(2025, 1, 3))
    spec = GridSearchSpec(
        strategy_key="sma_crossover",
        symbol="SPY",
        param_ranges={"short_window": ValueListRange((2.0,)), "long_window": ValueListRange((3.0,))},
        start_ms=start_ms,
        end_ms=end_ms,
        min_trades=0,
    )
    row = SearchRow(
        id="search-child-parity",
        owner=SearchOwner(),
        strategy_key="sma_crossover",
        symbol="SPY",
        status="queued",
        attempt=0,
        job_id="job-grid-child-parity",
        created_at_ms=0,
        updated_at_ms=0,
        finished_at_ms=None,
        request=spec.as_request_dict(),
        receipt={
            "interval_table": {
                "data_start_ms": start_ms,
                "evaluation_start_ms": start_ms,
                "evaluation_end_ms": end_ms,
            },
            "data_snapshot": {"artifacts": manifest},
            "code_identity": resolve_code_identity().as_dict(),
        },
        expected_cells=1,
        completed_cells=0,
        failed_cells=0,
        leader_params_hash=None,
        leader_params=None,
        incomplete=False,
        failure_reason=None,
    )
    candidate = RunSpec(
        symbol="SPY",
        strategy_key="sma_crossover",
        params={"short_window": 2.0, "long_window": 3.0},
        params_hash="parity-1",
    )

    local = default_execute_cell(row, spec)(candidate)
    assert local.status == "completed", f"the control must run real bars ({local.error})"

    context = WorkerDispatchContext(
        strategy_key="sma_crossover",
        execution={},
        manifest=manifest,
        identity=row.receipt["code_identity"],
        job_id="job-grid-child-parity",
        generation=1,
    )
    dispatch = WorkerCellDispatch(context, ready_cell_builder(row, spec, attempt=1))
    try:
        pairs = list(dispatch([candidate]))
    finally:
        dispatch.close()

    assert pairs == [(candidate, local)], "the child is the in-process adapter, cell for cell"


def test_a_straggler_from_a_superseded_re_offer_is_discarded_never_persisted(monkeypatch: pytest.MonkeyPatch) -> None:
    def answer(task: dict[str, Any]) -> dict[str, Any]:
        return _result_message(str(task["key"]))

    children: list[_PoolFakeChild] = []

    def spawn(context: WorkerDispatchContext) -> _PoolFakeChild:
        child = _PoolFakeChild(context, answer)
        children.append(child)
        return child

    monkeypatch.setattr(shared_pool_module, "ResearchWorker", spawn)
    context = WorkerDispatchContext(
        strategy_key="sma_crossover", execution={}, manifest=MANIFEST, identity=IDENTITY,
        job_id="job-grid-straggler", generation=1, cancel_check=lambda: None,
    )
    dispatch = dispatch_module.PooledCellDispatch(context, _ready_of, workers=1, window=1)
    try:
        # A stale-generation arrival lands before any real one: the owed item
        # still holds task_gen 1, so the answer for generation 9 is discarded
        # — consumed for budget, never delivered to the runner.
        dispatch._handle.arrived.put(("hash-0", 9, _straggler_outcome()))
        pairs = list(dispatch([_candidate(0)]))
    finally:
        dispatch.close()

    assert [spec.params_hash for spec, _ in pairs] == ["hash-0"], "the real generation's cell arrived once"
    assert pairs[0][1].net_profit == 100.0, "the persisted cell is the real answer, not the straggler's"


def _straggler_outcome() -> CellOutcome:
    return CellOutcome(
        key="hash-0",
        attempt=1,
        task_gen=9,
        cell=CellResult(params_hash="hash-0", params={}, status="completed", total_trades=99, net_profit=-1.0),
    )
