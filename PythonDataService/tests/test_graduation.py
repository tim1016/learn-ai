"""Tests for app.research.signal.graduation.

Graduation combines backtest grid, walk-forward, and data sufficiency into a
pass/fail assessment plus a 0/1/2/3 stage ladder. Tests exercise the
thresholds for each criterion, the legacy grade/status labelling, the
Stage 0 kill switch, and the stage-advancement criteria.
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
from app.research.signal.walk_forward import (
    AlphaDecayStats,
    WalkForwardResult,
    WalkForwardWindow,
)


def _bt(
    threshold: float,
    cost_bps: float = 2.0,
    net_sharpe: float = 0.0,
    max_drawdown: float = 0.10,
    annualized_turnover: float = 50.0,
) -> BacktestResult:
    return BacktestResult(
        threshold=threshold,
        cost_bps=cost_bps,
        net_sharpe=net_sharpe,
        max_drawdown=max_drawdown,
        annualized_turnover=annualized_turnover,
    )


def _wf(
    pct_windows_positive_sharpe: float = 1.0,
    n_windows: int = 5,
    slope: float = 0.0,
    median_oos_sharpe: float = 0.5,
    mean_oos_sharpe: float = 0.6,
    alpha_decay: AlphaDecayStats | None = None,
    window_sharpes: list[float] | None = None,
) -> WalkForwardResult:
    # Minimal stub windows to satisfy "windows must be non-empty".
    if window_sharpes is None:
        window_sharpes = [1.0] * n_windows
    windows = [
        WalkForwardWindow(fold_index=i, oos_net_sharpe=window_sharpes[i])
        for i in range(min(n_windows, len(window_sharpes)))
    ]
    return WalkForwardResult(
        windows=windows,
        pct_windows_positive_sharpe=pct_windows_positive_sharpe,
        median_oos_sharpe=median_oos_sharpe,
        mean_oos_sharpe=mean_oos_sharpe,
        oos_sharpe_trend_slope=slope,
        alpha_decay=alpha_decay or AlphaDecayStats(slope=slope),
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


@pytest.mark.parametrize(
    ("has_walk_forward", "effective_oos_bars"),
    [
        pytest.param(True, 500, id="insufficient_oos_bars"),
        pytest.param(False, 2000, id="missing_walk_forward"),
    ],
)
def test_evaluate_graduation_labels_exploratory(has_walk_forward, effective_oos_bars):
    grid = [_bt(threshold=1.0, net_sharpe=1.2, max_drawdown=0.08)]
    regime_coverage = {f"R{i}": 50 for i in range(6)}
    walk = _wf(pct_windows_positive_sharpe=0.80) if has_walk_forward else None

    result = evaluate_graduation(
        walk_forward=walk,
        backtest_grid=grid,
        regime_coverage=regime_coverage,
        signal_diagnostics=SignalDiagnostics(),
        data_sufficiency=_suff(effective_oos_bars=effective_oos_bars),
    )

    assert result.status_label == "Exploratory"


@pytest.mark.parametrize(
    ("p_value", "n_folds_used", "is_test_valid", "is_significant", "labelled_degrading"),
    [
        # Degrading status fires only when the alpha-decay test is statistically valid.
        #
        # The test was previously triggered by any negative slope, including the
        # underpowered N=3 case. With the alpha-decay power guard, ``Degrading``
        # requires both ``is_test_valid`` (>= 5 folds) and ``is_significant``
        # (p < 0.05).
        pytest.param(0.01, 6, True, True, True, id="negative_slope_labels_degrading_when_test_valid_and_significant"),
        # An underpowered alpha-decay regression must not drive the legacy label (< 5 folds).
        pytest.param(0.33, 3, False, False, False, id="negative_slope_does_not_label_degrading_when_underpowered"),
    ],
)
def test_evaluate_graduation_negative_slope_degrading_label(
    p_value, n_folds_used, is_test_valid, is_significant, labelled_degrading
):
    grid = [_bt(threshold=1.0, net_sharpe=1.2, max_drawdown=0.08)]
    regime_coverage = {f"R{i}": 50 for i in range(6)}
    decay = AlphaDecayStats(
        slope=-0.5,
        p_value=p_value,
        n_folds_used=n_folds_used,
        is_test_valid=is_test_valid,
        is_significant=is_significant,
    )
    walk = _wf(pct_windows_positive_sharpe=0.80, alpha_decay=decay)

    result = evaluate_graduation(
        walk_forward=walk,
        backtest_grid=grid,
        regime_coverage=regime_coverage,
        signal_diagnostics=SignalDiagnostics(),
        data_sufficiency=_suff(),
    )

    assert (result.status_label == "Degrading") is labelled_degrading


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


# ─── Stage 0 kill switch ──────────────────────────────────────────────────


def _strong_walk_forward() -> WalkForwardResult:
    """Walk-forward result that comfortably survives Stage 0 on every axis."""
    return _wf(
        pct_windows_positive_sharpe=0.80,
        n_windows=5,
        median_oos_sharpe=0.6,
        mean_oos_sharpe=0.65,
        window_sharpes=[0.4, 0.55, 0.6, 0.7, 0.9],
    )


def test_stage0_passes_when_signal_is_robust_across_all_axes():
    grid = [
        _bt(threshold=0.5, net_sharpe=1.0, max_drawdown=0.08, annualized_turnover=50.0),
        _bt(threshold=1.0, net_sharpe=1.05, max_drawdown=0.08, annualized_turnover=50.0),
        _bt(threshold=1.5, net_sharpe=1.02, max_drawdown=0.08, annualized_turnover=50.0),
    ]
    regime_coverage = {f"R{i}": 50 for i in range(6)}
    result = evaluate_graduation(
        walk_forward=_strong_walk_forward(),
        backtest_grid=grid,
        regime_coverage=regime_coverage,
        signal_diagnostics=SignalDiagnostics(),
        data_sufficiency=_suff(),
    )

    assert result.stage0_rejection.rejected is False
    assert result.stage0_rejection.failed_criteria == []
    assert result.stage_info.stage >= 1


def test_stage0_rejects_on_low_parameter_stability():
    """Stability < 0.25 alone is enough to land at Stage 0 — sensitivity to
    threshold choice is the strongest single indicator of a noise fit."""
    # Highly variable Sharpe across thresholds → low stability.
    grid = [
        _bt(threshold=0.5, net_sharpe=2.0, max_drawdown=0.08, annualized_turnover=50.0),
        _bt(threshold=1.0, net_sharpe=-1.0, max_drawdown=0.08, annualized_turnover=50.0),
        _bt(threshold=1.5, net_sharpe=0.1, max_drawdown=0.08, annualized_turnover=50.0),
    ]
    regime_coverage = {f"R{i}": 50 for i in range(6)}

    result = evaluate_graduation(
        walk_forward=_strong_walk_forward(),
        backtest_grid=grid,
        regime_coverage=regime_coverage,
        signal_diagnostics=SignalDiagnostics(),
        data_sufficiency=_suff(),
    )

    assert result.stage0_rejection.rejected is True
    failed_names = {f.criterion_name for f in result.stage0_rejection.failed_criteria}
    assert "Parameter Stability" in failed_names
    assert result.stage_info.stage == 0
    assert result.stage_info.label == "Rejected"


def test_stage0_rejects_when_median_oos_sharpe_is_zero_or_negative():
    grid = [_bt(threshold=1.0, net_sharpe=1.0, max_drawdown=0.08, annualized_turnover=50.0)]
    regime_coverage = {f"R{i}": 50 for i in range(6)}
    walk = _wf(
        pct_windows_positive_sharpe=0.50,
        median_oos_sharpe=0.0,  # right at the threshold, must fail
        mean_oos_sharpe=0.30,
        window_sharpes=[0.6, 0.0, 0.3, -0.2, 0.4],
    )

    result = evaluate_graduation(
        walk_forward=walk,
        backtest_grid=grid,
        regime_coverage=regime_coverage,
        signal_diagnostics=SignalDiagnostics(),
        data_sufficiency=_suff(),
    )

    assert result.stage0_rejection.rejected is True
    failed_names = {f.criterion_name for f in result.stage0_rejection.failed_criteria}
    assert "Median OOS Sharpe" in failed_names


def test_stage0_rejects_on_too_few_positive_folds():
    grid = [_bt(threshold=1.0, net_sharpe=1.0, max_drawdown=0.08, annualized_turnover=50.0)]
    regime_coverage = {f"R{i}": 50 for i in range(6)}
    # 1 / 5 = 20% positive — below 40%
    walk = _wf(
        pct_windows_positive_sharpe=0.20,
        median_oos_sharpe=0.10,
        window_sharpes=[0.5, -0.3, -0.2, -0.1, -0.4],
    )

    result = evaluate_graduation(
        walk_forward=walk,
        backtest_grid=grid,
        regime_coverage=regime_coverage,
        signal_diagnostics=SignalDiagnostics(),
        data_sufficiency=_suff(),
    )

    failed_names = {f.criterion_name for f in result.stage0_rejection.failed_criteria}
    assert "OOS Folds Positive" in failed_names
    assert result.stage_info.stage == 0


@pytest.mark.parametrize(
    ("net_sharpe", "annualized_turnover", "flagged"),
    [
        # Turnover > 200x AND IS Sharpe < 0.5 fails the economic-realism gate.
        pytest.param(0.40, 350.0, True, id="rejects_on_high_turnover_with_weak_sharpe"),
        # High turnover alone is fine; the gate fires only with weak Sharpe.
        pytest.param(1.20, 500.0, False, id="does_not_reject_high_turnover_with_strong_sharpe"),
    ],
)
def test_stage0_turnover_vs_edge_gate(net_sharpe, annualized_turnover, flagged):
    grid = [
        _bt(threshold=1.0, net_sharpe=net_sharpe, max_drawdown=0.08, annualized_turnover=annualized_turnover),
    ]
    regime_coverage = {f"R{i}": 50 for i in range(6)}

    result = evaluate_graduation(
        walk_forward=_strong_walk_forward(),
        backtest_grid=grid,
        regime_coverage=regime_coverage,
        signal_diagnostics=SignalDiagnostics(),
        data_sufficiency=_suff(),
    )

    failed_names = {f.criterion_name for f in result.stage0_rejection.failed_criteria}
    assert ("Turnover vs Edge" in failed_names) is flagged


# ─── Stage ladder ────────────────────────────────────────────────────────


def test_stage1_when_survives_stage0_but_below_stage2_thresholds():
    """A signal that passes Stage 0 but only weakly is at Stage 1."""
    # Mean OOS = 0.20 < 0.30 (Stage 2 threshold), median > 0, % positive ≥ 40%.
    grid = [
        _bt(threshold=0.5, net_sharpe=0.55, max_drawdown=0.08, annualized_turnover=50.0),
        _bt(threshold=1.0, net_sharpe=0.65, max_drawdown=0.08, annualized_turnover=50.0),
        _bt(threshold=1.5, net_sharpe=0.60, max_drawdown=0.08, annualized_turnover=50.0),
    ]
    regime_coverage = {f"R{i}": 50 for i in range(6)}
    walk = _wf(
        pct_windows_positive_sharpe=0.50,
        median_oos_sharpe=0.20,
        mean_oos_sharpe=0.20,
        n_windows=3,
        window_sharpes=[0.20, 0.30, 0.10],
    )

    result = evaluate_graduation(
        walk_forward=walk,
        backtest_grid=grid,
        regime_coverage=regime_coverage,
        signal_diagnostics=SignalDiagnostics(),
        data_sufficiency=_suff(),
    )

    assert result.stage_info.stage == 1
    assert result.stage_info.label == "Weak Candidate"
    assert result.stage_info.next_stage_label == "Research Candidate"
    # The advance criteria should be the three Stage 2 gates.
    names = {c.name for c in result.stage_info.advance_criteria}
    assert names == {"Mean OOS Sharpe", "Parameter Stability", "Walk-Forward Folds"}


@pytest.mark.parametrize(
    (
        "net_sharpes",
        "pct_windows_positive_sharpe",
        "oos_sharpe",
        "window_sharpes",
        "expected_stage",
        "expected_label",
        "expected_next_stage_label",
    ),
    [
        pytest.param(
            (0.40, 0.42, 0.41, 0.40),
            0.55,
            0.40,
            (0.4, 0.45, 0.5, 0.3, 0.35),
            2,
            "Research Candidate",
            "Promotion Candidate",
            id="stage2_when_meets_research_candidate_thresholds",
        ),
        # Tight grid -> very high stability; strong, consistent OOS performance.
        pytest.param(
            (0.78, 0.80, 0.79, 0.81),
            0.80,
            0.70,
            (0.6, 0.7, 0.8, 0.65, 0.75),
            3,
            "Promotion Candidate",
            "",
            id="stage3_when_meets_all_promotion_thresholds",
        ),
    ],
)
def test_stage_ladder_top_stages(
    net_sharpes,
    pct_windows_positive_sharpe,
    oos_sharpe,
    window_sharpes,
    expected_stage,
    expected_label,
    expected_next_stage_label,
):
    grid = [
        _bt(threshold=threshold, net_sharpe=net_sharpe, max_drawdown=0.08, annualized_turnover=50.0)
        for threshold, net_sharpe in zip((0.5, 1.0, 1.5, 2.0), net_sharpes, strict=True)
    ]
    regime_coverage = {f"R{i}": 50 for i in range(6)}
    walk = _wf(
        pct_windows_positive_sharpe=pct_windows_positive_sharpe,
        median_oos_sharpe=oos_sharpe,
        mean_oos_sharpe=oos_sharpe,
        n_windows=5,
        window_sharpes=list(window_sharpes),
    )

    result = evaluate_graduation(
        walk_forward=walk,
        backtest_grid=grid,
        regime_coverage=regime_coverage,
        signal_diagnostics=SignalDiagnostics(),
        data_sufficiency=_suff(),
    )

    assert result.stage_info.stage == expected_stage, "stage"
    assert result.stage_info.label == expected_label, "stage label"
    assert result.stage_info.next_stage_label == expected_next_stage_label, "next stage label"
