"""Recency Chart runner — mirrors the walk-forward runner's injected-
dependency seam. execute_backtest_fn stands in for an injected in-memory
data source: it decides what trades each run produces without hitting the
real engine or Polygon.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.research.recency.runner import RecencyLaunchConfig, run_recency
from app.research.sweep.grid import RunSpec, StrategyGridConfig, ValueListRange, expand_grid


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
    def test_two_symbols_persist_one_snapshot_each_and_report_phases_and_progress(self) -> None:
        persisted = []
        phases: list[str] = []
        progress_calls: list[tuple[int, int]] = []
        summary = run_recency(
            _config(),
            execute_backtest_fn=lambda run_spec, config: _one_trade_result(),
            persist_fn=persisted.append,
            strategy_code_version_fn=lambda strategy_key: "v1",
            on_phase=phases.append,
            on_progress=lambda done, total: progress_calls.append((done, total)),
        )
        assert summary.expected_runs == 2, "persists one snapshot per run spec: 2 symbols x 1 combo"
        assert summary.succeeded_runs == 2, "persists one snapshot per run spec: both succeeded"
        assert summary.failed_runs == 0, "persists one snapshot per run spec: none failed"
        assert len(persisted) == 2, "persists one snapshot per run spec"
        assert {snap.symbol for snap in persisted} == {"SPY", "AAPL"}, "persists one snapshot per run spec: both symbols"
        assert all(snap.launch_id == "launch-1" for snap in persisted), "snapshot carries launch id"
        assert all(
            isinstance(snap.params_hash, str) and snap.params_hash for snap in persisted
        ), "snapshot carries params hash"
        assert "expand" in phases, "progress phases are emitted: expand"
        assert "run" in phases, "progress phases are emitted: run"
        assert "completed" in phases, "progress phases are emitted: completed"
        assert progress_calls[-1] == (2, 2), "progress counts reach the expected total"

    def test_a_default_one_trade_snapshot_carries_stats_no_study_id_and_a_plain_trade(self) -> None:
        persisted = []
        run_recency(
            _config(symbols=["SPY"]),
            execute_backtest_fn=lambda run_spec, config: _one_trade_result(pnl_pts=2.0),
            persist_fn=persisted.append,
            strategy_code_version_fn=lambda strategy_key: "v1",
        )
        assert len(persisted) == 1, "one snapshot persisted"
        snap = persisted[0]
        assert snap.total_pnl == 20.0, "snapshot total pnl is pnl_pts(2.0) * quantity(10)"
        assert len(snap.trades) == 1, "snapshot carries its one trade"
        trade = snap.trades[0]
        assert trade.fingerprint, "snapshot trades carry a fingerprint"
        assert trade.pnl == 20.0, "snapshot trade carries its computed pnl"
        assert trade.holding_sessions >= 1, "snapshot trade carries computed holding sessions"
        assert snap.study_id is None, "snapshot study id is none when autosave did not happen"
        assert trade.is_synthetic_exit is False, "ordinary trades default to not synthetic"
        assert trade.signal_reason == "", "ordinary trades default to an empty signal reason"

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

    def test_the_fingerprint_follows_the_resolved_policy(self) -> None:
        adjusted = self._fingerprint_for_resolved_policy("polygon-adjusted-regular-minute")
        adjusted_again = self._fingerprint_for_resolved_policy("polygon-adjusted-regular-minute")
        raw = self._fingerprint_for_resolved_policy("ibkr-raw-regular-minute")
        assert adjusted != raw, "differing resolved policies produce differing fingerprints"
        assert adjusted == adjusted_again, "same resolved policy is deterministic"


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

    def test_cancellation_raised_during_the_final_batch_is_not_lost(self) -> None:
        """The loop polls cancel_check only *before* each batch, so a
        cancellation arriving while the last batch executes was never
        observed: its cells finished, persisted, and the run reported
        completion (issue #1928). Draining an already-dispatched batch is
        correct; reporting success afterwards is not."""
        state = {"cancelled": False}

        class _Cancelled(Exception):
            pass

        def cancel_check() -> None:
            if state["cancelled"]:
                raise _Cancelled("cancelled")

        def execute(run_spec, config):
            state["cancelled"] = True
            return _one_trade_result()

        persisted: list[object] = []
        phases: list[str] = []
        with pytest.raises(_Cancelled):
            run_recency(
                _config(symbols=["SPY"]),
                execute_backtest_fn=execute,
                persist_fn=persisted.append,
                strategy_code_version_fn=lambda strategy_key: "v1",
                cancel_check=cancel_check,
                on_phase=phases.append,
            )

        # The already-dispatched cell is allowed to finish and persist — that
        # is the deliberate half of the contract.
        assert len(persisted) == 1
        # But the run must not announce completion first. jobs.py wires
        # ``on_phase`` to the SSE emitter, so a "completed" phase here would
        # reach a consumer immediately before ``job.cancelled``. This is what
        # a check placed after ``on_phase("completed")`` would fail to catch.
        assert "completed" not in phases


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
        )

        assert pulled_count_at_first_done["count"] < 5


def _identity_of_first_spec() -> tuple[str, str, str]:
    spec = next(iter(expand_grid(_config().strategies, _config().symbols)))
    return (spec.symbol, spec.strategy_key, spec.params_hash)


class TestRunRecencySkipIdentities:
    @pytest.mark.parametrize(
        ("skip_identity", "expected_symbols", "expected_counts"),
        [
            pytest.param(
                _identity_of_first_spec(),
                ["AAPL"],
                (2, 1, 0, 1),
                id="a_recorded_cell_is_skipped_and_counts_toward_progress_only",
            ),
            # A params_hash that differs (a different combo of the same symbol/strategy) is a cell of its own.
            pytest.param(
                ("SPY", "ema_crossover_2_bps", "a-hash-not-in-this-grid"),
                ["AAPL", "SPY"],
                (2, 2, 0, 0),
                id="the_skip_identity_is_the_persisted_records_identity_triple",
            ),
        ],
    )
    def test_a_cell_is_skipped_only_on_its_exact_recorded_identity(
        self, skip_identity: tuple[str, str, str], expected_symbols: list[str], expected_counts: tuple[int, int, int, int]
    ) -> None:
        executed: list[str] = []
        persisted = []

        summary = run_recency(
            _config(),
            execute_backtest_fn=lambda run_spec, config: executed.append(run_spec.symbol) or _one_trade_result(),
            persist_fn=persisted.append,
            strategy_code_version_fn=lambda strategy_key: "v1",
            skip_identities=frozenset({skip_identity}),
        )

        assert sorted(executed) == expected_symbols, "a recorded cell is never re-run; every other cell runs"
        assert sorted(snap.symbol for snap in persisted) == expected_symbols, "a recorded cell is never persisted"
        assert (
            summary.expected_runs,
            summary.succeeded_runs,
            summary.failed_runs,
            summary.skipped_runs,
        ) == expected_counts, "a skipped cell counts toward progress but toward neither succeeded nor failed"
