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
from datetime import date
from pathlib import Path

import pytest

from app.config import settings
from app.research.golden_search.evaluator import (
    EvaluationRequest,
    ReadyEvaluation,
)
from app.research.golden_search.selection import Metrics
from app.research.golden_search.worker import (
    CHILD_ENV_FIXED,
    ResearchWorker,
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
    return ReadyEvaluation(key="k" * 64, attempt=3, request=request)


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
    return ReadyEvaluation(key="w" * 64, attempt=1, request=request)


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
        with pytest.raises(WorkerLost, match="identity"):
            worker.run_task(task, silence_bound_s=120.0)
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
