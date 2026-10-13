"""What an option structure costs to enter: its premium, the broker's requirement, and what the bot sets aside.

Formula: with ``s_i`` = +1 for a bought leg and -1 for a sold one, ``r_i`` its
  ratio, ``K_i`` its strike, ``m`` the contract multiplier and ``N`` the
  structures opened:
  payoff(p)   = Σ s_i · r_i · (max(p - K_i, 0) for a call, max(K_i - p, 0) for a put)
  requirement = N · m · max(0, -min over p ≥ 0 of payoff(p))
  portfolio requirement = Σ_underlying max_expiry(requirement for that expiry's holdings)
  held requirement = Σ_multiplier portfolio requirement of the contracts with that multiplier
  incremental account requirement = (held requirement of the account's holdings
    before an entry, and of them once it has filled)
  premium     = N · m · net price,  net price = Σ s_i · r_i · price_i   (positive is paid)
  commitment  = requirement + premium + fee provision
  loss at expiry (estimate) = max(0, premium - N · m · min over p ≥ 0 of payoff(p))
  round-trip cost = N · m · Σ r_i · (ask_i - bid_i)
  payoff is piecewise linear, so its minimum over p ≥ 0 is at p = 0 or at a
  strike, unless it still falls above the highest strike, where it has none.
Reference: Alpaca, "Options Level 3 Trading", on maintenance margin and cost
  basis, https://docs.alpaca.markets/docs/options-level-3-trading (page dated
  2025-09-24, read 2026-10-06 and 2026-10-08): the requirement is the lowest point of the
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
  cases, bit-exact), tests/fixtures/test_option_portfolio_money_fixture.py
  (OSM-002: independently bounded expiries and portfolios beyond four legs,
  bit-exact), and tests/options/test_requirement.py.

Every quantity is whole cents and no step here rounds. A strike is whole
thousandths of a dollar and a price is an exact decimal; an amount that does
not come to whole cents is refused, never rounded.

The requirement leaves premiums out, as Alpaca's rule does, so it and the
commitment are different numbers by design: a $5-wide spread sold for $3.07
has a requirement of $500.00 and a commitment of $193.00.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.options.contract import OptionContract
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


def _payoff_thousandths(legs: Sequence[ResolvedLeg], price_thousandths: int) -> int:
    """The legs' exercise payoff per share at an underlying price, in thousandths of a dollar."""
    total = 0
    for leg in legs:
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
    return _payoff_thousandths(structure.legs, price_thousandths)


def _lowest_legs_payoff_thousandths(legs: Sequence[ResolvedLeg]) -> int:
    """The lowest exercise payoff of legs sharing an underlying and expiry, with no order-size limit.

    Read at zero, at every strike, and from the slope above the highest
    strike. The strikes alone are not enough: a put structure can be at its
    worst at zero, and a call structure can keep losing above its last strike.
    """
    slope_above_every_strike = sum(_sign(leg) * leg.ratio for leg in legs if leg.contract.right == "call")
    if slope_above_every_strike < 0:
        raise StructureMoneyRefused(
            StructureMoneyRefusal.LOSS_UNBOUNDED,
            "The structure sells more calls than it buys, so its loss grows without limit as the price rises.",
        )
    turning_points = {0, *(leg.contract.strike_thousandths for leg in legs)}
    return min(_payoff_thousandths(legs, price) for price in turning_points)


def _lowest_payoff_thousandths(structure: ResolvedStructure) -> int:
    """One entry's lowest payoff; sold entries must share an expiry (ADR 0076)."""
    lowest = _lowest_legs_payoff_thousandths(structure.legs)
    if structure.has_sold_leg and not structure.has_one_expiry:
        raise StructureMoneyRefused(
            StructureMoneyRefusal.MIXED_EXPIRY_WITH_SOLD_LEG,
            "The structure's legs expire on different dates and one of them is sold; "
            "the broker's requirement for that is not known, so it cannot be set aside.",
        )
    return lowest


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


def portfolio_broker_requirement(legs: Sequence[ResolvedLeg], size: EntrySize) -> int:
    """Held collateral in cents: largest expiry requirement per underlying, summed across underlyings.

    Alpaca's published portfolio rule, distinct from the shape allowed in
    one submitted order. Every expiry must be independently bounded: a long
    call expiring later cannot hide an uncovered short call. All legs have
    the recorded multiplier in ``size``; their ratios count held contracts.
    """
    by_expiry: dict[tuple[str, int], list[ResolvedLeg]] = defaultdict(list)
    for leg in legs:
        by_expiry[leg.contract.root, leg.contract.expiry_ms].append(leg)
    by_underlying: dict[str, int] = {}
    for (root, _expiry), held in by_expiry.items():
        cents = _payoff_cents(max(0, -_lowest_legs_payoff_thousandths(held)), size)
        by_underlying[root] = max(by_underlying.get(root, 0), cents)
    return sum(by_underlying.values())


# One held contract: its OCC symbol and the shares its price stands for, as its listing recorded them.
ContractAtMultiplier = tuple[str, int]


def held_requirement_cents(signed_contracts: Mapping[ContractAtMultiplier, int]) -> int:
    """The collateral of held option contracts, in whole cents: the one composition every holder of it uses.

    ``signed_contracts`` is each held contract's net whole contracts, bought
    above zero and sold below, keyed by ``(OCC symbol, multiplier)``; one
    netting to zero holds nothing. A contract is valued at its own
    multiplier, so holdings with several are worked out apart, never
    offsetting each other, and their requirements add.

    Formula: requirement = Σ_m portfolio requirement of the contracts with
      multiplier m, where portfolio requirement is
      ``portfolio_broker_requirement`` (largest expiry requirement per
      underlying, summed across underlyings).
    Reference: Alpaca, "Options Level 3 Trading"
      (https://docs.alpaca.markets/docs/options-level-3-trading, read
      2026-10-08); ADR 0076 decision 28.
    Canonical implementation: this function over ``portfolio_broker_requirement``;
      it adds no payoff arithmetic of its own.
    Validated against: tests/options/test_requirement.py and
      tests/broker/alpaca/clerk/test_option_position_money.py (the money a
      bot's lots hold), and golden fixture OSM-002 through
      ``portfolio_broker_requirement``.

    Raises ``StructureMoneyRefused`` for holdings whose loss has no limit,
    and ``ValueError`` for a symbol that is not an option contract or a
    count that is not whole contracts. Nothing is rounded or guessed.
    """
    by_multiplier: dict[int, list[ResolvedLeg]] = defaultdict(list)
    for (symbol, multiplier), held in sorted(signed_contracts.items()):
        contract = OptionContract.from_occ_symbol(symbol)
        if isinstance(held, bool) or not isinstance(held, int):
            raise ValueError(f"{symbol} is held as {held!r}: option contracts are held in whole contracts")
        if held == 0:
            continue
        by_multiplier[multiplier].append(
            ResolvedLeg(leg_id=symbol, contract=contract, position="long" if held > 0 else "short", ratio=abs(held))
        )
    return sum(
        portfolio_broker_requirement(legs, EntrySize(multiplier=multiplier, units=1))
        for multiplier, legs in by_multiplier.items()
    )


def incremental_account_requirement(
    held_before: Mapping[ContractAtMultiplier, int], held_after: Mapping[ContractAtMultiplier, int]
) -> tuple[int, int]:
    """The account's collateral before and after an entry, in whole cents: ``(before, after)``.

    Both holdings are the account's own, netted across its bots: every
    contract's signed whole contracts, as ``held_requirement_cents`` takes
    them. Alpaca values the account together, so what an entry adds to the
    account (``after - before``) can be less than the requirement of the
    entry's own legs, down to zero or below. The bot's own commitment
    (``entry_money``) is independent of the account's other holdings and
    never says that.

    Formula: before = held requirement(held_before) and after = held
      requirement(held_after). A flat account entering a first $5-wide
      spread of 100 shares is ``(0, 50_000)``.
    Reference: Alpaca, "Options Level 3 Trading"
      (https://docs.alpaca.markets/docs/options-level-3-trading, read
      2026-10-08): positions are valued together, so two spreads that need
      $1,000 apart can need $0 together; ADR 0076 decision 28.
    Canonical implementation: this function over ``held_requirement_cents``;
      it adds no arithmetic of its own.
    Validated against: tests/options/test_requirement.py (the page's
      documented pair, and a first spread on a flat account).

    Refuses what ``held_requirement_cents`` refuses, whichever of the two
    holdings it is in.
    """
    return held_requirement_cents(held_before), held_requirement_cents(held_after)


def with_contracts(
    held: Mapping[ContractAtMultiplier, int], added: Mapping[ContractAtMultiplier, int]
) -> dict[ContractAtMultiplier, int]:
    """``held`` with ``added`` filled on top: the signed counts of one contract add, they never replace.

    A contract the account already holds and an entry also takes has one
    count, the sum: replacing it would drop the account's side of it and
    misstate the requirement. The one way holdings are put together: an
    entry's contracts onto the account's, and one bot's onto another's.
    """
    after = dict(held)
    for key, count in added.items():
        after[key] = after.get(key, 0) + count
    return after


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
