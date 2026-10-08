"""Money retained by open option lots, including covered sold legs (ADR 0076).

Formula: signed basis = Σ sign(lot) × lot cost × contracts × multiplier;
  collateral = Σ_(underlying, multiplier) max_expiry(canonical requirement of signed holdings);
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

from collections import defaultdict
from collections.abc import Sequence
from decimal import Decimal

from app.broker.alpaca.clerk.budgets import BudgetUnavailable
from app.broker.alpaca.clerk.fifo_pnl import OpenLot
from app.broker.alpaca.clerk.money import ZERO, money_context
from app.broker.contract.models import OrderSide
from app.options.contract import OptionContract
from app.options.requirement import EntrySize, StructureMoneyRefused, portfolio_broker_requirement
from app.options.structure import ResolvedLeg


@money_context()
def option_position_money(lots: Sequence[OpenLot]) -> tuple[Decimal, Decimal]:
    """Return (signed basis plus collateral, collateral); refuse unbounded or incoherent holdings."""
    groups: dict[tuple[str, int], dict[str, int]] = defaultdict(dict)
    basis = ZERO
    for lot in lots:
        if lot.multiplier is None or lot.exact_qty != lot.exact_qty.to_integral_value():
            raise BudgetUnavailable("Option collateral needs whole contract lots with recorded multipliers.")
        contract = OptionContract.from_occ_symbol(lot.symbol)
        sign = 1 if lot.side is OrderSide.BUY else -1
        group = groups[contract.root, lot.multiplier]
        group[lot.symbol] = group.get(lot.symbol, 0) + sign * int(lot.exact_qty)
        basis += sign * lot.exact_cost_basis
    collateral = ZERO
    for (_root, multiplier), signed in groups.items():
        held = {symbol: qty for symbol, qty in signed.items() if qty}
        if not held:
            continue
        legs = tuple(
            ResolvedLeg(
                leg_id=symbol, contract=OptionContract.from_occ_symbol(symbol),
                position="long" if qty > 0 else "short", ratio=abs(qty),
            ) for symbol, qty in held.items()
        )
        try:
            cents = portfolio_broker_requirement(legs, EntrySize(multiplier=multiplier, units=1))
        except StructureMoneyRefused as exc:
            raise BudgetUnavailable(f"The held option structure's collateral cannot be bounded: {exc}") from exc
        collateral += Decimal(cents).scaleb(-2)
    return basis + collateral, collateral
