"""Backtesting engine: position sizing, cost modeling, performance metrics."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.research.signal.standardize import apply_threshold_filter, compute_train_zscore

logger = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    """Performance metrics for a single threshold × cost configuration."""

    threshold: float
    cost_bps: float
    dates: list[str] = field(default_factory=list)
    cumulative_returns: list[float] = field(default_factory=list)
    positions: list[float] = field(default_factory=list)
    gross_sharpe: float = 0.0
    net_sharpe: float = 0.0
    max_drawdown: float = 0.0
    annualized_turnover: float = 0.0
    avg_holding_bars: float = 0.0
    win_rate: float = 0.0
    avg_win_loss_ratio: float = 0.0
    total_trades: int = 0
    net_total_return: float = 0.0
    gross_total_return: float = 0.0


def run_backtest(
    signal: pd.Series,
    forward_returns: pd.Series,
    cost_bps: float,
    bars_per_day: int = 390,
    timestamps: pd.Series | None = None,
    include_series: bool = False,
) -> BacktestResult:
    """Run a single backtest with transaction cost modeling.

    Step 4: w_t = sign(signal_t), cap |w_t| <= 1
    Step 5: r_net_t = w_{t-1} * r_t - cost * |w_t - w_{t-1}|  (NO lookahead)
    Step 6: Compute all performance metrics
    """
    cost_frac = cost_bps / 10_000.0
    bars_per_year = bars_per_day * 252

    # Align signal and returns, drop NaN
    mask = signal.notna() & forward_returns.notna()
    sig = signal[mask].values.astype(float)
    ret = forward_returns[mask].values.astype(float)

    n = len(sig)
    if n < 2:
        return BacktestResult(threshold=0.0, cost_bps=cost_bps)

    # Positions: w_t = sign(signal_t), cap at +/-1
    positions = np.clip(sig, -1.0, 1.0)

    # Returns: r_net_t = w_{t-1} * r_t - cost * |w_t - w_{t-1}|
    # Use w_{t-1} to avoid lookahead: position from previous bar earns current return
    turnover = np.abs(np.diff(positions, prepend=0.0))
    gross_returns = np.zeros(n)
    gross_returns[1:] = positions[:-1] * ret[1:]  # w_{t-1} * r_t
    net_returns = gross_returns - cost_frac * turnover

    # Metrics
    gross_sharpe = _annualized_sharpe(gross_returns, bars_per_year)
    net_sharpe = _annualized_sharpe(net_returns, bars_per_year)
    max_dd = _max_drawdown(net_returns)
    ann_turnover = float(np.mean(turnover) * bars_per_year)
    avg_hold = _avg_holding_bars(positions)
    win_rate, avg_wl = _win_loss_stats(net_returns)
    total_trades = int(np.sum(turnover > 0))
    net_total = float(np.sum(net_returns))
    gross_total = float(np.sum(gross_returns))

    result = BacktestResult(
        threshold=0.0,  # set by caller
        cost_bps=cost_bps,
        gross_sharpe=gross_sharpe,
        net_sharpe=net_sharpe,
        max_drawdown=max_dd,
        annualized_turnover=ann_turnover,
        avg_holding_bars=avg_hold,
        win_rate=win_rate,
        avg_win_loss_ratio=avg_wl,
        total_trades=total_trades,
        net_total_return=net_total,
        gross_total_return=gross_total,
    )

    if include_series and timestamps is not None:
        ts = timestamps[mask]
        result.dates = [str(pd.to_datetime(t, unit="ms").date()) for t in ts.values]
        cum = np.cumsum(net_returns)
        result.cumulative_returns = cum.tolist()
        result.positions = positions.tolist()

    return result


def run_backtest_grid(
    feature: pd.Series,
    forward_returns: pd.Series,
    regime_gate: pd.Series | None,
    train_mask: pd.Series,
    flip_sign: bool,
    thresholds: tuple[float, ...],
    cost_bps_options: tuple[float, ...],
    timestamps: pd.Series | None = None,
    bars_per_day: int = 390,
) -> list[BacktestResult]:
    """Run all (threshold x cost_bps) combinations."""
    z_scores = compute_train_zscore(feature, train_mask, flip_sign)

    results: list[BacktestResult] = []
    best_net_sharpe = -np.inf
    best_key: tuple[float, float] | None = None

    for threshold in thresholds:
        signal = apply_threshold_filter(z_scores, threshold)
        if regime_gate is not None:
            signal = signal * regime_gate

        for cost in cost_bps_options:
            bt = run_backtest(
                signal, forward_returns, cost,
                bars_per_day=bars_per_day,
                timestamps=timestamps,
                include_series=False,
            )
            bt.threshold = threshold
            results.append(bt)

            if bt.net_sharpe > best_net_sharpe:
                best_net_sharpe = bt.net_sharpe
                best_key = (threshold, cost)

    # Re-run best config with series data
    if best_key is not None:
        best_threshold, best_cost = best_key
        signal = apply_threshold_filter(z_scores, best_threshold)
        if regime_gate is not None:
            signal = signal * regime_gate
        best_bt = run_backtest(
            signal, forward_returns, best_cost,
            bars_per_day=bars_per_day,
            timestamps=timestamps,
            include_series=True,
        )
        best_bt.threshold = best_threshold
        # Replace the matching result
        for i, r in enumerate(results):
            if r.threshold == best_threshold and r.cost_bps == best_cost:
                results[i] = best_bt
                break

    return results


def _annualized_sharpe(returns: np.ndarray, bars_per_year: int) -> float:
    """Annualized Sharpe ratio."""
    if len(returns) < 2:
        return 0.0
    mean_r = float(np.mean(returns))
    std_r = float(np.std(returns, ddof=1))
    if std_r < 1e-12:
        return 0.0
    return mean_r / std_r * np.sqrt(bars_per_year)


def _max_drawdown(returns: np.ndarray) -> float:
    """Maximum drawdown from cumulative returns."""
    cum = np.cumsum(returns)
    peak = np.maximum.accumulate(cum)
    drawdown = peak - cum
    if len(drawdown) == 0:
        return 0.0
    return float(np.max(drawdown))


def _avg_holding_bars(positions: np.ndarray) -> float:
    """Average number of bars a position is held."""
    changes = np.sum(np.abs(np.diff(positions)) > 0)
    if changes == 0:
        return float(len(positions))
    return float(len(positions)) / float(changes)


def _win_loss_stats(returns: np.ndarray) -> tuple[float, float]:
    """Win rate and average win/loss ratio from non-zero returns."""
    active = returns[returns != 0]
    if len(active) == 0:
        return 0.0, 0.0

    wins = active[active > 0]
    losses = active[active < 0]
    win_rate = float(len(wins)) / float(len(active))

    if len(wins) == 0 or len(losses) == 0:
        avg_wl = 0.0
    else:
        avg_win = float(np.mean(wins))
        avg_loss = float(np.abs(np.mean(losses)))
        avg_wl = avg_win / avg_loss if avg_loss > 0 else 0.0

    return win_rate, avg_wl
