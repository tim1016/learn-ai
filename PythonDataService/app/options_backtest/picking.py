"""Which option contract a leg's rules come to on a past date (#3030).

Formula: on the standard contracts of a leg's right, with ``K_1 < ... < K_n`` the strikes listed for one
expiry and ``M`` the underlying's price:
    same day         the listed expiry on the trading date
    at least N days  the nearest listed expiry N or more calendar days after the trading date
    this week        the nearest listed expiry on or after the trading date that falls on the last
                     session of its Monday-to-Sunday week, by the canonical calendar
    the money        the K_i nearest M, the lower of two as near
    steps from it    the strike ``steps`` places along the ladder from the money; off either end is none
  Strikes are whole thousandths of a dollar and the price is exact: no float decides a pick.
Reference: the rules of ``app/options/resolver.py`` (#2913), which is what bots use, and the setup's own
  statement of each rule (``app/options_backtest/setup.py``).
Canonical implementation: this file for the backtest's picks; ``app/options/resolver.py`` for bots.
Validated against: tests/options_backtest/test_picking.py, and tests/options_backtest/test_picking_parity.py,
  which runs the resolver and this file on the same inputs.

Why this file exists beside the resolver. The resolver picks a contract from live two-sided quotes and refuses
a contract without one, for every rule including "at the money". A past date has no quotes: its listing says
which contracts existed and nothing more. So a past pick is made from the listing and the underlying's price
alone, on types of its own. The rules are the same ones and the parity test keeps them so; the one place they
differ is named at ``expiry_for``. Whether a date is a session, and whether it ends its week, are read through
``sessions.py``, which the series tags read too.

Only standard contracts are picked: American style, a hundred shares. As in the resolver, the expiries and the
strike ladder come from them alone, so a non-standard contract neither is picked nor shifts a count of steps.
When no standard date meets a rule but a date listed only as non-standard contracts does, the refusal says
that, and does not say nothing is listed. A listing that may be missing contracts is refused before any rule
is read, for a pick from half a chain is a guess presented as a fact.

A pick is made in two steps because only the second depends on the price. ``prepare_leg`` settles the expiry on a
date's listing, and ``PreparedLeg.pick`` takes the strike for one underlying price. A run that picks at every minute
of a session prepares once; ``pick_contract`` is the two steps in a row and answers exactly as it did before they
were separated.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction
from typing import assert_never

from app.options.contract import OptionContract
from app.options.expiry import option_expiry_date
from app.options_backtest.market import STANDARD_MULTIPLIER, Picked, PickRefusal, PickRefused
from app.options_backtest.sessions import ends_its_week, is_session
from app.options_backtest.setup import (
    AtTheMoney,
    ExpiryRule,
    LegSetup,
    MinDaysExpiryRule,
    SameDayExpiryRule,
    StepsFromTheMoney,
    StrikeRule,
    ThisWeekExpiryRule,
)

_THOUSANDTHS_PER_DOLLAR = 1000


@dataclass(frozen=True)
class ListedContract:
    """One contract a past listing holds, with the facts of its listing that its symbol cannot say."""

    contract: OptionContract
    multiplier: int
    # American style, a multiplier of 100, delivering 100 shares of the underlying.
    standard: bool

    @property
    def is_pickable(self) -> bool:
        """A standard contract of a hundred shares, the only one a backtest trades."""
        return self.standard and self.multiplier == STANDARD_MULTIPLIER


@dataclass(frozen=True)
class Listing:
    """The contracts an underlying listed on one trading date."""

    underlying: str
    # The trading date the listing describes.
    as_of: date
    contracts: tuple[ListedContract, ...]
    # False when the source could not say it returned every contract.
    complete: bool


# ---- The expiry and the strike a rule comes to --------------------------------


def expiry_for(rule: ExpiryRule, *, listed: Collection[date], trading_date: date) -> date | None:
    """The nearest of the ``listed`` expiry dates ``rule`` means on ``trading_date``, or None when it means none.

    An expiry before the trading date has gone and is never chosen. The live resolver's ``nearest_weekly``
    is the same rule except that it is strictly after the decision's date; here today's expiry is chosen
    when it is the last of its week, as the setup says.
    """
    ahead = sorted({day for day in listed if day >= trading_date})
    match rule:
        case SameDayExpiryRule():
            wanted = (day for day in ahead if day == trading_date)
        case ThisWeekExpiryRule():
            wanted = (day for day in ahead if ends_its_week(day))
        case MinDaysExpiryRule():
            wanted = (day for day in ahead if (day - trading_date).days >= rule.days)
        case _:
            assert_never(rule)
    return next(wanted, None)


def _require_a_price(underlying_price: Decimal) -> None:
    # An int or a float is a caller's mistake as much as a zero is: a float's rounding would decide a pick.
    if not (isinstance(underlying_price, Decimal) and underlying_price.is_finite() and underlying_price > 0):
        raise ValueError(
            "the underlying's price must be a Decimal above zero to call the money from; "
            f"got {underlying_price} ({type(underlying_price).__name__})"
        )


def _the_money(ladder: Sequence[int], underlying_price: Decimal) -> int:
    """Where in the sorted ``ladder`` the money is: the strike nearest the price, the lower of two as near."""
    money = Fraction(underlying_price) * _THOUSANDTHS_PER_DOLLAR
    return min(range(len(ladder)), key=lambda at: (abs(ladder[at] - money), ladder[at]))


def strike_for(rule: StrikeRule, *, ladder_thousandths: Collection[int], underlying_price: Decimal) -> int | None:
    """The strike, in thousandths of a dollar, ``rule`` means on a ladder, or None when it is off the ladder.

    A ladder is the set of strikes listed for one expiry and right, in whatever order and with whatever
    repeats it arrives in. Steps are counted along it and not in dollars. Raises ``ValueError`` for a price
    that is not a Decimal above zero.
    """
    _require_a_price(underlying_price)
    ladder = sorted(set(ladder_thousandths))
    if not ladder:
        return None
    return _strike_on(rule, ladder, underlying_price)


def _strike_on(rule: StrikeRule, ladder: Sequence[int], underlying_price: Decimal) -> int | None:
    """The strike ``rule`` means on a ladder that is sorted, without repeats and not empty, or None off its ends."""
    match rule:
        case AtTheMoney():
            steps = 0
        case StepsFromTheMoney():
            steps = rule.steps
        case _:
            assert_never(rule)
    place = _the_money(ladder, underlying_price) + steps
    return ladder[place] if 0 <= place < len(ladder) else None


# ---- Picking ------------------------------------------------------------------


def _by_expiry(contracts: Iterable[ListedContract]) -> dict[date, dict[int, ListedContract]]:
    """The contracts grouped by expiry date, then by strike in thousandths."""
    dates: dict[int, date] = {}
    grouped: dict[date, dict[int, ListedContract]] = {}
    for listed in contracts:
        expiry_ms = listed.contract.expiry_ms
        if expiry_ms not in dates:
            dates[expiry_ms] = option_expiry_date(expiry_ms)
        grouped.setdefault(dates[expiry_ms], {}).setdefault(listed.contract.strike_thousandths, listed)
    return grouped


def _none_listed_in_words(rule: ExpiryRule, right: str, trading_date: date) -> str:
    """The sentence for a listing that holds no ``right`` expiry ``rule`` could mean on ``trading_date``."""
    match rule:
        case SameDayExpiryRule():
            return f"the listing holds no {right}s expiring on {trading_date}"
        case ThisWeekExpiryRule():
            return f"the listing holds no {right}s expiring on the last session of a week, on or after {trading_date}"
        case MinDaysExpiryRule():
            return f"the listing holds no {right}s expiring {rule.days} or more days after {trading_date}"
        case _:
            assert_never(rule)


class PreparedLeg:
    """A leg whose expiry is settled on one date's listing: only the strike still depends on the price.

    Built by ``prepare_leg``. It holds the standard contracts of the one expiry the leg's rule came to, so a pick
    at another price does not regroup the listing.
    """

    __slots__ = ("_ladder", "_strikes", "expiry", "leg")

    def __init__(self, leg: LegSetup, expiry: date, ladder: dict[int, ListedContract]) -> None:
        self.leg = leg
        self.expiry = expiry
        self._ladder = ladder
        self._strikes = sorted(ladder)

    def pick(self, underlying_price: Decimal) -> Picked | PickRefused:
        """The contract the leg's strike rule comes to with the underlying at ``underlying_price``, or why none.

        Raises ``ValueError`` for a price that is not a Decimal above zero.
        """
        _require_a_price(underlying_price)
        strike = _strike_on(self.leg.strike, self._strikes, underlying_price)
        if strike is None:
            return PickRefused(
                PickRefusal.STRIKE_NOT_LISTED,
                f"the strike rule comes off the end of the {len(self._strikes)} {self.leg.right} strikes listed "
                f"for {self.expiry} with the underlying at {underlying_price}",
            )
        chosen = self._ladder[strike]
        return Picked(contract=chosen.contract, multiplier=chosen.multiplier)


def _require_the_listings_date(listing: Listing, trading_date: date) -> None:
    if listing.as_of != trading_date:
        raise ValueError(
            f"the listing describes {listing.as_of}, not {trading_date}: a pick is made from its own date's listing"
        )


def prepare_leg(leg: LegSetup, *, listing: Listing, trading_date: date) -> PreparedLeg | PickRefused:
    """Settle the expiry ``leg``'s rule comes to in ``listing``, or say why it comes to none.

    The refusals here are the ones that do not depend on the underlying's price: an incomplete listing, no listed
    expiry the rule means, an expiry that is not a session. A strike that is off the ladder depends on the price and
    is refused by ``PreparedLeg.pick``. Raises ``ValueError`` for a listing of another date than ``trading_date``.
    """
    _require_the_listings_date(listing, trading_date)
    if not listing.complete:
        return PickRefused(
            PickRefusal.CHAIN_INCOMPLETE,
            f"the {listing.underlying} listing for {trading_date} may be missing contracts, so none is picked from it",
        )
    of_its_right = [
        listed
        for listed in listing.contracts
        if listed.contract.root == listing.underlying and listed.contract.right == leg.right
    ]
    standard = _by_expiry(listed for listed in of_its_right if listed.is_pickable)
    expiry = expiry_for(leg.expiry, listed=standard.keys(), trading_date=trading_date)
    if expiry is None:
        return _no_expiry(leg, of_its_right, standard.keys(), trading_date)
    if not is_session(expiry):
        return PickRefused(
            PickRefusal.EXPIRY_NOT_A_SESSION,
            f"the rule comes to the {leg.right}s expiring {expiry}, and the calendar has no session that day",
        )
    return PreparedLeg(leg, expiry, standard[expiry])


def pick_contract(
    leg: LegSetup, *, listing: Listing, trading_date: date, underlying_price: Decimal
) -> Picked | PickRefused:
    """The contract ``leg``'s rules come to in ``listing`` with the underlying at ``underlying_price``, or why none.

    Raises ``ValueError`` for a listing of another date than ``trading_date`` and for a price that is not a
    Decimal above zero: those are the caller's mistake and not a fact about the market.
    """
    _require_the_listings_date(listing, trading_date)
    _require_a_price(underlying_price)
    prepared = prepare_leg(leg, listing=listing, trading_date=trading_date)
    return prepared if isinstance(prepared, PickRefused) else prepared.pick(underlying_price)


def _no_expiry(
    leg: LegSetup, of_its_right: Sequence[ListedContract], standard_dates: Collection[date], trading_date: date
) -> PickRefused:
    """Why no standard expiry meets the rule: none is listed, or the one that does is listed only as non-standard."""
    others = _by_expiry(listed for listed in of_its_right if not listed.is_pickable)
    reaches = expiry_for(leg.expiry, listed=set(standard_dates) | others.keys(), trading_date=trading_date)
    if reaches is not None:
        return PickRefused(
            PickRefusal.NOT_STANDARD,
            f"the rule comes to the {leg.right}s expiring {reaches}, which are listed only as non-standard contracts",
        )
    return PickRefused(PickRefusal.EXPIRY_NOT_LISTED, _none_listed_in_words(leg.expiry, leg.right, trading_date))
