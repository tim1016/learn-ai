"""Recency Chart runner — mirrors the walk-forward runner's injected-
dependency seam. execute_backtest_fn stands in for an injected in-memory
data source: it decides what trades each run produces without hitting the
real engine or Polygon.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.research.recency.grid import RunSpec, StrategyGridConfig, ValueListRange
from app.research.recency.runner import RecencyLaunchConfig, run_recency


@dataclass
class _FakeTrade:
    entry_time: int
    exit_time: int
    pnl_pts: float
    pnl_pct: float
    quantity: int
    is_synthetic_exit: bool = False
    signal_reason: str = ""


@dataclass
class _FakeDataPolicy:
    """Stands in for _EngineDataPolicyModel — the runner only needs
    something JSON-serializable back, duck-typed via model_dump_json()."""

    label: str = "polygon-adjusted-regular-minute"

    def model_dump_json(self) -> str:
        return f'{{"label": "{self.label}"}}'


@dataclass
class _FakeBacktestResult:
    success: bool = True
    error: str | None = None
    trades: list[_FakeTrade] = field(default_factory=list)
    study_id: int | None = None
    data_policy: _FakeDataPolicy = field(default_factory=_FakeDataPolicy)


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

    def test_snapshot_carries_study_id_from_the_backtest_result(self) -> None:
        persisted = []
        run_recency(
            _config(symbols=["SPY"]),
            execute_backtest_fn=lambda run_spec, config: _FakeBacktestResult(
                trades=[_FakeTrade(entry_time=100, exit_time=200, pnl_pts=2.0, pnl_pct=0.02, quantity=10)],
                study_id=42,
            ),
            persist_fn=persisted.append,
            strategy_code_version_fn=lambda strategy_key: "v1",
        )
        assert persisted[0].study_id == 42

    def test_snapshot_study_id_is_none_when_autosave_did_not_happen(self) -> None:
        persisted = []
        run_recency(
            _config(symbols=["SPY"]),
            execute_backtest_fn=lambda run_spec, config: _one_trade_result(),
            persist_fn=persisted.append,
            strategy_code_version_fn=lambda strategy_key: "v1",
        )
        assert persisted[0].study_id is None

    def test_snapshot_trades_carry_synthetic_exit_and_signal_reason(self) -> None:
        persisted = []
        run_recency(
            _config(symbols=["SPY"]),
            execute_backtest_fn=lambda run_spec, config: _FakeBacktestResult(
                trades=[
                    _FakeTrade(
                        entry_time=100,
                        exit_time=200,
                        pnl_pts=2.0,
                        pnl_pct=0.02,
                        quantity=10,
                        is_synthetic_exit=True,
                        signal_reason="window_close",
                    )
                ]
            ),
            persist_fn=persisted.append,
            strategy_code_version_fn=lambda strategy_key: "v1",
        )
        trade = persisted[0].trades[0]
        assert trade.is_synthetic_exit is True
        assert trade.signal_reason == "window_close"

    def test_ordinary_trades_default_to_not_synthetic(self) -> None:
        persisted = []
        run_recency(
            _config(symbols=["SPY"]),
            execute_backtest_fn=lambda run_spec, config: _one_trade_result(),
            persist_fn=persisted.append,
            strategy_code_version_fn=lambda strategy_key: "v1",
        )
        trade = persisted[0].trades[0]
        assert trade.is_synthetic_exit is False
        assert trade.signal_reason == ""

    def test_total_pnl_and_trade_pnl_are_net_of_configured_commission(self) -> None:
        persisted = []
        run_recency(
            _config(symbols=["SPY"], commission_per_order=1.0),
            execute_backtest_fn=lambda run_spec, config: _one_trade_result(pnl_pts=2.0),
            persist_fn=persisted.append,
            strategy_code_version_fn=lambda strategy_key: "v1",
        )
        snap = persisted[0]
        assert snap.total_pnl == pytest.approx(18.0, rel=0.0, abs=1e-12)  # gross 20 - 2*1.0
        assert snap.trades[0].pnl == pytest.approx(18.0, rel=0.0, abs=1e-12)


class TestRunRecencyDataPolicyFingerprint:
    """The fingerprint must reflect what actually governed execution
    (EngineBacktestResponse.data_policy, resolved session/adjustment/
    resolution/provenance) rather than the caller's requested label —
    otherwise a stale/direct client's unhonored data_policy request
    mislabels evidence as if it had been applied."""

    def _fingerprint_for_resolved_policy(self, label: str) -> str:
        persisted: list[object] = []
        run_recency(
            _config(symbols=["SPY"]),  # config.data_policy stays the default label throughout
            execute_backtest_fn=lambda run_spec, config: _FakeBacktestResult(
                trades=[_FakeTrade(entry_time=100, exit_time=200, pnl_pts=2.0, pnl_pct=0.02, quantity=10)],
                data_policy=_FakeDataPolicy(label=label),
            ),
            persist_fn=persisted.append,
            strategy_code_version_fn=lambda strategy_key: "v1",
        )
        return persisted[0].trades[0].fingerprint

    def test_differing_resolved_policies_produce_differing_fingerprints(self) -> None:
        a = self._fingerprint_for_resolved_policy("polygon-adjusted-regular-minute")
        b = self._fingerprint_for_resolved_policy("ibkr-raw-regular-minute")
        assert a != b

    def test_same_resolved_policy_is_deterministic(self) -> None:
        a = self._fingerprint_for_resolved_policy("polygon-adjusted-regular-minute")
        b = self._fingerprint_for_resolved_policy("polygon-adjusted-regular-minute")
        assert a == b


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
    """Mirrors app/research/walk_forward/runner.py's CancelCheck contract:
    cancel_check is called and its return value is IGNORED — cancellation
    works only by raising. run_recency does not catch or translate the
    exception; it propagates to the caller (jobs.py wires a raising
    wrapper around CancellationCheck.raise_if_cancelled so run_in_thread's
    JobCancelled handler produces a real job.cancelled terminal state)."""

    def test_cancel_check_raising_aborts_the_run_and_propagates(self) -> None:
        calls = {"n": 0}

        class _Cancelled(Exception):
            pass

        def cancel_after_first_batch() -> None:
            calls["n"] += 1
            if calls["n"] > 1:
                raise _Cancelled("cancelled")

        persisted: list[object] = []
        with pytest.raises(_Cancelled):
            run_recency(
                _config(symbols=["SPY", "AAPL", "QQQ"]),
                execute_backtest_fn=lambda run_spec, config: _one_trade_result(),
                persist_fn=persisted.append,
                strategy_code_version_fn=lambda strategy_key: "v1",
                cancel_check=cancel_after_first_batch,
                max_workers=1,
            )
        assert len(persisted) < 3

    def test_cancel_check_return_value_is_ignored(self) -> None:
        # A boolean-returning cancel_check (the old, broken contract) must
        # NOT stop the run — only a raise does. This documents the
        # deliberate break from the pre-fix branch-on-bool semantics.
        summary = run_recency(
            _config(symbols=["SPY", "AAPL"]),
            execute_backtest_fn=lambda run_spec, config: _one_trade_result(),
            persist_fn=lambda snap: None,
            strategy_code_version_fn=lambda strategy_key: "v1",
            cancel_check=lambda: True,
        )
        assert summary.succeeded_runs == summary.expected_runs == 2


class TestRunRecencyLazyGridExecution:
    def test_does_not_materialize_the_full_grid_before_execution_starts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pulled: list[RunSpec] = []

        def fake_run_specs():
            for i in range(5):
                spec = RunSpec(symbol=f"SYM{i}", strategy_key="ema_crossover_2_bps", params={"gap_bps": 2.0}, params_hash=f"h{i}")
                pulled.append(spec)
                yield spec

        monkeypatch.setattr("app.research.recency.runner.expand_grid", lambda strategies, symbols: fake_run_specs())

        pulled_count_at_first_done = {}

        def on_progress(done: int, total: int) -> None:
            if done == 1 and "count" not in pulled_count_at_first_done:
                pulled_count_at_first_done["count"] = len(pulled)

        run_recency(
            _config(symbols=["SPY"]),
            execute_backtest_fn=lambda run_spec, config: _one_trade_result(),
            persist_fn=lambda snap: None,
            strategy_code_version_fn=lambda strategy_key: "v1",
            on_progress=on_progress,
            max_workers=1,
        )

        assert pulled_count_at_first_done["count"] < 5
