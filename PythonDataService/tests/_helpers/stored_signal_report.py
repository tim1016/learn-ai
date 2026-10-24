"""A signal-engine report with every field set, as the engine would store it, and what the history route must serve of it.

The report is built from the engine's own dataclasses and serialized by the engine's own serializer
(``serialize_signal_report``), so a field the engine adds shows up in ``stored_text`` and, unless the route's model
carries it, in a failing comparison with ``served``.
"""

from __future__ import annotations

import json
from typing import Any

from app.research.signal.backtest import BacktestResult
from app.research.signal.diagnostics import (
    DataSufficiency,
    DeflatedSharpe,
    EffectiveSampleSize,
    RegimeBucket,
    SharpeCi,
    SignalDiagnostics,
)
from app.research.signal.engine import SignalBehaviorMetrics, SignalEngineReport
from app.research.signal.graduation import (
    GraduationCriterion,
    GraduationResult,
    GraduationStageInfo,
    ParameterStability,
    Stage0Failure,
    Stage0Rejection,
    StageAdvanceCriterion,
)
from app.research.signal.walk_forward import AlphaDecayStats, WalkForwardResult, WalkForwardWindow
from app.services.signal_study_service import serialize_signal_report

# Stored by the engine but not served: the fold-concatenated bar returns the Sharpe interval is computed from. The
# retired GraphQL report never carried it either, and no screen reads it.
UNSERVED_WALK_FORWARD_FIELDS = ("combined_oos_bar_returns",)


def a_busy_signal_report() -> SignalEngineReport:
    return SignalEngineReport(
        ticker="SPY",
        feature_name="momentum_5m",
        start_date="2025-01-02",
        end_date="2025-06-30",
        bars_used=48_750,
        flip_sign=False,
        thresholds_tested=[0.5, 1.0, 1.5],
        cost_bps_options=[1.0, 2.5],
        best_threshold=1.0,
        best_cost_bps=2.5,
        backtest_grid=[
            BacktestResult(
                threshold=1.0,
                cost_bps=2.5,
                dates=["2025-01-02", "2025-01-03"],
                cumulative_returns=[0.001, -0.0025],
                positions=[1.0, -1.0],
                gross_sharpe=1.4,
                net_sharpe=0.9,
                max_drawdown=-0.07,
                annualized_turnover=31.5,
                avg_holding_bars=12.25,
                win_rate=0.52,
                avg_win_loss_ratio=1.1,
                total_trades=412,
                net_total_return=0.18,
                gross_total_return=0.23,
            )
        ],
        walk_forward=WalkForwardResult(
            windows=[
                WalkForwardWindow(
                    fold_index=2,
                    train_start="2025-01-02",
                    train_end="2025-03-31",
                    test_start="2025-04-01",
                    test_end="2025-04-30",
                    train_bars=19_500,
                    test_bars=6_500,
                    mu=0.0003,
                    sigma=0.0011,
                    best_threshold=1.5,
                    oos_net_sharpe=0.7,
                    oos_gross_sharpe=1.1,
                    oos_max_drawdown=-0.03,
                    oos_net_return=0.04,
                    oos_win_rate=0.51,
                    oos_total_trades=77,
                    oos_dates=["2025-04-01"],
                    oos_cumulative_returns=[0.002],
                )
            ],
            mean_oos_sharpe=0.65,
            std_oos_sharpe=0.3,
            median_oos_sharpe=0.6,
            pct_windows_profitable=0.66,
            pct_windows_positive_sharpe=0.75,
            worst_window_sharpe=-0.4,
            best_window_sharpe=1.3,
            total_oos_bars=26_000,
            combined_oos_dates=["2025-04-01", "2025-05-01"],
            combined_oos_cumulative_returns=[0.002, 0.011],
            combined_oos_bar_returns=[0.0001, -0.0002, 0.0003],
            oos_sharpe_trend_slope=-0.05,
            alpha_decay=AlphaDecayStats(
                slope=-0.05,
                intercept=0.9,
                t_stat=-1.7,
                p_value=0.12,
                r_squared=0.31,
                n_folds_used=6,
                is_test_valid=True,
                is_significant=False,
            ),
        ),
        graduation=GraduationResult(
            criteria=[
                GraduationCriterion(
                    name="oos_sharpe",
                    description="Mean out-of-sample Sharpe",
                    passed=True,
                    value=0.65,
                    threshold=0.5,
                    label="Pass",
                    failure_reason="",
                )
            ],
            overall_passed=True,
            overall_grade="B",
            summary="Holds out of sample.",
            status_label="Research",
            parameter_stability=ParameterStability(
                sharpe_values_by_threshold={0.5: 0.4, 1.0: 0.9, 1.5: 0.7},
                stability_score=0.82,
                stability_label="Stable",
            ),
            stage0_rejection=Stage0Rejection(
                rejected=True,
                failed_criteria=[
                    Stage0Failure(
                        criterion_name="turnover", value=210.0, threshold_repr="> 200x", message="Trades too often."
                    )
                ],
            ),
            stage_info=GraduationStageInfo(
                stage=2,
                label="Research",
                description="Worth studying further.",
                next_stage_label="Promotion",
                advance_criteria=[
                    StageAdvanceCriterion(
                        name="stability",
                        description="Parameter stability",
                        current_value=0.82,
                        required_repr=">= 0.5",
                        met=True,
                    )
                ],
            ),
        ),
        signal_diagnostics=SignalDiagnostics(
            signal_mean=0.01,
            signal_std=1.02,
            pct_time_active=0.43,
            avg_abs_signal=0.8,
            pct_filtered_by_threshold=0.2,
            pct_gated_by_regime=0.1,
        ),
        data_sufficiency=DataSufficiency(
            total_bars=48_750,
            train_bars=19_500,
            test_bars=6_500,
            walk_forward_folds=6,
            effective_oos_bars=26_000,
            regimes_covered=3,
            regime_coverage={"low_vol": 60, "high_vol": 25},
            coverage_warnings=["Few high-vol days."],
        ),
        effective_sample=EffectiveSampleSize(
            raw_n=48_750,
            effective_n=9_100.5,
            autocorrelation_lag1=0.12,
            independent_bets=410,
            max_lag_used=15,
            rho_sum=1.9,
        ),
        regime_coverage={"low_vol_up": 40, "high_vol_down": 12},
        joint_regime_coverage=[
            RegimeBucket(vol_label="low", trend_label="up", days=40, effective_trades=31.5, badge="Pass")
        ],
        signal_behavior=SignalBehaviorMetrics(
            avg_forward_return_when_active=0.0002,
            skewness_active_returns=0.3,
            avg_win_return=0.0009,
            avg_loss_return=-0.0008,
            hit_rate=0.53,
        ),
        oos_sharpe_ci=SharpeCi(
            point=0.65, se=0.2, ci_lower=0.26, ci_upper=1.04, confidence_level=0.95, n_eff_used=9_100.5, valid=True
        ),
        deflated_sharpe=DeflatedSharpe(
            raw_sharpe=0.9,
            expected_max_under_null=0.5,
            dsr_probability=0.71,
            n_trials=12,
            skewness=0.1,
            kurtosis=3.4,
            valid=True,
        ),
        methodology={
            "train_months": 3,
            "test_months": 1,
            "window_type": "rolling",
            "optimization_target": "net_sharpe",
            "annualization_factor": 98_280,
            "bars_per_day": 390,
            "horizon": 15,
            "default_cost_bps": 2.0,
            "min_bars_for_signal": 500,
            "flip_sign": False,
            "regime_gate_enabled": True,
            "thresholds": [0.5, 1.0, 1.5],
            "cost_bps_options": [1.0, 2.5],
        },
        research_log="Loaded 48,750 bars\nRan 12 configs",
        error=None,
    )


def stored_text(report: SignalEngineReport) -> str:
    """The report as the JSON text a ``JsonReport`` column held."""
    return json.dumps(serialize_signal_report(report))


def served(report: SignalEngineReport) -> dict[str, Any]:
    """What the history route must answer for ``report``: the stored content, with its three known reshapings."""
    content = json.loads(stored_text(report))
    for name in UNSERVED_WALK_FORWARD_FIELDS:
        content["walk_forward"].pop(name)
    content["regime_coverage"] = _regime_entries(content["regime_coverage"])
    content["data_sufficiency"]["regime_coverage"] = _regime_entries(content["data_sufficiency"]["regime_coverage"])
    stability = content["graduation"]["parameter_stability"]
    stability["sharpe_values_by_threshold"] = [
        {"threshold": float(threshold), "sharpe": sharpe}
        for threshold, sharpe in stability["sharpe_values_by_threshold"].items()
    ]
    return content


def _regime_entries(counts: dict[str, int]) -> list[dict[str, Any]]:
    return [{"regime": regime, "count": count} for regime, count in counts.items()]
