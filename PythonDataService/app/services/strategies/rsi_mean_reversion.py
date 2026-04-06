"""RSI Mean Reversion strategy — buy below oversold, sell above overbought.

Ported from Backend/Services/Implementation/BacktestService.cs RunRsiMeanReversion.
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
    window = params.get("Window", 14)
    oversold = params.get("Oversold", 30.0)
    overbought = params.get("Overbought", 70.0)

    result = StrategyResult(success=False, strategy_name="rsi_mean_reversion")
    result.bars_processed = len(df)

    if len(df) < window + 1:
        result.error = f"Not enough bars ({len(df)}) for RSI({window})"
        return result

    df = df.copy()
    df["rsi"] = ta.rsi(df["close"], length=window)

    trades: list[TradeRecord] = []
    in_position = False
    entry_idx = 0
    cum_pnl_pct = 0.0
    trade_num = 0

    for i in range(window + 1, len(df)):
        rsi_val = df.iloc[i]["rsi"]
        if pd.isna(rsi_val):
            continue

        # Buy when RSI drops below oversold
        if not in_position and rsi_val < oversold:
            in_position = True
            entry_idx = i

        # Sell when RSI rises above overbought
        elif in_position and rsi_val > overbought:
            trade_num += 1
            t = make_trade(
                trade_num, "Buy",
                df.iloc[entry_idx], df.iloc[i],
                cum_pnl_pct,
                f"RSI({window}) crossed above {overbought}",
                {"rsi": round(float(rsi_val), 2)},
            )
            cum_pnl_pct = t.cumulative_pnl_pct
            trades.append(t)
            in_position = False

    # Close open position at end
    if in_position:
        trade_num += 1
        t = make_trade(
            trade_num, "Buy",
            df.iloc[entry_idx], df.iloc[-1],
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
