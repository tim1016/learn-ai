"""The Recency launch worker body through the real persistence (#1938).

``run_launch`` claims the launch's next attempt, skips the cells the durable
record already holds, and records the terminal state from the persisted
record — never from this execution's replay: on a resume the recorded cells
are not re-run, so this execution's success count alone would understate the
launch and fail the COMPLETED reconciliation. Runs against the ephemeral
database (same attestation as the repository suites); Redis is the fake.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from app.jobs import progress
from app.research.persistence import fence
from app.research.recency import repository as repo
from app.research.recency import service
from app.research.recency.runner import RecencyLaunchConfig, RecencyRunSnapshot
from app.research.sweep.grid import StrategyGridConfig, ValueListRange, expand_grid
from tests.jobs.conftest import _FakeRedis

pytestmark = pytest.mark.asyncio


def _config(launch_id: str) -> RecencyLaunchConfig:
    return RecencyLaunchConfig(
        launch_id=launch_id,
        strategies=[StrategyGridConfig(strategy_key="ema_crossover_signal", param_ranges={"gap_bps": ValueListRange((2.0, 3.0))})],
        symbols=["SPY"],
        window_start_ms=0,
        window_end_ms=1_000,
    )


@dataclass
class _FakeDataPolicy:
    def model_dump_json(self) -> str:
        return "{}"


@dataclass
class _FakeBacktestResult:
    success: bool = True
    error: str | None = None
    trades: list[object] = field(default_factory=list)
    study_id: int | None = None
    data_policy: _FakeDataPolicy = field(default_factory=_FakeDataPolicy)


def _snapshot(config: RecencyLaunchConfig, run_spec) -> RecencyRunSnapshot:
    return RecencyRunSnapshot(
        launch_id=config.launch_id,
        symbol=run_spec.symbol,
        strategy_key=run_spec.strategy_key,
        params=run_spec.params,
        params_hash=run_spec.params_hash,
        total_pnl=0.0,
        sharpe=None,
        trades=[],
        study_id=None,
    )


async def _run_launch(config: RecencyLaunchConfig, *, job_id: str, fake_redis: _FakeRedis, monkeypatch: pytest.MonkeyPatch) -> dict:
    """run_launch on a worker-style thread, against the fake job store."""
    monkeypatch.setattr(progress, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(service, "resolved_code_revision", lambda: "test-revision")
    executed: list[tuple[str, str]] = []

    def execute(run_spec, config_: RecencyLaunchConfig) -> _FakeBacktestResult:
        executed.append((run_spec.symbol, run_spec.params_hash))
        return _FakeBacktestResult()

    emit = progress.ProgressEmitter(job_id)
    cancel = progress.CancellationCheck(job_id, check_every_n=1)
    summary = await asyncio.to_thread(service.run_launch, config, emit=emit, cancel=cancel, execute_backtest=execute)
    summary["_executed"] = executed
    return summary


async def test_a_resume_runs_only_the_missing_cells_and_accounts_from_the_persisted_record(
    conn, unique: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hazard-d regression: a cell whose durable success exists is skipped, and the
    terminal accounting comes from the persisted record — the old code wrote this
    execution's success count over the launch's and reported the launch FAILED."""
    config = _config(f"launch-{unique}")
    specs = list(expand_grid(config.strategies, config.symbols))
    assert len(specs) == 2
    recorded, missing = specs[0], specs[1]

    assert await repo.create_launch(conn, launch_id=config.launch_id, config_json="{}", expected_runs=2) is True
    assert await repo.claim_launch(conn, launch_id=config.launch_id, job_id="first-worker") == 1
    await repo.persist_snapshot(conn, _snapshot(config, recorded), attempt=1)
    assert await repo.set_terminal_status(conn, config.launch_id, status="FAILED", attempt=1, succeeded_runs=1, failed_runs=0) is True

    new_job = f"job-{unique}-resume"
    summary = await _run_launch(config, job_id=new_job, fake_redis=_FakeRedis(), monkeypatch=monkeypatch)

    assert summary["_executed"] == [(missing.symbol, missing.params_hash)]  # the recorded cell is never re-run
    assert (summary["succeeded_runs"], summary["skipped_runs"], summary["failed_runs"]) == (1, 1, 0)
    row = await conn.fetchrow(
        'SELECT "Status", "Attempt", "JobId", "SucceededRuns", "FailedRuns" FROM "RecencyLaunches" WHERE "Id" = $1',
        config.launch_id,
    )
    assert (row["Status"], row["Attempt"], row["JobId"]) == ("COMPLETED", 2, new_job)
    assert (row["SucceededRuns"], row["FailedRuns"]) == (2, 0)  # both cells accounted, from the record


async def test_a_fresh_launch_claims_attempt_one_under_its_own_id(conn, unique: str, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(f"launch-{unique}")
    assert await repo.create_launch(conn, launch_id=config.launch_id, config_json="{}", expected_runs=2) is True

    summary = await _run_launch(config, job_id=config.launch_id, fake_redis=_FakeRedis(), monkeypatch=monkeypatch)

    assert (summary["attempt"], summary["succeeded_runs"], summary["skipped_runs"]) == (1, 2, 0)
    row = await conn.fetchrow('SELECT "Status", "JobId", "SucceededRuns" FROM "RecencyLaunches" WHERE "Id" = $1', config.launch_id)
    assert (row["Status"], row["JobId"], row["SucceededRuns"]) == ("COMPLETED", config.launch_id, 2)


async def test_a_completed_launch_refuses_a_second_worker(conn, unique: str, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(f"launch-{unique}")
    assert await repo.create_launch(conn, launch_id=config.launch_id, config_json="{}", expected_runs=2) is True
    await _run_launch(config, job_id=config.launch_id, fake_redis=_FakeRedis(), monkeypatch=monkeypatch)

    with pytest.raises(fence.RecordNotClaimableError):
        await _run_launch(config, job_id=f"late-{unique}", fake_redis=_FakeRedis(), monkeypatch=monkeypatch)
