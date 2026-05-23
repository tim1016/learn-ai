"""Position-sizing models for ``set_holdings``.

A backtest's share count for ``SetHoldings(symbol, fraction)`` depends on
the sizing policy. Two are available:

* ``SimpleFloorSizing`` — ``floor(portfolio_value * fraction / price)``.
  No buffer. Not LEAN-equivalent; fine for quick research runs where exact
  cross-engine parity is not required.
* ``LeanSetHoldingsSizing`` — mirrors LEAN's ``QCAlgorithm.SetHoldings`` for
  the long-only equity case: reserves a free-portfolio-value buffer and the
  per-order fee before flooring to whole shares, so it buys the same share
  count LEAN does. Use for LEAN-pinned / cross-engine-parity runs.

This is a deliberately narrow port: it covers the long-only equity
``SetHoldings(symbol, target)`` path the parity matrix exercises, NOT LEAN's
full buying-power universe (margin/cash accounts, leverage, shorts, option
models, open-order reservations, iterative multi-asset fee models).

Provenance:
  Formula: qty = floor((min(target_value, buying_power) - order_fee) / price)
           target_value = portfolio_value * target_fraction
           buying_power = portfolio_value * (1 - FreePortfolioValuePercentage)
  Reference: LEAN QCAlgorithm.SetHoldings -> IBuyingPowerModel
             .GetMaximumOrderQuantityForTargetBuyingPower
  Canonical implementation: this file (LeanSetHoldingsSizing).
  Validated against: tests/fixtures/golden/lean-set-holdings/ — 20 SPY
             entries from a pinned LEAN run, reproduced at atol=0 by
             tests/engine/execution/test_sizing.py. See
             docs/references/lean-set-holdings.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

# LEAN's documented default ``Settings.FreePortfolioValuePercentage`` — the
# slice of portfolio value LEAN holds back from ``SetHoldings`` sizing so an
# order does not get rejected for insufficient buying power.
LEAN_FREE_PORTFOLIO_VALUE_PCT = Decimal("0.0025")


@runtime_checkable
class SizingModel(Protocol):
    """Maps a target portfolio fraction to a whole-share quantity."""

    name: str

    def target_quantity(
        self,
        *,
        portfolio_value: Decimal,
        price: Decimal,
        target_fraction: Decimal,
        order_fee: Decimal,
    ) -> int:
        """Whole-share count to hold ``target_fraction`` of ``portfolio_value``."""
        ...


@dataclass(frozen=True)
class SimpleFloorSizing:
    """``floor(portfolio_value * target_fraction / price)`` — no buffer.

    The historical Engine behaviour. Not LEAN parity: it spends the whole
    portfolio value and so buys one or more shares more than LEAN's
    buffered ``SetHoldings``.
    """

    name: str = "simple_floor"

    def target_quantity(
        self,
        *,
        portfolio_value: Decimal,
        price: Decimal,
        target_fraction: Decimal,
        order_fee: Decimal,
    ) -> int:
        if price <= 0:
            raise ValueError(f"sizing price must be positive, got {price}")
        return int(portfolio_value * target_fraction / price)


@dataclass(frozen=True)
class LeanSetHoldingsSizing:
    """LEAN-equivalent ``SetHoldings`` sizing for long-only equity.

    Reserves ``free_portfolio_value_pct`` of portfolio value plus the
    per-order fee, then floors to whole shares — matching LEAN's
    ``GetMaximumOrderQuantityForTargetBuyingPower`` for this case.
    """

    name: str = "lean_set_holdings"
    free_portfolio_value_pct: Decimal = LEAN_FREE_PORTFOLIO_VALUE_PCT

    def target_quantity(
        self,
        *,
        portfolio_value: Decimal,
        price: Decimal,
        target_fraction: Decimal,
        order_fee: Decimal,
    ) -> int:
        if price <= 0:
            raise ValueError(f"sizing price must be positive, got {price}")
        target_value = portfolio_value * target_fraction
        buying_power = portfolio_value * (Decimal(1) - self.free_portfolio_value_pct)
        budget = min(target_value, buying_power) - order_fee
        if budget <= 0:
            return 0
        return int(budget / price)
