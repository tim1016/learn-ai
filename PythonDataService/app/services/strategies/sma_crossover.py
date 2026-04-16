"""SMA Crossover strategy — golden cross entry, death cross exit.

Ported from Backend/Services/Implementation/BacktestService.cs RunSmaCrossover.
"""

from __future__ import annotations

import pandas as pd
import pandas_ta as ta

from app.services.strategies.common import (
    StrategyResult,
    TradeRecord,
    compute_metrics,
    make_trade,
)


def run(df: pd.DataFrame, params: dict) -> StrategyResult:
    short_window = params.get("ShortWindow", 10)
    long_window = params.get("LongWindow", 30)

    result = StrategyResult(success=False, strategy_name="sma_crossover")
    result.bars_processed = len(df)

    if len(df) < long_window:
        result.error = f"Not enough bars ({len(df)}) for SMA({long_window})"
        return result

    df = df.copy()
    df["sma_short"] = ta.sma(df["close"], length=short_window)
    df["sma_long"] = ta.sma(df["close"], length=long_window)

    trades: list[TradeRecord] = []
    in_position = False
    entry_idx = 0
    cum_pnl_pct = 0.0
    trade_num = 0

    for i in range(long_window, len(df)):
        prev_short = df.iloc[i - 1]["sma_short"]
        prev_long = df.iloc[i - 1]["sma_long"]
        curr_short = df.iloc[i]["sma_short"]
        curr_long = df.iloc[i]["sma_long"]

        if pd.isna(prev_short) or pd.isna(prev_long) or pd.isna(curr_short) or pd.isna(curr_long):
            continue

        # Golden cross: short crosses above long
        if not in_position and prev_short <= prev_long and curr_short > curr_long:
            in_position = True
            entry_idx = i

        # Death cross: short crosses below long
        elif in_position and prev_short >= prev_long and curr_short < curr_long:
            trade_num += 1
            t = make_trade(
                trade_num,
                "Buy",
                df.iloc[entry_idx],
                df.iloc[i],
                cum_pnl_pct,
                f"SMA({short_window}) crossed below SMA({long_window})",
                {"sma_short": round(float(curr_short), 4), "sma_long": round(float(curr_long), 4)},
            )
            cum_pnl_pct = t.cumulative_pnl_pct
            trades.append(t)
            in_position = False

    # Close open position at end
    if in_position:
        trade_num += 1
        t = make_trade(
            trade_num,
            "Buy",
            df.iloc[entry_idx],
            df.iloc[-1],
            cum_pnl_pct,
            "Position closed at end of period",
        )
        cum_pnl_pct = t.cumulative_pnl_pct
        trades.append(t)

    result.trades = trades
    result.success = True
    metrics = compute_metrics(trades)
    for k, v in metrics.items():
        setattr(result, k, v)

    return result
