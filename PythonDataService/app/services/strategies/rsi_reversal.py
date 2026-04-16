"""RSI Reversal strategy — always-in-market, flips between long and short.

Ported from Backend/Services/Implementation/BacktestService.cs RunRsiReversal.

When RSI crosses below oversold: close short, open long (RsiLE).
When RSI crosses above overbought: close long, open short (RsiSE).
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

    result = StrategyResult(success=False, strategy_name="rsi_reversal")
    result.bars_processed = len(df)

    if len(df) < window + 2:
        result.error = f"Not enough bars ({len(df)}) for RSI({window})"
        return result

    df = df.copy()
    df["rsi"] = ta.rsi(df["close"], length=window)

    trades: list[TradeRecord] = []
    position_type: str | None = None  # "Long" or "Short"
    entry_idx = 0
    cum_pnl_pct = 0.0
    trade_num = 0

    for i in range(window + 1, len(df)):
        curr_rsi = df.iloc[i]["rsi"]
        prev_rsi = df.iloc[i - 1]["rsi"]

        if pd.isna(curr_rsi) or pd.isna(prev_rsi):
            continue

        # RsiLE: RSI crosses below oversold → close short, open long
        if prev_rsi >= oversold and curr_rsi < oversold:
            if position_type == "Short":
                trade_num += 1
                t = make_trade(
                    trade_num,
                    "Sell",
                    df.iloc[entry_idx],
                    df.iloc[i],
                    cum_pnl_pct,
                    f"RsiLE: RSI({window}) crossed below {oversold}",
                    {"rsi": round(float(curr_rsi), 2)},
                )
                cum_pnl_pct = t.cumulative_pnl_pct
                trades.append(t)

            position_type = "Long"
            entry_idx = i

        # RsiSE: RSI crosses above overbought → close long, open short
        elif prev_rsi <= overbought and curr_rsi > overbought:
            if position_type == "Long":
                trade_num += 1
                t = make_trade(
                    trade_num,
                    "Buy",
                    df.iloc[entry_idx],
                    df.iloc[i],
                    cum_pnl_pct,
                    f"RsiSE: RSI({window}) crossed above {overbought}",
                    {"rsi": round(float(curr_rsi), 2)},
                )
                cum_pnl_pct = t.cumulative_pnl_pct
                trades.append(t)

            position_type = "Short"
            entry_idx = i

    # Close open position at end
    if position_type is not None:
        trade_type = "Buy" if position_type == "Long" else "Sell"
        trade_num += 1
        t = make_trade(
            trade_num,
            trade_type,
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
