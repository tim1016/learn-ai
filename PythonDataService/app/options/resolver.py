"""The contracts an option plan's rules come to at one entry, and the evidence for each pick.

Formula: on the ladder ``K_1 < ... < K_n`` of a leg's right and expiry, with
  ``M`` the decision bar's close:
    the money         the K_i nearest M, the lower of two as near
    steps from it     the strike ``offset`` places along the ladder from the money
    percent           target = M·(100 + p)/100,   tolerance = M·t/100
    distance          target = K_other + d,       tolerance = t dollars
    a rule by strike  the usable K_i within the tolerance, nearest the target
    a rule by delta   the usable contract whose |Δ_i| is nearest the target, where
                      Δ_i is the American delta at the volatility implied by its mid
                      quote (bid + ask)/2, against the underlying's mid quote, over
                      (session close on the expiry date - selection time) / one year
  Strikes, targets and tolerances are exact: thousandths of a dollar, as
  rationals. Only the model's delta is a float.
Reference: ADR 0076 decision 8. The rules, their ties and their tolerances
  are this repo's own definitions (#2913). The delta and the implied
  volatility are ``app/options/american.py``'s, which carries their sources.
Canonical implementation: this file.
Validated against: tests/options/test_resolver.py.

One job: given a plan's option legs, the chain's definition, the quotes held,
the underlying's price and the calendar, return one contract for every leg or
one typed refusal. It reads no feed and no clock. Every fact is handed in,
each quote side with the time it was received.

Four moments are kept apart, each ``int64 ms UTC``: the decision bar's close;
the selection time, when the quotes were read for this pick; each quote
side's receipt time; and the recheck time, just before an order is sent
(``recheck``).

What a rule may pick. The strikes and expiries listed are known from the
chain's definition; quotes are held only for the contracts being watched. A
rule picks the nearest contract with a usable quote. Each contract stands one
of three ways for a leg:

- Its quote has not arrived: none is held, or one side only, or only one
  from before the last reconnect. Nothing is known of it and it could have
  been the pick, so it stops the search.
- Its quote has arrived and fails a test (not live, not a price, nothing bid
  or offered, crossed, stale), or the rule picks by delta and the model
  cannot turn the quote into one. It is passed over.
- It is usable.

``SEARCH_INCOMPLETE``     a contract that could be the pick has a quote that
    has not arrived. Never reported as "nothing close enough".
``NO_USABLE_QUOTE``       every contract the rule may pick was passed over.
    Names the first one's fault.
``NOTHING_CLOSE_ENOUGH``  the chain lists nothing within the rule's
    tolerance.

A chain may name its expiries before the contracts of each are known
(``Chain.expiries_ms``). An expiry rule chooses among every date named, and a
leg that comes to an expiry whose contracts are not known yet is incomplete
as well.

A rule by strike knows every listed strike, so its search is incomplete only
when a listed strike within its tolerance, nearer than the pick would be, has
a quote that has not arrived. A rule by delta cannot know the delta of a
contract with no quote. From the usable contract nearest its target it walks
toward the target until it meets a usable contract beyond the target, or the
last listed strike. It walks past the contracts passed over, and past a
usable one still short of the target. A contract on the way whose quote has
not arrived makes the search incomplete, even with a pick inside the
tolerance. The way to walk takes delta to fall as a call's strike rises and
to grow as a put's does; where the walk ends does not.

With no usable contract a rule by delta has nothing to walk from, and the
watched window is all it can judge by. While any watched contract's quote
has not arrived the search is incomplete, whatever has arrived faulty. Once
every watched one has arrived and been passed over it is "no usable quote",
though strikes nobody watches are listed beyond.

"The money" is the close of the decision minute's pricing bar. Delta and
implied volatility come from ``app.options.american``, solved from each
candidate's mid quote against the underlying's mid quote, which was received
with the option quotes where the bar's close was not. A vendor's delta is
recorded beside ours and never used.

Time to expiry runs from the selection time to the session close of the
expiry date, so a half day and a same-day contract are counted as they are.
A product that trades past the session close on its last day is counted as
stopping with the session.

A refusal is a fact about the market. An input that could not describe one (a
bar close that is no price above zero, a selection before its bar's close, a
recheck before its selection) is the caller's mistake and raises
``ValueError``. A quote side received after the moment it is used at is not
that: it fails as stale, and the contract is passed over.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from functools import lru_cache
from types import MappingProxyType
from typing import Literal, NoReturn, assert_never

from app.lean_sidecar.trading_calendar import expected_sessions, session_close_ms_utc
from app.options.american import (
    AmericanIvStatus,
    CashDividend,
    american_implied_volatility,
    years_from_ms,
)
from app.options.contract import OptionContract, strike_thousandths
from app.options.expiry import OptionExpiryMs, option_expiry_anchor_ms, option_expiry_date
from app.options.quotes import NOT_YET_ARRIVED, HeldQuote, QuoteFault, quote_fault, receipt_times
from app.schemas.action_plan import (
    AbsoluteExpiry,
    AbsoluteStrike,
    ActionPlan,
    AtmOffsetStrike,
    AtmStrike,
    DeltaStrike,
    LegDistanceStrike,
    MinDteExpiry,
    NearestWeeklyExpiry,
    OptionEntryLeg,
    PercentStrike,
    SameDayExpiry,
)
from app.utils.session_anchors import et_date_at_ms

DEFAULT_COHERENCE_WINDOW_MS = 2_000
_STANDARD_MULTIPLIER = 100
_STANDARD_SHARES = 100
_THOUSANDTHS_PER_DOLLAR = 1000


@dataclass(frozen=True, slots=True)
class Listing:
    """One contract the chain lists, with the facts of its listing that its symbol cannot say."""

    contract: OptionContract
    multiplier: int
    # Shares of the underlying one contract delivers.
    shares_delivered: int
    exercise: Literal["american", "european"]

    @property
    def is_standard(self) -> bool:
        return (
            self.multiplier == _STANDARD_MULTIPLIER
            and self.shares_delivered == _STANDARD_SHARES
            and self.exercise == "american"
        )


@dataclass(frozen=True, slots=True)
class WatchedContract:
    """A contract a quote line is open for: the newest quote received for it, if any has been."""

    quote: HeldQuote | None
    # The provider's own model delta, when it has sent one. Recorded beside ours, never used.
    vendor_delta: float | None


@dataclass(frozen=True, slots=True)
class Chain:
    """What the underlying lists, and the contracts quotes are held for, by contract symbol."""

    listings: tuple[Listing, ...]
    watched: Mapping[str, WatchedContract]
    # Every date the underlying lists standard contracts for, each as its expiry anchor (ADR 0022), when
    # ``listings`` holds the contracts of only some of them: a provider names a chain's expiries at once and
    # its contracts one expiry at a time. None: the listings are the whole chain, and its expiries are theirs.
    expiries_ms: tuple[OptionExpiryMs, ...] | None = None

    @property
    def expiry_dates(self) -> tuple[date, ...] | None:
        """The dates ``expiries_ms`` names, for the calendar arithmetic a rule does."""
        return None if self.expiries_ms is None else tuple(option_expiry_date(anchor) for anchor in self.expiries_ms)


@dataclass(frozen=True, slots=True)
class UnderlyingObservation:
    # The close of the decision minute's pricing bar: "the money". None when the underlying did not trade in it.
    pricing_bar_close: Decimal | None
    quote: HeldQuote | None


@dataclass(frozen=True, slots=True)
class FeedState:
    # The current subscription generation. A quote from an older one is unusable.
    generation: int
    # Why the feed is unhealthy, in its supervisor's words; None while it is healthy.
    unhealthy: str | None


@dataclass(frozen=True, slots=True)
class DividendOn:
    """One cash dividend: the open of the session the stock first trades without it, and the cash per share."""

    ex_ms: int
    cash: float


@dataclass(frozen=True, slots=True)
class ModelInputs:
    """What the pricing model is given beside the quotes. Each source is recorded with the pick."""

    rate: float
    rate_source: str
    dividends: tuple[DividendOn, ...]
    dividends_source: str
    # None lets the canonical pricer choose its default for this contract; an explicit count pins the tree.
    tree_steps: int | None = None

    def __post_init__(self) -> None:
        # What the pricer has no meaning for is refused here, as the caller's mistake. Left to reach it, a
        # delta rule would report it as a fault of every quote.
        if not math.isfinite(self.rate):
            raise ValueError(f"the model's rate must be a finite number; got {self.rate}")
        if self.tree_steps is not None and (self.tree_steps < 3 or self.tree_steps % 2 == 0):
            raise ValueError(f"the model's tree takes an odd number of steps, three or more; got {self.tree_steps}")
        for dividend in self.dividends:
            if not (math.isfinite(dividend.cash) and dividend.cash > 0):
                raise ValueError(f"a cash dividend is a finite amount above zero; got {dividend}")


@dataclass(frozen=True, slots=True, kw_only=True)
class ResolverLimits:
    # The oldest a quote side may be at the moment it is used (ADR 0067's decision-data budget).
    max_quote_age_ms: int
    # How long before a contract's last trade an entry on it must be selected: the entry cutoff.
    entry_cutoff_lead_ms: int
    # How far apart the receipt times of the legs' quotes and the underlying's may lie.
    coherence_window_ms: int = DEFAULT_COHERENCE_WINDOW_MS

    def __post_init__(self) -> None:
        # A lead below zero would put the cutoff after the contract's last trade.
        for name in ("max_quote_age_ms", "entry_cutoff_lead_ms", "coherence_window_ms"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be below zero; got {getattr(self, name)}")


class ResolverRefusal(StrEnum):
    FEED_UNHEALTHY = "OPTION_CONTRACT_FEED_UNHEALTHY"
    # The feed reconnected between the selection and the recheck: the picks stand on quotes of a generation gone.
    FEED_RECONNECTED = "OPTION_CONTRACT_FEED_RECONNECTED"
    # No trade in the decision minute, so there is no price to call the money.
    UNDERLYING_DID_NOT_TRADE = "OPTION_CONTRACT_UNDERLYING_DID_NOT_TRADE"
    UNDERLYING_QUOTE_UNUSABLE = "OPTION_CONTRACT_UNDERLYING_QUOTE_UNUSABLE"
    # The expiry rule names no expiry the chain lists.
    EXPIRY_NOT_LISTED = "OPTION_CONTRACT_EXPIRY_NOT_LISTED"
    # The chain lists the expiry on a day the calendar has no session for.
    EXPIRY_NOT_A_SESSION = "OPTION_CONTRACT_EXPIRY_NOT_A_SESSION"
    # The contract stops trading too soon for an entry on it to be seen through.
    PAST_ENTRY_CUTOFF = "OPTION_CONTRACT_PAST_ENTRY_CUTOFF"
    # A rule that names one exact strike names one the chain does not list.
    STRIKE_NOT_LISTED = "OPTION_CONTRACT_STRIKE_NOT_LISTED"
    SEARCH_INCOMPLETE = "OPTION_CONTRACT_SEARCH_INCOMPLETE"
    NO_USABLE_QUOTE = "OPTION_CONTRACT_NO_USABLE_QUOTE"
    NOTHING_CLOSE_ENOUGH = "OPTION_CONTRACT_NOTHING_CLOSE_ENOUGH"
    # The quotes were not received close enough together to describe one market.
    OBSERVATIONS_APART = "OPTION_CONTRACT_OBSERVATIONS_APART"


class PassedOver(StrEnum):
    """Why a watched contract was not the pick."""

    NOT_STANDARD = "not_standard"
    # No whole quote has arrived for it since the last reconnect. The candidate's ``fault`` says which, when one is held.
    QUOTE_NOT_ARRIVED = "quote_not_arrived"
    # See the candidate's ``fault``.
    QUOTE_FAULT = "quote_fault"
    # The rule picks by delta and the model gave none; see the candidate's ``priced.status``.
    NOT_SOLVED = "not_solved"
    # Usable, but another was nearer the target or won the tie.
    NOT_THE_NEAREST = "not_the_nearest"
    # Usable, and the leg was refused: nothing was picked for it.
    LEG_REFUSED = "leg_refused"


@dataclass(frozen=True, slots=True)
class Priced:
    """What the model was asked about one contract, and what it answered."""

    mid: float
    years_to_expiry: float
    status: AmericanIvStatus
    implied_volatility: float | None
    delta: float | None


@dataclass(frozen=True, slots=True)
class Candidate:
    """One watched contract of a leg's right and expiry, as it stood for the leg at the selection time."""

    contract: OptionContract
    quote: HeldQuote | None
    vendor_delta: float | None
    fault: QuoteFault | None
    # Set for the pick, and for every contract a rule by delta asked the model about.
    priced: Priced | None
    # None for the pick.
    passed_over: PassedOver | None


@dataclass(frozen=True, slots=True)
class LegEvidence:
    leg_id: str
    # The standard expiries listed for the leg's right from the decision's date on: what its expiry rule chose from.
    listed_expiries_ms: tuple[int, ...]
    # None when the expiry rule named none of them.
    expiry_ms: int | None
    # The standard strikes listed for that expiry, in thousandths of a dollar: the ladder its strike rule counted along.
    listed_strikes_thousandths: tuple[int, ...]
    # By strike. Empty when the leg was refused before a strike was searched for.
    candidates: tuple[Candidate, ...]
    picked: OptionContract | None


@dataclass(frozen=True, slots=True)
class Evidence:
    """Enough to reproduce a pick, or to see why there was none. Legs are in the order they were resolved."""

    decision_bar_close_ms: int
    selection_ms: int
    generation: int
    money: Decimal | None
    underlying_quote: HeldQuote | None
    # The underlying's mid quote: the price the model was given. None when the quote was unusable.
    model_spot: float | None
    model: ModelInputs
    limits: ResolverLimits
    # The watched window: every contract a quote line was open for.
    watched: tuple[str, ...]
    legs: tuple[LegEvidence, ...]


@dataclass(frozen=True, slots=True)
class Resolution:
    # The contract picked for each option leg, by leg id, in the plan's order.
    picked: Mapping[str, OptionContract]
    # The quote each pick was made on, by leg id: what the structure's price is worked out from.
    quotes: Mapping[str, HeldQuote]
    # The legs to be sold: the ones whose bid is a price something would be sold at.
    sold: frozenset[str]
    # The multiplier each pick is listed with, by leg id: what a price per share is turned into cash by.
    multipliers: Mapping[str, int]
    evidence: Evidence


@dataclass(frozen=True, slots=True)
class Recheck:
    """The picked legs' quotes as they stood just before the order was sent."""

    recheck_ms: int
    # The feed's generation at the recheck. The selection's is in the evidence.
    generation: int
    underlying_quote: HeldQuote | None
    quotes: Mapping[str, HeldQuote | None]


class ContractsNotResolved(ValueError):
    """A typed refusal: the rules did not come to a contract for every leg, or the picks no longer stand.

    ``fault`` is the test that failed when a quote was the reason, and
    ``contract`` the contract the refusal turns on when it turns on one: the
    one whose quote had not arrived, or failed. ``recheck`` is set when the
    refusal came from the recheck and not from the selection.
    """

    def __init__(
        self,
        reason: ResolverRefusal,
        leg_ids: tuple[str, ...],
        detail: str,
        *,
        fault: QuoteFault | AmericanIvStatus | None,
        contract: OptionContract | None,
        evidence: Evidence,
        recheck: Recheck | None = None,
    ) -> None:
        super().__init__(detail)
        self.reason = reason
        self.leg_ids = leg_ids
        self.fault = fault
        self.contract = contract
        self.evidence = evidence
        self.recheck = recheck


class _NoPick(Exception):
    """Raised inside one resolution; the caller adds the evidence gathered so far."""

    def __init__(
        self,
        reason: ResolverRefusal,
        detail: str,
        *,
        leg_ids: tuple[str, ...] = (),
        fault: QuoteFault | AmericanIvStatus | None = None,
        contract: OptionContract | None = None,
    ) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.leg_ids = leg_ids
        self.fault = fault
        self.contract = contract

    def as_refusal(self, evidence: Evidence, recheck: Recheck | None = None) -> ContractsNotResolved:
        return ContractsNotResolved(
            self.reason,
            self.leg_ids,
            self.detail,
            fault=self.fault,
            contract=self.contract,
            evidence=evidence,
            recheck=recheck,
        )


def _symbol(listing: Listing) -> str:
    return listing.contract.occ_symbol


def _strike(listing: Listing) -> int:
    return listing.contract.strike_thousandths


def _mid(quote: HeldQuote) -> Fraction:
    assert quote.bid is not None and quote.ask is not None
    return (Fraction(quote.bid.price) + Fraction(quote.ask.price)) / 2


def _thousandths(dollars: Decimal) -> Fraction:
    return Fraction(dollars) * _THOUSANDTHS_PER_DOLLAR


def _signed_thousandths(dollars: float) -> int:
    return strike_thousandths(abs(dollars)) * (-1 if dollars < 0 else 1)


@lru_cache(maxsize=1024)
def _ends_its_week(day: date) -> bool:
    """Whether ``day`` is the last session of its Monday-to-Sunday week."""
    monday = day - timedelta(days=day.weekday())
    sessions = expected_sessions(monday, monday + timedelta(days=6))
    return bool(sessions) and sessions[-1] == day


def _in_reference_order(legs: Sequence[OptionEntryLeg]) -> list[OptionEntryLeg]:
    """Each leg after the leg its strike is measured from. The plan's own validation forbids a loop."""
    ordered: list[OptionEntryLeg] = []
    waiting = list(legs)
    while waiting:
        placed = {leg.leg_id for leg in ordered}
        ready = [leg for leg in waiting if not isinstance(leg.strike, LegDistanceStrike) or leg.strike.leg_id in placed]
        if not ready:
            raise ValueError(f"legs {sorted(leg.leg_id for leg in waiting)} are measured from one another")
        ordered.extend(ready)
        waiting = [leg for leg in waiting if leg not in ready]
    return ordered


@dataclass(slots=True)
class _Market:
    """The facts every leg is resolved against, checked once."""

    chain: Chain
    feed: FeedState
    limits: ResolverLimits
    model: ModelInputs
    selection_ms: int
    decision_date: date
    money: Fraction
    spot: float
    priced: dict[str, Priced]

    def held(self, listing: Listing) -> HeldQuote | None:
        watched = self.chain.watched.get(_symbol(listing))
        return None if watched is None else watched.quote

    def price(self, listing: Listing) -> Priced:
        """The model's implied volatility and delta for a contract with a usable quote; solved once."""
        symbol = _symbol(listing)
        if symbol not in self.priced:
            quote = self.held(listing)
            assert quote is not None
            contract = listing.contract
            mid = float(_mid(quote))
            years = years_from_ms(session_close_ms_utc(contract.expiry_date) - self.selection_ms)
            solved = american_implied_volatility(
                price=mid,
                spot=self.spot,
                strike=float(contract.strike),
                years_to_expiry=years,
                rate=self.model.rate,
                right=contract.right,
                dividends=tuple(
                    CashDividend(years_to_ex=years_from_ms(dividend.ex_ms - self.selection_ms), cash=dividend.cash)
                    for dividend in self.model.dividends
                ),
                steps=self.model.tree_steps,
            )
            self.priced[symbol] = Priced(
                mid=mid,
                years_to_expiry=years,
                status=solved.status,
                implied_volatility=solved.volatility,
                delta=solved.delta,
            )
        return self.priced[symbol]

    def from_the_money(self, listing: Listing) -> Fraction:
        return abs(_strike(listing) - self.money)


@dataclass(frozen=True, slots=True)
class _Stood:
    """How one contract stands for one leg's rule."""

    quote: HeldQuote | None
    fault: QuoteFault | None
    priced: Priced | None
    # Why it cannot be the pick; None when it can.
    unusable: PassedOver | None

    @property
    def not_arrived(self) -> bool:
        return self.unusable is PassedOver.QUOTE_NOT_ARRIVED

    @property
    def failed_as(self) -> QuoteFault | AmericanIvStatus | None:
        """The test an unusable contract failed, for a refusal to name. None when no quote is held for it."""
        if self.fault is not None:
            return self.fault
        return None if self.priced is None else self.priced.status


@dataclass(slots=True)
class _Search:
    """One leg's look at the contracts of its right. How each stands is worked out once, here, and both the
    search and the evidence read it."""

    leg: OptionEntryLeg
    market: _Market
    stood: dict[str, _Stood] = field(default_factory=dict)

    def of(self, listing: Listing) -> _Stood:
        symbol = _symbol(listing)
        if symbol not in self.stood:
            self.stood[symbol] = self._standing(listing)
        return self.stood[symbol]

    def _standing(self, listing: Listing) -> _Stood:
        market = self.market
        quote = market.held(listing)
        if not listing.is_standard:
            return _Stood(quote, None, None, PassedOver.NOT_STANDARD)
        if quote is None:
            return _Stood(None, None, None, PassedOver.QUOTE_NOT_ARRIVED)
        fault = quote_fault(
            quote,
            at_ms=market.selection_ms,
            generation=market.feed.generation,
            max_age_ms=market.limits.max_quote_age_ms,
            needs_a_bid=self.leg.position == "short",
        )
        if fault in NOT_YET_ARRIVED:
            return _Stood(quote, fault, None, PassedOver.QUOTE_NOT_ARRIVED)
        if fault is not None:
            return _Stood(quote, fault, None, PassedOver.QUOTE_FAULT)
        if not isinstance(self.leg.strike, DeltaStrike):
            return _Stood(quote, None, None, None)
        priced = market.price(listing)
        return _Stood(quote, None, priced, None if priced.status is AmericanIvStatus.SOLVED else PassedOver.NOT_SOLVED)


def _awaited(listing: Listing, stood: _Stood) -> str:
    """What is missing of a contract whose quote has not arrived."""
    if stood.quote is None:
        return f"no quote is held for {_symbol(listing)}"
    if stood.fault is QuoteFault.OLD_GENERATION:
        return f"{_symbol(listing)} has not been quoted since the last reconnect"
    return f"only one side of {_symbol(listing)}'s quote has arrived"


def _listed_expiries(chain: Chain, standard: Sequence[Listing], today: date) -> list[date]:
    """The expiries listed from ``today`` on, soonest first."""
    named = chain.expiry_dates
    listed = named if named is not None else {listing.contract.expiry_date for listing in standard}
    return sorted({day for day in listed if day >= today})


def _expiry_date(leg: OptionEntryLeg, offered: Sequence[date], today: date) -> date:
    """The first of ``offered``, soonest first, that the leg's expiry rule means."""
    rule = leg.expiry
    match rule:
        case SameDayExpiry():
            wanted = (day for day in offered if day == today)
            meant = f"an expiry on {today}"
        case MinDteExpiry():
            wanted = (day for day in offered if (day - today).days >= rule.days)
            meant = f"an expiry {rule.days} or more days after {today}"
        case NearestWeeklyExpiry():
            wanted = (day for day in offered if day > today and _ends_its_week(day))
            meant = f"an expiry after {today} on the last session of its week"
        case AbsoluteExpiry():
            named = option_expiry_date(rule.expiration_ms)
            wanted = (day for day in offered if day == named)
            meant = f"an expiry on {named}"
        case _:
            assert_never(rule)
    expiry = next(wanted, None)
    if expiry is None:
        raise _NoPick(
            ResolverRefusal.EXPIRY_NOT_LISTED,
            f"leg {leg.leg_id!r} asks for {meant}; the chain lists no such {leg.right}",
            leg_ids=(leg.leg_id,),
        )
    return expiry


def expiry_a_rule_comes_to(leg: OptionEntryLeg, listed: Sequence[date], today: date) -> date | None:
    """The expiry ``leg``'s rule picks among the dates ``listed`` on ``today``, or None when it picks none.

    The question a resolution asks, for a caller that must know which expiry's contracts to watch before
    any quote is held.
    """
    try:
        return _expiry_date(leg, sorted({day for day in listed if day >= today}), today)
    except _NoPick:
        return None


def _require_open_long_enough(leg: OptionEntryLeg, expiry: date, market: _Market) -> None:
    """The contract trades on its expiry date, and for long enough yet for an entry on it to be seen through."""
    try:
        close_ms = session_close_ms_utc(expiry)
    except LookupError:
        raise _NoPick(
            ResolverRefusal.EXPIRY_NOT_A_SESSION,
            f"leg {leg.leg_id!r} comes to a {leg.right} expiring {expiry}, and the calendar has no session that day",
            leg_ids=(leg.leg_id,),
        ) from None
    if market.selection_ms >= close_ms - market.limits.entry_cutoff_lead_ms:
        raise _NoPick(
            ResolverRefusal.PAST_ENTRY_CUTOFF,
            f"leg {leg.leg_id!r} would open a {leg.right} expiring {expiry}, too close to its last trade "
            "for the entry to be seen through",
            leg_ids=(leg.leg_id,),
        )


def _first_usable(search: _Search, nearest_first: Sequence[Listing]) -> Listing:
    """The first of ``nearest_first`` that is usable. One passed over is skipped; one whose quote has not
    arrived stops the search."""
    leg = search.leg
    for listing in nearest_first:
        stood = search.of(listing)
        if stood.not_arrived:
            raise _NoPick(
                ResolverRefusal.SEARCH_INCOMPLETE,
                f"leg {leg.leg_id!r} could pick a contract nothing is known of yet: {_awaited(listing, stood)}",
                leg_ids=(leg.leg_id,),
                fault=stood.fault,
                contract=listing.contract,
            )
        if stood.unusable is None:
            return listing
    first = nearest_first[0]
    fault = search.of(first).failed_as
    others = len(nearest_first) - 1
    raise _NoPick(
        ResolverRefusal.NO_USABLE_QUOTE,
        f"leg {leg.leg_id!r} would pick {_symbol(first)}, whose quote fails as {fault}"
        + (f"; the {others} others it may pick have faulty quotes too" if others else ""),
        leg_ids=(leg.leg_id,),
        fault=fault,
        contract=first.contract,
    )


def _the_money(strikes: Sequence[Listing], market: _Market) -> int:
    """Where in ``strikes`` the money is: the listed strike nearest the price, the lower of two as near."""
    return min(range(len(strikes)), key=lambda at: (market.from_the_money(strikes[at]), _strike(strikes[at])))


def _the_one(search: _Search, named: Sequence[Listing], meant: str) -> Listing:
    """The pick of a rule that names one exact strike: ``named`` holds it, or nothing when it is not listed."""
    leg = search.leg
    if not named:
        raise _NoPick(
            ResolverRefusal.STRIKE_NOT_LISTED,
            f"leg {leg.leg_id!r} asks for {meant}; the chain lists no such {leg.right} for its expiry",
            leg_ids=(leg.leg_id,),
        )
    return _first_usable(search, named)


def _counted_from_the_money(search: _Search, strikes: Sequence[Listing], offset: int) -> Listing:
    at = _the_money(strikes, search.market) + offset
    return _the_one(
        search,
        [strikes[at]] if 0 <= at < len(strikes) else [],
        f"the strike {offset:+d} from the money" if offset else "the strike at the money",
    )


def _nearest_strike(
    search: _Search,
    strikes: Sequence[Listing],
    *,
    target: Fraction,
    tolerance: Fraction,
    tie: Callable[[Listing], Fraction | int],
    meant: str,
) -> Listing:
    """The pick of a rule that names a point on the strike ladder and how far from it a strike may be."""
    leg = search.leg

    def nearness(listing: Listing) -> tuple[Fraction, Fraction | int, int]:
        return (abs(_strike(listing) - target), tie(listing), _strike(listing))

    within = sorted((listing for listing in strikes if abs(_strike(listing) - target) <= tolerance), key=nearness)
    if not within:
        raise _NoPick(
            ResolverRefusal.NOTHING_CLOSE_ENOUGH,
            f"leg {leg.leg_id!r} asks for {meant}; the chain lists no {leg.right} that close for its expiry",
            leg_ids=(leg.leg_id,),
        )
    return _first_usable(search, within)


def _refuse_nothing_to_walk_from(search: _Search, strikes: Sequence[Listing]) -> NoReturn:
    """A rule by delta with no usable contract has no delta to walk from, so the watched window is all it can
    judge by: incomplete while any of it has not arrived, whatever has arrived faulty."""
    leg, market = search.leg, search.market

    def nearest_the_money(among: Sequence[Listing]) -> Listing:
        return min(among, key=lambda listing: (market.from_the_money(listing), _strike(listing)))

    awaited = [
        listing for listing in strikes if _symbol(listing) in market.chain.watched and search.of(listing).not_arrived
    ]
    if awaited:
        nearest = nearest_the_money(awaited)
        stood = search.of(nearest)
        raise _NoPick(
            ResolverRefusal.SEARCH_INCOMPLETE,
            f"leg {leg.leg_id!r} picks by delta, and no {leg.right} of its expiry has a usable quote yet: "
            f"{_awaited(nearest, stood)}",
            leg_ids=(leg.leg_id,),
            fault=stood.fault,
            contract=nearest.contract,
        )
    arrived = [listing for listing in strikes if not search.of(listing).not_arrived]
    if not arrived:
        raise _NoPick(
            ResolverRefusal.SEARCH_INCOMPLETE,
            f"leg {leg.leg_id!r} picks by delta, and no {leg.right} of its expiry is being watched",
            leg_ids=(leg.leg_id,),
        )
    nearest = nearest_the_money(arrived)
    fault = search.of(nearest).failed_as
    raise _NoPick(
        ResolverRefusal.NO_USABLE_QUOTE,
        f"leg {leg.leg_id!r} picks by delta, and every watched {leg.right} of its expiry has a quote the model "
        f"cannot turn into one; {_symbol(nearest)}, the one nearest the money, fails as {fault}",
        leg_ids=(leg.leg_id,),
        fault=fault,
        contract=nearest.contract,
    )


def _strike_by_delta(search: _Search, rule: DeltaStrike, strikes: Sequence[Listing]) -> Listing:
    leg, market = search.leg, search.market
    sizes: dict[int, float] = {}
    for at, listing in enumerate(strikes):
        stood = search.of(listing)
        if stood.unusable is None:
            assert stood.priced is not None and stood.priced.delta is not None
            sizes[at] = abs(stood.priced.delta)
    if not sizes:
        _refuse_nothing_to_walk_from(search, strikes)
    best = min(
        sizes, key=lambda at: (abs(sizes[at] - rule.target), market.from_the_money(strikes[at]), _strike(strikes[at]))
    )
    # A call's delta falls as the strike rises and a put's grows, so the target lies one way along the ladder.
    wants_more = rule.target > sizes[best]
    step = 0 if rule.target == sizes[best] else (-1 if wants_more == (leg.right == "call") else 1)
    # From the best toward the target, to the first usable contract beyond it or the ladder's end: nothing past
    # a usable contract beyond the target could be nearer. One passed over is walked past. So is a usable one
    # still short of the target, which far from the money a cent of quote can put out of its order. One whose
    # quote has not arrived could be the pick.
    passed: list[Listing] = []
    at = best + step
    while step and 0 <= at < len(strikes):
        if at in sizes:
            if (rule.target > sizes[at]) != wants_more:
                break
        else:
            stood = search.of(strikes[at])
            if stood.not_arrived:
                raise _NoPick(
                    ResolverRefusal.SEARCH_INCOMPLETE,
                    f"leg {leg.leg_id!r} asks for a delta of {rule.target}; {_symbol(strikes[best])} has "
                    f"{sizes[best]:.4f}, and toward the target {_awaited(strikes[at], stood)}",
                    leg_ids=(leg.leg_id,),
                    fault=stood.fault,
                    contract=strikes[at].contract,
                )
            passed.append(strikes[at])
        at += step
    if abs(sizes[best] - rule.target) <= rule.tolerance_delta:
        return strikes[best]
    if passed:
        fault = search.of(passed[0]).failed_as
        raise _NoPick(
            ResolverRefusal.NO_USABLE_QUOTE,
            f"leg {leg.leg_id!r} asks for a delta within {rule.tolerance_delta} of {rule.target}; "
            f"{_symbol(strikes[best])} has {sizes[best]:.4f}, and {_symbol(passed[0])}, toward the target, "
            f"fails as {fault}",
            leg_ids=(leg.leg_id,),
            fault=fault,
            contract=passed[0].contract,
        )
    raise _NoPick(
        ResolverRefusal.NOTHING_CLOSE_ENOUGH,
        f"leg {leg.leg_id!r} asks for a delta within {rule.tolerance_delta} of {rule.target}; the nearest is "
        f"{_symbol(strikes[best])} at {sizes[best]:.4f}",
        leg_ids=(leg.leg_id,),
    )


def _pick(search: _Search, strikes: Sequence[Listing], picked: Mapping[str, Listing]) -> Listing:
    market = search.market
    rule = search.leg.strike
    match rule:
        case AtmStrike():
            return _counted_from_the_money(search, strikes, 0)
        case AtmOffsetStrike():
            return _counted_from_the_money(search, strikes, rule.offset)
        case AbsoluteStrike():
            named = [listing for listing in strikes if _strike(listing) == strike_thousandths(rule.strike)]
            return _the_one(search, named, f"the {rule.strike} strike")
        case DeltaStrike():
            return _strike_by_delta(search, rule, strikes)
        case LegDistanceStrike():
            other = _strike(picked[rule.leg_id])
            return _nearest_strike(
                search,
                strikes,
                target=Fraction(other + _signed_thousandths(rule.dollars)),
                tolerance=Fraction(strike_thousandths(rule.tolerance_dollars)),
                # Of two as near, the one that makes the spread narrower.
                tie=lambda listing: abs(_strike(listing) - other),
                meant=f"a strike {rule.dollars:+g} from leg {rule.leg_id!r}'s, give or take {rule.tolerance_dollars:g}",
            )
        case PercentStrike():
            percent, tolerance = Fraction(Decimal(str(rule.percent))), Fraction(Decimal(str(rule.tolerance_percent)))
            return _nearest_strike(
                search,
                strikes,
                target=market.money * (100 + percent) / 100,
                tolerance=market.money * tolerance / 100,
                # Of two as near, the one nearer the money.
                tie=market.from_the_money,
                meant=f"a strike {rule.percent:+g}% from the price, give or take {rule.tolerance_percent:g}%",
            )
        case _:
            assert_never(rule)


def _candidates(
    search: _Search, of_its_right: Sequence[Listing], expiry: date, pick: Listing | None
) -> tuple[Candidate, ...]:
    """Every watched contract of the leg's right and expiry, as the search left it standing."""
    market = search.market
    seen = []
    not_picked = PassedOver.LEG_REFUSED if pick is None else PassedOver.NOT_THE_NEAREST
    for listing in sorted(of_its_right, key=_strike):
        watched = market.chain.watched.get(_symbol(listing))
        if watched is None or listing.contract.expiry_date != expiry:
            continue
        stood = search.of(listing)
        seen.append(
            Candidate(
                contract=listing.contract,
                quote=watched.quote,
                vendor_delta=watched.vendor_delta,
                fault=stood.fault,
                # The pick is priced by whatever rule picked it.
                priced=market.priced.get(_symbol(listing)) if listing is pick else stood.priced,
                passed_over=None if listing is pick else stood.unusable or not_picked,
            )
        )
    return tuple(seen)


def _require_healthy(feed: FeedState) -> None:
    if feed.unhealthy is not None:
        raise _NoPick(ResolverRefusal.FEED_UNHEALTHY, f"the quote feed is not healthy: {feed.unhealthy}")


def _require_usable_underlying(
    underlying: UnderlyingObservation, feed: FeedState, limits: ResolverLimits, at_ms: int
) -> HeldQuote:
    if underlying.quote is None:
        raise _NoPick(ResolverRefusal.UNDERLYING_QUOTE_UNUSABLE, "no quote is held for the underlying")
    fault = quote_fault(
        underlying.quote,
        at_ms=at_ms,
        generation=feed.generation,
        max_age_ms=limits.max_quote_age_ms,
        needs_a_bid=True,
    )
    if fault is not None:
        raise _NoPick(
            ResolverRefusal.UNDERLYING_QUOTE_UNUSABLE, f"the underlying's quote fails as {fault}", fault=fault
        )
    return underlying.quote


def _require_coherent(
    underlying_quote: HeldQuote, leg_quotes: Mapping[str, HeldQuote], limits: ResolverLimits
) -> None:
    """Every observation was received within the coherence window of every other."""
    received = [(when, None) for when in receipt_times(underlying_quote)] + [
        (when, leg_id) for leg_id, quote in leg_quotes.items() for when in receipt_times(quote)
    ]
    (earliest, first), (latest, last) = min(received, key=lambda seen: seen[0]), max(received, key=lambda seen: seen[0])
    if latest - earliest > limits.coherence_window_ms:

        def named(leg_id: str | None) -> str:
            return "the underlying's quote" if leg_id is None else f"leg {leg_id!r}'s quote"

        raise _NoPick(
            ResolverRefusal.OBSERVATIONS_APART,
            f"{named(first)} and {named(last)} were received {latest - earliest} ms apart; "
            f"the limit is {limits.coherence_window_ms}",
            leg_ids=tuple(sorted({leg_id for leg_id in (first, last) if leg_id is not None})),
        )


def resolve_contracts(
    plan: ActionPlan,
    *,
    chain: Chain,
    underlying: UnderlyingObservation,
    feed: FeedState,
    model: ModelInputs,
    limits: ResolverLimits,
    decision_bar_close_ms: int,
    selection_ms: int,
) -> Resolution:
    """The contract each option leg of ``plan`` comes to, with the evidence for each pick.

    Raises ``ContractsNotResolved`` when any leg has no pick: nothing is
    resolved in part. The picks are not yet a checked structure; that is
    ``app.options.structure_check``'s question. How long after the bar's
    close a selection may still be made is the caller's to limit.
    """
    legs = [leg for leg in plan.on_enter if isinstance(leg, OptionEntryLeg)]
    if not legs:
        raise ValueError("a plan with no option leg has no contracts to resolve")
    if selection_ms < decision_bar_close_ms:
        raise ValueError(
            f"the selection ({selection_ms}) is before the close of the bar it was decided on ({decision_bar_close_ms})"
        )
    bar_close = underlying.pricing_bar_close
    if bar_close is not None and not (bar_close.is_finite() and bar_close > 0):
        raise ValueError(f"the pricing bar's close must be a price above zero; got {bar_close}")
    root = legs[0].instrument.underlying.upper()
    today = et_date_at_ms(decision_bar_close_ms)
    resolved: list[LegEvidence] = []
    market: _Market | None = None

    def evidence() -> Evidence:
        return Evidence(
            decision_bar_close_ms=decision_bar_close_ms,
            selection_ms=selection_ms,
            generation=feed.generation,
            money=bar_close,
            underlying_quote=underlying.quote,
            model_spot=None if market is None else market.spot,
            model=model,
            limits=limits,
            watched=tuple(sorted(chain.watched)),
            legs=tuple(resolved),
        )

    try:
        _require_healthy(feed)
        underlying_quote = _require_usable_underlying(underlying, feed, limits, selection_ms)
        if bar_close is None:
            raise _NoPick(
                ResolverRefusal.UNDERLYING_DID_NOT_TRADE,
                "the underlying did not trade in the decision minute, so there is no price to call the money",
            )
        market = _Market(
            chain=chain,
            feed=feed,
            limits=limits,
            model=model,
            selection_ms=selection_ms,
            decision_date=today,
            money=_thousandths(bar_close),
            spot=float(_mid(underlying_quote)),
            priced={},
        )
        picked: dict[str, Listing] = {}
        for leg in _in_reference_order(legs):
            of_its_right = [
                listing
                for listing in chain.listings
                if listing.contract.root == root and listing.contract.right == leg.right
            ]
            standard = [listing for listing in of_its_right if listing.is_standard]
            offered = _listed_expiries(chain, standard, today)
            expiry: date | None = None
            strikes: list[Listing] = []
            candidates: tuple[Candidate, ...] = ()
            pick: Listing | None = None
            try:
                expiry = _expiry_date(leg, offered, today)
                strikes = sorted((listing for listing in standard if listing.contract.expiry_date == expiry), key=_strike)
                _require_open_long_enough(leg, expiry, market)
                if not strikes:
                    raise _NoPick(
                        ResolverRefusal.SEARCH_INCOMPLETE,
                        f"leg {leg.leg_id!r} comes to the {leg.right}s expiring {expiry}, and the chain's "
                        "contracts for that expiry are not known yet",
                        leg_ids=(leg.leg_id,),
                    )
                search = _Search(leg, market)
                try:
                    pick = _pick(search, strikes, picked)
                    # Every pick is priced, by whatever rule it was picked: the evidence and a preview show its delta.
                    market.price(pick)
                    picked[leg.leg_id] = pick
                finally:
                    candidates = _candidates(search, of_its_right, expiry, pick)
            finally:
                resolved.append(
                    LegEvidence(
                        leg_id=leg.leg_id,
                        listed_expiries_ms=tuple(option_expiry_anchor_ms(day) for day in offered),
                        expiry_ms=None if expiry is None else option_expiry_anchor_ms(expiry),
                        listed_strikes_thousandths=tuple(_strike(listing) for listing in strikes),
                        candidates=candidates,
                        picked=None if pick is None else pick.contract,
                    )
                )
        quotes = {leg_id: quote for leg_id, listing in picked.items() if (quote := market.held(listing)) is not None}
        _require_coherent(underlying_quote, quotes, limits)
    except _NoPick as no_pick:
        raise no_pick.as_refusal(evidence()) from None
    return Resolution(
        picked=MappingProxyType({leg.leg_id: picked[leg.leg_id].contract for leg in legs}),
        quotes=MappingProxyType({leg.leg_id: quotes[leg.leg_id] for leg in legs}),
        sold=frozenset(leg.leg_id for leg in legs if leg.position == "short"),
        multipliers=MappingProxyType({leg.leg_id: picked[leg.leg_id].multiplier for leg in legs}),
        evidence=evidence(),
    )


def recheck(
    resolution: Resolution,
    *,
    watched: Mapping[str, WatchedContract],
    underlying: UnderlyingObservation,
    feed: FeedState,
    recheck_ms: int,
) -> Recheck:
    """Whether the picked contracts' quotes still stand, just before the order is sent.

    Nothing is picked again. Raises ``ContractsNotResolved`` when the feed is
    unhealthy or has reconnected since the selection (ADR 0067: a new
    generation is refused even when it is healthy), a picked contract has
    come inside its entry cutoff, a picked leg's quote or the underlying's
    is no longer usable, or the quotes are now too far apart; nothing is
    sent then. The limits are the ones the selection was
    made under. What the structure now costs, and how wide its legs' markets
    are, is not asked here.
    """
    selected = resolution.evidence
    limits = selected.limits
    if recheck_ms < selected.selection_ms:
        raise ValueError(f"the recheck ({recheck_ms}) is before the selection it rechecks ({selected.selection_ms})")
    held = {
        leg_id: None if (now := watched.get(contract.occ_symbol)) is None else now.quote
        for leg_id, contract in resolution.picked.items()
    }
    seen = Recheck(
        recheck_ms=recheck_ms,
        generation=feed.generation,
        underlying_quote=underlying.quote,
        quotes=MappingProxyType(held),
    )
    try:
        _require_healthy(feed)
        if feed.generation != selected.generation:
            raise _NoPick(
                ResolverRefusal.FEED_RECONNECTED,
                "the quote feed has reconnected since the contracts were picked; they stand on quotes of a "
                "generation that is gone",
            )
        underlying_quote = _require_usable_underlying(underlying, feed, limits, recheck_ms)
        for leg_id, contract in resolution.picked.items():
            # The selection was outside the cutoff. The order is sent now, and now may not be.
            if recheck_ms >= session_close_ms_utc(contract.expiry_date) - limits.entry_cutoff_lead_ms:
                raise _NoPick(
                    ResolverRefusal.PAST_ENTRY_CUTOFF,
                    f"leg {leg_id!r} picked {contract.occ_symbol}, which is now too close to its last trade "
                    "for the entry to be seen through",
                    leg_ids=(leg_id,),
                    contract=contract,
                )
        for leg_id, quote in held.items():
            contract = resolution.picked[leg_id]
            fault = (
                None
                if quote is None
                else quote_fault(
                    quote,
                    at_ms=recheck_ms,
                    generation=feed.generation,
                    max_age_ms=limits.max_quote_age_ms,
                    needs_a_bid=leg_id in resolution.sold,
                )
            )
            if quote is None or fault is not None:
                raise _NoPick(
                    ResolverRefusal.NO_USABLE_QUOTE,
                    f"leg {leg_id!r} picked {contract.occ_symbol}, "
                    + ("and no quote is held for it now" if quote is None else f"whose quote now fails as {fault}"),
                    leg_ids=(leg_id,),
                    fault=fault,
                    contract=contract,
                )
        _require_coherent(underlying_quote, {leg_id: quote for leg_id, quote in held.items() if quote is not None}, limits)
    except _NoPick as no_pick:
        raise no_pick.as_refusal(selected, seen) from None
    return seen
