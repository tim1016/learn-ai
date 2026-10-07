"""How a Dry Run or a Live Shadow fills an option order: at the natural price of the quotes kept with it.

A simulated venue cannot rest an order, so nothing here waits, cancels or
fills in part. Three claims are kept apart, and only the first two are within
a simulation's reach: that the decision can be replayed, that the contracts,
the limit and the refusals can be reproduced from the evidence kept, and what
a real venue would have filled. This models the third and proves nothing
about it.

Formula: a bought leg is filled at its ask and a sold leg at its bid, every
  leg at once and in full. An entry is filled from the quotes it was priced
  from. An exit reverses each leg: a bought leg is sold at its bid, a sold
  leg bought back at its ask, from the newest quote held for the contract.
  A quote's age is the fill's time less the receipt time of the side used.
Reference: ADR 0076 (#2913: what a simulation fills, and what it does not
  claim). The natural price is ``app/options/requirement.py``'s.
Canonical implementation: this file.
Validated against: tests/options/test_simulated_fill.py.

Every fill says which model priced it and how old the quote side it used
was. An exit whose contract has no quote a decision could use is still
priced, from the newest quote held, and says so. One with nothing to price it
from is unpriced: it has no price, and whoever keeps the books leaves it out
of profit and loss and flags it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from app.options.contract import OptionContract
from app.options.quotes import HeldQuote, QuoteSide, quote_fault
from app.options.structure import ResolvedLeg, ResolvedStructure


class SimulatedFillModel(StrEnum):
    # From a quote a decision could use at the fill's time.
    NATURAL_PRICE = "natural_price"
    # An exit only: from the newest quote held, which no decision could use at that time. See its age.
    NATURAL_PRICE_OLDER_QUOTE = "natural_price_older_quote"
    # An exit only: nothing held to price it from.
    UNPRICED = "unpriced"


@dataclass(frozen=True, slots=True)
class SimulatedLegFill:
    """One leg of a simulated fill."""

    leg_id: str
    contract: OptionContract
    side: Literal["buy", "sell"]
    contracts: int
    # Per share. None when the leg is unpriced.
    price: Decimal | None
    model: SimulatedFillModel
    # How old the quote side it was priced from was at the fill. None when the leg is unpriced.
    quote_age_ms: int | None


def _contracts(leg: ResolvedLeg, units: int) -> int:
    if isinstance(units, bool) or not isinstance(units, int) or units < 1:
        raise ValueError(f"an order is for a whole number of structures, one or more; got {units!r}")
    return units * leg.ratio


def simulated_entry_fills(
    structure: ResolvedStructure, quotes: Mapping[str, HeldQuote], *, units: int, at_ms: int
) -> tuple[SimulatedLegFill, ...]:
    """Every leg of an entry, filled in full at the natural price of the quotes it was priced from.

    ``quotes`` are the usable quotes read just before the order would have
    been sent, one for each leg. Refuses to invent a fill without them.
    """
    fills = []
    for leg in structure.legs:
        quote = quotes.get(leg.leg_id)
        buys = leg.position == "long"
        side = None if quote is None else (quote.ask if buys else quote.bid)
        if side is None:
            raise ValueError(f"leg {leg.leg_id!r} has no quote kept to fill it from")
        fills.append(
            SimulatedLegFill(
                leg_id=leg.leg_id,
                contract=leg.contract,
                side="buy" if buys else "sell",
                contracts=_contracts(leg, units),
                price=side.price,
                model=SimulatedFillModel.NATURAL_PRICE,
                quote_age_ms=at_ms - side.received_ms,
            )
        )
    return tuple(fills)


def _a_price_to_close_at(side: QuoteSide | None, *, buys: bool) -> bool:
    """Whether a quote side is a price a close can be filled at. Nothing bid is a price to sell at, zero;
    nothing offered is no price to buy at."""
    if side is None or not side.price.is_finite() or side.price < 0:
        return False
    return side.price > 0 or not buys


def simulated_exit_fills(
    structure: ResolvedStructure,
    quotes: Mapping[str, HeldQuote | None],
    *,
    units: int,
    at_ms: int,
    generation: int,
    max_age_ms: int,
) -> tuple[SimulatedLegFill, ...]:
    """Every leg of an exit, each closed at the natural price of the newest quote held for its contract.

    ``quotes`` holds the newest quote held for each leg's contract, whatever
    its age, or None. ``generation`` and ``max_age_ms`` say which of them a
    decision could have used at ``at_ms``.
    """
    fills = []
    for leg in structure.legs:
        quote = quotes.get(leg.leg_id)
        # The exit reverses the leg: what was bought is sold, what was sold is bought back.
        buys = leg.position == "short"
        side = None if quote is None else (quote.ask if buys else quote.bid)
        priced = quote is not None and _a_price_to_close_at(side, buys=buys)
        usable = priced and quote_fault(
            quote, at_ms=at_ms, generation=generation, max_age_ms=max_age_ms, needs_a_bid=False
        ) is None
        fills.append(
            SimulatedLegFill(
                leg_id=leg.leg_id,
                contract=leg.contract,
                side="buy" if buys else "sell",
                contracts=_contracts(leg, units),
                price=side.price if priced else None,
                model=(
                    SimulatedFillModel.UNPRICED
                    if not priced
                    else SimulatedFillModel.NATURAL_PRICE
                    if usable
                    else SimulatedFillModel.NATURAL_PRICE_OLDER_QUOTE
                ),
                quote_age_ms=at_ms - side.received_ms if priced else None,
            )
        )
    return tuple(fills)
