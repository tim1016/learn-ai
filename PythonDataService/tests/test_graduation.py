"""Tests for app.research.signal.graduation.

Graduation combines backtest grid, walk-forward, and data sufficiency into a
pass/fail assessment. Tests exercise the thresholds for each criterion and
the overall grade/status labelling logic.
"""

from __future__ import annotations

import pytest

from app.research.signal.backtest import BacktestResult
from app.research.signal.diagnostics import DataSufficiency, SignalDiagnostics
from app.research.signal.graduation import (
    GraduationResult,
    ParameterStability,
    evaluate_graduation,
)
from app.research.signal.walk_forward import WalkForwardResult, WalkForwardWindow


def _bt(
    threshold: float,
    cost_bps: float = 2.0,
    net_sharpe: float = 0.0,
    max_drawdown: float = 0.10,
) -> BacktestResult:
    return BacktestResult(
        threshold=threshold,
        cost_bps=cost_bps,
        net_sharpe=net_sharpe,
        max_drawdown=max_drawdown,
    )


def _wf(
    pct_windows_positive_sharpe: float = 1.0,
    n_windows: int = 5,
    slope: float = 0.0,
) -> WalkForwardResult:
    # Minimal stub windows to satisfy "windows must be non-empty".
    windows = [WalkForwardWindow(fold_index=i) for i in range(n_windows)]
    return WalkForwardResult(
        windows=windows,
        pct_windows_positive_sharpe=pct_windows_positive_sharpe,
        oos_sharpe_trend_slope=slope,
    )


def _suff(effective_oos_bars: int = 2000) -> DataSufficiency:
    return DataSufficiency(effective_oos_bars=effective_oos_bars)


def test_evaluate_graduation_all_pass_yields_grade_a_and_robust_alpha():
    grid = [
        _bt(threshold=0.5, net_sharpe=1.0, max_drawdown=0.08),
        _bt(threshold=1.0, net_sharpe=1.05, max_drawdown=0.08),
        _bt(threshold=1.5, net_sharpe=1.02, max_drawdown=0.08),
    ]
    regime_coverage = {f"R{i}": 50 for i in range(6)}
    walk = _wf(pct_windows_positive_sharpe=0.80, n_windows=5)

    result = evaluate_graduation(
        walk_forward=walk,
        backtest_grid=grid,
        regime_coverage=regime_coverage,
        signal_diagnostics=SignalDiagnostics(),
        data_sufficiency=_suff(),
    )

    assert isinstance(result, GraduationResult)
    assert result.overall_passed is True
    assert result.overall_grade == "A"
    assert result.status_label == "Robust Alpha"
    assert all(c.passed for c in result.criteria)


def test_evaluate_graduation_low_sharpe_fails_first_criterion():
    grid = [_bt(threshold=1.0, net_sharpe=0.5, max_drawdown=0.08)]
    regime_coverage = {f"R{i}": 50 for i in range(6)}
    walk = _wf(pct_windows_positive_sharpe=0.80)

    result = evaluate_graduation(
        walk_forward=walk,
        backtest_grid=grid,
        regime_coverage=regime_coverage,
        signal_diagnostics=SignalDiagnostics(),
        data_sufficiency=_suff(),
    )

    first_criterion = result.criteria[0]
    assert first_criterion.name == "Net Sharpe Ratio"
    assert first_criterion.passed is False
    assert "Net Sharpe" in first_criterion.failure_reason


def test_evaluate_graduation_high_drawdown_fails_second_criterion():
    grid = [_bt(threshold=1.0, net_sharpe=1.2, max_drawdown=0.30)]
    regime_coverage = {f"R{i}": 50 for i in range(6)}
    walk = _wf(pct_windows_positive_sharpe=0.80)

    result = evaluate_graduation(
        walk_forward=walk,
        backtest_grid=grid,
        regime_coverage=regime_coverage,
        signal_diagnostics=SignalDiagnostics(),
        data_sufficiency=_suff(),
    )

    assert result.criteria[1].name == "Maximum Drawdown"
    assert result.criteria[1].passed is False
    assert result.overall_passed is False


def test_evaluate_graduation_insufficient_oos_bars_labels_exploratory():
    grid = [_bt(threshold=1.0, net_sharpe=1.2, max_drawdown=0.08)]
    regime_coverage = {f"R{i}": 50 for i in range(6)}
    walk = _wf(pct_windows_positive_sharpe=0.80)

    result = evaluate_graduation(
        walk_forward=walk,
        backtest_grid=grid,
        regime_coverage=regime_coverage,
        signal_diagnostics=SignalDiagnostics(),
        data_sufficiency=_suff(effective_oos_bars=500),
    )

    assert result.status_label == "Exploratory"


def test_evaluate_graduation_missing_walk_forward_labels_exploratory():
    grid = [_bt(threshold=1.0, net_sharpe=1.2, max_drawdown=0.08)]
    regime_coverage = {f"R{i}": 50 for i in range(6)}

    result = evaluate_graduation(
        walk_forward=None,
        backtest_grid=grid,
        regime_coverage=regime_coverage,
        signal_diagnostics=SignalDiagnostics(),
        data_sufficiency=_suff(),
    )

    assert result.status_label == "Exploratory"


def test_evaluate_graduation_negative_slope_labels_degrading():
    grid = [_bt(threshold=1.0, net_sharpe=1.2, max_drawdown=0.08)]
    regime_coverage = {f"R{i}": 50 for i in range(6)}
    walk = _wf(pct_windows_positive_sharpe=0.80, slope=-0.5)

    result = evaluate_graduation(
        walk_forward=walk,
        backtest_grid=grid,
        regime_coverage=regime_coverage,
        signal_diagnostics=SignalDiagnostics(),
        data_sufficiency=_suff(),
    )

    assert result.status_label == "Degrading"


def test_evaluate_graduation_parameter_stability_single_grid_point_is_stable():
    grid = [_bt(threshold=1.0, net_sharpe=1.2, max_drawdown=0.08)]
    regime_coverage = {f"R{i}": 50 for i in range(6)}
    walk = _wf(pct_windows_positive_sharpe=0.80)

    result = evaluate_graduation(
        walk_forward=walk,
        backtest_grid=grid,
        regime_coverage=regime_coverage,
        signal_diagnostics=SignalDiagnostics(),
        data_sufficiency=_suff(),
    )

    # With only 1 threshold value in the grid, stability defaults to 1.0/Stable.
    assert isinstance(result.parameter_stability, ParameterStability)
    assert result.parameter_stability.stability_score == pytest.approx(1.0, abs=1e-12, rel=0)
    assert result.parameter_stability.stability_label == "Stable"


def test_evaluate_graduation_parameter_stability_fragile_when_variance_high():
    # Spread Sharpe widely across thresholds → std/|mean| > 1 → score clamps to 0.
    grid = [
        _bt(threshold=0.5, net_sharpe=2.0, max_drawdown=0.08),
        _bt(threshold=1.0, net_sharpe=-1.0, max_drawdown=0.08),
        _bt(threshold=1.5, net_sharpe=0.1, max_drawdown=0.08),
    ]
    regime_coverage = {f"R{i}": 50 for i in range(6)}
    walk = _wf(pct_windows_positive_sharpe=0.80)

    result = evaluate_graduation(
        walk_forward=walk,
        backtest_grid=grid,
        regime_coverage=regime_coverage,
        signal_diagnostics=SignalDiagnostics(),
        data_sufficiency=_suff(),
    )

    assert result.parameter_stability.stability_label in {"Fragile", "Sensitive"}
    assert result.parameter_stability.stability_score < 0.7


def test_evaluate_graduation_regime_coverage_fail_when_fewer_than_four():
    grid = [_bt(threshold=1.0, net_sharpe=1.2, max_drawdown=0.08)]
    regime_coverage = {"R0": 100, "R1": 100}  # only 2 regimes with observations
    walk = _wf(pct_windows_positive_sharpe=0.80)

    result = evaluate_graduation(
        walk_forward=walk,
        backtest_grid=grid,
        regime_coverage=regime_coverage,
        signal_diagnostics=SignalDiagnostics(),
        data_sufficiency=_suff(),
    )

    regime_criterion = next(c for c in result.criteria if c.name == "Regime Coverage")
    assert regime_criterion.passed is False
    assert regime_criterion.value == pytest.approx(2.0, abs=1e-12, rel=0)
    assert result.overall_passed is False
