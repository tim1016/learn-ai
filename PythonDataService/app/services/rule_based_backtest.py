"""Rule-based backtest engine — configurable entry/exit conditions via JSON parameters.

Supports composable entry conditions (EMA crossover, RSI band, ADX filter, gap filter)
and multiple exit modes (fixed candles, indicator-based).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import pandas_ta as ta

from app.utils.timestamps import timestamp_like_to_ms_utc

logger = logging.getLogger(__name__)


@dataclass
class RuleBasedTrade:
    trade_number: int
    trade_type: str  # "Buy"
    entry_timestamp: int
    exit_timestamp: int
    entry_price: float
    exit_price: float
    pnl: float
    pnl_pct: float
    cumulative_pnl_pct: float
    signal_reason: str
    # Indicator snapshots at entry for validation
    ema_fast: float | None = None
    ema_slow: float | None = None
    ema_gap: float | None = None
    rsi: float | None = None
    adx: float | None = None


@dataclass
class RuleBasedBacktestResult:
    success: bool
    ticker: str
    strategy_name: str
    parameters: dict
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    win_loss_ratio: float = 0.0
    profit_factor: float = 0.0
    expectancy_per_trade: float = 0.0
    total_pnl_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    total_pnl_pts: float = 0.0
    sharpe_ratio: float = 0.0
    bars_processed: int = 0
    trades: list[RuleBasedTrade] = field(default_factory=list)
    error: str | None = None


def run_rule_based_backtest(
    ticker: str,
    bars: list[dict],
    params: dict,
) -> RuleBasedBacktestResult:
    """Run a configurable rule-based backtest on OHLCV bars.

    Parameters (in `params`):
        fast_ema_period: int (default 5)
        slow_ema_period: int (default 10)
        rsi_period: int (default 14)
        adx_period: int (default 14)
        min_ema_gap: float (default 0.20)
        rsi_min: float (default 50)
        rsi_max: float (default 70)
        adx_min: float | None (optional, no filter if None)
        exit_mode: str ("fixed_bars")
        exit_bars: int (default 5) — number of candles after entry
        direction: str ("long")
    """
    fast_period = params.get("fast_ema_period", 5)
    slow_period = params.get("slow_ema_period", 10)
    rsi_period = params.get("rsi_period", 14)
    adx_period = params.get("adx_period", 14)
    min_ema_gap = params.get("min_ema_gap", 0.20)
    rsi_min = params.get("rsi_min", 50.0)
    rsi_max = params.get("rsi_max", 70.0)
    adx_min = params.get("adx_min")
    exit_bars = params.get("exit_bars", 5)

    strategy_name = params.get("strategy_name", "ema_crossover_rsi")

    result = RuleBasedBacktestResult(
        success=False,
        ticker=ticker,
        strategy_name=strategy_name,
        parameters=params,
    )

    if len(bars) < max(fast_period, slow_period, rsi_period, adx_period) + 10:
        result.error = f"Not enough bars ({len(bars)}) for indicator warm-up"
        return result

    df = pd.DataFrame(bars)
    df = df.drop_duplicates(subset=["timestamp"], keep="last")
    df = df.sort_values("timestamp").reset_index(drop=True)
    if not df["timestamp"].is_monotonic_increasing:
        result.error = "Input bars are not monotonic in timestamp after dedup"
        return result
    result.bars_processed = len(df)

    # Compute indicators via pandas-ta
    df["ema_fast"] = ta.ema(df["close"], length=fast_period)
    df["ema_slow"] = ta.ema(df["close"], length=slow_period)
    df["ema_gap"] = df["ema_fast"] - df["ema_slow"]

    rsi_series = ta.rsi(df["close"], length=rsi_period)
    df["rsi"] = rsi_series

    adx_df = ta.adx(df["high"], df["low"], df["close"], length=adx_period)
    if adx_df is not None and not adx_df.empty:
        df["adx"] = adx_df.iloc[:, 0]
    else:
        df["adx"] = np.nan

    # Detect fresh EMA crossovers:
    # Previous bar: ema_fast <= ema_slow
    # Current bar: ema_fast > ema_slow
    df["prev_ema_fast"] = df["ema_fast"].shift(1)
    df["prev_ema_slow"] = df["ema_slow"].shift(1)
    df["crossover"] = (df["prev_ema_fast"] <= df["prev_ema_slow"]) & (df["ema_fast"] > df["ema_slow"])

    trades: list[RuleBasedTrade] = []
    cum_pnl_pct = 0.0
    trade_num = 0

    i = 0
    while i < len(df):
        row = df.iloc[i]

        # Skip if indicators not ready
        if pd.isna(row["ema_fast"]) or pd.isna(row["ema_slow"]) or pd.isna(row["rsi"]):
            i += 1
            continue

        # Check entry conditions
        is_crossover = bool(row["crossover"])
        gap_ok = row["ema_gap"] >= min_ema_gap
        rsi_ok = rsi_min <= row["rsi"] <= rsi_max
        adx_ok = True
        if adx_min is not None and not pd.isna(row.get("adx", np.nan)):
            adx_ok = row["adx"] >= adx_min

        if is_crossover and gap_ok and rsi_ok and adx_ok:
            entry_idx = i
            entry_price = float(row["close"])
            entry_ts = _format_timestamp(row["timestamp"])

            # Fixed-bar exit
            exit_idx = min(entry_idx + exit_bars, len(df) - 1)
            exit_row = df.iloc[exit_idx]
            exit_price = float(exit_row["close"])
            exit_ts = _format_timestamp(exit_row["timestamp"])

            pnl_pts = exit_price - entry_price
            pnl_pct = pnl_pts / entry_price
            cum_pnl_pct += pnl_pct
            trade_num += 1

            trades.append(
                RuleBasedTrade(
                    trade_number=trade_num,
                    trade_type="Buy",
                    entry_timestamp=entry_ts,
                    exit_timestamp=exit_ts,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    pnl=pnl_pts,
                    pnl_pct=pnl_pct,
                    cumulative_pnl_pct=cum_pnl_pct,
                    signal_reason=(
                        f"EMA({fast_period}) crossed above EMA({slow_period}), "
                        f"gap={row['ema_gap']:.4f}, RSI={row['rsi']:.2f}"
                    ),
                    ema_fast=round(float(row["ema_fast"]), 4),
                    ema_slow=round(float(row["ema_slow"]), 4),
                    ema_gap=round(float(row["ema_gap"]), 4),
                    rsi=round(float(row["rsi"]), 2),
                    adx=round(float(row["adx"]), 2) if not pd.isna(row.get("adx", np.nan)) else None,
                )
            )

            # Jump past exit to avoid overlapping trades
            i = exit_idx + 1
            continue

        i += 1

    # Compute performance metrics
    result.trades = trades
    result.total_trades = len(trades)
    result.success = True

    if trades:
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl <= 0]
        result.winning_trades = len(wins)
        result.losing_trades = len(losses)
        result.win_rate = len(wins) / len(trades)
        result.total_pnl_pct = cum_pnl_pct
        result.total_pnl_pts = sum(t.pnl for t in trades)

        if wins:
            result.avg_win_pct = sum(t.pnl_pct for t in wins) / len(wins)
        if losses:
            result.avg_loss_pct = sum(t.pnl_pct for t in losses) / len(losses)

        if result.avg_loss_pct != 0:
            result.win_loss_ratio = abs(result.avg_win_pct / result.avg_loss_pct)

        total_win = sum(t.pnl_pct for t in wins)
        total_loss = abs(sum(t.pnl_pct for t in losses))
        if total_loss > 0:
            result.profit_factor = total_win / total_loss

        result.expectancy_per_trade = cum_pnl_pct / len(trades)
        result.max_drawdown_pct = _compute_max_drawdown([t.cumulative_pnl_pct for t in trades])

        pnl_series = np.array([t.pnl_pct for t in trades])
        if len(pnl_series) > 1:
            mean_r = float(np.mean(pnl_series))
            std_r = float(np.std(pnl_series, ddof=1))
            if std_r > 1e-12:
                result.sharpe_ratio = round(mean_r / std_r * np.sqrt(252), 4)

    return result


def _compute_max_drawdown(cum_pnl: list[float]) -> float:
    if not cum_pnl:
        return 0.0
    peak = 0.0
    max_dd = 0.0
    for val in cum_pnl:
        if val > peak:
            peak = val
        dd = peak - val
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _format_timestamp(ts: object) -> int:
    """Convert a timestamp-like value to canonical int64 ms UTC."""
    return timestamp_like_to_ms_utc(ts, field_name="rule-based trade timestamp")
