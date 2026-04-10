"""Shared types and utilities for strategy implementations."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
import pandas as pd


@dataclass
class TradeRecord:
    trade_number: int
    trade_type: str  # "Buy" or "Sell"
    entry_timestamp: str
    exit_timestamp: str
    entry_price: float
    exit_price: float
    pnl: float
    pnl_pct: float
    cumulative_pnl_pct: float
    signal_reason: str
    indicator_snapshot: dict[str, float | None] = field(default_factory=dict)


@dataclass
class StrategyResult:
    success: bool
    strategy_name: str
    trades: list[TradeRecord] = field(default_factory=list)
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
    total_pnl_pts: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    bars_processed: int = 0
    error: str | None = None
    # LEAN-compatible statistics (populated by backtest router)
    lean_statistics: dict | None = None


def compute_metrics(trades: list[TradeRecord]) -> dict:
    """Compute performance metrics from a list of trades."""
    if not trades:
        return {}

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]

    total_pnl_pct = trades[-1].cumulative_pnl_pct if trades else 0.0
    total_pnl_pts = sum(t.pnl for t in trades)

    avg_win_pct = (sum(t.pnl_pct for t in wins) / len(wins)) if wins else 0.0
    avg_loss_pct = (sum(t.pnl_pct for t in losses) / len(losses)) if losses else 0.0

    win_loss_ratio = abs(avg_win_pct / avg_loss_pct) if avg_loss_pct != 0 else 0.0

    total_win = sum(t.pnl_pct for t in wins)
    total_loss = abs(sum(t.pnl_pct for t in losses))
    profit_factor = (total_win / total_loss) if total_loss > 0 else 0.0

    expectancy = total_pnl_pct / len(trades) if trades else 0.0

    cum_pnl = [t.cumulative_pnl_pct for t in trades]
    max_dd = _compute_max_drawdown(cum_pnl)

    sharpe = 0.0
    if len(trades) > 1:
        pnl_arr = np.array([t.pnl_pct for t in trades])
        mean_r = float(np.mean(pnl_arr))
        std_r = float(np.std(pnl_arr, ddof=1))
        if std_r > 1e-12:
            sharpe = round(mean_r / std_r * np.sqrt(252), 4)

    return {
        "total_trades": len(trades),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate": len(wins) / len(trades) if trades else 0.0,
        "avg_win_pct": avg_win_pct,
        "avg_loss_pct": avg_loss_pct,
        "win_loss_ratio": win_loss_ratio,
        "profit_factor": profit_factor,
        "expectancy_per_trade": expectancy,
        "total_pnl_pct": total_pnl_pct,
        "total_pnl_pts": total_pnl_pts,
        "max_drawdown_pct": max_dd,
        "sharpe_ratio": sharpe,
    }


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


def format_timestamp(ts) -> str:
    """Convert a timestamp (ms epoch or datetime) to ISO 8601."""
    if isinstance(ts, (int, float, np.integer, np.floating)):
        return datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    if isinstance(ts, datetime):
        return ts.strftime("%Y-%m-%d %H:%M")
    if isinstance(ts, str):
        return ts
    return str(ts)


def make_trade(
    trade_num: int,
    trade_type: str,
    entry_row: pd.Series,
    exit_row: pd.Series,
    cum_pnl_pct: float,
    signal_reason: str,
    indicator_snapshot: dict[str, float | None] | None = None,
) -> TradeRecord:
    """Helper to create a TradeRecord from DataFrame rows."""
    entry_price = float(entry_row["close"])
    exit_price = float(exit_row["close"])

    if trade_type == "Sell":
        pnl_pts = entry_price - exit_price
    else:
        pnl_pts = exit_price - entry_price

    pnl_pct = pnl_pts / entry_price if entry_price != 0 else 0.0
    new_cum = cum_pnl_pct + pnl_pct

    return TradeRecord(
        trade_number=trade_num,
        trade_type=trade_type,
        entry_timestamp=format_timestamp(entry_row["timestamp"]),
        exit_timestamp=format_timestamp(exit_row["timestamp"]),
        entry_price=entry_price,
        exit_price=exit_price,
        pnl=pnl_pts,
        pnl_pct=pnl_pct,
        cumulative_pnl_pct=new_cum,
        signal_reason=signal_reason,
        indicator_snapshot=indicator_snapshot or {},
    )
