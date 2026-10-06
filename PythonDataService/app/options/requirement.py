"""What an option structure costs to enter: its premium, the broker's requirement, and what the bot sets aside.

Formula: with ``s_i`` = +1 for a bought leg and -1 for a sold one, ``r_i`` its
  ratio, ``K_i`` its strike, ``m`` the contract multiplier and ``N`` the
  structures opened:
  payoff(p)   = Σ s_i · r_i · (max(p - K_i, 0) for a call, max(K_i - p, 0) for a put)
  requirement = N · m · max(0, -min over p ≥ 0 of payoff(p))
  premium     = N · m · Σ s_i · r_i · price_i           (positive is paid)
  commitment  = requirement + premium + fee provision
  loss at expiry (estimate) = max(0, premium - N · m · min over p ≥ 0 of payoff(p))
  round-trip cost = N · m · Σ r_i · (ask_i - bid_i)
  payoff is piecewise linear, so its minimum over p ≥ 0 is at p = 0 or at a
  strike, unless it still falls above the highest strike, where it has none.
Reference: Alpaca, "Options Level 3 Trading", sections on maintenance margin
  (the "universal spread rule") and cost basis,
  https://docs.alpaca.markets/docs/options-level-3-trading (page dated
  2025-09-24, read 2026-10-06). The loss estimate and the round-trip cost are
  defined in ADR 0076.
Canonical implementation: this file.
Validated against: tests/fixtures/test_option_structure_money_fixture.py
  (golden fixture OSM-001: Alpaca's two documented examples and hand-derived
  cases, bit-exact) and tests/options/test_requirement.py.

Every quantity is whole cents and no step rounds. A strike is whole
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


class StructureMoneyRefused(ValueError):
    """A typed refusal from the structure's money rules."""

    def __init__(self, reason: StructureMoneyRefusal, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class LegQuote:
    """One leg's bid and ask, in dollars per share."""

    bid: Decimal
    ask: Decimal


@dataclass(frozen=True, slots=True)
class EntryMoney:
    """What entering ``units`` structures at a given premium means in cash, in whole cents."""

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
    turning_points = {0, *(leg.contract.strike_thousandths for leg in structure.legs)}
    return min(_payoff_thousandths(structure, price) for price in turning_points)


def _cents(thousandths_per_share: int, *, multiplier: int) -> int:
    """A per-share amount in thousandths of a dollar, for one contract's worth of shares, in whole cents."""
    cents, remainder = divmod(thousandths_per_share * multiplier, _THOUSANDTHS_PER_CENT)
    if remainder:
        raise ValueError(
            f"{thousandths_per_share} thousandths of a dollar per share times a multiplier of {multiplier} "
            "is not a whole number of cents"
        )
    return cents


def _price_cents(price: Decimal, *, multiplier: int) -> int:
    """A per-share price in dollars, for one contract's worth of shares, in whole cents."""
    if not price.is_finite() or price < 0:
        raise ValueError(f"an option price is a finite number of dollars, zero or more; got {price}")
    numerator, denominator = price.as_integer_ratio()
    cents, remainder = divmod(numerator * multiplier * _CENTS_PER_DOLLAR, denominator)
    if remainder:
        raise ValueError(f"a price of {price} per share times a multiplier of {multiplier} is not a whole number of cents")
    return cents


def _require_multiplier_and_units(*, multiplier: int, units: int = 1) -> None:
    if multiplier < 1:
        raise ValueError(f"a contract multiplier is a positive number of shares; got {multiplier}")
    if units < 1:
        raise ValueError(f"an entry opens one structure or more; got {units}")


def broker_requirement_cents(structure: ResolvedStructure, *, multiplier: int, units: int = 1) -> int:
    """The broker's maintenance requirement for ``units`` structures, by Alpaca's published rule.

    Alpaca works a position with several expiries out expiry by expiry and
    takes the largest figure. Its documents do not say how that treats a sold
    leg covered by a bought leg that expires later, so a structure with mixed
    expiries and a sold leg is refused rather than guessed at. With bought
    legs only the answer is zero under either reading.
    """
    _require_multiplier_and_units(multiplier=multiplier, units=units)
    if structure.has_sold_leg and not structure.has_one_expiry:
        raise StructureMoneyRefused(
            StructureMoneyRefusal.MIXED_EXPIRY_WITH_SOLD_LEG,
            "The structure's legs expire on different dates and one of them is sold; "
            "the broker's requirement for that is not known, so it cannot be set aside.",
        )
    return units * _cents(max(0, -_lowest_payoff_thousandths(structure)), multiplier=multiplier)


def net_entry_premium_cents(
    structure: ResolvedStructure,
    leg_prices: Mapping[str, Decimal],
    *,
    multiplier: int,
    units: int = 1,
) -> int:
    """What ``units`` structures pay (positive) or receive (negative) at each leg's per-share price.

    Bought legs' prices minus sold legs' prices, the sign a multi-leg limit
    price carries at Alpaca.
    """
    _require_multiplier_and_units(multiplier=multiplier, units=units)
    _require_one_entry_per_leg(structure, leg_prices)
    return units * sum(
        _sign(leg) * leg.ratio * _price_cents(leg_prices[leg.leg_id], multiplier=multiplier) for leg in structure.legs
    )


def natural_entry_prices(structure: ResolvedStructure, quotes: Mapping[str, LegQuote]) -> dict[str, Decimal]:
    """Each leg's price when the entry crosses the market: the ask for a bought leg, the bid for a sold one."""
    _require_one_entry_per_leg(structure, quotes)
    return {
        leg.leg_id: quotes[leg.leg_id].ask if leg.position == "long" else quotes[leg.leg_id].bid
        for leg in structure.legs
    }


def round_trip_cost_cents(
    structure: ResolvedStructure,
    quotes: Mapping[str, LegQuote],
    *,
    multiplier: int,
    units: int = 1,
) -> int:
    """What opening ``units`` structures and closing them at once would cost: every leg's bid-ask gap, by its ratio."""
    _require_multiplier_and_units(multiplier=multiplier, units=units)
    _require_one_entry_per_leg(structure, quotes)
    total = 0
    for leg in structure.legs:
        quote = quotes[leg.leg_id]
        if quote.bid > quote.ask:
            raise ValueError(f"leg {leg.leg_id!r} has its bid {quote.bid} above its ask {quote.ask}")
        total += leg.ratio * (_price_cents(quote.ask, multiplier=multiplier) - _price_cents(quote.bid, multiplier=multiplier))
    return units * total


def entry_money(
    structure: ResolvedStructure,
    *,
    net_entry_premium_cents: int,
    fee_provision_cents: int,
    multiplier: int,
    units: int = 1,
) -> EntryMoney:
    """The cash figures of entering ``units`` structures for a net premium already in cents.

    The premium is taken as given because it is the price actually sent,
    which need not be the quoted one. Refuses an entry whose requirement plus
    premium is zero or less: a structure that costs nothing or pays to hold
    its own worst case means the prices do not belong together.
    """
    _require_multiplier_and_units(multiplier=multiplier, units=units)
    if fee_provision_cents < 0:
        raise ValueError(f"a fee provision is zero or more; got {fee_provision_cents}")
    requirement = broker_requirement_cents(structure, multiplier=multiplier, units=units)
    before_fees = requirement + net_entry_premium_cents
    if before_fees <= 0:
        raise StructureMoneyRefused(
            StructureMoneyRefusal.COMMITMENT_NOT_POSITIVE,
            f"Requirement {requirement} cents plus premium {net_entry_premium_cents} cents is not above zero; "
            "the prices do not belong together.",
        )
    loss_at_expiry = None
    if structure.has_one_expiry:
        lowest_payoff = units * _cents(_lowest_payoff_thousandths(structure), multiplier=multiplier)
        loss_at_expiry = max(0, net_entry_premium_cents - lowest_payoff)
    return EntryMoney(
        net_entry_premium_cents=net_entry_premium_cents,
        broker_requirement_cents=requirement,
        entry_commitment_cents=before_fees + fee_provision_cents,
        loss_at_expiry_cents=loss_at_expiry,
    )


def round_trip_cost_is_over_limit(*, round_trip_cost_cents: int, entry_commitment_cents: int, limit_percent: Decimal) -> bool:
    """Whether a round-trip cost is more than ``limit_percent`` of the entry commitment, compared exactly."""
    if not limit_percent.is_finite() or limit_percent < 0:
        raise ValueError(f"a structure cost limit is a percent, zero or more; got {limit_percent}")
    numerator, denominator = limit_percent.as_integer_ratio()
    return round_trip_cost_cents * 100 * denominator > numerator * entry_commitment_cents


def _require_one_entry_per_leg(structure: ResolvedStructure, by_leg: Mapping[str, object]) -> None:
    leg_ids = {leg.leg_id for leg in structure.legs}
    if set(by_leg) != leg_ids:
        raise ValueError(f"expected one entry for each of the legs {sorted(leg_ids)}; got {sorted(by_leg)}")
