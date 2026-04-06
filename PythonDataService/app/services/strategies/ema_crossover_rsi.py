"""EMA Crossover + RSI strategy — wraps the existing rule_based_backtest engine.

Adapts the existing run_rule_based_backtest() to the unified strategy interface.
"""
from __future__ import annotations

import pandas as pd

from app.services.rule_based_backtest import run_rule_based_backtest
from app.services.strategies.common import StrategyResult, TradeRecord


def run(df: pd.DataFrame, params: dict) -> StrategyResult:
    result = StrategyResult(success=False, strategy_name="ema_crossover_rsi")
    result.bars_processed = len(df)

    # Convert DataFrame rows to list[dict] format expected by rule_based_backtest
    bars = df[["timestamp", "open", "high", "low", "close", "volume"]].to_dict("records")

    rb_result = run_rule_based_backtest(
        ticker=params.get("_ticker", ""),
        bars=bars,
        params=params,
    )

    if not rb_result.success:
        result.error = rb_result.error
        return result

    # Map RuleBasedTrade -> TradeRecord
    trades: list[TradeRecord] = []
    for t in rb_result.trades:
        trades.append(TradeRecord(
            trade_number=t.trade_number,
            trade_type=t.trade_type,
            entry_timestamp=t.entry_timestamp,
            exit_timestamp=t.exit_timestamp,
            entry_price=t.entry_price,
            exit_price=t.exit_price,
            pnl=t.pnl,
            pnl_pct=t.pnl_pct,
            cumulative_pnl_pct=t.cumulative_pnl_pct,
            signal_reason=t.signal_reason,
            indicator_snapshot={
                "ema_fast": t.ema_fast,
                "ema_slow": t.ema_slow,
                "ema_gap": t.ema_gap,
                "rsi": t.rsi,
                "adx": t.adx,
            },
        ))

    result.trades = trades
    result.success = True
    result.total_trades = rb_result.total_trades
    result.winning_trades = rb_result.winning_trades
    result.losing_trades = rb_result.losing_trades
    result.win_rate = rb_result.win_rate
    result.avg_win_pct = rb_result.avg_win_pct
    result.avg_loss_pct = rb_result.avg_loss_pct
    result.win_loss_ratio = rb_result.win_loss_ratio
    result.profit_factor = rb_result.profit_factor
    result.expectancy_per_trade = rb_result.expectancy_per_trade
    result.total_pnl_pct = rb_result.total_pnl_pct
    result.total_pnl_pts = rb_result.total_pnl_pts
    result.max_drawdown_pct = rb_result.max_drawdown_pct
    result.sharpe_ratio = rb_result.sharpe_ratio
    result.bars_processed = rb_result.bars_processed

    return result
