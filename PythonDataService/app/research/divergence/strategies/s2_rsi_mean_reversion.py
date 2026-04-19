"""S2 — RSI(14) mean reversion, long-only.

Entry when RSI crosses below ``entry_threshold`` (default 30) from above.
Exit when RSI crosses above ``exit_threshold`` (default 50) from below,
or after ``max_bars`` bars (default 20) as a time stop.
"""

from __future__ import annotations

import pandas as pd

from app.research.divergence.strategies.common import Trade, TradeList, _validate_required


def run_s2_rsi_mean_reversion(
    df: pd.DataFrame,
    *,
    rsi_col: str,
    time_col: str = "time_utc",
    close_col: str = "close_pg",
    entry_threshold: float = 30.0,
    exit_threshold: float = 50.0,
    max_bars: int = 20,
    variant: str = "V-?",
    timeframe: str = "15m",
) -> TradeList:
    _validate_required(df, [rsi_col, time_col, close_col])

    tl = TradeList(strategy="s2_rsi_mean_reversion", variant=variant, timeframe=timeframe)
    rsi = df[rsi_col].values
    close = df[close_col].values
    times = df[time_col].reset_index(drop=True)
    n = len(df)

    in_position = False
    entry_idx = -1
    bars_held = 0

    for i in range(1, n):
        if pd.isna(rsi[i]) or pd.isna(rsi[i - 1]):
            continue

        if in_position:
            bars_held += 1
            crossed_exit = rsi[i] > exit_threshold and rsi[i - 1] <= exit_threshold
            hit_time_stop = bars_held >= max_bars
            if crossed_exit or hit_time_stop:
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
                        exit_reason="rsi_exit" if crossed_exit else "time_stop",
                    )
                )
                in_position = False
                bars_held = 0
            continue

        crossed_entry = rsi[i] < entry_threshold and rsi[i - 1] >= entry_threshold
        if crossed_entry:
            in_position = True
            entry_idx = i
            bars_held = 0

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
