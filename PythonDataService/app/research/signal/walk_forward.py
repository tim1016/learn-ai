"""Walk-forward validation with rolling train/test windows."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

from app.research.signal.backtest import run_backtest
from app.research.signal.config import SignalConfig
from app.research.signal.regime import compute_bar_regime_gate
from app.research.signal.standardize import apply_threshold_filter, compute_train_zscore
from app.research.features.ta_features import TechnicalFeatures
from app.research.target import compute_15min_forward_return

logger = logging.getLogger(__name__)


@dataclass
class AlphaDecayStats:
    """Regression statistics for OOS Sharpe trend (alpha decay)."""

    slope: float = 0.0
    intercept: float = 0.0
    t_stat: float = 0.0
    p_value: float = 1.0
    r_squared: float = 0.0


@dataclass
class WalkForwardWindow:
    """Metrics for a single walk-forward fold."""

    fold_index: int = 0
    train_start: str = ""
    train_end: str = ""
    test_start: str = ""
    test_end: str = ""
    train_bars: int = 0
    test_bars: int = 0
    mu: float = 0.0
    sigma: float = 0.0
    best_threshold: float = 0.0
    oos_net_sharpe: float = 0.0
    oos_gross_sharpe: float = 0.0
    oos_max_drawdown: float = 0.0
    oos_net_return: float = 0.0
    oos_win_rate: float = 0.0
    oos_total_trades: int = 0
    oos_dates: list[str] = field(default_factory=list)
    oos_cumulative_returns: list[float] = field(default_factory=list)


@dataclass
class WalkForwardResult:
    """Aggregated walk-forward validation results."""

    windows: list[WalkForwardWindow] = field(default_factory=list)
    mean_oos_sharpe: float = 0.0
    std_oos_sharpe: float = 0.0
    median_oos_sharpe: float = 0.0
    pct_windows_profitable: float = 0.0
    pct_windows_positive_sharpe: float = 0.0
    worst_window_sharpe: float = 0.0
    best_window_sharpe: float = 0.0
    total_oos_bars: int = 0
    combined_oos_dates: list[str] = field(default_factory=list)
    combined_oos_cumulative_returns: list[float] = field(default_factory=list)
    oos_sharpe_trend_slope: float = 0.0
    alpha_decay: AlphaDecayStats = field(default_factory=AlphaDecayStats)


def run_walk_forward(
    bars: list[dict],
    feature_name: str,
    config: SignalConfig,
) -> WalkForwardResult:
    """Run walk-forward validation with rolling train/test windows."""
    df = pd.DataFrame(bars).sort_values("timestamp").reset_index(drop=True)
    df["date"] = pd.to_datetime(df["timestamp"], unit="ms").dt.date

    # Build month boundaries
    unique_dates = sorted(df["date"].unique())
    if len(unique_dates) < 10:
        logger.warning("[Signal] Not enough dates for walk-forward: %d", len(unique_dates))
        return WalkForwardResult()

    months = _get_month_boundaries(unique_dates)
    train_months = config.walk_forward_train_months
    test_months = config.walk_forward_test_months
    total_needed = train_months + test_months

    if len(months) < total_needed:
        logger.warning("[Signal] Not enough months for walk-forward: %d < %d", len(months), total_needed)
        return WalkForwardResult()

    windows: list[WalkForwardWindow] = []
    fold_idx = 0

    for start_idx in range(0, len(months) - total_needed + 1):
        train_month_range = months[start_idx : start_idx + train_months]
        test_month_range = months[start_idx + train_months : start_idx + total_needed]

        train_start_date = train_month_range[0][0]
        train_end_date = train_month_range[-1][1]
        test_start_date = test_month_range[0][0]
        test_end_date = test_month_range[-1][1]

        # Slice bars for this fold
        fold_mask = (df["date"] >= train_start_date) & (df["date"] <= test_end_date)
        fold_df = df[fold_mask].reset_index(drop=True)

        if len(fold_df) < config.min_bars_for_signal:
            continue

        fold_bars = fold_df.drop(columns=["date"]).to_dict("records")

        # Compute feature and returns on full fold
        feature = TechnicalFeatures.compute_feature(feature_name, fold_bars)
        forward_returns = compute_15min_forward_return(fold_bars, config.horizon)

        fold_dates = fold_df["date"]
        train_mask = pd.Series(
            (fold_dates >= train_start_date) & (fold_dates <= train_end_date),
            index=fold_df.index,
        )
        test_mask = pd.Series(
            (fold_dates >= test_start_date) & (fold_dates <= test_end_date),
            index=fold_df.index,
        )

        # Fit mu, sigma from train only
        train_feature = feature[train_mask].dropna()
        if len(train_feature) < 10:
            continue
        mu = float(train_feature.mean())
        sigma = float(train_feature.std())
        if sigma < 1e-10:
            continue

        # Z-score all bars with train params
        z_all = (feature - mu) / sigma
        if config.flip_sign:
            z_all = -z_all

        # Select best threshold on train by net Sharpe
        best_threshold = config.thresholds[0]
        best_train_sharpe = -np.inf
        for threshold in config.thresholds:
            train_signal = apply_threshold_filter(z_all[train_mask], threshold)
            train_returns = forward_returns[train_mask]
            from app.research.signal.backtest import run_backtest as _run_bt
            bt = _run_bt(train_signal, train_returns, config.default_cost_bps)
            if bt.net_sharpe > best_train_sharpe:
                best_train_sharpe = bt.net_sharpe
                best_threshold = threshold

        # Apply frozen (mu, sigma, theta) to test
        test_signal = apply_threshold_filter(z_all[test_mask], best_threshold)

        # Regime gate on test
        if config.regime_gate_enabled:
            gate = compute_bar_regime_gate(fold_bars, fold_df["timestamp"])
            test_gate = gate[test_mask]
            test_signal = test_signal * test_gate

        test_returns = forward_returns[test_mask]
        test_timestamps = fold_df["timestamp"][test_mask]

        oos_bt = run_backtest(
            test_signal, test_returns, config.default_cost_bps,
            timestamps=test_timestamps,
            include_series=True,
        )

        window = WalkForwardWindow(
            fold_index=fold_idx,
            train_start=str(train_start_date),
            train_end=str(train_end_date),
            test_start=str(test_start_date),
            test_end=str(test_end_date),
            train_bars=int(train_mask.sum()),
            test_bars=int(test_mask.sum()),
            mu=mu,
            sigma=sigma,
            best_threshold=best_threshold,
            oos_net_sharpe=oos_bt.net_sharpe,
            oos_gross_sharpe=oos_bt.gross_sharpe,
            oos_max_drawdown=oos_bt.max_drawdown,
            oos_net_return=oos_bt.net_total_return,
            oos_win_rate=oos_bt.win_rate,
            oos_total_trades=oos_bt.total_trades,
            oos_dates=oos_bt.dates,
            oos_cumulative_returns=oos_bt.cumulative_returns,
        )
        windows.append(window)
        fold_idx += 1

    if not windows:
        return WalkForwardResult()

    return _aggregate_walk_forward(windows)


def _aggregate_walk_forward(windows: list[WalkForwardWindow]) -> WalkForwardResult:
    """Compute aggregate statistics from walk-forward windows."""
    sharpes = [w.oos_net_sharpe for w in windows]
    returns = [w.oos_net_return for w in windows]

    mean_sharpe = float(np.mean(sharpes))
    std_sharpe = float(np.std(sharpes, ddof=1)) if len(sharpes) > 1 else 0.0
    median_sharpe = float(np.median(sharpes))

    pct_profitable = sum(1 for r in returns if r > 0) / len(returns)
    pct_positive_sharpe = sum(1 for s in sharpes if s > 0) / len(sharpes)

    total_oos_bars = sum(w.test_bars for w in windows)

    # Combined OOS equity curve
    combined_dates: list[str] = []
    combined_cum: list[float] = []
    running_total = 0.0
    for w in windows:
        for i, d in enumerate(w.oos_dates):
            combined_dates.append(d)
            if i < len(w.oos_cumulative_returns):
                combined_cum.append(running_total + w.oos_cumulative_returns[i])
        if w.oos_cumulative_returns:
            running_total += w.oos_cumulative_returns[-1]

    # Alpha decay: linear regression of OOS Sharpe vs fold index
    decay = _compute_sharpe_trend_slope(sharpes)

    return WalkForwardResult(
        windows=windows,
        mean_oos_sharpe=mean_sharpe,
        std_oos_sharpe=std_sharpe,
        median_oos_sharpe=median_sharpe,
        pct_windows_profitable=pct_profitable,
        pct_windows_positive_sharpe=pct_positive_sharpe,
        worst_window_sharpe=float(np.min(sharpes)),
        best_window_sharpe=float(np.max(sharpes)),
        total_oos_bars=total_oos_bars,
        combined_oos_dates=combined_dates,
        combined_oos_cumulative_returns=combined_cum,
        oos_sharpe_trend_slope=decay.slope,
        alpha_decay=decay,
    )


def _compute_sharpe_trend_slope(sharpes: list[float]) -> AlphaDecayStats:
    """Linear regression of OOS Sharpe over fold indices with t-stat/p-value."""
    n = len(sharpes)
    if n < 2:
        return AlphaDecayStats()

    x = np.arange(n, dtype=float)
    y = np.array(sharpes, dtype=float)
    x_mean = float(np.mean(x))
    y_mean = float(np.mean(y))
    ss_xy = float(np.sum((x - x_mean) * (y - y_mean)))
    ss_xx = float(np.sum((x - x_mean) ** 2))

    if ss_xx < 1e-12:
        return AlphaDecayStats()

    slope = ss_xy / ss_xx
    intercept = y_mean - slope * x_mean

    # Residuals and standard error
    y_hat = slope * x + intercept
    ssr = float(np.sum((y - y_hat) ** 2))
    ss_tot = float(np.sum((y - y_mean) ** 2))
    r_squared = 1.0 - ssr / ss_tot if ss_tot > 1e-12 else 0.0

    if n > 2:
        mse = ssr / (n - 2)
        se_slope = float(np.sqrt(mse / ss_xx)) if mse > 0 else 0.0
        t_stat = slope / se_slope if se_slope > 1e-12 else 0.0
        p_value = float(2.0 * scipy_stats.t.sf(abs(t_stat), df=n - 2))
    else:
        t_stat = 0.0
        p_value = 1.0

    return AlphaDecayStats(
        slope=slope,
        intercept=intercept,
        t_stat=t_stat,
        p_value=p_value,
        r_squared=r_squared,
    )


def _get_month_boundaries(dates: list[date]) -> list[tuple[date, date]]:
    """Group dates into (first_date, last_date) per calendar month."""
    months: dict[tuple[int, int], list[date]] = {}
    for d in dates:
        key = (d.year, d.month)
        if key not in months:
            months[key] = []
        months[key].append(d)

    result = []
    for key in sorted(months.keys()):
        month_dates = months[key]
        result.append((min(month_dates), max(month_dates)))
    return result
