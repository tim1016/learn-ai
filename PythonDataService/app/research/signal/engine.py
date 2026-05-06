"""Signal engine orchestrator — converts validated features into tradable signals.

Formula: Orchestration only — wraps standardize, backtest, walk_forward, diagnostics, graduation into a single Signal pipeline. No new arithmetic.
Reference: Internal — docs/signal-engine-authority.md.
Canonical implementation: app/research/signal/engine.py
Validated against: NONE — pending (pending-fixture per registry)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.stats import skew as scipy_skew

from app.research.features.ta_features import TechnicalFeatures
from app.research.signal.backtest import BacktestResult, run_backtest_grid
from app.research.signal.config import SignalConfig
from app.research.signal.diagnostics import (
    DataSufficiency,
    DeflatedSharpe,
    EffectiveSampleSize,
    RegimeBucket,
    SharpeCi,
    SignalDiagnostics,
    compute_data_sufficiency,
    compute_deflated_sharpe,
    compute_effective_sample_size,
    compute_joint_regime_coverage,
    compute_sharpe_ci,
    compute_signal_diagnostics,
)
from app.research.signal.graduation import GraduationResult, evaluate_graduation
from app.research.signal.regime import compute_bar_regime_gate, compute_daily_regime_labels
from app.research.signal.standardize import apply_threshold_filter, compute_train_zscore
from app.research.signal.walk_forward import WalkForwardResult, run_walk_forward
from app.research.target import compute_15min_forward_return, validate_return_series

logger = logging.getLogger(__name__)


# Optional progress callbacks used by the Jobs/SSE wrapper. Default no-ops
# keep synchronous callers (existing GraphQL endpoint, tests) unaffected.
PhaseCallback = Callable[[str], None]
LogCallback = Callable[[str, str], None]
ProgressCallback = Callable[[int, int, str, "str | None"], None]
CancelCallback = Callable[[], bool]


def _noop_phase(phase: str) -> None:
    return None


def _noop_log(message: str, level: str = "info") -> None:
    return None


def _noop_progress(current: int, total: int, unit: str = "configs", message: str | None = None) -> None:
    return None


def _no_cancel() -> bool:
    return False


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
    """Marginal day counts by regime label. Kept for backwards compat;
    new consumers should use ``joint_regime_coverage`` which is a true
    (vol × trend) joint distribution with effective-trades estimates."""
    joint_regime_coverage: list[RegimeBucket] = field(default_factory=list)
    signal_behavior: SignalBehaviorMetrics | None = None
    oos_sharpe_ci: SharpeCi | None = None
    """Lo (2002) confidence interval for the headline OOS Sharpe."""
    deflated_sharpe: DeflatedSharpe | None = None
    """Bailey & López de Prado DSR for the in-sample grid headline."""
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
    on_phase: PhaseCallback = _noop_phase,
    on_log: LogCallback = _noop_log,
    on_progress: ProgressCallback = _noop_progress,
    cancel_check: CancelCallback = _no_cancel,
) -> SignalEngineReport:
    """Run the complete signal engine pipeline.

    Phases (emitted via ``on_phase``):
      ``compute_feature`` → ``diagnostics`` → ``regime_coverage`` →
      ``backtest_grid`` → ``walk_forward`` → ``effective_sample`` →
      ``graduation``.

    The Jobs/SSE wrapper passes a ``cancel_check`` that raises
    ``JobCancelled`` when the user cancels the run; we let that
    propagate so ``run_in_thread`` emits ``job.cancelled``.
    """
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
            ticker,
            feature_name,
            start_date,
            end_date,
            len(bars),
        )

        # Validate minimum data
        if len(bars) < config.min_bars_for_signal:
            raise ValueError(f"Not enough bars: {len(bars)} < {config.min_bars_for_signal} minimum")

        report.bars_used = len(bars)
        df = pd.DataFrame(bars).sort_values("timestamp").reset_index(drop=True)

        # ── Step 1: Compute feature and forward returns ────────────────
        cancel_check()
        on_phase("compute_feature")
        on_log(f"Computing the '{feature_name}' signal feature on {len(bars):,} bars")
        feature = TechnicalFeatures.compute_feature(feature_name, bars)
        forward_returns = compute_15min_forward_return(bars, config.horizon)
        if not validate_return_series(forward_returns):
            raise ValueError("Forward return series failed validation")

        # ── Step 2: 70/30 train/test split ─────────────────────────────
        n = len(df)
        split_idx = int(n * 0.70)
        train_mask = pd.Series([True] * split_idx + [False] * (n - split_idx), index=df.index)

        # ── Step 3: Signal diagnostics ─────────────────────────────────
        cancel_check()
        on_phase("diagnostics")
        on_log("Running diagnostic checks (z-score on training set, threshold filter, regime gate)")
        z_scores = compute_train_zscore(feature, train_mask, config.flip_sign)
        default_threshold = config.thresholds[0] if config.thresholds else 1.0
        threshold_signal = apply_threshold_filter(z_scores, default_threshold)

        regime_gate = None
        regime_gated_signal = None
        if config.regime_gate_enabled:
            regime_gate = compute_bar_regime_gate(bars, df["timestamp"])
            regime_gated_signal = threshold_signal * regime_gate

        report.signal_diagnostics = compute_signal_diagnostics(
            z_scores,
            threshold_signal,
            regime_gated_signal,
        )
        if report.signal_diagnostics is not None:
            on_log(
                f"Active on {report.signal_diagnostics.pct_time_active * 100:.1f}% of bars "
                f"after thresholding"
            )

        # ── Step 4: Regime coverage ────────────────────────────────────
        cancel_check()
        on_phase("regime_coverage")
        on_log("Computing daily regime labels across volatility and trend buckets")
        daily_regimes = compute_daily_regime_labels(bars)
        regime_cov: dict[str, int] = {}
        for _, row in daily_regimes.iterrows():
            for regime_type in ["vol_regime", "trend_regime"]:
                label = str(row[regime_type])
                regime_cov[label] = regime_cov.get(label, 0) + 1
        report.regime_coverage = regime_cov
        on_log(f"Observed {len(regime_cov)} regime labels over {len(daily_regimes)} trading days")

        # ── Step 5: In-sample backtest grid ────────────────────────────
        cancel_check()
        on_phase("backtest_grid")
        n_configs = len(config.thresholds) * len(config.cost_bps_options)
        on_log(
            f"Sweeping {n_configs} backtest configurations "
            f"({len(config.thresholds)} thresholds × {len(config.cost_bps_options)} cost levels)"
        )
        on_progress(0, n_configs, "configs", "Starting in-sample sweep")
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
        on_progress(n_configs, n_configs, "configs", "In-sample sweep complete")

        # Find best config
        best: BacktestResult | None = None
        if report.backtest_grid:
            best = max(report.backtest_grid, key=lambda r: r.net_sharpe)
            report.best_threshold = best.threshold
            report.best_cost_bps = best.cost_bps
            on_log(
                f"Best in-sample config: threshold = {best.threshold:.2f}σ, "
                f"cost = {best.cost_bps:.0f} bps, net Sharpe = {best.net_sharpe:.2f}"
            )

        # Step 5a: Deflated Sharpe on the in-sample grid headline.
        # The grid evaluates ``len(thresholds) × len(cost_bps_options)``
        # configurations and we report the maximum; under the null of
        # zero true Sharpe, the maximum is upward-biased. DSR computes
        # the probability that the true Sharpe exceeds zero given the
        # selection — see authority doc § 4.7.
        if best is not None and best.cumulative_returns:
            best_bar_returns = np.diff(
                np.asarray(best.cumulative_returns, dtype=float),
                prepend=0.0,
            )
            if best_bar_returns.size >= 3:
                best_n_eff = compute_effective_sample_size(
                    pd.Series(best_bar_returns)
                ).effective_n
                n_trials = len(config.thresholds) * len(config.cost_bps_options)
                report.deflated_sharpe = compute_deflated_sharpe(
                    selected_sharpe_annual=best.net_sharpe,
                    bar_returns=best_bar_returns,
                    n_trials=n_trials,
                    n_eff=best_n_eff,
                )
                if report.deflated_sharpe is not None:
                    on_log(
                        f"Deflated Sharpe = {report.deflated_sharpe.raw_sharpe:.2f} "
                        f"(P(true Sharpe > 0) = {report.deflated_sharpe.dsr_probability * 100:.1f}%)"
                    )

        # Step 5b: Signal behavior metrics on best config
        if report.best_threshold > 0:
            best_signal = apply_threshold_filter(z_scores, report.best_threshold)
            if regime_gate is not None:
                best_signal = best_signal * regime_gate
            report.signal_behavior = _compute_signal_behavior(
                best_signal,
                forward_returns,
            )
            if report.signal_behavior is not None:
                on_log(
                    f"Hit rate on active bars = {report.signal_behavior.hit_rate * 100:.1f}%"
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

        # ── Step 6: Walk-forward validation ────────────────────────────
        cancel_check()
        on_phase("walk_forward")
        on_log(
            f"Running walk-forward validation: {config.walk_forward_train_months}-month train, "
            f"{config.walk_forward_test_months}-month test, rolling windows"
        )
        report.walk_forward = run_walk_forward(bars, feature_name, config)
        if report.walk_forward is not None and report.walk_forward.windows:
            wf = report.walk_forward
            on_log(
                f"Walk-forward complete: {len(wf.windows)} windows, "
                f"mean OOS Sharpe = {wf.mean_oos_sharpe:.2f}, "
                f"{wf.pct_windows_positive_sharpe * 100:.0f}% windows positive"
            )

        # Step 6b: Lo (2002) confidence interval on the headline OOS Sharpe.
        if (
            report.walk_forward is not None
            and report.walk_forward.combined_oos_bar_returns
        ):
            oos_bars_arr = np.asarray(
                report.walk_forward.combined_oos_bar_returns, dtype=float
            )
            if oos_bars_arr.size >= 3:
                oos_n_eff = compute_effective_sample_size(
                    pd.Series(oos_bars_arr)
                ).effective_n
                report.oos_sharpe_ci = compute_sharpe_ci(
                    bar_returns=oos_bars_arr,
                    n_eff=oos_n_eff,
                )

        # ── Step 7: Effective sample size ──────────────────────────────
        cancel_check()
        on_phase("effective_sample")
        on_log("Estimating effective sample size from autocorrelation structure")
        report.effective_sample = compute_effective_sample_size(forward_returns)
        if report.effective_sample is not None:
            on_log(
                f"Effective N = {report.effective_sample.effective_n:.0f} "
                f"(raw N = {report.effective_sample.raw_n:,})"
            )

        # Step 7b: Joint regime coverage (vol × trend)
        if (
            report.effective_sample is not None
            and report.signal_diagnostics is not None
        ):
            report.joint_regime_coverage = compute_joint_regime_coverage(
                daily_regimes=daily_regimes,
                n_eff=report.effective_sample.effective_n,
                pct_active=report.signal_diagnostics.pct_time_active,
            )

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

        # ── Step 9: Graduation ─────────────────────────────────────────
        cancel_check()
        on_phase("graduation")
        on_log("Evaluating graduation criteria across the 0/1/2/3 ladder")
        report.graduation = evaluate_graduation(
            walk_forward=report.walk_forward,
            backtest_grid=report.backtest_grid,
            regime_coverage=regime_cov,
            signal_diagnostics=report.signal_diagnostics,
            data_sufficiency=report.data_sufficiency,
        )
        if report.graduation is not None:
            on_log(
                f"Graduation: {report.graduation.status_label} "
                f"(grade {report.graduation.overall_grade})",
                level="info",
            )

        # Step 10: Research log
        report.research_log = _generate_research_log(report, config)

        logger.info(
            "[Signal] Complete: %s %s — grade=%s, status=%s",
            ticker,
            feature_name,
            report.graduation.overall_grade if report.graduation else "N/A",
            report.graduation.status_label if report.graduation else "N/A",
        )

    except Exception as e:
        # Cancellation propagates to the wrapper untouched (sniffed by
        # name to avoid a layering import). Other exceptions land on the
        # report so downstream consumers see a structured failure.
        if type(e).__name__ == "JobCancelled":
            raise
        logger.error("[Signal] Error: %s", str(e), exc_info=True)
        report.error = str(e)

    return report


def _compute_signal_behavior(
    signal: pd.Series,
    forward_returns: pd.Series,
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
        parts.append(f"Net Sharpe: {best.net_sharpe:.2f} ({best.cost_bps:.0f}bps cost).")
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
