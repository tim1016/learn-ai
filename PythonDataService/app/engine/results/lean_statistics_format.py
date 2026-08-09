"""Exact invariant formatting for LEAN's native statistics dashboard."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any


def _decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def format_lean_statistics_summary(
    reproduced: Mapping[str, Mapping[str, Any]],
    *,
    total_orders: int,
    total_fees: Decimal,
) -> dict[str, str]:
    """Reproduce StatisticsBuilder.GetSummary's invariant display strings."""
    portfolio = reproduced["portfolioStatistics"]

    def rounded(value: Any, places: int) -> str:
        quantum = Decimal(1).scaleb(-places)
        result = _decimal(value).quantize(quantum, rounding=ROUND_HALF_EVEN)
        if result == 0:
            result = abs(result)
        text = format(result, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text

    def rounded_fixed(value: Any, places: int) -> str:
        quantum = Decimal(1).scaleb(-places)
        result = _decimal(value).quantize(quantum, rounding=ROUND_HALF_EVEN)
        if result == 0:
            result = abs(result)
        return f"{result:.{places}f}"

    def percentage(key: str, places: int) -> str:
        return f"{rounded_fixed(_decimal(portfolio[key]) * 100, places)}%"

    drawdown_percent = (_decimal(portfolio["drawdown"]) * 100).quantize(
        Decimal("0.001"),
        rounding=ROUND_HALF_EVEN,
    )

    return {
        "Total Orders": str(total_orders),
        "Average Win": percentage("averageWinRate", 2),
        "Average Loss": percentage("averageLossRate", 2),
        "Compounding Annual Return": percentage("compoundingAnnualReturn", 3),
        # CalculateDrawdownMetrics returns a scale-3 decimal; Decimal's
        # invariant ToString preserves that scale (for example ``2.600%``).
        "Drawdown": f"{drawdown_percent:.3f}%",
        "Expectancy": rounded_fixed(portfolio["expectancy"], 3),
        "Start Equity": rounded(portfolio["startEquity"], 2),
        "End Equity": rounded(portfolio["endEquity"], 2),
        "Net Profit": percentage("totalNetProfit", 3),
        "Sharpe Ratio": rounded(portfolio["sharpeRatio"], 3),
        "Sortino Ratio": rounded(portfolio["sortinoRatio"], 3),
        "Probabilistic Sharpe Ratio": percentage("probabilisticSharpeRatio", 3),
        "Loss Rate": percentage("lossRate", 0),
        "Win Rate": percentage("winRate", 0),
        "Profit-Loss Ratio": rounded(portfolio["profitLossRatio"], 2),
        "Alpha": rounded(portfolio["alpha"], 3),
        "Beta": rounded(portfolio["beta"], 3),
        "Annual Standard Deviation": rounded(portfolio["annualStandardDeviation"], 3),
        "Annual Variance": rounded(portfolio["annualVariance"], 3),
        "Information Ratio": rounded(portfolio["informationRatio"], 3),
        "Tracking Error": rounded(portfolio["trackingError"], 3),
        "Treynor Ratio": rounded(portfolio["treynorRatio"], 3),
        "Total Fees": f"${rounded_fixed(total_fees, 2)}",
        "Portfolio Turnover": percentage("portfolioTurnover", 2),
        "Drawdown Recovery": str(portfolio["drawdownRecovery"]),
    }
