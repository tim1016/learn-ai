"""Extended backtest statistics.

Formula: Sharpe = mean(daily_returns)/stddev(daily_returns) · √252; MaxDrawdown = max_t(running_peak_t - equity_t); per-trade win rate, profit factor, expected value, average winning/losing trade duration. All annualized with TRADING_DAYS_PER_YEAR=252.
Reference: Sharpe (1994) "The Sharpe Ratio", Journal of Portfolio Management 21(1) §IV; Bacon, *Practical Portfolio Performance Measurement* (2e), §8.2 for max drawdown; standard portfolio statistics.
Canonical implementation: this file. The .NET duplicates `Backend/Services/Implementation/BacktestService.cs::CalculateSharpeRatio` and `CalculateMaxDrawdown` are pending-migration (Phase 3.2). The .NET `SnapshotService.cs::ComputeMetrics` is legacy-ok-pending-parity (live-portfolio path; finding F-0011).
Validated against: backtest engine test suite (`PythonDataService/tests/test_strategy_engine.py`) covers the integrated path; per-statistic golden fixture is owed (registry: pending-migration row).

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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True)
class EquityPoint:
    timestamp: datetime
    equity: float


@dataclass(frozen=True)
class ValidationError:
    code: str
    message: str


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
    cagr: float | None
    mae_pct: float | None  # Maximum Adverse Excursion — needs intra-trade data
    mfe_pct: float | None  # Maximum Favorable Excursion — needs intra-trade data


# ---------------------------------------------------------------------------
# Trade-level statistics
# ---------------------------------------------------------------------------
def compute_trade_statistics(trades: Sequence[_TradeLike]) -> TradeStatistics:
    errors = validate_trade_log(trades)
    if errors:
        raise ValueError("; ".join(f"{e.code}: {e.message}" for e in errors))

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
def _equity_curve_from_trades(initial_equity: float, trades: Sequence[_TradeLike]) -> list[float]:
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


def _resample_to_daily(points: Sequence[EquityPoint]) -> list[float]:
    if not points:
        return []
    daily_values: dict[datetime, float] = {}
    for point in points:
        date_key = point.timestamp.date()
        daily_values[date_key] = point.equity
    return list(daily_values.values())


def _daily_returns(daily_equity: Sequence[float]) -> list[float]:
    if len(daily_equity) < 2:
        return []
    return [(daily_equity[i] / daily_equity[i - 1]) - 1.0 for i in range(1, len(daily_equity))]


def validate_trade_log(trades: Sequence[_TradeLike]) -> list[ValidationError]:
    errors: list[ValidationError] = []
    for i, trade in enumerate(trades):
        if (
            hasattr(trade, "entry_time")
            and hasattr(trade, "exit_time")
            and trade.entry_time is not None
            and trade.exit_time is not None
            and trade.entry_time >= trade.exit_time
        ):
            errors.append(
                ValidationError(
                    code="invalid_trade_times",
                    message=f"Trade {i}: entry_time >= exit_time",
                )
            )
        if hasattr(trade, "pnl_pct") and math.isnan(float(trade.pnl_pct)):
            errors.append(
                ValidationError(
                    code="nan_pnl_pct",
                    message=f"Trade {i}: pnl_pct is NaN",
                )
            )
        if hasattr(trade, "indicators"):
            indicators = getattr(trade, "indicators", None) or {}
            for ind_name, ind_val in indicators.items():
                try:
                    if math.isnan(float(ind_val)):
                        errors.append(
                            ValidationError(
                                code="nan_indicator",
                                message=f"Trade {i}: indicator {ind_name} is NaN",
                            )
                        )
                except (TypeError, ValueError):
                    pass
    if trades:
        winning = sum(1 for t in trades if float(t.pnl_pct) > 0)
        win_rate = winning / len(trades)
        if not (0 <= win_rate <= 1):
            errors.append(
                ValidationError(
                    code="invalid_win_rate",
                    message=f"Computed win_rate {win_rate} not in [0, 1]",
                )
            )
    return errors


def validate_equity_curve(curve: Sequence[float]) -> list[ValidationError]:
    errors: list[ValidationError] = []
    if len(curve) < 1:
        errors.append(
            ValidationError(
                code="empty_curve",
                message="Equity curve has no points",
            )
        )
        return errors
    for i, value in enumerate(curve):
        if math.isnan(value):
            errors.append(
                ValidationError(
                    code="nan_equity",
                    message=f"Equity point {i}: value is NaN",
                )
            )
        if value < 0:
            errors.append(
                ValidationError(
                    code="negative_equity",
                    message=f"Equity point {i}: value is negative ({value})",
                )
            )
    return errors


def validate_statistics(stats: dict[str, float | int | None]) -> list[ValidationError]:
    errors: list[ValidationError] = []
    if "win_rate" in stats and stats["win_rate"] is not None:
        wr = float(stats["win_rate"])
        if not (0 <= wr <= 1):
            errors.append(
                ValidationError(
                    code="invalid_win_rate_bounds",
                    message=f"win_rate {wr} not in [0, 1]",
                )
            )
    if "max_drawdown_pct" in stats and stats["max_drawdown_pct"] is not None:
        mdd = float(stats["max_drawdown_pct"])
        if not (0 <= mdd <= 1):
            errors.append(
                ValidationError(
                    code="invalid_max_drawdown_bounds",
                    message=f"max_drawdown_pct {mdd} not in [0, 1]",
                )
            )
    if "profit_factor" in stats and stats["profit_factor"] is not None:
        pf = float(stats["profit_factor"])
        if pf < 0:
            errors.append(
                ValidationError(
                    code="negative_profit_factor",
                    message=f"profit_factor {pf} is negative",
                )
            )
    if "sharpe_ratio" in stats and stats["sharpe_ratio"] is not None:
        sr = float(stats["sharpe_ratio"])
        if math.isnan(sr):
            errors.append(
                ValidationError(
                    code="nan_sharpe",
                    message="sharpe_ratio is NaN",
                )
            )
    if "sortino_ratio" in stats and stats["sortino_ratio"] is not None:
        sort = float(stats["sortino_ratio"])
        if math.isnan(sort):
            errors.append(
                ValidationError(
                    code="nan_sortino",
                    message="sortino_ratio is NaN",
                )
            )
    return errors


def compute_portfolio_statistics(
    initial_cash: float,
    final_equity: float,
    trades: Sequence[_TradeLike],
    trading_days: int | None = None,
    equity_curve: Sequence[EquityPoint] | None = None,
) -> PortfolioStatistics:
    """Compute equity-curve metrics from equity curve or per-trade returns.

    When ``equity_curve`` is provided, uses the true equity curve for metrics.
    Otherwise falls back to rebuilding an all-in per-trade equity curve.

    ``trading_days`` should be the number of distinct trading days the
    backtest covered. If omitted, Sharpe/Sortino/Calmar fall back to
    annualizing against the trade count — which is less meaningful but
    avoids returning None unnecessarily.
    """
    net_profit = final_equity - initial_cash
    net_profit_pct = (net_profit / initial_cash) if initial_cash > 0 else 0.0

    cagr: float | None = None

    if equity_curve:
        curve = [float(p.equity) for p in equity_curve]
        max_dd = _max_drawdown(curve)

        daily_equity = _resample_to_daily(equity_curve)
        daily_rets = _daily_returns(daily_equity)
        sharpe = _sharpe(daily_rets, TRADING_DAYS_PER_YEAR)
        sortino = _sortino(daily_rets, TRADING_DAYS_PER_YEAR)

        calmar: float | None = None
        if max_dd > 0 and trading_days and trading_days > 0:
            years = trading_days / TRADING_DAYS_PER_YEAR
            if years > 0 and initial_cash > 0 and final_equity > 0:
                ann_return = (final_equity / initial_cash) ** (1 / years) - 1
                calmar = ann_return / max_dd
                cagr = ann_return
    else:
        curve = _equity_curve_from_trades(initial_cash, trades)
        max_dd = _max_drawdown(curve)
        returns = _returns_from_curve(curve)

        if trading_days and len(trades) > 0:
            periods_per_year = max(1, round(TRADING_DAYS_PER_YEAR * len(trades) / trading_days))
        else:
            periods_per_year = TRADING_DAYS_PER_YEAR

        sharpe = _sharpe(returns, periods_per_year)
        sortino = _sortino(returns, periods_per_year)

        calmar: float | None = None
        if max_dd > 0 and trading_days and trading_days > 0:
            years = trading_days / TRADING_DAYS_PER_YEAR
            if years > 0 and initial_cash > 0 and final_equity > 0:
                ann_return = (final_equity / initial_cash) ** (1 / years) - 1
                calmar = ann_return / max_dd
                cagr = ann_return

    return PortfolioStatistics(
        net_profit=net_profit,
        net_profit_pct=net_profit_pct,
        max_drawdown_pct=max_dd,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        calmar_ratio=calmar,
        cagr=cagr,
        mae_pct=None,
        mfe_pct=None,
    )


def _finite_or_none(value: float | int | None) -> float | int | None:
    """Coerce non-finite floats (inf, -inf, NaN) to None for JSON compliance.

    Upstream callers (profit_factor on zero-loss runs, payoff_ratio with
    zero-loss, Sharpe with zero std) legitimately emit inf; Pydantic /
    FastAPI then raise ValueError at serialization time. Surface the
    degeneracy as None rather than a 500.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        if not math.isfinite(float(value)):
            return None
    except (TypeError, ValueError):
        return None
    return value


def summarize(
    initial_cash: float,
    final_equity: float,
    trades: Sequence[_TradeLike],
    trading_days: int | None = None,
    equity_curve: Sequence[EquityPoint] | None = None,
) -> dict[str, float | int | None]:
    """Convenience wrapper returning a flat dict of all metrics.

    Useful for JSON responses where a nested shape is awkward.
    All float fields are sanitized to None if non-finite (inf/-inf/NaN)
    so the result is JSON-serializable by FastAPI/Pydantic unchanged.
    """
    ts = compute_trade_statistics(trades)
    ps = compute_portfolio_statistics(
        initial_cash=initial_cash,
        final_equity=final_equity,
        trades=trades,
        trading_days=trading_days,
        equity_curve=equity_curve,
    )
    result: dict[str, float | int | None] = {
        # Trade-level
        "total_trades": ts.total_trades,
        "winning_trades": ts.winning_trades,
        "losing_trades": ts.losing_trades,
        "win_rate": _finite_or_none(ts.win_rate),
        "avg_win_pct": _finite_or_none(ts.avg_win_pct),
        "avg_loss_pct": _finite_or_none(ts.avg_loss_pct),
        "avg_trade_pct": _finite_or_none(ts.avg_trade_pct),
        "largest_win_pct": _finite_or_none(ts.largest_win_pct),
        "largest_loss_pct": _finite_or_none(ts.largest_loss_pct),
        "profit_factor": _finite_or_none(ts.profit_factor),
        "expectancy_pct": _finite_or_none(ts.expectancy_pct),
        "payoff_ratio": _finite_or_none(ts.payoff_ratio),
        # Portfolio-level
        "net_profit": _finite_or_none(ps.net_profit),
        "net_profit_pct": _finite_or_none(ps.net_profit_pct),
        "max_drawdown_pct": _finite_or_none(ps.max_drawdown_pct),
        "sharpe_ratio": _finite_or_none(ps.sharpe_ratio),
        "sortino_ratio": _finite_or_none(ps.sortino_ratio),
        "calmar_ratio": _finite_or_none(ps.calmar_ratio),
        "cagr": _finite_or_none(ps.cagr),
        "mae_pct": _finite_or_none(ps.mae_pct),
        "mfe_pct": _finite_or_none(ps.mfe_pct),
    }
    import logging

    logger = logging.getLogger(__name__)
    stat_errors = validate_statistics(result)
    for error in stat_errors:
        logger.warning("[STATS] %s: %s", error.code, error.message)
    return result
