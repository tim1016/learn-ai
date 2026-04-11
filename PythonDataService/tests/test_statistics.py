"""Snapshot and unit tests for the statistics module.

These tests ensure that statistics computations are deterministic and
catch accidental regressions in the math. The snapshot test uses a
frozen set of synthetic trades and equity points to assert the full
statistics dict matches expected values.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Sequence

import pytest

from app.engine.results.statistics import (
    EquityPoint,
    ValidationError,
    compute_portfolio_statistics,
    compute_trade_statistics,
    summarize,
    validate_equity_curve,
    validate_statistics,
    validate_trade_log,
    _daily_returns,
    _max_drawdown,
    _resample_to_daily,
    _sharpe,
    _sortino,
)


# ---------------------------------------------------------------------------
# Helpers — lightweight trade-like objects for testing
# ---------------------------------------------------------------------------
@dataclass
class FakeTrade:
    pnl_pts: Decimal
    pnl_pct: Decimal
    result: str
    entry_time: datetime | None = None
    exit_time: datetime | None = None
    indicators: dict | None = None


def _make_trades() -> list[FakeTrade]:
    """Frozen set of 10 trades with known outcomes."""
    base = datetime(2024, 1, 2, 10, 0)
    return [
        FakeTrade(Decimal("2.50"), Decimal("0.0125"), "WIN", base, base + timedelta(hours=2)),
        FakeTrade(Decimal("-1.00"), Decimal("-0.005"), "LOSS", base + timedelta(days=1), base + timedelta(days=1, hours=1)),
        FakeTrade(Decimal("3.00"), Decimal("0.015"), "WIN", base + timedelta(days=2), base + timedelta(days=2, hours=3)),
        FakeTrade(Decimal("1.50"), Decimal("0.0075"), "WIN", base + timedelta(days=3), base + timedelta(days=3, hours=1)),
        FakeTrade(Decimal("-2.00"), Decimal("-0.01"), "LOSS", base + timedelta(days=4), base + timedelta(days=4, hours=2)),
        FakeTrade(Decimal("4.00"), Decimal("0.02"), "WIN", base + timedelta(days=7), base + timedelta(days=7, hours=1)),
        FakeTrade(Decimal("-0.50"), Decimal("-0.0025"), "LOSS", base + timedelta(days=8), base + timedelta(days=8, hours=2)),
        FakeTrade(Decimal("1.00"), Decimal("0.005"), "WIN", base + timedelta(days=9), base + timedelta(days=9, hours=1)),
        FakeTrade(Decimal("2.00"), Decimal("0.01"), "WIN", base + timedelta(days=10), base + timedelta(days=10, hours=3)),
        FakeTrade(Decimal("-1.50"), Decimal("-0.0075"), "LOSS", base + timedelta(days=11), base + timedelta(days=11, hours=1)),
    ]


def _make_equity_curve(initial: float = 100_000.0) -> list[EquityPoint]:
    """Synthetic daily equity curve over ~12 trading days."""
    base = datetime(2024, 1, 2, 16, 0)
    values = [
        100_000.0, 100_500.0, 100_200.0, 101_000.0, 101_400.0,
        100_800.0, 101_500.0, 102_000.0, 101_700.0, 102_200.0,
        102_800.0, 102_300.0,
    ]
    return [
        EquityPoint(timestamp=base + timedelta(days=i), equity=v)
        for i, v in enumerate(values)
    ]


# ---------------------------------------------------------------------------
# Unit tests: max drawdown
# ---------------------------------------------------------------------------
class TestMaxDrawdown:
    def test_monotonic_up(self) -> None:
        assert _max_drawdown([100, 110, 120, 130]) == 0.0

    def test_simple_drawdown(self) -> None:
        dd = _max_drawdown([100, 120, 90, 110])
        assert abs(dd - 0.25) < 1e-10  # 30/120 = 0.25

    def test_empty_curve(self) -> None:
        assert _max_drawdown([]) == 0.0

    def test_single_point(self) -> None:
        assert _max_drawdown([100]) == 0.0


# ---------------------------------------------------------------------------
# Unit tests: Sharpe & Sortino
# ---------------------------------------------------------------------------
class TestSharpe:
    def test_constant_returns(self) -> None:
        assert _sharpe([0.01, 0.01, 0.01, 0.01], 252) is None  # std = 0

    def test_insufficient_data(self) -> None:
        assert _sharpe([0.01], 252) is None

    def test_known_value(self) -> None:
        returns = [0.01, -0.005, 0.015, 0.0075, -0.01, 0.02, -0.0025, 0.005, 0.01, -0.0075]
        result = _sharpe(returns, 252)
        assert result is not None
        assert result > 0  # net-positive returns → positive Sharpe


class TestSortino:
    def test_no_downside(self) -> None:
        assert _sortino([0.01, 0.02, 0.03], 252) is None  # no negative returns

    def test_insufficient_data(self) -> None:
        assert _sortino([0.01], 252) is None

    def test_known_positive(self) -> None:
        returns = [0.01, -0.005, 0.015, -0.01, 0.02]
        result = _sortino(returns, 252)
        assert result is not None
        assert result > 0


# ---------------------------------------------------------------------------
# Unit tests: daily resampling
# ---------------------------------------------------------------------------
class TestResampleToDaily:
    def test_multiple_points_per_day(self) -> None:
        base = datetime(2024, 1, 2, 10, 0)
        points = [
            EquityPoint(timestamp=base, equity=100.0),
            EquityPoint(timestamp=base + timedelta(hours=2), equity=101.0),
            EquityPoint(timestamp=base + timedelta(hours=4), equity=102.0),
            EquityPoint(timestamp=base + timedelta(days=1), equity=103.0),
            EquityPoint(timestamp=base + timedelta(days=1, hours=2), equity=104.0),
        ]
        daily = _resample_to_daily(points)
        assert len(daily) == 2
        assert daily[0] == 102.0  # last value of day 1
        assert daily[1] == 104.0  # last value of day 2

    def test_empty(self) -> None:
        assert _resample_to_daily([]) == []


class TestDailyReturns:
    def test_basic(self) -> None:
        rets = _daily_returns([100.0, 110.0, 105.0])
        assert len(rets) == 2
        assert abs(rets[0] - 0.1) < 1e-10
        assert abs(rets[1] - (-5 / 110)) < 1e-10

    def test_single_value(self) -> None:
        assert _daily_returns([100.0]) == []


# ---------------------------------------------------------------------------
# Unit tests: trade statistics
# ---------------------------------------------------------------------------
class TestTradeStatistics:
    def test_known_trades(self) -> None:
        trades = _make_trades()
        ts = compute_trade_statistics(trades)
        assert ts.total_trades == 10
        assert ts.winning_trades == 6
        assert ts.losing_trades == 4
        assert abs(ts.win_rate - 0.6) < 1e-10
        assert ts.profit_factor > 1.0  # net winner
        assert ts.largest_win_pct == 0.02
        assert ts.largest_loss_pct == -0.01

    def test_empty(self) -> None:
        ts = compute_trade_statistics([])
        assert ts.total_trades == 0
        assert ts.win_rate == 0.0
        assert ts.profit_factor == 0.0

    def test_all_wins(self) -> None:
        trades = [FakeTrade(Decimal("1"), Decimal("0.01"), "WIN") for _ in range(5)]
        ts = compute_trade_statistics(trades)
        assert ts.win_rate == 1.0
        assert ts.profit_factor == float("inf")


# ---------------------------------------------------------------------------
# Unit tests: portfolio statistics with equity curve
# ---------------------------------------------------------------------------
class TestPortfolioStatisticsWithCurve:
    def test_uses_real_curve(self) -> None:
        trades = _make_trades()
        curve = _make_equity_curve()
        ps = compute_portfolio_statistics(
            initial_cash=100_000.0,
            final_equity=102_300.0,
            trades=trades,
            trading_days=12,
            equity_curve=curve,
        )
        assert ps.max_drawdown_pct > 0
        assert ps.sharpe_ratio is not None
        assert ps.net_profit == 2_300.0

    def test_fallback_without_curve(self) -> None:
        trades = _make_trades()
        ps = compute_portfolio_statistics(
            initial_cash=100_000.0,
            final_equity=103_000.0,
            trades=trades,
            trading_days=12,
        )
        assert ps.max_drawdown_pct >= 0
        assert ps.sharpe_ratio is not None


# ---------------------------------------------------------------------------
# Snapshot test: full summarize output
# ---------------------------------------------------------------------------
EXPECTED_SNAPSHOT = {
    "total_trades": 10,
    "winning_trades": 6,
    "losing_trades": 4,
    "win_rate": 0.6,
    "profit_factor": 2.8,
    "payoff_ratio": 1.8666666666666667,
}


class TestSummarizeSnapshot:
    def test_trade_level_metrics_frozen(self) -> None:
        """Assert trade-level metrics match frozen expected values.

        This catches any accidental change to the trade statistics math.
        """
        trades = _make_trades()
        curve = _make_equity_curve()
        stats = summarize(
            initial_cash=100_000.0,
            final_equity=102_300.0,
            trades=trades,
            trading_days=12,
            equity_curve=curve,
        )
        assert stats["total_trades"] == EXPECTED_SNAPSHOT["total_trades"]
        assert stats["winning_trades"] == EXPECTED_SNAPSHOT["winning_trades"]
        assert stats["losing_trades"] == EXPECTED_SNAPSHOT["losing_trades"]
        assert abs(stats["win_rate"] - EXPECTED_SNAPSHOT["win_rate"]) < 1e-10
        assert abs(stats["profit_factor"] - EXPECTED_SNAPSHOT["profit_factor"]) < 1e-10
        assert abs(stats["payoff_ratio"] - EXPECTED_SNAPSHOT["payoff_ratio"]) < 1e-10

    def test_portfolio_metrics_present(self) -> None:
        """Ensure all portfolio-level keys are present and non-None when curve provided."""
        trades = _make_trades()
        curve = _make_equity_curve()
        stats = summarize(
            initial_cash=100_000.0,
            final_equity=102_300.0,
            trades=trades,
            trading_days=12,
            equity_curve=curve,
        )
        assert stats["sharpe_ratio"] is not None
        assert stats["sortino_ratio"] is not None
        assert stats["max_drawdown_pct"] > 0
        assert stats["cagr"] is not None
        assert "net_profit" in stats
        assert "net_profit_pct" in stats

    def test_cagr_included(self) -> None:
        trades = _make_trades()
        curve = _make_equity_curve()
        stats = summarize(
            initial_cash=100_000.0,
            final_equity=102_300.0,
            trades=trades,
            trading_days=60,
            equity_curve=curve,
        )
        assert stats["cagr"] is not None
        assert stats["cagr"] > 0  # positive return → positive CAGR


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------
class TestValidateTradeLog:
    def test_valid_trades(self) -> None:
        errors = validate_trade_log(_make_trades())
        assert errors == []

    def test_bad_times(self) -> None:
        t = FakeTrade(
            Decimal("1"), Decimal("0.01"), "WIN",
            entry_time=datetime(2024, 1, 2, 12, 0),
            exit_time=datetime(2024, 1, 2, 10, 0),  # before entry
        )
        errors = validate_trade_log([t])
        assert len(errors) == 1
        assert errors[0].code == "invalid_trade_times"

    def test_nan_pnl(self) -> None:
        t = FakeTrade(Decimal("nan"), Decimal("nan"), "WIN")
        errors = validate_trade_log([t])
        assert any(e.code == "nan_pnl_pct" for e in errors)

    def test_nan_indicator(self) -> None:
        t = FakeTrade(
            Decimal("1"), Decimal("0.01"), "WIN",
            indicators={"ema": float("nan")},
        )
        errors = validate_trade_log([t])
        assert any(e.code == "nan_indicator" for e in errors)


class TestValidateEquityCurve:
    def test_valid_curve(self) -> None:
        errors = validate_equity_curve([100.0, 101.0, 99.0, 102.0])
        assert errors == []

    def test_empty_curve(self) -> None:
        errors = validate_equity_curve([])
        assert len(errors) == 1
        assert errors[0].code == "empty_curve"

    def test_nan_value(self) -> None:
        errors = validate_equity_curve([100.0, float("nan"), 102.0])
        assert any(e.code == "nan_equity" for e in errors)

    def test_negative_value(self) -> None:
        errors = validate_equity_curve([100.0, -5.0, 102.0])
        assert any(e.code == "negative_equity" for e in errors)


class TestValidateStatistics:
    def test_valid_stats(self) -> None:
        stats = {"win_rate": 0.6, "max_drawdown_pct": 0.15, "profit_factor": 2.0, "sharpe_ratio": 1.5}
        errors = validate_statistics(stats)
        assert errors == []

    def test_invalid_win_rate(self) -> None:
        errors = validate_statistics({"win_rate": 1.5})
        assert any(e.code == "invalid_win_rate_bounds" for e in errors)

    def test_negative_profit_factor(self) -> None:
        errors = validate_statistics({"profit_factor": -1.0})
        assert any(e.code == "negative_profit_factor" for e in errors)

    def test_nan_sharpe(self) -> None:
        errors = validate_statistics({"sharpe_ratio": float("nan")})
        assert any(e.code == "nan_sharpe" for e in errors)
