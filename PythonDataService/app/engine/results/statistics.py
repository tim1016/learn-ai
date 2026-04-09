"""Extended backtest statistics.

Computes per-trade and per-period metrics from a trade log. Kept
independent from the core engine loop so the same module can be reused
for:
  * the ``/api/engine/backtest`` response payload
  * offline analysis notebooks
  * future portfolio-level aggregation

All ratio calculations are annualized using 252 trading days per year.
The functions accept ``Decimal`` or ``float`` inputs interchangeably and
return ``float`` so the results are JSON-friendly.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Protocol, Sequence

TRADING_DAYS_PER_YEAR = 252


class _TradeLike(Protocol):
    """Structural type matching ``_LoggedTrade`` and similar records."""

    pnl_pts: Decimal
    pnl_pct: Decimal
    result: str


@dataclass(frozen=True)
class TradeStatistics:
    """Per-trade summary metrics."""

    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_win_pct: float
    avg_loss_pct: float
    avg_trade_pct: float
    largest_win_pct: float
    largest_loss_pct: float
    profit_factor: float
    expectancy_pct: float
    payoff_ratio: float  # avg_win / |avg_loss|


@dataclass(frozen=True)
class PortfolioStatistics:
    """Per-period / equity-curve metrics.

    MAE/MFE are placeholders: computing them correctly requires
    intra-trade bar data, which the engine does not currently retain.
    They're exposed as ``None`` here so downstream code can depend on a
    stable shape, and will be populated once the engine stores per-bar
    equity during open positions.
    """

    net_profit: float
    net_profit_pct: float
    max_drawdown_pct: float
    sharpe_ratio: float | None
    sortino_ratio: float | None
    calmar_ratio: float | None
    mae_pct: float | None  # Maximum Adverse Excursion — needs intra-trade data
    mfe_pct: float | None  # Maximum Favorable Excursion — needs intra-trade data


# ---------------------------------------------------------------------------
# Trade-level statistics
# ---------------------------------------------------------------------------
def compute_trade_statistics(trades: Sequence[_TradeLike]) -> TradeStatistics:
    total = len(trades)
    if total == 0:
        return TradeStatistics(
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
            win_rate=0.0,
            avg_win_pct=0.0,
            avg_loss_pct=0.0,
            avg_trade_pct=0.0,
            largest_win_pct=0.0,
            largest_loss_pct=0.0,
            profit_factor=0.0,
            expectancy_pct=0.0,
            payoff_ratio=0.0,
        )

    pcts = [float(t.pnl_pct) for t in trades]
    wins = [p for p in pcts if p > 0]
    losses = [p for p in pcts if p < 0]

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    avg_win = (gross_win / len(wins)) if wins else 0.0
    avg_loss = -(gross_loss / len(losses)) if losses else 0.0  # signed (negative)
    avg_trade = sum(pcts) / total

    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0
    expectancy = avg_trade
    payoff_ratio = (avg_win / abs(avg_loss)) if avg_loss != 0 else (float("inf") if avg_win > 0 else 0.0)

    return TradeStatistics(
        total_trades=total,
        winning_trades=len(wins),
        losing_trades=len(losses),
        win_rate=len(wins) / total,
        avg_win_pct=avg_win,
        avg_loss_pct=avg_loss,
        avg_trade_pct=avg_trade,
        largest_win_pct=max(pcts) if pcts else 0.0,
        largest_loss_pct=min(pcts) if pcts else 0.0,
        profit_factor=profit_factor,
        expectancy_pct=expectancy,
        payoff_ratio=payoff_ratio,
    )


# ---------------------------------------------------------------------------
# Equity-curve statistics
# ---------------------------------------------------------------------------
def _equity_curve_from_trades(
    initial_equity: float, trades: Sequence[_TradeLike]
) -> list[float]:
    """Rebuild a per-trade equity curve assuming all-in on each trade.

    This matches the SPY EMA crossover's ``SetHoldings(1.0)`` behavior.
    For leveraged or partial-allocation strategies, this will be
    inaccurate and the engine should provide a real curve instead.
    """
    equity = initial_equity
    curve = [equity]
    for t in trades:
        equity *= float(1 + t.pnl_pct)
        curve.append(equity)
    return curve


def _max_drawdown(curve: Sequence[float]) -> float:
    """Return max drawdown as a positive fraction (e.g. 0.18 = 18% DD)."""
    if not curve:
        return 0.0
    peak = curve[0]
    max_dd = 0.0
    for value in curve:
        if value > peak:
            peak = value
        if peak > 0:
            dd = (peak - value) / peak
            if dd > max_dd:
                max_dd = dd
    return max_dd


def _returns_from_curve(curve: Sequence[float]) -> list[float]:
    if len(curve) < 2:
        return []
    return [(curve[i] / curve[i - 1]) - 1.0 for i in range(1, len(curve))]


def _sharpe(returns: Sequence[float], periods_per_year: int) -> float | None:
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(var)
    if std == 0:
        return None
    return (mean / std) * math.sqrt(periods_per_year)


def _sortino(returns: Sequence[float], periods_per_year: int) -> float | None:
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    downside = [r for r in returns if r < 0]
    if not downside:
        return None
    downside_var = sum(r * r for r in downside) / len(returns)
    downside_std = math.sqrt(downside_var)
    if downside_std == 0:
        return None
    return (mean / downside_std) * math.sqrt(periods_per_year)


def compute_portfolio_statistics(
    initial_cash: float,
    final_equity: float,
    trades: Sequence[_TradeLike],
    trading_days: int | None = None,
) -> PortfolioStatistics:
    """Compute equity-curve metrics from an all-in per-trade equity curve.

    ``trading_days`` should be the number of distinct trading days the
    backtest covered. If omitted, Sharpe/Sortino/Calmar fall back to
    annualizing against the trade count — which is less meaningful but
    avoids returning None unnecessarily.
    """
    net_profit = final_equity - initial_cash
    net_profit_pct = (net_profit / initial_cash) if initial_cash > 0 else 0.0

    curve = _equity_curve_from_trades(initial_cash, trades)
    max_dd = _max_drawdown(curve)
    returns = _returns_from_curve(curve)

    # Annualization: if we know the number of trading days, treat each
    # trade as a period of (days / trades) days and scale from there.
    if trading_days and len(trades) > 0:
        periods_per_year = max(1, int(round(TRADING_DAYS_PER_YEAR * len(trades) / trading_days)))
    else:
        periods_per_year = TRADING_DAYS_PER_YEAR  # pessimistic fallback

    sharpe = _sharpe(returns, periods_per_year)
    sortino = _sortino(returns, periods_per_year)

    # Calmar: annualized return / max drawdown.
    calmar: float | None = None
    if max_dd > 0 and trading_days and trading_days > 0:
        years = trading_days / TRADING_DAYS_PER_YEAR
        if years > 0 and initial_cash > 0 and final_equity > 0:
            ann_return = (final_equity / initial_cash) ** (1 / years) - 1
            calmar = ann_return / max_dd

    return PortfolioStatistics(
        net_profit=net_profit,
        net_profit_pct=net_profit_pct,
        max_drawdown_pct=max_dd,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        calmar_ratio=calmar,
        mae_pct=None,
        mfe_pct=None,
    )


def summarize(
    initial_cash: float,
    final_equity: float,
    trades: Sequence[_TradeLike],
    trading_days: int | None = None,
) -> dict[str, float | int | None]:
    """Convenience wrapper returning a flat dict of all metrics.

    Useful for JSON responses where a nested shape is awkward.
    """
    ts = compute_trade_statistics(trades)
    ps = compute_portfolio_statistics(
        initial_cash=initial_cash,
        final_equity=final_equity,
        trades=trades,
        trading_days=trading_days,
    )
    return {
        # Trade-level
        "total_trades": ts.total_trades,
        "winning_trades": ts.winning_trades,
        "losing_trades": ts.losing_trades,
        "win_rate": ts.win_rate,
        "avg_win_pct": ts.avg_win_pct,
        "avg_loss_pct": ts.avg_loss_pct,
        "avg_trade_pct": ts.avg_trade_pct,
        "largest_win_pct": ts.largest_win_pct,
        "largest_loss_pct": ts.largest_loss_pct,
        "profit_factor": ts.profit_factor,
        "expectancy_pct": ts.expectancy_pct,
        "payoff_ratio": ts.payoff_ratio,
        # Portfolio-level
        "net_profit": ps.net_profit,
        "net_profit_pct": ps.net_profit_pct,
        "max_drawdown_pct": ps.max_drawdown_pct,
        "sharpe_ratio": ps.sharpe_ratio,
        "sortino_ratio": ps.sortino_ratio,
        "calmar_ratio": ps.calmar_ratio,
        "mae_pct": ps.mae_pct,
        "mfe_pct": ps.mfe_pct,
    }
