"""Momentum RSI + Stochastic strategy — state-based with EOD exit.

Ported from Backend/Services/Implementation/BacktestService.cs RunMomentumRsiStochastic.

Entry: RSI in band + price > fast SMA > slow SMA + %K > %D
Exit: N minutes before last bar of trading day, or end of period.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pandas_ta as ta

from app.services.strategies.common import (
    StrategyResult,
    TradeRecord,
    compute_metrics,
    make_trade,
)


def run(df: pd.DataFrame, params: dict) -> StrategyResult:
    rsi_length = params.get("RsiLength", 14)
    rsi_low = params.get("RsiLow", 40.0)
    rsi_high = params.get("RsiHigh", 60.0)
    fast_ma = params.get("FastMa", 20)
    slow_ma = params.get("SlowMa", 50)
    stoch_k = params.get("StochK", 14)
    stoch_d = params.get("StochD", 3)
    exit_minutes = params.get("ExitMinutesBefore", 15)

    result = StrategyResult(success=False, strategy_name="momentum_rsi_stochastic")
    result.bars_processed = len(df)

    min_bars = max(slow_ma, rsi_length, stoch_k + stoch_d) + 1
    if len(df) < min_bars:
        result.error = f"Not enough bars ({len(df)}) for indicators"
        return result

    df = df.copy()
    df["rsi"] = ta.rsi(df["close"], length=rsi_length)
    df["sma_fast"] = ta.sma(df["close"], length=fast_ma)
    df["sma_slow"] = ta.sma(df["close"], length=slow_ma)

    stoch_df = ta.stoch(df["high"], df["low"], df["close"], k=stoch_k, d=stoch_d)
    if stoch_df is not None and not stoch_df.empty:
        df["stoch_k"] = stoch_df.iloc[:, 0]
        df["stoch_d"] = stoch_df.iloc[:, 1]
    else:
        df["stoch_k"] = np.nan
        df["stoch_d"] = np.nan

    # Pre-compute last bar per trading day (by date portion of timestamp)
    def _get_day(ts):
        if isinstance(ts, (int, float, np.integer, np.floating)):
            return datetime.fromtimestamp(int(ts) / 1000, tz=UTC).date()
        if isinstance(ts, datetime):
            return ts.date()
        return None

    df["_day"] = df["timestamp"].apply(_get_day)
    last_bar_by_day: dict = {}
    for _idx, row in df.iterrows():
        day = row["_day"]
        if day is not None:
            ts = row["timestamp"]
            if day not in last_bar_by_day or ts > last_bar_by_day[day]:
                last_bar_by_day[day] = ts

    trades: list[TradeRecord] = []
    in_position = False
    entry_idx = 0
    cum_pnl_pct = 0.0
    trade_num = 0

    for i in range(min_bars, len(df)):
        row = df.iloc[i]

        # EOD exit check
        if in_position:
            day = row["_day"]
            ts = row["timestamp"]
            day_last = last_bar_by_day.get(day)
            is_eod = False
            if day_last is not None:
                # Convert both to comparable ms
                ts_val = (
                    int(ts)
                    if isinstance(ts, (int, float))
                    else int(ts.timestamp() * 1000)
                    if isinstance(ts, datetime)
                    else 0
                )
                dl_val = (
                    int(day_last)
                    if isinstance(day_last, (int, float))
                    else int(day_last.timestamp() * 1000)
                    if isinstance(day_last, datetime)
                    else 0
                )
                is_eod = ts_val >= dl_val - exit_minutes * 60 * 1000

            is_last = i == len(df) - 1

            if is_eod or is_last:
                trade_num += 1
                reason = f"EOD exit {exit_minutes}min before close" if is_eod else "Position closed at end of period"
                t = make_trade(
                    trade_num,
                    "Buy",
                    df.iloc[entry_idx],
                    row,
                    cum_pnl_pct,
                    reason,
                )
                cum_pnl_pct = t.cumulative_pnl_pct
                trades.append(t)
                in_position = False
                continue

        # Entry check
        if not in_position:
            rsi_val = row.get("rsi")
            sma_f = row.get("sma_fast")
            sma_s = row.get("sma_slow")
            sk = row.get("stoch_k")
            sd = row.get("stoch_d")

            if any(pd.isna(v) for v in [rsi_val, sma_f, sma_s, sk, sd]):
                continue

            rsi_ok = rsi_low <= rsi_val <= rsi_high
            trend_ok = sma_f > sma_s and row["close"] > sma_f
            stoch_ok = sk > sd

            if rsi_ok and trend_ok and stoch_ok and i + 1 < len(df):
                in_position = True
                entry_idx = i + 1  # Enter at next bar

    result.trades = trades
    result.success = True
    metrics = compute_metrics(trades)
    for k, v in metrics.items():
        setattr(result, k, v)

    return result
