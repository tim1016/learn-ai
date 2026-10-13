"""Money retained by open option lots, including covered sold legs (ADR 0076).

Formula: signed basis = Σ sign(lot) × lot cost × contracts × multiplier;
  collateral = held requirement of the signed holdings, each contract at its
  recorded multiplier (``held_requirement_cents``);
  position commitment = signed basis + collateral. Cash reserves collateral
  separately, because selling an option brings cash in without releasing it.
Reference: ADR 0076 decision 28; Alpaca's Options Level 3 maintenance rule,
  pinned by ``app/options/requirement.py`` and its OSM-001/OSM-002 fixtures.
Canonical implementation: this FIFO composition; ``fifo_pnl.py`` owns basis
  and ``app/options/requirement.py`` owns collateral.
Validated against: tests/broker/alpaca/clerk/test_option_position_money.py
  and tests/broker/alpaca/clerk/sqlite/test_multi_leg_close.py.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from app.broker.alpaca.clerk.budgets import BudgetUnavailable
from app.broker.alpaca.clerk.fifo_pnl import OpenLot
from app.broker.alpaca.clerk.money import ZERO, money_context
from app.broker.contract.models import OrderSide
from app.options.requirement import ContractAtMultiplier, StructureMoneyRefused, held_requirement_cents


@money_context()
def option_position_money(lots: Sequence[OpenLot]) -> tuple[Decimal, Decimal]:
    """Return (signed basis plus collateral, collateral); refuse unbounded or incoherent holdings."""
    signed: dict[ContractAtMultiplier, int] = {}
    basis = ZERO
    for lot in lots:
        if lot.multiplier is None or lot.exact_qty != lot.exact_qty.to_integral_value():
            raise BudgetUnavailable("Option collateral needs whole contract lots with recorded multipliers.")
        sign = 1 if lot.side is OrderSide.BUY else -1
        held = (lot.symbol, lot.multiplier)
        signed[held] = signed.get(held, 0) + sign * int(lot.exact_qty)
        basis += sign * lot.exact_cost_basis
    try:
        cents = held_requirement_cents(signed)
    except StructureMoneyRefused as exc:
        raise BudgetUnavailable(f"The held option structure's collateral cannot be bounded: {exc}") from exc
    collateral = Decimal(cents).scaleb(-2)
    return basis + collateral, collateral
