"""Golden evidence for every formula displayed by Strategy Lab metric help."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest

from app.engine.results.statistics import (
    EquityPoint,
    compute_portfolio_statistics,
    compute_trade_statistics,
)

FIXTURE_PATH = Path(__file__).resolve().parents[3] / "contracts" / "fixtures" / "strategy-metric-help-golden-v1.json"


@dataclass(frozen=True)
class _FixtureTrade:
    pnl_pts: Decimal
    pnl_pct: Decimal
    result: str


def test_strategy_metric_help_matches_canonical_golden_values() -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    inputs = fixture["inputs"]
    trades = [
        _FixtureTrade(
            pnl_pts=Decimal(str(value * 100)),
            pnl_pct=Decimal(str(value)),
            result="WIN" if value > 0 else "LOSS",
        )
        for value in inputs["trade_returns"]
    ]
    equity_curve = [
        EquityPoint(
            timestamp_ms=point["t"],
            equity=point["equity"],
        )
        for point in inputs["equity_points"]
    ]

    trade_stats = compute_trade_statistics(trades)
    portfolio_stats = compute_portfolio_statistics(
        initial_cash=inputs["initial_cash"],
        final_equity=inputs["final_equity"],
        trades=trades,
        trading_days=inputs["trading_days"],
        equity_curve=equity_curve,
    )
    actual = {
        "net-profit": portfolio_stats.net_profit,
        "profit-factor": trade_stats.profit_factor,
        "expectancy": trade_stats.expectancy_pct,
        "sharpe": portfolio_stats.sharpe_ratio,
        "sortino": portfolio_stats.sortino_ratio,
        "max-drawdown": portfolio_stats.max_drawdown_pct,
        "win-rate": trade_stats.win_rate,
        "trades": trade_stats.total_trades,
    }

    assert actual.keys() == fixture["expected"].keys()
    for metric, expected in fixture["expected"].items():
        assert actual[metric] == pytest.approx(
            expected,
            abs=fixture["absolute_tolerance"],
            rel=fixture["relative_tolerance"],
        )
