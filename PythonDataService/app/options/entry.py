"""An option entry, prepared: the picked contracts as a checked structure, its limit price and its money.

A resolution says which contract each leg's rule came to. Before anything is
set aside or sent, four more questions are asked in order, and the first
"no" refuses the entry:

1. Are the picked contracts a structure the plan may open, and of the shape
   its name means? (``app.options.structure_check``)
2. Is any leg's own market wider than the owner's limit? A leg's gap is its
   ask less its bid, held against its mid.
3. What does it cost? The limit price is the natural price: each bought leg's
   ask less each sold leg's bid, by ratio, positive when paid. From it come
   the premium, the broker's requirement and what the bot sets aside.
   (``app.options.requirement``)
4. Would opening it and closing it at once cost more than the owner's limit?

Formula: limit price = Σ s_i · r_i · (ask_i for a bought leg, bid_i for a sold one);
  a leg is refused when (ask_i - bid_i) · 100 > leg gap limit percent · (bid_i + ask_i) / 2;
  the entry is refused when round-trip cost · 100 > cost limit percent · entry commitment.
  The premium, requirement, commitment, loss estimate and round-trip cost are
  ``app/options/requirement.py``'s, which carries their formulas and sources.
Reference: ADR 0076 decisions 2, 5 and 7; the leg gap limit is #2913's
  "widest bid-ask gap, each leg", a percent of the leg's mid.
Canonical implementation: ``app/options/requirement.py`` and
  ``app/options/structure_check.py`` for everything but the leg gap, which is
  this file's.
Validated against: tests/options/test_entry.py.

A leg can pass its own gap limit and the structure still cost most of itself
to trade, which is why both limits are asked: two legs with mids of $5.00 and
$4.90, each four cents wide, are each well inside 8% and together cost $8 of a
$14 commitment.

The same questions are asked again of the quotes read just before the order
is sent: the price sent is the one those quotes give, and the commitment is
worked out at the price sent.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

from app.options.contract import OptionContract
from app.options.exact import require_everyday
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
# The widest bid-ask gap an owner starts from, for each leg: a percent of the leg's mid.
DEFAULT_LEG_GAP_LIMIT_PERCENT = Decimal(8)


class LegGapOverLimit(StructureMoneyRefused):
    """One leg's bid-ask gap is wider than the owner's limit. Carries what a screen needs to say so."""

    def __init__(
        self, *, leg_id: str, contract: OptionContract, bid: Decimal, ask: Decimal, limit_percent: Decimal
    ) -> None:
        super().__init__(
            StructureMoneyRefusal.LEG_GAP_OVER_LIMIT,
            f"The gap between bid {bid} and ask {ask} on {contract.occ_symbol} (leg {leg_id!r}) is over "
            f"{limit_percent}% of its mid.",
        )
        self.leg_id = leg_id
        self.contract = contract
        self.bid = bid
        self.ask = ask
        self.limit_percent = limit_percent

    @property
    def gap_percent_of_mid(self) -> Fraction:
        """The gap as a percent of the mid, exact. A screen rounds it; nothing here does."""
        return _gap_percent_of_mid(self.bid, self.ask)


def _gap_percent_of_mid(bid: Decimal, ask: Decimal) -> Fraction:
    """(ask - bid) · 100 over (bid + ask) / 2, exact. The mid is above zero because the ask is."""
    return (Fraction(ask) - Fraction(bid)) * 200 / (Fraction(bid) + Fraction(ask))


def _require_each_leg_inside_its_gap_limit(
    structure: ResolvedStructure, markets: Mapping[str, LegQuote], *, limit_percent: Decimal
) -> None:
    """Refuse the first leg, in the structure's own order, whose market is wider than the limit."""
    limit = Fraction(require_everyday(limit_percent, what="a leg gap limit"))
    if limit < 0:
        raise ValueError(f"a leg gap limit is a percent, zero or more; got {limit_percent}")
    for leg in structure.legs:
        market = markets[leg.leg_id]
        if _gap_percent_of_mid(market.bid, market.ask) > limit:
            raise LegGapOverLimit(
                leg_id=leg.leg_id, contract=leg.contract, bid=market.bid, ask=market.ask, limit_percent=limit_percent
            )


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
    leg_gap_limit_percent: Decimal,
    cost_limit_percent: Decimal,
) -> PreparedEntry:
    """The entry ``plan`` would make of the picked contracts at the quotes handed in.

    Raises ``StructureRefused`` when the contracts are not a structure the
    plan may open, and ``StructureMoneyRefused`` when a leg's bid-ask gap is
    over its limit (``LegGapOverLimit``, which names the leg), when its money
    cannot be set aside, or when its round-trip cost is over the limit.
    ``quotes`` are usable quotes: the resolver's, or the recheck's.
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
    _require_each_leg_inside_its_gap_limit(structure, markets, limit_percent=leg_gap_limit_percent)
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
