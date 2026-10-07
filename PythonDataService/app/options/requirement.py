"""What an option structure costs to enter: its premium, the broker's requirement, and what the bot sets aside.

Formula: with ``s_i`` = +1 for a bought leg and -1 for a sold one, ``r_i`` its
  ratio, ``K_i`` its strike, ``m`` the contract multiplier and ``N`` the
  structures opened:
  payoff(p)   = Σ s_i · r_i · (max(p - K_i, 0) for a call, max(K_i - p, 0) for a put)
  requirement = N · m · max(0, -min over p ≥ 0 of payoff(p))
  premium     = N · m · net price,  net price = Σ s_i · r_i · price_i   (positive is paid)
  commitment  = requirement + premium + fee provision
  loss at expiry (estimate) = max(0, premium - N · m · min over p ≥ 0 of payoff(p))
  round-trip cost = N · m · Σ r_i · (ask_i - bid_i)
  payoff is piecewise linear, so its minimum over p ≥ 0 is at p = 0 or at a
  strike, unless it still falls above the highest strike, where it has none.
Reference: Alpaca, "Options Level 3 Trading", on maintenance margin and cost
  basis, https://docs.alpaca.markets/docs/options-level-3-trading (page dated
  2025-09-24, read 2026-10-06): the requirement is the lowest point of the
  summed exercise payoffs, premiums left out, and an order costs that plus
  its net premium. The sign of a multi-leg price, positive for a debit, is
  from Alpaca's order reference (``POST /v2/orders``, ``limit_price``,
  https://docs.alpaca.markets/reference/postorder, read 2026-10-06). Reading
  the payoff at zero and from its slope above the highest strike is how this
  file finds that lowest point; the loss estimate and the round-trip cost are
  this repo's own definitions (ADR 0076).
Canonical implementation: this file.
Validated against: tests/fixtures/test_option_structure_money_fixture.py
  (golden fixture OSM-001: Alpaca's two documented examples and hand-derived
  cases, bit-exact) and tests/options/test_requirement.py.

Every quantity is whole cents and no step here rounds. A strike is whole
thousandths of a dollar and a price is an exact decimal; an amount that does
not come to whole cents is refused, never rounded.

The requirement leaves premiums out, as Alpaca's rule does, so it and the
commitment are different numbers by design: a $5-wide spread sold for $3.07
has a requirement of $500.00 and a commitment of $193.00.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.options.exact import exact_sum, require_everyday, whole_units
from app.options.structure import ResolvedLeg, ResolvedStructure

# A strike is in thousandths of a dollar; ten of them make a cent.
_THOUSANDTHS_PER_CENT = 10
_CENTS_PER_DOLLAR = 100


class StructureMoneyRefusal(StrEnum):
    """Why a structure's money cannot be worked out, or must not be set aside."""

    # The loss grows without limit as the underlying rises.
    LOSS_UNBOUNDED = "OPTION_STRUCTURE_LOSS_UNBOUNDED"
    # Legs expire on different dates and one of them is sold.
    MIXED_EXPIRY_WITH_SOLD_LEG = "OPTION_STRUCTURE_MIXED_EXPIRY_WITH_SOLD_LEG"
    # Requirement plus premium is zero or less: the prices do not belong together.
    COMMITMENT_NOT_POSITIVE = "OPTION_STRUCTURE_COMMITMENT_NOT_POSITIVE"
    # Opening and closing at once would cost more than the owner's limit, as a percent of the commitment.
    COST_OVER_LIMIT = "OPTION_STRUCTURE_COST_OVER_LIMIT"
    # One leg's own market is wider than the owner's limit, as a percent of its mid.
    LEG_GAP_OVER_LIMIT = "OPTION_STRUCTURE_LEG_GAP_OVER_LIMIT"


class StructureMoneyRefused(ValueError):
    """A typed refusal from the structure's money rules."""

    def __init__(self, reason: StructureMoneyRefusal, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class EntrySize:
    """How much one entry is: shares per contract, and structures opened. Every figure is for exactly this."""

    multiplier: int
    units: int

    def __post_init__(self) -> None:
        for name, value in (("multiplier", self.multiplier), ("units", self.units)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a whole number, one or more; got {value!r}")


@dataclass(frozen=True, slots=True)
class LegQuote:
    """One leg's bid and ask, in dollars per share."""

    bid: Decimal
    ask: Decimal


@dataclass(frozen=True, slots=True)
class EntryMoney:
    """What an entry of a given size at a given net price means in cash, in whole cents."""

    # What the entry pays (positive) or receives (negative).
    net_entry_premium_cents: int
    # The broker's maintenance requirement. Premiums are left out of it.
    broker_requirement_cents: int
    # What the bot sets aside: requirement plus premium plus the fee provision.
    entry_commitment_cents: int
    # The most the structure loses if every leg is held to one shared expiry,
    # is never exercised or assigned early, and settles at its exercise value,
    # before fees. ``None`` when the legs do not share an expiry: not estimated.
    loss_at_expiry_cents: int | None


def _sign(leg: ResolvedLeg) -> int:
    return 1 if leg.position == "long" else -1


def _payoff_thousandths(structure: ResolvedStructure, price_thousandths: int) -> int:
    """One structure's exercise payoff per share at an underlying price, in thousandths of a dollar."""
    total = 0
    for leg in structure.legs:
        strike = leg.contract.strike_thousandths
        exercise_value = price_thousandths - strike if leg.contract.right == "call" else strike - price_thousandths
        total += _sign(leg) * leg.ratio * max(exercise_value, 0)
    return total


def exercise_payoff_thousandths(structure: ResolvedStructure, price_thousandths: int) -> int:
    """One structure's exercise payoff per share at an underlying price, in thousandths of a dollar.

    The one payoff every figure at expiry is read from: the requirement and
    the loss estimate here, and the gain, the ranges and the break-evens in
    ``app.options.at_expiry``.
    """
    if isinstance(price_thousandths, bool) or not isinstance(price_thousandths, int) or price_thousandths < 0:
        raise ValueError(f"an underlying price is whole thousandths of a dollar, zero or more; got {price_thousandths!r}")
    return _payoff_thousandths(structure, price_thousandths)


def _lowest_payoff_thousandths(structure: ResolvedStructure) -> int:
    """The lowest point of one structure's exercise payoff per share over every underlying price from zero up.

    Read at zero, at every strike, and from the slope above the highest
    strike. The strikes alone are not enough: a put structure can be at its
    worst at zero, and a call structure can keep losing above its last strike.
    """
    slope_above_every_strike = sum(_sign(leg) * leg.ratio for leg in structure.legs if leg.contract.right == "call")
    if slope_above_every_strike < 0:
        raise StructureMoneyRefused(
            StructureMoneyRefusal.LOSS_UNBOUNDED,
            "The structure sells more calls than it buys, so its loss grows without limit as the price rises.",
        )
    if structure.has_sold_leg and not structure.has_one_expiry:
        raise StructureMoneyRefused(
            StructureMoneyRefusal.MIXED_EXPIRY_WITH_SOLD_LEG,
            "The structure's legs expire on different dates and one of them is sold; "
            "the broker's requirement for that is not known, so it cannot be set aside.",
        )
    turning_points = {0, *(leg.contract.strike_thousandths for leg in structure.legs)}
    return min(_payoff_thousandths(structure, price) for price in turning_points)


def _payoff_cents(per_share_thousandths: int, size: EntrySize) -> int:
    """A per-share payoff in thousandths of a dollar, for the whole entry, in cents."""
    cents, remainder = divmod(per_share_thousandths * size.multiplier, _THOUSANDTHS_PER_CENT)
    if remainder:
        raise ValueError(
            f"a payoff of {per_share_thousandths} thousandths of a dollar a share at {size.multiplier} shares "
            "a contract is not a whole number of cents"
        )
    return size.units * cents


def _contract_cents(price: Decimal, size: EntrySize, *, what: str) -> int:
    """A per-share amount in dollars, for one contract's worth of shares, in whole cents."""
    return whole_units(
        price,
        units_per_one=size.multiplier * _CENTS_PER_DOLLAR,
        what=f"{what} at {size.multiplier} shares a contract",
        unit="cents",
    )


def broker_requirement(structure: ResolvedStructure, size: EntrySize) -> int:
    """The broker's maintenance requirement for the entry, in cents, by Alpaca's published rule.

    Alpaca works a position with several expiries out expiry by expiry and
    takes the largest figure. Its documents do not say how that treats a sold
    leg covered by a bought leg that expires later, so a structure with mixed
    expiries and a sold leg is refused rather than guessed at. With bought
    legs only the answer is zero under either reading.
    """
    return _payoff_cents(max(0, -_lowest_payoff_thousandths(structure)), size)


def _markets(structure: ResolvedStructure, quotes: Mapping[str, LegQuote]) -> dict[str, LegQuote]:
    """Each leg's quote, once it is a market that can be crossed: a bid of zero or more, under an ask above zero."""
    _require_one_entry_per_leg(structure, quotes)
    for leg in structure.legs:
        quote = quotes[leg.leg_id]
        bid = require_everyday(quote.bid, what=f"the bid of leg {leg.leg_id!r}")
        ask = require_everyday(quote.ask, what=f"the ask of leg {leg.leg_id!r}")
        if bid < 0 or ask <= 0 or bid > ask:
            raise ValueError(f"leg {leg.leg_id!r} has no market to cross: bid {quote.bid}, ask {quote.ask}")
    return dict(quotes)


def natural_net_price(structure: ResolvedStructure, quotes: Mapping[str, LegQuote]) -> Decimal:
    """One structure's net price per share when the entry crosses the market: positive is paid.

    The ask for each bought leg less the bid for each sold one, by ratio. This
    is the sign a multi-leg limit price carries at Alpaca.
    """
    markets = _markets(structure, quotes)
    return exact_sum(
        (
            _sign(leg) * leg.ratio,
            markets[leg.leg_id].ask if leg.position == "long" else markets[leg.leg_id].bid,
        )
        for leg in structure.legs
    )


def round_trip_cost(structure: ResolvedStructure, quotes: Mapping[str, LegQuote], size: EntrySize) -> int:
    """What opening the entry and closing it at once would cost, in cents: every leg's bid-ask gap, by its ratio."""
    markets = _markets(structure, quotes)
    gap = exact_sum(
        term
        for leg in structure.legs
        for term in ((leg.ratio, markets[leg.leg_id].ask), (-leg.ratio, markets[leg.leg_id].bid))
    )
    return size.units * _contract_cents(gap, size, what="one structure's bid-ask gap")


def entry_money(
    structure: ResolvedStructure,
    size: EntrySize,
    *,
    net_price: Decimal,
    fee_provision_cents: int,
) -> EntryMoney:
    """The cash figures of entering ``size`` at ``net_price`` a share for one structure (positive is paid).

    The price is the one actually sent, which need not be the quoted one.
    Refuses an entry whose requirement plus premium is zero or less: a
    structure that costs nothing or pays to hold its own worst case means the
    prices do not belong together.
    """
    if isinstance(fee_provision_cents, bool) or not isinstance(fee_provision_cents, int) or fee_provision_cents < 0:
        raise ValueError(f"a fee provision is a whole number of cents, zero or more; got {fee_provision_cents!r}")
    lowest_per_share = _lowest_payoff_thousandths(structure)
    requirement = _payoff_cents(max(0, -lowest_per_share), size)
    premium = size.units * _contract_cents(net_price, size, what="a net price")
    before_fees = requirement + premium
    if before_fees <= 0:
        raise StructureMoneyRefused(
            StructureMoneyRefusal.COMMITMENT_NOT_POSITIVE,
            f"Requirement {requirement} cents plus premium {premium} cents is not above zero; "
            "the prices do not belong together.",
        )
    return EntryMoney(
        net_entry_premium_cents=premium,
        broker_requirement_cents=requirement,
        entry_commitment_cents=before_fees + fee_provision_cents,
        loss_at_expiry_cents=(
            max(0, premium - _payoff_cents(lowest_per_share, size)) if structure.has_one_expiry else None
        ),
    )


def round_trip_cost_is_over_limit(*, cost_cents: int, commitment_cents: int, limit_percent: Decimal) -> bool:
    """Whether a round-trip cost is more than ``limit_percent`` of an entry commitment, compared exactly."""
    for name, cents in (("a round-trip cost", cost_cents), ("an entry commitment", commitment_cents)):
        if isinstance(cents, bool) or not isinstance(cents, int):
            raise ValueError(f"{name} is a whole number of cents; got {cents!r}")
    if cost_cents < 0:
        raise ValueError(f"a round-trip cost is zero or more; got {cost_cents}")
    if commitment_cents <= 0:
        raise ValueError(f"an entry commitment is above zero; got {commitment_cents}")
    # In millionths of a percent: fine enough for any limit an owner would set, and exact.
    limit_millionths = whole_units(
        limit_percent, units_per_one=1_000_000, what="a structure cost limit", unit="millionths of a percent"
    )
    if limit_millionths < 0:
        raise ValueError(f"a structure cost limit is a percent, zero or more; got {limit_percent}")
    return cost_cents * 100 * 1_000_000 > limit_millionths * commitment_cents


def _require_one_entry_per_leg(structure: ResolvedStructure, by_leg: Mapping[str, object]) -> None:
    leg_ids = {leg.leg_id for leg in structure.legs}
    if set(by_leg) != leg_ids:
        raise ValueError(f"expected one entry for each of the legs {sorted(leg_ids)}; got {sorted(by_leg)}")
