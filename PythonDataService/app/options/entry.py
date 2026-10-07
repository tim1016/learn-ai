"""An option entry, prepared: the picked contracts as a checked structure, its limit price and its money.

A resolution says which contract each leg's rule came to. Before anything is
set aside or sent, three more questions are asked in order, and the first
"no" refuses the entry:

1. Are the picked contracts a structure the plan may open, and of the shape
   its name means? (``app.options.structure_check``)
2. What does it cost? The limit price is the natural price: each bought leg's
   ask less each sold leg's bid, by ratio, positive when paid. From it come
   the premium, the broker's requirement and what the bot sets aside.
   (``app.options.requirement``)
3. Would opening it and closing it at once cost more than the owner's limit?

Formula: limit price = Σ s_i · r_i · (ask_i for a bought leg, bid_i for a sold one);
  refused when round-trip cost · 100 > cost limit percent · entry commitment.
  The premium, requirement, commitment, loss estimate and round-trip cost are
  ``app/options/requirement.py``'s, which carries their formulas and sources.
Reference: ADR 0076 decisions 2, 5 and 7.
Canonical implementation: ``app/options/requirement.py`` and
  ``app/options/structure_check.py``. This file composes them and computes
  nothing of its own.
Validated against: tests/options/test_entry.py.

The same questions are asked again of the quotes read just before the order
is sent: the price sent is the one those quotes give, and the commitment is
worked out at the price sent.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from app.options.contract import OptionContract
from app.options.quotes import HeldQuote
from app.options.requirement import (
    EntryMoney,
    EntrySize,
    LegQuote,
    StructureMoneyRefusal,
    StructureMoneyRefused,
    entry_money,
    natural_net_price,
    round_trip_cost,
    round_trip_cost_is_over_limit,
)
from app.options.structure import ResolvedStructure
from app.options.structure_check import require_named_shape, resolved_structure
from app.schemas.action_plan import ActionPlan

# The structure cost limit an owner starts from: a percent of the entry commitment.
DEFAULT_COST_LIMIT_PERCENT = Decimal(10)


@dataclass(frozen=True, slots=True)
class PreparedEntry:
    """One entry, ready to be admitted and sent, or shown."""

    structure: ResolvedStructure
    size: EntrySize
    # What one structure is sent at, per share: positive is paid, negative received.
    net_limit_price: Decimal
    money: EntryMoney
    round_trip_cost_cents: int
    # The quote each leg's price was read from, by leg id.
    quotes: Mapping[str, HeldQuote]


def prepare_entry(
    plan: ActionPlan,
    *,
    picked: Mapping[str, OptionContract],
    multipliers: Mapping[str, int],
    quotes: Mapping[str, HeldQuote],
    units: int,
    fee_provision_cents: int,
    cost_limit_percent: Decimal,
) -> PreparedEntry:
    """The entry ``plan`` would make of the picked contracts at the quotes handed in.

    Raises ``StructureRefused`` when the contracts are not a structure the
    plan may open, and ``StructureMoneyRefused`` when its money cannot be set
    aside or its round-trip cost is over the limit. ``quotes`` are usable
    quotes: the resolver's, or the recheck's.
    """
    structure = resolved_structure(plan, picked)
    listed = {multipliers[leg.leg_id] for leg in structure.legs}
    if len(listed) != 1:
        # One order's legs are priced against one underlying in one unit.
        raise ValueError(f"every leg of a structure has one multiplier; these have {sorted(listed)}")
    size = EntrySize(multiplier=listed.pop(), units=units)
    markets = {}
    for leg in structure.legs:
        quote = quotes[leg.leg_id]
        if quote.bid is None or quote.ask is None:
            raise ValueError(f"leg {leg.leg_id!r} has no whole quote to price it from")
        markets[leg.leg_id] = LegQuote(bid=quote.bid.price, ask=quote.ask.price)
    net_limit_price = natural_net_price(structure, markets)
    require_named_shape(plan, structure, net_price=net_limit_price)
    money = entry_money(structure, size, net_price=net_limit_price, fee_provision_cents=fee_provision_cents)
    cost = round_trip_cost(structure, markets, size)
    if round_trip_cost_is_over_limit(
        cost_cents=cost, commitment_cents=money.entry_commitment_cents, limit_percent=cost_limit_percent
    ):
        raise StructureMoneyRefused(
            StructureMoneyRefusal.COST_OVER_LIMIT,
            f"Opening and closing at once would cost {cost} cents, more than {cost_limit_percent}% of the "
            f"{money.entry_commitment_cents} cents the entry sets aside.",
        )
    return PreparedEntry(
        structure=structure,
        size=size,
        net_limit_price=net_limit_price,
        money=money,
        round_trip_cost_cents=cost,
        quotes=dict(quotes),
    )
