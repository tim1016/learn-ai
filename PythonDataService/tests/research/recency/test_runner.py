"""Recency Chart runner — mirrors the walk-forward runner's injected-
dependency seam. execute_backtest_fn stands in for an injected in-memory
data source: it decides what trades each run produces without hitting the
real engine or Polygon.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.research.recency.grid import RunSpec, StrategyGridConfig, ValueListRange
from app.research.recency.runner import RecencyLaunchConfig, run_recency


@dataclass
class _FakeTrade:
    entry_time: int
    exit_time: int
    pnl_pts: float
    pnl_pct: float
    quantity: int


@dataclass
class _FakeBacktestResult:
    success: bool = True
    error: str | None = None
    trades: list[_FakeTrade] = field(default_factory=list)


def _config(**overrides: object) -> RecencyLaunchConfig:
    defaults = dict(
        launch_id="launch-1",
        strategies=[
            StrategyGridConfig(
                strategy_key="ema_crossover_2_bps",
                param_ranges={"gap_bps": ValueListRange((2.0,))},
            )
        ],
        symbols=["SPY", "AAPL"],
        window_start_ms=0,
        window_end_ms=1_000_000,
    )
    defaults.update(overrides)
    return RecencyLaunchConfig(**defaults)  # type: ignore[arg-type]


def _one_trade_result(pnl_pts: float = 2.0) -> _FakeBacktestResult:
    return _FakeBacktestResult(trades=[_FakeTrade(entry_time=100, exit_time=200, pnl_pts=pnl_pts, pnl_pct=0.02, quantity=10)])


class TestRunRecencySuccessPath:
    def test_persists_one_snapshot_per_run_spec(self) -> None:
        persisted = []
        summary = run_recency(
            _config(),
            execute_backtest_fn=lambda run_spec, config: _one_trade_result(),
            persist_fn=persisted.append,
            strategy_code_version_fn=lambda strategy_key: "v1",
        )
        assert summary.expected_runs == 2  # 2 symbols x 1 combo
        assert summary.succeeded_runs == 2
        assert summary.failed_runs == 0
        assert len(persisted) == 2
        assert {snap.symbol for snap in persisted} == {"SPY", "AAPL"}

    def test_snapshot_carries_launch_id_and_params_hash(self) -> None:
        persisted = []
        run_recency(
            _config(),
            execute_backtest_fn=lambda run_spec, config: _one_trade_result(),
            persist_fn=persisted.append,
            strategy_code_version_fn=lambda strategy_key: "v1",
        )
        assert all(snap.launch_id == "launch-1" for snap in persisted)
        assert all(isinstance(snap.params_hash, str) and snap.params_hash for snap in persisted)

    def test_snapshot_trades_carry_a_fingerprint_and_computed_stats(self) -> None:
        persisted = []
        run_recency(
            _config(symbols=["SPY"]),
            execute_backtest_fn=lambda run_spec, config: _one_trade_result(pnl_pts=2.0),
            persist_fn=persisted.append,
            strategy_code_version_fn=lambda strategy_key: "v1",
        )
        assert len(persisted) == 1
        snap = persisted[0]
        assert snap.total_pnl == 20.0  # pnl_pts(2.0) * quantity(10)
        assert len(snap.trades) == 1
        trade = snap.trades[0]
        assert trade.fingerprint
        assert trade.pnl == 20.0
        assert trade.holding_sessions >= 1

    def test_progress_phases_are_emitted(self) -> None:
        phases: list[str] = []
        run_recency(
            _config(),
            execute_backtest_fn=lambda run_spec, config: _one_trade_result(),
            persist_fn=lambda snap: None,
            strategy_code_version_fn=lambda strategy_key: "v1",
            on_phase=phases.append,
        )
        assert "expand" in phases
        assert "run" in phases
        assert "completed" in phases

    def test_progress_counts_reach_the_expected_total(self) -> None:
        progress_calls: list[tuple[int, int]] = []
        run_recency(
            _config(),
            execute_backtest_fn=lambda run_spec, config: _one_trade_result(),
            persist_fn=lambda snap: None,
            strategy_code_version_fn=lambda strategy_key: "v1",
            on_progress=lambda done, total: progress_calls.append((done, total)),
        )
        assert progress_calls[-1] == (2, 2)


class TestRunRecencyFailureIsolation:
    def test_a_failing_backtest_is_isolated_and_reported_not_dropped(self) -> None:
        def flaky_execute(run_spec: RunSpec, config: RecencyLaunchConfig) -> _FakeBacktestResult:
            if run_spec.symbol == "AAPL":
                return _FakeBacktestResult(success=False, error="no data for AAPL")
            return _one_trade_result()

        persisted = []
        failures: list[tuple[RunSpec, str]] = []
        summary = run_recency(
            _config(),
            execute_backtest_fn=flaky_execute,
            persist_fn=persisted.append,
            strategy_code_version_fn=lambda strategy_key: "v1",
            on_run_failed=lambda run_spec, message: failures.append((run_spec, message)),
        )
        assert summary.expected_runs == 2
        assert summary.succeeded_runs == 1
        assert summary.failed_runs == 1
        assert len(persisted) == 1  # the failing run never persists a snapshot
        assert len(failures) == 1
        assert failures[0][0].symbol == "AAPL"
        assert "no data for AAPL" in failures[0][1]

    def test_a_persist_failure_is_also_isolated_and_reported(self) -> None:
        def flaky_persist(snapshot: object) -> None:
            raise RuntimeError("backend unreachable")

        summary = run_recency(
            _config(symbols=["SPY"]),
            execute_backtest_fn=lambda run_spec, config: _one_trade_result(),
            persist_fn=flaky_persist,
            strategy_code_version_fn=lambda strategy_key: "v1",
        )
        assert summary.succeeded_runs == 0
        assert summary.failed_runs == 1
        assert "backend unreachable" in summary.outcomes[0].error


class TestRunRecencyCancellation:
    def test_cancel_check_stops_dispatching_new_runs(self) -> None:
        calls = {"n": 0}

        def cancel_after_first_check() -> bool:
            calls["n"] += 1
            return calls["n"] > 1

        summary = run_recency(
            _config(symbols=["SPY", "AAPL", "QQQ"]),
            execute_backtest_fn=lambda run_spec, config: _one_trade_result(),
            persist_fn=lambda snap: None,
            strategy_code_version_fn=lambda strategy_key: "v1",
            cancel_check=cancel_after_first_check,
        )
        assert summary.succeeded_runs < summary.expected_runs
