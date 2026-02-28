"""Signal engine orchestrator — converts validated features into tradable signals."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.stats import skew as scipy_skew

from app.research.signal.backtest import BacktestResult, run_backtest_grid
from app.research.signal.config import SignalConfig
from app.research.signal.diagnostics import (
    DataSufficiency,
    EffectiveSampleSize,
    SignalDiagnostics,
    compute_data_sufficiency,
    compute_effective_sample_size,
    compute_signal_diagnostics,
)
from app.research.signal.graduation import GraduationResult, evaluate_graduation
from app.research.signal.regime import compute_bar_regime_gate, compute_daily_regime_labels
from app.research.signal.standardize import apply_threshold_filter, compute_train_zscore
from app.research.signal.walk_forward import WalkForwardResult, run_walk_forward
from app.research.features.ta_features import TechnicalFeatures
from app.research.target import compute_15min_forward_return, validate_return_series

logger = logging.getLogger(__name__)


@dataclass
class SignalBehaviorMetrics:
    """Signal behavior analysis on active bars."""

    avg_forward_return_when_active: float = 0.0
    skewness_active_returns: float = 0.0
    avg_win_return: float = 0.0
    avg_loss_return: float = 0.0
    hit_rate: float = 0.0


@dataclass
class SignalEngineReport:
    """Complete signal engine report."""

    ticker: str = ""
    feature_name: str = ""
    start_date: str = ""
    end_date: str = ""
    bars_used: int = 0
    flip_sign: bool = True
    thresholds_tested: list[float] = field(default_factory=list)
    cost_bps_options: list[float] = field(default_factory=list)
    best_threshold: float = 0.0
    best_cost_bps: float = 0.0
    backtest_grid: list[BacktestResult] = field(default_factory=list)
    walk_forward: WalkForwardResult | None = None
    graduation: GraduationResult | None = None
    signal_diagnostics: SignalDiagnostics | None = None
    data_sufficiency: DataSufficiency | None = None
    effective_sample: EffectiveSampleSize | None = None
    regime_coverage: dict[str, int] = field(default_factory=dict)
    signal_behavior: SignalBehaviorMetrics | None = None
    methodology: dict | None = None
    research_log: str = ""
    error: str | None = None


def run_signal_engine(
    ticker: str,
    feature_name: str,
    bars: list[dict],
    start_date: str,
    end_date: str,
    config: SignalConfig | None = None,
) -> SignalEngineReport:
    """Run the complete signal engine pipeline."""
    if config is None:
        config = SignalConfig()

    report = SignalEngineReport(
        ticker=ticker,
        feature_name=feature_name,
        start_date=start_date,
        end_date=end_date,
        flip_sign=config.flip_sign,
        thresholds_tested=list(config.thresholds),
        cost_bps_options=list(config.cost_bps_options),
    )

    try:
        logger.info(
            "[Signal] Starting: %s %s [%s to %s] (%d bars)",
            ticker, feature_name, start_date, end_date, len(bars),
        )

        # Validate minimum data
        if len(bars) < config.min_bars_for_signal:
            raise ValueError(
                f"Not enough bars: {len(bars)} < {config.min_bars_for_signal} minimum"
            )

        report.bars_used = len(bars)
        df = pd.DataFrame(bars).sort_values("timestamp").reset_index(drop=True)

        # Step 1: Compute feature and forward returns
        logger.info("[Signal] Step 1: Computing feature and forward returns")
        feature = TechnicalFeatures.compute_feature(feature_name, bars)
        forward_returns = compute_15min_forward_return(bars, config.horizon)
        if not validate_return_series(forward_returns):
            raise ValueError("Forward return series failed validation")

        # Step 2: 70/30 train/test split
        n = len(df)
        split_idx = int(n * 0.70)
        train_mask = pd.Series([True] * split_idx + [False] * (n - split_idx), index=df.index)

        # Step 3: Signal diagnostics
        logger.info("[Signal] Step 2: Computing signal diagnostics")
        z_scores = compute_train_zscore(feature, train_mask, config.flip_sign)
        default_threshold = config.thresholds[0] if config.thresholds else 1.0
        threshold_signal = apply_threshold_filter(z_scores, default_threshold)

        regime_gate = None
        regime_gated_signal = None
        if config.regime_gate_enabled:
            regime_gate = compute_bar_regime_gate(bars, df["timestamp"])
            regime_gated_signal = threshold_signal * regime_gate

        report.signal_diagnostics = compute_signal_diagnostics(
            z_scores, threshold_signal, regime_gated_signal,
        )

        # Step 4: Regime coverage
        logger.info("[Signal] Step 3: Computing regime coverage")
        daily_regimes = compute_daily_regime_labels(bars)
        regime_cov: dict[str, int] = {}
        for _, row in daily_regimes.iterrows():
            for regime_type in ["vol_regime", "trend_regime"]:
                label = str(row[regime_type])
                regime_cov[label] = regime_cov.get(label, 0) + 1
        report.regime_coverage = regime_cov

        # Step 5: In-sample backtest grid
        logger.info("[Signal] Step 4: Running backtest grid")
        report.backtest_grid = run_backtest_grid(
            feature=feature,
            forward_returns=forward_returns,
            regime_gate=regime_gate,
            train_mask=train_mask,
            flip_sign=config.flip_sign,
            thresholds=config.thresholds,
            cost_bps_options=config.cost_bps_options,
            timestamps=df["timestamp"],
        )

        # Find best config
        if report.backtest_grid:
            best = max(report.backtest_grid, key=lambda r: r.net_sharpe)
            report.best_threshold = best.threshold
            report.best_cost_bps = best.cost_bps

        # Step 5b: Signal behavior metrics on best config
        logger.info("[Signal] Step 5b: Computing signal behavior metrics")
        if report.best_threshold > 0:
            best_signal = apply_threshold_filter(z_scores, report.best_threshold)
            if regime_gate is not None:
                best_signal = best_signal * regime_gate
            report.signal_behavior = _compute_signal_behavior(
                best_signal, forward_returns,
            )

        # Step 5c: Methodology metadata
        report.methodology = {
            "train_months": config.walk_forward_train_months,
            "test_months": config.walk_forward_test_months,
            "window_type": "rolling",
            "optimization_target": "net_sharpe",
            "annualization_factor": 98280,
            "bars_per_day": 390,
            "horizon": config.horizon,
            "default_cost_bps": config.default_cost_bps,
            "min_bars_for_signal": config.min_bars_for_signal,
            "flip_sign": config.flip_sign,
            "regime_gate_enabled": config.regime_gate_enabled,
            "thresholds": list(config.thresholds),
            "cost_bps_options": list(config.cost_bps_options),
        }

        # Step 6: Walk-forward validation
        logger.info("[Signal] Step 5: Running walk-forward validation")
        report.walk_forward = run_walk_forward(bars, feature_name, config)

        # Step 7: Effective sample size
        logger.info("[Signal] Step 6: Computing effective sample size")
        report.effective_sample = compute_effective_sample_size(forward_returns)

        # Step 8: Data sufficiency
        train_bars = int(train_mask.sum())
        test_bars = n - train_bars
        wf_folds = len(report.walk_forward.windows) if report.walk_forward else 0
        oos_bars = report.walk_forward.total_oos_bars if report.walk_forward else 0
        report.data_sufficiency = compute_data_sufficiency(
            total_bars=n,
            train_bars=train_bars,
            test_bars=test_bars,
            walk_forward_folds=wf_folds,
            effective_oos_bars=oos_bars,
            regime_coverage=regime_cov,
        )

        # Step 9: Graduation
        logger.info("[Signal] Step 7: Evaluating graduation criteria")
        report.graduation = evaluate_graduation(
            walk_forward=report.walk_forward,
            backtest_grid=report.backtest_grid,
            regime_coverage=regime_cov,
            signal_diagnostics=report.signal_diagnostics,
            data_sufficiency=report.data_sufficiency,
        )

        # Step 10: Research log
        report.research_log = _generate_research_log(report, config)

        logger.info(
            "[Signal] Complete: %s %s — grade=%s, status=%s",
            ticker, feature_name,
            report.graduation.overall_grade if report.graduation else "N/A",
            report.graduation.status_label if report.graduation else "N/A",
        )

    except Exception as e:
        logger.error("[Signal] Error: %s", str(e), exc_info=True)
        report.error = str(e)

    return report


def _compute_signal_behavior(
    signal: pd.Series, forward_returns: pd.Series,
) -> SignalBehaviorMetrics:
    """Compute signal behavior metrics on active bars."""
    active_mask = signal != 0
    active_returns = (signal[active_mask] * forward_returns[active_mask]).dropna()

    if len(active_returns) < 2:
        return SignalBehaviorMetrics()

    wins = active_returns[active_returns > 0]
    losses = active_returns[active_returns <= 0]

    return SignalBehaviorMetrics(
        avg_forward_return_when_active=float(active_returns.mean()),
        skewness_active_returns=float(scipy_skew(active_returns.values, bias=False)),
        avg_win_return=float(wins.mean()) if len(wins) > 0 else 0.0,
        avg_loss_return=float(losses.mean()) if len(losses) > 0 else 0.0,
        hit_rate=float(len(wins) / len(active_returns)),
    )


def _generate_research_log(report: SignalEngineReport, config: SignalConfig) -> str:
    """Generate auto-summary paragraph for research tracking."""
    parts: list[str] = []
    direction = "mean-reversion" if config.flip_sign else "momentum"
    parts.append(f"{report.feature_name} ({direction}) tested on {report.ticker}.")

    if report.backtest_grid:
        best = max(report.backtest_grid, key=lambda r: r.net_sharpe)
        parts.append(
            f"Net Sharpe: {best.net_sharpe:.2f} ({best.cost_bps:.0f}bps cost)."
        )
        parts.append(f"Turnover: {best.annualized_turnover:.1f}x/year.")

    if report.walk_forward and report.walk_forward.windows:
        wf = report.walk_forward
        parts.append(
            f"Walk-forward OOS Sharpe: {wf.mean_oos_sharpe:.2f}. "
            f"{wf.pct_windows_positive_sharpe * 100:.0f}% windows positive."
        )

    if report.graduation:
        g = report.graduation
        parts.append(f"Status: {g.status_label}.")
        if g.status_label == "Conditional Alpha":
            parts.append("Requires regime gating.")
        elif g.status_label == "Degrading":
            parts.append("Alpha decay detected.")

    return " ".join(parts)
