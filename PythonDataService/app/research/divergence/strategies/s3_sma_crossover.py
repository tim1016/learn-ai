"""S3 — SMA(50)/SMA(200) golden / death cross. Long-only.

Entry on a fresh SMA50 > SMA200 crossover (golden cross). Exit on the
opposite crossover (death cross). Always one position open at a time.
"""

from __future__ import annotations

import pandas as pd

from app.research.divergence.strategies.common import Trade, TradeList, _validate_required


def run_s3_sma_crossover(
    df: pd.DataFrame,
    *,
    sma_fast_col: str,
    sma_slow_col: str,
    time_col: str = "time_utc",
    close_col: str = "close_pg",
    variant: str = "V-?",
    timeframe: str = "15m",
) -> TradeList:
    _validate_required(df, [sma_fast_col, sma_slow_col, time_col, close_col])

    tl = TradeList(strategy="s3_sma_crossover", variant=variant, timeframe=timeframe)
    fast = df[sma_fast_col].values
    slow = df[sma_slow_col].values
    close = df[close_col].values
    times = df[time_col].reset_index(drop=True)
    n = len(df)

    in_position = False
    entry_idx = -1

    for i in range(1, n):
        if pd.isna(fast[i]) or pd.isna(slow[i]) or pd.isna(fast[i - 1]) or pd.isna(slow[i - 1]):
            continue

        if not in_position:
            if fast[i] > slow[i] and fast[i - 1] <= slow[i - 1]:
                in_position = True
                entry_idx = i
        else:
            if fast[i] < slow[i] and fast[i - 1] >= slow[i - 1]:
                exit_idx = i
                entry_px = float(close[entry_idx])
                exit_px = float(close[exit_idx])
                pnl = exit_px - entry_px
                tl.trades.append(
                    Trade(
                        entry_idx=entry_idx,
                        exit_idx=exit_idx,
                        entry_time=times.iloc[entry_idx],
                        exit_time=times.iloc[exit_idx],
                        entry_price=entry_px,
                        exit_price=exit_px,
                        bars_held=exit_idx - entry_idx,
                        pnl_dollars=pnl,
                        pnl_pct=pnl / entry_px * 100.0,
                        exit_reason="death_cross",
                    )
                )
                in_position = False

    if in_position and entry_idx >= 0 and entry_idx < n - 1:
        exit_idx = n - 1
        entry_px = float(close[entry_idx])
        exit_px = float(close[exit_idx])
        pnl = exit_px - entry_px
        tl.trades.append(
            Trade(
                entry_idx=entry_idx,
                exit_idx=exit_idx,
                entry_time=times.iloc[entry_idx],
                exit_time=times.iloc[exit_idx],
                entry_price=entry_px,
                exit_price=exit_px,
                bars_held=exit_idx - entry_idx,
                pnl_dollars=pnl,
                pnl_pct=pnl / entry_px * 100.0,
                exit_reason="end_of_data",
            )
        )
    return tl
