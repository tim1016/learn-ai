"""Golden evidence for every formula displayed by Strategy Lab metric help."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, localcontext
from itertools import pairwise
from pathlib import Path

import pytest

from app.engine.results.statistics import (
    EquityPoint,
    compute_portfolio_statistics,
    compute_trade_statistics,
)

FIXTURE_PATH = Path(__file__).resolve().parents[3] / "contracts" / "fixtures" / "strategy-metric-help-golden-v2.json"


@dataclass(frozen=True)
class _FixtureTrade:
    pnl_pts: Decimal
    pnl_pct: Decimal
    result: str


def test_v2_return_metrics_have_an_independent_decimal_oracle() -> None:
    """Check the versioned fixture without calling the production estimators.

    Formula: Sharpe = mean / sqrt(sample variance) * sqrt(252);
      Sortino = mean / sqrt(mean squared downside) * sqrt(252).
    Reference: docs/references/strategy-metric-help.md, v2 derivation.
    Canonical implementation: app/engine/results/statistics.py (float64);
      this is the independent 50-digit Decimal fixture oracle.
    Validated against: exact rational intermediate values pinned below.
    """
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    historical = json.loads(FIXTURE_PATH.with_name("strategy-metric-help-golden-v1.json").read_text(encoding="utf-8"))
    assert fixture["inputs"] == historical["inputs"]
    assert fixture["absolute_tolerance"] == historical["absolute_tolerance"] == 1e-12
    assert fixture["relative_tolerance"] == historical["relative_tolerance"] == 0
    for metric in fixture["expected"].keys() - {"sharpe", "sortino"}:
        assert fixture["expected"][metric] == historical["expected"][metric]

    with localcontext() as context:
        context.prec = 50
        equity = [Decimal(fixture["inputs"]["initial_cash"])] + [
            Decimal(point["equity"]) for point in fixture["inputs"]["equity_points"]
        ]
        returns = [current / previous - 1 for previous, current in pairwise(equity)]
        assert returns == [Decimal(value) for value in ("0", "0.1", "-0.1", "0.2", "-0.05")]
        mean = sum(returns) / len(returns)
        variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
        downside = sum(min(value, Decimal(0)) ** 2 for value in returns) / len(returns)
        assert mean == Decimal("0.03")
        assert variance == Decimal("0.0145")
        assert downside == Decimal("0.0025")
        assert fixture["expected"]["sharpe"] == pytest.approx(
            float(mean / variance.sqrt() * Decimal(252).sqrt()), abs=1e-12, rel=0,
        )
        assert fixture["expected"]["sortino"] == pytest.approx(
            float(mean / downside.sqrt() * Decimal(252).sqrt()), abs=1e-12, rel=0,
        )


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
