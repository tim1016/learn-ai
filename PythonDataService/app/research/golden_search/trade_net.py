"""Each Golden Search trade's own net profit, and the check that a run's trades account for its result (#2821).

Formula: t_i = pnl_i − 2c, where pnl_i is the trade's price change times its
  filled quantity and c is the run's commission per order, paid once on the
  entry order and once on the exit order. The engine applies slippage to fill
  prices, so pnl_i already carries it. A run's trades reconcile when
  |Σ t_i − N| ≤ $0.01, N the run's net profit (final equity less starting
  capital).
Reference: PRD https://github.com/tim1016/learn-ai/issues/2821 "Per-trade net
  P&L"; the fee is ``FillModel.compute_fee`` in
  app/engine/execution/fill_model.py, the flat commission per order a Golden
  Search run (no compatibility profile) pays.
Canonical implementation: this file.
Validated against: tests/research/golden_search/test_trade_net.py.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.research.golden_search.guidance import usd
from app.research.golden_search.selection import Metrics

RECONCILE_ATOL = 0.01
# A cent is not exactly representable, so a difference of exactly one cent may read a hair above it.
_FLOAT_SLACK = 1e-9

NOT_EVALUATED = "Not evaluated: the study's budget ran out before this candidate's development run."


def trade_nets(trades: Sequence[Mapping[str, Any]], commission_per_order: float) -> list[float]:
    """Each trade's net profit: its P&L before fees less its entry and exit commission."""
    return [float(trade["pnl"]) - 2.0 * commission_per_order for trade in trades]


def reconciles(nets: Sequence[float], net_profit: float) -> bool:
    """Whether the trades add up to their run's net profit within a cent."""
    return abs(math.fsum(nets) - net_profit) <= RECONCILE_ATOL + _FLOAT_SLACK


@dataclass(frozen=True)
class ReconciledRun:
    """A development detail run whose trades account for its net profit."""

    net_profit: float
    daily: list[tuple[int, float]]
    capital: float
    trades: list[Mapping[str, Any]]
    # Each trade's net profit, in the run's trade order.
    nets: list[float]


def reconciled_run(metrics: Metrics | None, detail: Mapping[str, Any] | None, *, commission_per_order: float) -> ReconciledRun | str:
    """The run with each trade's net profit, or why its trades cannot be read: the reason the trade charts and the concentration measure give."""
    if metrics is None:
        return NOT_EVALUATED
    if metrics.status != "completed":
        return "The development run failed, so there is nothing to measure."
    if metrics.net_profit is None:
        return "The development run recorded no net profit."
    if detail is None:
        return "The development run kept no trade list."
    trades: list[Mapping[str, Any]] = list(detail["trades"])
    if not trades:
        return "The development run made no trades."
    nets = trade_nets(trades, commission_per_order)
    if not reconciles(nets, metrics.net_profit):
        return (
            f"Its trades add up to {usd(math.fsum(nets))} after commission, but the run's net profit is "
            f"{usd(metrics.net_profit)}, so the trades do not account for the whole result."
        )
    return ReconciledRun(
        net_profit=metrics.net_profit,
        daily=[(int(ms), float(equity)) for ms, equity in detail["daily_equity"]],
        capital=float(detail["initial_cash"]),
        trades=trades,
        nets=nets,
    )
