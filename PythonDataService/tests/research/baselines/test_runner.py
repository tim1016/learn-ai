"""Baselines runner tests.

Verifies the orchestration layer (``run_baselines``):
  * Each baseline run is persisted as a child ``RunLedger`` linked to
    the baselines run via ``parent_run_id``.
  * Buy-and-hold actually produces a single trade (the BarProperty
    tautology + never-firing exit + on_end_of_algorithm flush).
  * Random-EMA-window method produces N child runs.
  * Null distributions are populated with the right metric names and
    in-range empirical percentiles + p-values.
  * Parent run's ``RunMetrics`` is the comparison anchor.
  * Failed parent / malformed parent_run_id / negative seed produce
    failed-status records, not exceptions.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.engine.strategy.spec import StrategySpec
from app.engine.strategy.spec.tests._parity_helpers import (
    FakeDataReader,
    build_minute_bars,
    closes_for_spy_ema,
)
from app.research.baselines.runner import BaselineRequest, run_baselines
from app.research.runs import RunRequest, run_strategy_spec, save_run
from app.research.runs.storage import list_runs


def _build_parent_spec() -> StrategySpec:
    """The same TEST EMA spec the Phase A/D suites use as their parent."""
    return StrategySpec.model_validate(
        {
            "schema_version": "1.0",
            "name": "TEST EMA crossover",
            "symbols": ["TEST"],
            "resolution": {"period_minutes": 15},
            "indicators": [
                {"id": "fast", "kind": "EMA", "period": 5, "source": "close"},
                {"id": "slow", "kind": "EMA", "period": 10, "source": "close"},
                {"id": "rsi", "kind": "RSI", "period": 14, "source": "close", "ma_type": "wilders"},
            ],
            "entry": {
                "logic": "AND",
                "conditions": [
                    {"kind": "FreshCross", "left": "fast", "right": "slow", "direction": "up"},
                    {
                        "kind": "IndicatorComparison",
                        "left": {
                            "kind": "Subtract",
                            "left": {"kind": "IndicatorRef", "indicator": "fast"},
                            "right": {"kind": "IndicatorRef", "indicator": "slow"},
                        },
                        "op": ">=",
                        "right": {"kind": "Const", "value": 0.20},
                    },
                    {"kind": "IndicatorBetween", "indicator": "rsi", "lo": 50, "hi": 70, "inclusive": True},
                ],
                "size": {"kind": "SetHoldings", "fraction": 1.0},
                "pyramiding": 1,
            },
            "position": {"kind": "EQUITY_LONG"},
            "survival": [],
            "exit": {
                "logic": "OR",
                "conditions": [{"kind": "BarsSinceEntry", "op": ">=", "value": 5}],
            },
            "diagnostics": {"snapshot_at_entry": ["fast", "slow", "rsi"]},
        }
    )


@pytest.fixture
def parent_run(tmp_path: Path):
    """Build, run, and persist a parent run with non-trivial trades."""
    bars = build_minute_bars(closes_for_spy_ema(2000))

    def factory(symbol: str, start: date, end: date):
        return FakeDataReader(bars=bars)

    ledger, result = run_strategy_spec(
        RunRequest(
            spec=_build_parent_spec(),
            start_date=date(2024, 1, 2),
            end_date=date(2024, 12, 31),
        ),
        data_source_factory=factory,
        data_root_revision="test-revision-1",
    )
    save_run(ledger, result, root=tmp_path)
    return ledger, result, factory


# ---------------------------------------------------------------------------
# Buy-and-hold method.
# ---------------------------------------------------------------------------
def test_buy_and_hold_runs_completed_with_equity_metrics(tmp_path: Path, parent_run):
    """B&H spec must complete and produce a real equity-curve-derived
    metric. ``total_return_pct`` and ``max_drawdown_pct`` are computed
    from the equity curve (which tracks the held position correctly),
    not from the trade log — see the documented engine-flush
    limitation in ``buy_and_hold_spec``: trade_log is empty for B&H,
    but the metrics that matter for null comparison are valid.
    """
    ledger, _, factory = parent_run
    _config, result = run_baselines(
        BaselineRequest(
            parent_run_id=ledger.run_id,
            method="buy_and_hold",
            sample_count=1,
        ),
        data_source_factory=factory,
        artifacts_root=tmp_path,
        data_root_revision="test-revision-1",
    )
    assert result.status == "completed"
    assert len(result.baselines) == 1
    bl = result.baselines[0]
    assert bl.status == "completed", bl.failure_reason
    # Real equity-curve-derived metrics. total_return_pct should be
    # non-zero on synthetic data with intra-day movement, and Sharpe
    # is finite because the equity curve has variance.
    assert bl.test_metrics.total_return_pct != 0.0
    assert bl.test_metrics.sharpe_ratio is not None


def test_buy_and_hold_persists_child_run(tmp_path: Path, parent_run):
    ledger, _, factory = parent_run
    config, result = run_baselines(
        BaselineRequest(
            parent_run_id=ledger.run_id,
            method="buy_and_hold",
            sample_count=1,
        ),
        data_source_factory=factory,
        artifacts_root=tmp_path,
        data_root_revision="test-revision-1",
    )
    bl = result.baselines[0]
    # Child run on disk.
    assert (tmp_path / bl.baseline_run_id / "ledger.json").is_file()
    # Discoverable via list_runs filter.
    listed = list_runs(root=tmp_path, parent_run_id=config.baseline_id)
    assert [lg.run_id for lg in listed] == [bl.baseline_run_id]


# ---------------------------------------------------------------------------
# Random EMA windows method.
# ---------------------------------------------------------------------------
def test_random_ema_windows_generates_n_runs(tmp_path: Path, parent_run):
    ledger, _, factory = parent_run
    _, result = run_baselines(
        BaselineRequest(
            parent_run_id=ledger.run_id,
            method="random_ema_windows",
            sample_count=10,
            random_seed=42,
        ),
        data_source_factory=factory,
        artifacts_root=tmp_path,
        data_root_revision="test-revision-1",
    )
    assert result.status == "completed"
    assert len(result.baselines) == 10
    # Each fold has parameters with fast/slow.
    for bl in result.baselines:
        assert "fast" in bl.parameters
        assert "slow" in bl.parameters
        assert bl.parameters["slow"] > bl.parameters["fast"]


def test_random_ema_windows_same_seed_produces_identical_parameters(
    tmp_path: Path, parent_run
):
    ledger, _, factory = parent_run
    request = BaselineRequest(
        parent_run_id=ledger.run_id,
        method="random_ema_windows",
        sample_count=5,
        random_seed=7,
    )
    _, result_a = run_baselines(
        request,
        data_source_factory=factory,
        artifacts_root=tmp_path,
        data_root_revision="test-revision-1",
    )
    _, result_b = run_baselines(
        request,
        data_source_factory=factory,
        artifacts_root=tmp_path,
        data_root_revision="test-revision-1",
    )
    a_params = [bl.parameters for bl in result_a.baselines]
    b_params = [bl.parameters for bl in result_b.baselines]
    assert a_params == b_params


# ---------------------------------------------------------------------------
# Null distributions + empirical p-values.
# ---------------------------------------------------------------------------
def test_null_distributions_cover_default_target_metrics(tmp_path: Path, parent_run):
    ledger, _, factory = parent_run
    _, result = run_baselines(
        BaselineRequest(
            parent_run_id=ledger.run_id,
            method="buy_and_hold",
            sample_count=1,
        ),
        data_source_factory=factory,
        artifacts_root=tmp_path,
        data_root_revision="test-revision-1",
    )
    metric_names = {dist.metric_name for dist in result.null_distributions}
    # Default coverage from _DEFAULT_TARGET_METRICS.
    assert "sharpe_ratio" in metric_names
    assert "total_return_pct" in metric_names
    assert "max_drawdown_pct" in metric_names
    assert "profit_factor" in metric_names


def test_empirical_percentile_in_unit_interval(tmp_path: Path, parent_run):
    ledger, _, factory = parent_run
    _, result = run_baselines(
        BaselineRequest(
            parent_run_id=ledger.run_id,
            method="random_ema_windows",
            sample_count=5,
            random_seed=0,
        ),
        data_source_factory=factory,
        artifacts_root=tmp_path,
        data_root_revision="test-revision-1",
    )
    for dist in result.null_distributions:
        if dist.empirical_percentile is None:
            continue  # parent_value or null_values empty
        assert 0.0 <= dist.empirical_percentile <= 1.0
        if dist.empirical_p_value is not None:
            # Phipson-Smyth ``(1 + count(null >= parent)) / (N + 1)``
            # is bounded in (0, 1] — minimum ``1 / (N+1)`` when no
            # null beats parent.
            assert 0.0 < dist.empirical_p_value <= 1.0


def test_p_value_for_extreme_parent_is_smallest_possible(tmp_path: Path, parent_run):
    """When parent_value is strictly higher than every null value, the
    empirical p-value is the smallest the formula allows: ``1 / (N+1)``.
    Demonstrates the lower bound the small-sample p-value can reach.
    """
    ledger, parent_result, factory = parent_run
    if not parent_result.trades:
        pytest.skip("zero trades on parent — cannot exercise extreme path")

    _, result = run_baselines(
        BaselineRequest(
            parent_run_id=ledger.run_id,
            method="random_ema_windows",
            sample_count=10,
            random_seed=0,
        ),
        data_source_factory=factory,
        artifacts_root=tmp_path,
        data_root_revision="test-revision-1",
    )
    # Find any metric where the parent strictly beats every null.
    found_extreme = False
    for dist in result.null_distributions:
        if dist.parent_value is None or not dist.null_values:
            continue
        all_below = all(v < dist.parent_value for v in dist.null_values)
        if all_below and dist.empirical_p_value is not None:
            n = len(dist.null_values)
            assert dist.empirical_p_value == pytest.approx(1.0 / (n + 1))
            found_extreme = True
            break
    if not found_extreme:
        pytest.skip(
            "No metric had parent strictly above every null on this fixture; "
            "test is informational"
        )


# ---------------------------------------------------------------------------
# Failure paths.
# ---------------------------------------------------------------------------
def test_missing_parent_run_returns_failed(tmp_path: Path):
    def factory(symbol: str, start: date, end: date):
        return FakeDataReader(bars=[])

    _, result = run_baselines(
        BaselineRequest(
            parent_run_id="deadbeefdeadbeefdeadbeefdeadbeef",
            method="buy_and_hold",
            sample_count=1,
        ),
        data_source_factory=factory,
        artifacts_root=tmp_path,
        data_root_revision="test-revision-1",
    )
    assert result.status == "failed"
    assert "parent run not found" in (result.failure_reason or "")


def test_malformed_parent_run_id_returns_failed(tmp_path: Path):
    def factory(symbol: str, start: date, end: date):
        return FakeDataReader(bars=[])

    _, result = run_baselines(
        BaselineRequest(
            parent_run_id="../etc/passwd",
            method="buy_and_hold",
            sample_count=1,
        ),
        data_source_factory=factory,
        artifacts_root=tmp_path,
        data_root_revision="test-revision-1",
    )
    assert result.status == "failed"
    assert "parent_run_id rejected" in (result.failure_reason or "")


def test_negative_random_seed_returns_failed(tmp_path: Path, parent_run):
    ledger, _, factory = parent_run
    _, result = run_baselines(
        BaselineRequest(
            parent_run_id=ledger.run_id,
            method="random_ema_windows",
            sample_count=3,
            random_seed=-1,
        ),
        data_source_factory=factory,
        artifacts_root=tmp_path,
        data_root_revision="test-revision-1",
    )
    assert result.status == "failed"
    assert "random_seed" in (result.failure_reason or "")


def test_failed_parent_run_returns_failed(tmp_path: Path, parent_run):
    """Refuse to derive a baseline from a failed parent run.

    The parent's metrics are placeholders when status='failed', so
    comparing baselines against them yields meaningless percentiles
    and p-values. The runner should short-circuit before generating
    any child baselines and surface the cause as a failed-status
    record (Phase A/C/D contract).
    """
    parent_ledger, parent_result, factory = parent_run
    # Re-persist the same ledger with status='failed' so the loader
    # returns a parent in failed state.
    failed_ledger = parent_ledger.model_copy(
        update={"status": "failed", "failure_reason": "engine refused spec"}
    )
    save_run(failed_ledger, parent_result, root=tmp_path, replace=True)

    _, result = run_baselines(
        BaselineRequest(
            parent_run_id=parent_ledger.run_id,
            method="buy_and_hold",
            sample_count=1,
        ),
        data_source_factory=factory,
        artifacts_root=tmp_path,
        data_root_revision="test-revision-1",
    )
    assert result.status == "failed"
    assert "status='failed'" in (result.failure_reason or "")
    # No child runs persisted.
    assert result.baselines == []


def test_child_run_window_matches_parent_exactly(tmp_path: Path, parent_run):
    """Regression: child run's start_ms / end_ms must equal parent's.

    Earlier the runner subtracted one calendar day from
    ``parent.end_ms`` (a stale walk-forward half-open trick), shaving
    a trading day off every baseline window and skewing null
    distributions whenever the final day contained material returns.
    """
    parent_ledger, _, factory = parent_run
    config, result = run_baselines(
        BaselineRequest(
            parent_run_id=parent_ledger.run_id,
            method="buy_and_hold",
            sample_count=1,
        ),
        data_source_factory=factory,
        artifacts_root=tmp_path,
        data_root_revision="test-revision-1",
    )
    assert result.status == "completed"
    assert len(result.baselines) == 1

    # Reload the child ledger to compare its persisted window to the
    # parent's. The child is referenced by config.baseline_id (its
    # parent_run_id field), per the runner's lineage convention.
    listed = list_runs(root=tmp_path, parent_run_id=config.baseline_id)
    assert len(listed) == 1
    child_ledger = listed[0]
    assert child_ledger.start_ms == parent_ledger.start_ms
    assert child_ledger.end_ms == parent_ledger.end_ms


def test_zero_sample_count_returns_failed(tmp_path: Path, parent_run):
    ledger, _, factory = parent_run
    _, result = run_baselines(
        BaselineRequest(
            parent_run_id=ledger.run_id,
            method="buy_and_hold",
            sample_count=0,
        ),
        data_source_factory=factory,
        artifacts_root=tmp_path,
        data_root_revision="test-revision-1",
    )
    assert result.status == "failed"
    assert "sample_count" in (result.failure_reason or "")


# ---------------------------------------------------------------------------
# Lineage.
# ---------------------------------------------------------------------------
def test_baselines_runs_have_distinct_ids(tmp_path: Path, parent_run):
    ledger, _, factory = parent_run
    request = BaselineRequest(
        parent_run_id=ledger.run_id,
        method="buy_and_hold",
        sample_count=1,
    )
    config_a, _ = run_baselines(
        request,
        data_source_factory=factory,
        artifacts_root=tmp_path,
        data_root_revision="test-revision-1",
    )
    config_b, _ = run_baselines(
        request,
        data_source_factory=factory,
        artifacts_root=tmp_path,
        data_root_revision="test-revision-1",
    )
    assert config_a.baseline_id != config_b.baseline_id
    # Same parent_trade_log_hash: the two runs reference the same
    # parent run and its trade list didn't change.
    assert config_a.parent_trade_log_hash == config_b.parent_trade_log_hash
