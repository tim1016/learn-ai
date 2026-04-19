"""S1 — EMA(5)/EMA(10) crossover + RSI(14) filter. 5-bar hold.

Mirrors the rule-set of ``app.engine.strategy.algorithms.spy_ema_crossover``
but in a vectorized pandas form so we can drive it against TV, native,
and engine indicator columns without touching the LEAN streaming loop.

Entry (long) when all hold:
    * Fresh EMA5 crosses above EMA10 on this bar (prev: EMA5 <= EMA10).
    * (EMA5 - EMA10) >= gap_threshold (default 0.20).
    * rsi_min <= RSI(14) <= rsi_max (default 50-70).

Exit:
    * After ``hold_bars`` bars (default 5). Sell at that bar's close.
"""

from __future__ import annotations

import pandas as pd

from app.research.divergence.strategies.common import Trade, TradeList, _validate_required


def run_s1_ema_crossover(
    df: pd.DataFrame,
    *,
    ema_fast_col: str,
    ema_slow_col: str,
    rsi_col: str,
    time_col: str = "time_utc",
    close_col: str = "close_pg",
    gap_threshold: float = 0.20,
    rsi_min: float = 50.0,
    rsi_max: float = 70.0,
    hold_bars: int = 5,
    variant: str = "V-?",
    timeframe: str = "15m",
) -> TradeList:
    """Run S1 across ``df`` and return matched trades.

    ``df`` must be in chronological order, RTH-only for V-A/V-B/V-C. For V-D
    the caller is expected to have already computed indicators on ETH-
    contaminated data before filtering to RTH execution bars.
    """
    required = [ema_fast_col, ema_slow_col, rsi_col, time_col, close_col]
    _validate_required(df, required)

    tl = TradeList(strategy="s1_ema_crossover", variant=variant, timeframe=timeframe)

    fast = df[ema_fast_col].values
    slow = df[ema_slow_col].values
    rsi = df[rsi_col].values
    close = df[close_col].values
    times = df[time_col].reset_index(drop=True)
    n = len(df)

    in_position = False
    entry_idx = -1
    bars_until_exit = 0

    for i in range(1, n):
        # Bail if any indicator is NaN (still in warmup)
        if pd.isna(fast[i]) or pd.isna(slow[i]) or pd.isna(rsi[i]) or pd.isna(fast[i - 1]) or pd.isna(slow[i - 1]):
            continue

        if in_position:
            bars_until_exit -= 1
            if bars_until_exit <= 0:
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
                        exit_reason="hold_expired",
                    )
                )
                in_position = False
            continue

        # Entry check
        fresh_cross = fast[i] > slow[i] and fast[i - 1] <= slow[i - 1]
        gap_ok = (fast[i] - slow[i]) >= gap_threshold
        rsi_ok = rsi_min <= rsi[i] <= rsi_max

        if fresh_cross and gap_ok and rsi_ok:
            in_position = True
            entry_idx = i
            bars_until_exit = hold_bars

    # Flush any open position at end of data
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
