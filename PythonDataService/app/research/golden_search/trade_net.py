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
from typing import Any

RECONCILE_ATOL = 0.01
# A cent is not exactly representable, so a difference of exactly one cent may read a hair above it.
_FLOAT_SLACK = 1e-9


def trade_nets(trades: Sequence[Mapping[str, Any]], commission_per_order: float) -> list[float]:
    """Each trade's net profit: its P&L before fees less its entry and exit commission."""
    return [float(trade["pnl"]) - 2.0 * commission_per_order for trade in trades]


def usd(value: float) -> str:
    """Dollars and cents with the sign before the dollar sign (``-$12.50``), for a server-written sentence."""
    return f"-${-value:,.2f}" if value < 0 else f"${value:,.2f}"


def unreconciled(nets: Sequence[float], net_profit: float) -> str | None:
    """Why the trades cannot stand for their run, or ``None`` when they add up to its net profit within a cent."""
    total = math.fsum(nets)
    if abs(total - net_profit) <= RECONCILE_ATOL + _FLOAT_SLACK:
        return None
    return f"Its trades add up to {usd(total)} after commission, but the run's net profit is {usd(net_profit)}, so the trades do not account for the whole result."
