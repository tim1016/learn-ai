"""The spawned research worker: one lane, proxied admission, identical answers (#3044).

Fast tier: the wire's round-trips — dates as ET-midnight anchors, scenarios,
full keys, metrics — because a malformed task is a silently different
evaluation. Slow tier: a real spawned child runs one evaluation over a
reference lake and returns the serial answer's exact metrics; a sealed
identity that differs refuses; kill-and-reap leaves no orphan; and a study
stage runs to completion through the worker dispatch.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from app.config import settings
from app.research.golden_search.evaluator import (
    EvaluationRequest,
    IdentityRefusalError,
    ReadyEvaluation,
)
from app.research.golden_search.selection import Metrics
from app.research.golden_search.worker import (
    CHILD_ENV_FIXED,
    FENCE_POLL_INTERVAL_S,
    ResearchWorker,
    WorkerDispatch,
    WorkerDispatchContext,
    WorkerLost,
    request_of_wire,
    task_wire,
)

pytestmark = pytest.mark.usefixtures("seeded_lake_catalog")

IDENTITY = {"source_digest": "s" * 64, "environment_digest": "e" * 64, "digest_scheme": 2}
# ET trading dates over the seeded days: the engine scores real bars (#3044).
from app.utils.session_anchors import et_midnight_ms as _et_midnight  # noqa: E402

WINDOW = (_et_midnight(date(2025, 1, 2)), _et_midnight(date(2025, 1, 3)))


def _ready_item(point_gap: float = 0.2, *, detail: bool = False) -> ReadyEvaluation:
    from app.research.golden_search.protocol import StressScenario

    scenario = StressScenario("harsh", "Harsh", slippage_add=0.01) if detail else None
    request = EvaluationRequest(
        point={"symbol": "SPY", "gap": point_gap, "gap_bps": 0.0, "rsi_min": 50.0, "rsi_max": 70.0},
        point_hash="p" * 64,
        window=WINDOW,
        warmup_from=date(2024, 12, 30),
        scenario=scenario,
        detail=detail,
        stage="search",
        fold_index=None,
    )
    return ReadyEvaluation(key="k" * 64, attempt=3, task_gen=1, request=request)


# ── The wire (fast) ────────────────────────────────────────────────────────


def test_the_wire_round_trips_dates_scenarios_and_the_full_key() -> None:
    item = _ready_item(detail=True)
    task = task_wire(
        item,
        strategy_key="ema_crossover_signal",
        execution={"fill_mode": "next_bar_open", "commission_per_order": 1.0, "slippage_per_share": 0.01, "initial_cash": 50_000.0},
        manifest={"equity/usa/minute/spy/20250101_trade.zip": "a" * 64},
        identity=IDENTITY,
    )

    import json

    encoded = json.dumps(task, allow_nan=False)  # the child's exact framing
    rebuilt, strategy_key = request_of_wire(json.loads(encoded))

    assert strategy_key == "ema_crossover_signal"
    assert rebuilt.point == item.request.point and rebuilt.point_hash == item.request.point_hash
    assert rebuilt.window == item.request.window
    assert rebuilt.warmup_from == item.request.warmup_from, "dates cross as ET-midnight anchors and come back"
    assert rebuilt.scenario is not None and rebuilt.scenario.slippage_add == 0.01
    assert rebuilt.detail and rebuilt.stage == "search" and rebuilt.fold_index is None


def test_metrics_round_trip_through_the_wire() -> None:
    metrics = Metrics(status="completed", total_trades=2, net_profit=750.0, total_return_pct=0.0075,
                      sharpe_ratio=1.25, max_drawdown_pct=0.004, win_rate=0.5)
    import json

    assert Metrics.from_dict(json.loads(json.dumps(metrics.as_dict(), allow_nan=False))) == metrics


def test_the_child_environment_is_pinned_before_interpreter_start() -> None:
    assert CHILD_ENV_FIXED == {
        "PYTHONHASHSEED": "0", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
    }, "deterministic hashing and single-threaded native math, neither settable by an initializer"


# ── A real spawned child (slow: real process, real engine) ────────────────


def _reference_lake(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    """An adjusted-root lake (the engine's default policy) with admitted days."""
    import hashlib
    from uuid import UUID

    from app.data_lake.root_identity import init_empty_root
    from tests._helpers.lake_fixture import seed_lake_minute_day

    base = tmp_path / "writer-root"
    context = init_empty_root(base, UUID(int=3044))
    lake = context.lake_root("polygon_split_adjusted")
    lake.mkdir(parents=True)
    manifest: dict[str, str] = {}
    for day in (date(2025, 1, 2), date(2025, 1, 3)):
        trade_zip, _quote = seed_lake_minute_day(lake, "SPY", day)
        relative = trade_zip.relative_to(lake).as_posix()
        manifest[relative] = hashlib.sha256(trade_zip.read_bytes()).hexdigest()
        companion = trade_zip.with_name(trade_zip.name + ".adjustment.json")
        if companion.exists():
            manifest[companion.relative_to(lake).as_posix()] = hashlib.sha256(companion.read_bytes()).hexdigest()
    return base, lake, manifest


def _assumptions() -> dict:
    """One assumptions mapping feeding BOTH paths — the control and the worker (#3044)."""
    from dataclasses import asdict

    from app.research.golden_search.protocol import ExecutionAssumptions

    return asdict(ExecutionAssumptions())


def _serial_answer(manifest: dict[str, str]) -> Metrics:
    """The same evaluation on today's in-process path: the control the worker must match."""
    from app.research.golden_search.evaluator import engine_executor
    from app.research.golden_search.protocol import ExecutionAssumptions

    execute = engine_executor(strategy_key="sma_crossover", execution=ExecutionAssumptions(**_assumptions()), manifest=manifest)
    return execute(_serial_item().request).metrics


def _serial_item() -> ReadyEvaluation:
    request = EvaluationRequest(
        point={"symbol": "SPY", "short_window": 2, "long_window": 3, "resolution_minutes": 1},
        point_hash="q" * 64,
        window=WINDOW,
        warmup_from=date(2025, 1, 2),  # equals the window start: a single-phase run, so the control scores real bars
        scenario=None,
        detail=False,
        stage="search",
        fold_index=None,
    )
    return ReadyEvaluation(key="w" * 64, attempt=1, task_gen=1, request=request)


@pytest.fixture
def no_active_children(monkeypatch):
    """No spawned child outlives its test (#3044).

    ``multiprocessing.active_children`` never sees ``subprocess.Popen``
    children, so the guard tracks every worker this test builds and asserts
    each one closed — a close means killed and reaped.
    """
    built: list[ResearchWorker] = []
    real_init = ResearchWorker.__init__

    def tracking_init(self, context):
        real_init(self, context)
        built.append(self)

    # Patch the class in place: every binding (this module's import, the
    # factory's) sees one tracked constructor.
    monkeypatch.setattr(ResearchWorker, "__init__", tracking_init)
    yield
    for worker in built:
        assert worker._child is None, "a spawned worker was never closed"


@pytest.mark.slow
def test_a_spawned_worker_returns_the_serial_answer_exactly(tmp_path: Path, no_active_children, monkeypatch) -> None:
    write_root, _lake, manifest = _reference_lake(tmp_path)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))  # this process's settings object
    monkeypatch.setenv("LEAN_DATA_WRITE_ROOT", str(write_root))  # the child reads env, not the object

    serial = _serial_answer(manifest)
    assert serial.status == "completed", (
        f"the control must run real bars; an equality of two failures proves nothing ({serial.error})"
    )

    checkpoints: list[dict] = []
    worker = ResearchWorker(WorkerDispatchContext(
        strategy_key="sma_crossover",
        execution=_assumptions(),
        manifest=manifest,
        identity=_runtime_identity(),
        job_id="job-worker-test",
        generation=1,
        on_checkpoint=checkpoints.append,
    ))
    try:
        task = task_wire(_serial_item(), strategy_key="sma_crossover",
                         execution=_assumptions(),
                         manifest=manifest, identity=_runtime_identity())
        message = worker.run_task(task, silence_bound_s=120.0)
    finally:
        worker.close()

    assert message["type"] == "result", message
    assert Metrics.from_dict(message["metrics"]) == serial, "the worker is the serial path, metric for metric"
    phases = [checkpoint.get("phase") for checkpoint in checkpoints]
    assert "preparation" in phases and "consolidating_bars" in phases, (
        "the child's real progress reached the parent's checkpoint sink"
    )


@pytest.mark.slow
def test_a_refused_sealed_identity_interrupts_instead_of_computing(tmp_path: Path, no_active_children, monkeypatch) -> None:
    write_root, _lake, manifest = _reference_lake(tmp_path)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))  # this process's settings object
    monkeypatch.setenv("LEAN_DATA_WRITE_ROOT", str(write_root))  # the child reads env, not the object
    forged = {**_runtime_identity(), "source_digest": "0" * 64}

    worker = ResearchWorker(WorkerDispatchContext(
        strategy_key="sma_crossover",
        execution=_assumptions(),
        manifest=manifest,
        identity=_runtime_identity(),
        job_id="job-worker-test",
        generation=1,
    ))
    try:
        task = task_wire(_serial_item(), strategy_key="sma_crossover",
                         execution=_assumptions(),
                         manifest=manifest, identity=forged)
        # Terminal, never WorkerLost (#3065 review): a retried refusal would
        # spend the allowance and fabricate failed evidence for a code
        # mismatch instead of failing the stage closed.
        with pytest.raises(IdentityRefusalError, match="identity"):
            worker.run_task(task, silence_bound_s=120.0)
        assert worker._child is None, "the refusing child was reaped, not left idling"
    finally:
        worker.close()


@pytest.mark.slow
def test_close_kills_and_reaps_the_child_promptly(tmp_path: Path, no_active_children) -> None:
    manifest: dict[str, str] = {}  # readiness needs no data; only the protocol matters here
    worker = ResearchWorker(WorkerDispatchContext(
        strategy_key="sma_crossover",
        execution=_assumptions(),
        manifest=manifest,
        identity=_runtime_identity(),
        job_id="job-worker-test",
        generation=1,
    ))
    pid = worker._child.pid  # the owned handle, the option-search precedent's assertion shape
    worker.close()
    worker.close()  # idempotent

    with pytest.raises(OSError):
        os.kill(pid, 0)
    assert worker._child is None


def _runtime_identity() -> dict:
    from app.research.sweep.identity import resolve_code_identity

    identity = resolve_code_identity()
    return {
        "source_digest": identity.source_digest,
        "environment_digest": identity.environment_digest,
        "digest_scheme": identity.digest_scheme,
    }


def _unspawned_worker(cancel_check: Callable[[], object] = lambda: None) -> ResearchWorker:
    """A ResearchWorker with its spawn bypassed: the pump loop driven by hand."""
    from concurrent.futures import ThreadPoolExecutor

    worker = ResearchWorker.__new__(ResearchWorker)
    worker._context = WorkerDispatchContext(
        strategy_key="sma_crossover",
        execution={},
        manifest={},
        identity={},
        job_id="job-worker-test",
        generation=1,
        cancel_check=cancel_check,
    )
    worker._lock = threading.Lock()
    worker._outcomes = {}
    worker._arrived = threading.Condition(worker._lock)
    worker._alive_signal = threading.Event()
    worker._progress_signal = threading.Event()
    worker._child = None
    worker._lost = None
    worker._refusal = None
    worker._pump = None
    worker._verdicts = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-verdicts")
    return worker


def test_heartbeats_alone_do_not_defeat_the_task_silence_bound() -> None:
    """A wedged-but-alive worker is fenced by the task bound, not trusted forever (#3044 review)."""
    worker = _unspawned_worker()
    stop = threading.Event()

    def heartbeats() -> None:
        while not stop.wait(0.05):
            worker._alive_signal.set()

    beating = threading.Thread(target=heartbeats, daemon=True)
    beating.start()
    try:
        with pytest.raises(WorkerLost, match="no task progress"):
            worker.run_task({"key": "k"}, silence_bound_s=5.0, task_silence_bound_s=0.8)
    finally:
        stop.set()
        beating.join(timeout=2.0)
        worker._verdicts.shutdown(wait=False)


def test_task_progress_resets_both_clocks_and_the_outcome_wins() -> None:
    """Progress signals keep a legitimately slow worker alive until its outcome arrives."""
    worker = _unspawned_worker()

    def slow_progress() -> None:
        for _ in range(6):
            time.sleep(0.1)
            worker._progress_signal.set()

    progressing = threading.Thread(target=slow_progress, daemon=True)
    progressing.start()
    try:
        delivering = threading.Timer(
            0.45,
            lambda: (
                worker._arrived.__enter__(),
                worker._outcomes.__setitem__("k", {"type": "result", "key": "k"}),
                worker._arrived.notify_all(),
                worker._arrived.__exit__(None, None, None),
            ),
        )
        delivering.start()
        outcome = worker.run_task({"key": "k"}, silence_bound_s=0.6, task_silence_bound_s=0.6)
        assert outcome["key"] == "k"
    finally:
        progressing.join(timeout=2.0)
        worker._verdicts.shutdown(wait=False)


@pytest.mark.slow
def test_cancellation_frees_the_execution_slot_within_ten_seconds(no_active_children) -> None:
    """#3045's bound: cancelling with a write's fate uncertain still reaps the child promptly.

    The durable reconciliation is separate and may be slow; the execution
    slot — the child — must not wait for it.
    """
    manifest: dict[str, str] = {}
    worker = ResearchWorker(WorkerDispatchContext(
        strategy_key="sma_crossover",
        execution=_assumptions(),
        manifest=manifest,
        identity=_runtime_identity(),
        job_id="job-worker-test",
        generation=1,
    ))
    pid = worker._child.pid

    started = time.monotonic()
    worker.cancel()
    elapsed = time.monotonic() - started

    assert elapsed < 10.0, f"the slot took {elapsed:.1f}s to free; the bound is ten"
    with pytest.raises(OSError):
        os.kill(pid, 0)
    assert worker._child is None


# ── #3065 review regressions (fast: the pump's own machinery, no real child) ─


def test_a_fenced_generation_while_a_task_runs_kills_the_child_promptly() -> None:
    """#3065 review: the authority is polled while awaiting the child.

    Without the poll, a dead generation's checkpoints kept renewing its
    lease until the task returned — up to the task silence bound.
    """
    from app.research.persistence.ownership import GenerationFencedError

    fenced = threading.Event()

    def poll() -> None:
        if fenced.is_set():
            raise GenerationFencedError("coordinator authority connection lost mid-run")

    worker = _unspawned_worker(cancel_check=poll)
    reaped = threading.Event()
    worker.close = lambda: reaped.set()  # instance attribute: the reap is the assertion

    fenced.set()
    started = time.monotonic()
    with pytest.raises(GenerationFencedError):
        worker.run_task({"key": "k"}, silence_bound_s=60.0, task_silence_bound_s=60.0)
    elapsed = time.monotonic() - started

    assert reaped.is_set(), "the child was killed, not left renewing a dead generation's lease"
    assert elapsed < FENCE_POLL_INTERVAL_S + 5.0, f"the fence was noticed within one poll cadence ({elapsed:.1f}s)"


def test_a_refused_identity_is_terminal_not_a_worker_loss() -> None:
    """#3065 review: the refusal must never enter the worker-loss retry path."""
    worker = _unspawned_worker()
    reaped: list[bool] = []
    worker.close = lambda: reaped.append(True)
    worker._refusal = "sealed identity differs from the task's"

    with pytest.raises(IdentityRefusalError, match="identity"):
        worker.run_task({"key": "k"})

    assert reaped, "the refusing child was reaped, not left idling"


def test_a_dead_idle_child_is_replaced_before_the_next_start_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """#3065 review: a child that died between tasks never receives a marker it cannot serve.

    The start marker decides whether a re-admission spends retry allowance;
    writing it against a corpse would charge a run nobody dispatched.
    """
    from typing import Any

    import app.research.golden_search.worker as worker_module
    from tests.research.golden_search.test_evaluator import (
        _EngineSpy,
        _key_of,
        _point,
        _ReservationBridge,
        _seamed_evaluator,
    )
    from tests.research.golden_search.test_worker_loss_recovery import JAN

    events: list[tuple[str, str]] = []
    spawned = iter(["first", "second"])

    class _DyingChild:
        """Answers one task, then dies where only a health check can see it."""

        def __init__(self, context: Any) -> None:
            self.name = next(spawned)
            self.lost: str | None = None

        def run_task(self, task: dict[str, Any]) -> dict[str, Any]:
            from tests.research.golden_search.test_worker_pool import _gap_message

            message = _gap_message(task)
            self.lost = "the research worker exited with code 1"  # dies right after its answer
            return message

        def lost_reason(self) -> str | None:
            return self.lost

        def cancel(self) -> None:
            pass

        def close(self) -> None:
            pass

    class _OrderingBridge(_ReservationBridge):
        async def start(self, conn, study_id, attempt, key, *, task_gen):
            events.append(("start", key))
            return await super().start(conn, study_id, attempt, key, task_gen=task_gen)

    def spawn(context: Any) -> _DyingChild:
        child = _DyingChild(context)
        events.append(("spawn", child.name))
        return child

    monkeypatch.setattr(worker_module, "ResearchWorker", spawn)
    context = WorkerDispatchContext(
        strategy_key="ema_crossover_signal",
        execution={"fill_mode": "next_bar_open", "commission_per_order": 0.0, "slippage_per_share": 0.0, "initial_cash": 100_000.0},
        manifest={},
        identity=IDENTITY,
        job_id="job-worker-test",
        generation=1,
    )
    dispatch = WorkerDispatch(context)
    engine, bridge = _EngineSpy(), _OrderingBridge(limit=10)
    evaluator = _seamed_evaluator(monkeypatch, engine, bridge)
    evaluator.dispatch = dispatch

    first, second = _point(0.1), _point(0.2)
    metrics = evaluator.evaluate([first, second], window=JAN, stage="search")

    assert [m.net_profit for m in metrics] == [0.1, 0.2], "the batch completed through the replacement"
    order = [(kind, name) for kind, name in events]
    assert [kind for kind, _ in order] == ["spawn", "start", "spawn", "start"], f"each marker landed on a live child: {order}"
    for key in (_key_of(evaluator, first), _key_of(evaluator, second)):
        assert bridge.rows[key]["retries"] == 0, "no allowance spent on a run nobody dispatched"


def test_the_spawn_environment_carries_the_sized_cache_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """#3066 review: the parent sizes the child's decoded cache — the shared budget's share."""
    import json as json_module
    from typing import Any

    import app.research.golden_search.worker as worker_module
    from app.research.golden_search.worker import WORKER_DECODED_CACHE_BYTES

    captured: dict[str, str] = {}

    class _StubPipe:
        """One readable, writable, closeable line: the ready handshake, then an open pipe."""

        line = json_module.dumps({"type": "ready", "pid": 4242, "identity": {}}) + "\n"

        def __iter__(self):
            yield self.line
            threading.Event().wait()  # stdout stays open for the child's life; the pump never sees EOF

        def write(self, data: str) -> None:
            pass

        def flush(self) -> None:
            pass

        def close(self) -> None:
            pass

    class _StubChild:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured.update(kwargs.get("env") or {})
            self.pid = 4242
            self.stdin = _StubPipe()
            self.stdout = _StubPipe()

        def poll(self) -> int | None:
            return None

        def kill(self) -> None:
            pass

        def wait(self, timeout: float | None = None) -> int:
            return 0

    monkeypatch.setattr(worker_module.subprocess, "Popen", _StubChild)

    context = WorkerDispatchContext(
        strategy_key="sma_crossover",
        execution={},
        manifest={},
        identity=IDENTITY,
        job_id="job-worker-test",
        generation=1,
        decoded_cache_bytes=123,
    )
    worker = ResearchWorker(context)
    assert captured["RESEARCH_WORKER_DECODED_CACHE_BYTES"] == "123"
    assert captured["PYTHONHASHSEED"] == "0", "the pinned environment rides along"
    parity = ResearchWorker(WorkerDispatchContext(
        strategy_key="sma_crossover", execution={}, manifest={}, identity=IDENTITY,
        job_id="job-worker-test", generation=1,
    ))
    try:
        assert parity._context.decoded_cache_bytes == WORKER_DECODED_CACHE_BYTES, (
            "an unsized child keeps the serial parity ceiling"
        )
    finally:
        worker.close()
        parity.close()
