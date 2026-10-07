"""An option entry, prepared: the picked contracts as a checked structure, its limit price and its money.

A resolution says which contract each leg's rule came to. Before anything is
set aside or sent, four more questions are asked in order. An entry is
refused by the first "no" (``prepare_entry``). A preview is shown every
figure that can be worked out and every "no" (``entry_figures``). Both are
one function, so what a preview shows is what an entry would do:

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
  gap percent of mid = (ask_i - bid_i) / ((bid_i + ask_i) / 2) · 100, for one leg's quote;
  a leg is refused when (ask_i - bid_i) · 100 > leg gap limit percent · (bid_i + ask_i) / 2;
  the entry is refused when round-trip cost · 100 > cost limit percent · entry commitment.
  The premium, requirement, commitment, loss estimate and round-trip cost are
  ``app/options/requirement.py``'s, which carries their formulas and sources.
Reference: ADR 0076 decisions 2, 5 and 7; the leg gap limit is #2913's
  "widest bid-ask gap, each leg", a percent of the leg's mid.
Canonical implementation: ``app/options/requirement.py`` and
  ``app/options/structure_check.py`` for everything but the leg gap, which is
  this file's.
Validated against: tests/options/test_entry.py, and
  tests/options/test_preview_and_entry_agree.py for an entry and a preview
  never disagreeing over the same picks and quotes.

A leg can pass its own gap limit and the structure still cost most of itself
to trade, which is why both limits are asked: two legs with mids of $5.00 and
$4.90, each four cents wide, are each well inside 8% and together cost $8 of a
$14 commitment.

The same questions are asked again of the quotes read just before the order
is sent: the price sent is the one those quotes give, and the commitment is
worked out at the price sent.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
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
from app.options.structure_check import StructureRefused, require_named_shape, resolved_structure
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
        return bid_ask_gap_percent_of_mid(self.bid, self.ask)


def bid_ask_gap_percent_of_mid(bid: Decimal, ask: Decimal) -> Fraction:
    """One quote's bid-ask gap as a percent of its mid, exact. The mid is above zero because the ask is."""
    if not (bid.is_finite() and ask.is_finite()) or bid < 0 or ask <= 0 or bid > ask:
        raise ValueError(f"a gap is read from a market that can be crossed; got bid {bid}, ask {ask}")
    return (Fraction(ask) - Fraction(bid)) * 200 / (Fraction(bid) + Fraction(ask))


def _legs_over_their_gap_limit(
    structure: ResolvedStructure, markets: Mapping[str, LegQuote], *, limit_percent: Decimal
) -> list[LegGapOverLimit]:
    """Each leg, in the structure's own order, whose market is wider than the limit."""
    limit = Fraction(require_everyday(limit_percent, what="a leg gap limit"))
    if limit < 0:
        raise ValueError(f"a leg gap limit is a percent, zero or more; got {limit_percent}")
    over = []
    for leg in structure.legs:
        market = markets[leg.leg_id]
        if bid_ask_gap_percent_of_mid(market.bid, market.ask) > limit:
            over.append(
                LegGapOverLimit(
                    leg_id=leg.leg_id, contract=leg.contract, bid=market.bid, ask=market.ask, limit_percent=limit_percent
                )
            )
    return over


# What an entry sets aside for its own fees, in whole cents, or None when no rate is pinned for it.
FeeProvision = Callable[[ResolvedStructure, Mapping[str, LegQuote], EntrySize], int | None]

# A typed "no" an entry meets: the picks are not a structure the plan may open, or their money is refused.
EntryRefusal = StructureRefused | StructureMoneyRefused


@dataclass(frozen=True, slots=True)
class EntryFigures:
    """What an entry on the picked contracts comes to at the quotes handed in, and every "no" it would meet.

    ``refusals`` are the exceptions an entry is refused by, in the order an
    entry asks its questions: ``prepare_entry`` raises the first. Each figure
    is ``None`` when it could not be worked out, and a refusal says why.
    """

    refusals: tuple[EntryRefusal, ...] = ()
    structure: ResolvedStructure | None = None
    size: EntrySize | None = None
    # Each leg's bid and ask, by leg id: what every price here is read from.
    markets: Mapping[str, LegQuote] | None = None
    # What one structure is entered at, per share: positive is paid, negative received.
    net_price: Decimal | None = None
    # None beside a structure: no fee rate is pinned, so nothing can be set aside.
    fee_provision_cents: int | None = None
    money: EntryMoney | None = None
    round_trip_cost_cents: int | None = None

    @property
    def legs_over_gap_limit(self) -> frozenset[str]:
        """The legs whose own market is wider than the owner's limit, by leg id."""
        return frozenset(refused.leg_id for refused in self.refusals if isinstance(refused, LegGapOverLimit))

    @property
    def cost_is_over_limit(self) -> bool | None:
        """Whether the round-trip cost is over the owner's limit. None when no commitment came out to hold it to."""
        if self.money is None:
            return None
        return any(
            isinstance(refused, StructureMoneyRefused) and refused.reason is StructureMoneyRefusal.COST_OVER_LIMIT
            for refused in self.refusals
        )


def entry_figures(
    plan: ActionPlan,
    *,
    picked: Mapping[str, OptionContract],
    multipliers: Mapping[str, int],
    quotes: Mapping[str, HeldQuote],
    units: int,
    fee_provision: FeeProvision,
    leg_gap_limit_percent: Decimal,
    cost_limit_percent: Decimal,
    first_refusal_only: bool = False,
) -> EntryFigures:
    """The figures of the entry ``plan`` would make of the picked contracts, and every reason it would be refused.

    The questions are an entry's, in an entry's order, and a refusal is
    returned, never raised. With ``first_refusal_only`` no question is asked
    after the first one that refuses: that is an entry. Without it every
    question that can still be asked is, and every leg over the gap limit is
    named: that is a preview. Picks that are not the plan's legs at all end both,
    since nothing can be priced from them.

    A mistake of the caller's is still raised as one: legs listed with more
    than one multiplier, a leg with no whole quote, a limit that is no
    percent. ``quotes`` are usable quotes: the resolver's, or the recheck's.
    """
    try:
        structure = resolved_structure(plan, picked)
    except StructureRefused as refused:
        return EntryFigures(refusals=(refused,))
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
    net_price = natural_net_price(structure, markets)
    refusals: list[EntryRefusal] = []

    def so_far(
        *, fees: int | None = None, money: EntryMoney | None = None, cost: int | None = None
    ) -> EntryFigures:
        return EntryFigures(
            refusals=tuple(refusals),
            structure=structure,
            size=size,
            markets=markets,
            net_price=net_price,
            fee_provision_cents=fees,
            money=money,
            round_trip_cost_cents=cost,
        )

    try:
        require_named_shape(plan, structure, net_price=net_price)
    except StructureRefused as refused:
        refusals.append(refused)
        if first_refusal_only:
            return so_far()
    refusals.extend(_legs_over_their_gap_limit(structure, markets, limit_percent=leg_gap_limit_percent))
    if refusals and first_refusal_only:
        return so_far()
    fees = fee_provision(structure, markets, size)
    money = None
    if fees is not None:
        try:
            money = entry_money(structure, size, net_price=net_price, fee_provision_cents=fees)
        except StructureMoneyRefused as refused:
            refusals.append(refused)
    if money is None and first_refusal_only:
        return so_far(fees=fees)
    cost = round_trip_cost(structure, markets, size)
    if money is not None and round_trip_cost_is_over_limit(
        cost_cents=cost, commitment_cents=money.entry_commitment_cents, limit_percent=cost_limit_percent
    ):
        refusals.append(
            StructureMoneyRefused(
                StructureMoneyRefusal.COST_OVER_LIMIT,
                f"Opening and closing at once would cost {cost} cents, more than {cost_limit_percent}% of the "
                f"{money.entry_commitment_cents} cents the entry sets aside.",
            )
        )
    return so_far(fees=fees, money=money, cost=cost)


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
    cannot be set aside, or when its round-trip cost is over the limit: the
    first of ``entry_figures``'s refusals. ``quotes`` are usable quotes: the
    resolver's, or the recheck's.
    """
    worked = entry_figures(
        plan,
        picked=picked,
        multipliers=multipliers,
        quotes=quotes,
        units=units,
        fee_provision=lambda _structure, _markets, _size: fee_provision_cents,
        leg_gap_limit_percent=leg_gap_limit_percent,
        cost_limit_percent=cost_limit_percent,
        first_refusal_only=True,
    )
    if worked.refusals:
        raise worked.refusals[0]
    assert worked.structure is not None and worked.size is not None and worked.net_price is not None
    money = worked.money
    if money is None:
        # Only a provision that was handed over as nothing gets here, and the money refuses it in its own words.
        money = entry_money(
            worked.structure, worked.size, net_price=worked.net_price, fee_provision_cents=fee_provision_cents
        )
    assert worked.round_trip_cost_cents is not None
    return PreparedEntry(
        structure=worked.structure,
        size=worked.size,
        net_limit_price=worked.net_price,
        money=money,
        round_trip_cost_cents=worked.round_trip_cost_cents,
        quotes=dict(quotes),
    )
