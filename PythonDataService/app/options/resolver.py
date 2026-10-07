"""The contracts an option plan's rules come to at one entry, and the evidence for each pick.

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
rule picks the nearest contract with a usable quote. A contract with a faulty
quote is passed over; a contract no quote is held for stops the search,
because it could have been the pick:

``SEARCH_INCOMPLETE``     a contract nearer the rule's target than any usable
    one has no quote held. Never reported as "nothing close enough".
``NO_USABLE_QUOTE``       the contracts the rule may pick all have a faulty
    quote, or one the model cannot turn into a delta. Names the fault.
``NOTHING_CLOSE_ENOUGH``  the chain lists nothing within the rule's
    tolerance. For a delta rule: the usable contract nearest the target is
    farther from it than the tolerance, and the next strike toward the
    target is usable too, or is not listed.

A rule by strike knows every listed strike, so its search is incomplete only
when a listed strike within its tolerance, nearer than the pick would be, is
unwatched. A rule by delta cannot know an unwatched contract's delta, so its
search is incomplete whenever the next strike toward its target is unwatched,
even with a pick inside the tolerance.

"The money" is the close of the decision minute's pricing bar. Delta and
implied volatility come from ``app.options.american``, solved from each
candidate's mid quote against the underlying's mid quote, which was received
with the option quotes where the bar's close was not. A vendor's delta is
recorded beside ours and never used.

Time to expiry runs from the selection time to the session close of the
expiry date, so a half day and a same-day contract are counted as they are.
A product that trades past the session close on its last day is counted as
stopping with the session.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from types import MappingProxyType
from typing import Literal, assert_never

from app.lean_sidecar.trading_calendar import expected_sessions, session_close_ms_utc
from app.options.american import (
    DEFAULT_TREE_STEPS,
    AmericanIvStatus,
    CashDividend,
    american_implied_volatility,
    years_from_ms,
)
from app.options.contract import OptionContract, strike_thousandths
from app.options.expiry import option_expiry_anchor_ms, option_expiry_date
from app.options.quotes import HeldQuote, QuoteFault, quote_fault, receipt_times
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
    tree_steps: int = DEFAULT_TREE_STEPS


@dataclass(frozen=True, slots=True)
class ResolverLimits:
    # The oldest a quote side may be at the moment it is used (ADR 0067's decision-data budget).
    max_quote_age_ms: int
    # How far apart the receipt times of the legs' quotes and the underlying's may lie.
    coherence_window_ms: int
    # How long before a contract's last trade an entry on it must be selected: the entry cutoff.
    entry_cutoff_lead_ms: int


class ResolverRefusal(StrEnum):
    FEED_UNHEALTHY = "OPTION_CONTRACT_FEED_UNHEALTHY"
    # No trade in the decision minute, so there is no price to call the money.
    UNDERLYING_DID_NOT_TRADE = "OPTION_CONTRACT_UNDERLYING_DID_NOT_TRADE"
    UNDERLYING_QUOTE_UNUSABLE = "OPTION_CONTRACT_UNDERLYING_QUOTE_UNUSABLE"
    # The expiry rule names no expiry the chain lists.
    EXPIRY_NOT_LISTED = "OPTION_CONTRACT_EXPIRY_NOT_LISTED"
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
    NO_QUOTE_HELD = "no_quote_held"
    # See the candidate's ``fault``.
    QUOTE_FAULT = "quote_fault"
    # The model gave no delta; see the candidate's ``priced.status``.
    NOT_SOLVED = "not_solved"
    # Usable, but another was nearer the target or won the tie.
    NOT_THE_NEAREST = "not_the_nearest"


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
    """One watched contract a leg's rule looked at, as it stood at the selection time."""

    contract: OptionContract
    quote: HeldQuote | None
    vendor_delta: float | None
    fault: QuoteFault | None
    priced: Priced | None
    # None for the pick.
    passed_over: PassedOver | None


@dataclass(frozen=True, slots=True)
class LegEvidence:
    leg_id: str
    # None when the expiry rule named no listed expiry.
    expiry_ms: int | None
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
    evidence: Evidence


@dataclass(frozen=True, slots=True)
class Recheck:
    """The picked legs' quotes as they stood just before the order was sent."""

    recheck_ms: int
    underlying_quote: HeldQuote | None
    quotes: Mapping[str, HeldQuote | None]


class ContractsNotResolved(ValueError):
    """A typed refusal: the rules did not come to a contract for every leg, or the picks no longer stand.

    ``fault`` is the test that failed when a quote was the reason. ``recheck``
    is set when the refusal came from the recheck and not from the selection.
    """

    def __init__(
        self,
        reason: ResolverRefusal,
        leg_ids: tuple[str, ...],
        detail: str,
        *,
        fault: QuoteFault | AmericanIvStatus | None,
        evidence: Evidence,
        recheck: Recheck | None = None,
    ) -> None:
        super().__init__(detail)
        self.reason = reason
        self.leg_ids = leg_ids
        self.fault = fault
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
    ) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.leg_ids = leg_ids
        self.fault = fault


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

    def fault(self, listing: Listing, leg: OptionEntryLeg) -> QuoteFault | None:
        quote = self.held(listing)
        assert quote is not None
        return quote_fault(
            quote,
            at_ms=self.selection_ms,
            generation=self.feed.generation,
            max_age_ms=self.limits.max_quote_age_ms,
            needs_a_bid=leg.position == "short",
        )

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


def _expiry_date(leg: OptionEntryLeg, listed: Sequence[Listing], market: _Market) -> date:
    today = market.decision_date
    offered = sorted({listing.contract.expiry_date for listing in listed if listing.contract.expiry_date >= today})
    rule = leg.expiry
    match rule:
        case SameDayExpiry():
            wanted = [day for day in offered if day == today]
            meant = f"an expiry on {today}"
        case MinDteExpiry():
            wanted = [day for day in offered if (day - today).days >= rule.days]
            meant = f"an expiry {rule.days} or more days after {today}"
        case NearestWeeklyExpiry():
            wanted = [day for day in offered if day > today and _ends_its_week(day)]
            meant = f"an expiry after {today} on the last session of its week"
        case AbsoluteExpiry():
            named = option_expiry_date(rule.expiration_ms)
            wanted = [day for day in offered if day == named]
            meant = f"an expiry on {named}"
        case _:
            assert_never(rule)
    if not wanted:
        raise _NoPick(
            ResolverRefusal.EXPIRY_NOT_LISTED,
            f"leg {leg.leg_id!r} asks for {meant}; the chain lists no such {leg.right}",
            leg_ids=(leg.leg_id,),
        )
    expiry = wanted[0]
    if market.selection_ms >= session_close_ms_utc(expiry) - market.limits.entry_cutoff_lead_ms:
        raise _NoPick(
            ResolverRefusal.PAST_ENTRY_CUTOFF,
            f"leg {leg.leg_id!r} would open a {leg.right} expiring {expiry}, too close to its last trade "
            "for the entry to be seen through",
            leg_ids=(leg.leg_id,),
        )
    return expiry


def _first_usable(leg: OptionEntryLeg, nearest_first: Sequence[Listing], market: _Market) -> Listing:
    """The first of ``nearest_first`` with a usable quote. A faulty quote is passed over; no quote held stops the search."""
    for listing in nearest_first:
        if market.held(listing) is None:
            raise _NoPick(
                ResolverRefusal.SEARCH_INCOMPLETE,
                f"leg {leg.leg_id!r} could pick {_symbol(listing)}, and no quote is held for it",
                leg_ids=(leg.leg_id,),
            )
        if market.fault(listing, leg) is None:
            return listing
    first = nearest_first[0]
    fault = market.fault(first, leg)
    raise _NoPick(
        ResolverRefusal.NO_USABLE_QUOTE,
        f"leg {leg.leg_id!r} would pick {_symbol(first)}, whose quote fails as {fault}"
        + ("" if len(nearest_first) == 1 else f"; so do the {len(nearest_first) - 1} others it may pick"),
        leg_ids=(leg.leg_id,),
        fault=fault,
    )


def _the_money(strikes: Sequence[Listing], market: _Market) -> int:
    """Where in ``strikes`` the money is: the listed strike nearest the price, the lower of two as near."""
    return min(range(len(strikes)), key=lambda at: (market.from_the_money(strikes[at]), _strike(strikes[at])))


def _one_strike(leg: OptionEntryLeg, strikes: Sequence[Listing], market: _Market) -> Listing:
    """The pick of a rule that names one exact listed strike."""
    rule = leg.strike
    if isinstance(rule, AbsoluteStrike):
        named = [listing for listing in strikes if _strike(listing) == strike_thousandths(rule.strike)]
        meant = f"the {rule.strike} strike"
    else:
        offset = rule.offset if isinstance(rule, AtmOffsetStrike) else 0
        at = _the_money(strikes, market) + offset
        named = [strikes[at]] if 0 <= at < len(strikes) else []
        meant = f"the strike {offset:+d} from the money" if offset else "the strike at the money"
    if not named:
        raise _NoPick(
            ResolverRefusal.STRIKE_NOT_LISTED,
            f"leg {leg.leg_id!r} asks for {meant}; the chain lists no such {leg.right} for its expiry",
            leg_ids=(leg.leg_id,),
        )
    return _first_usable(leg, named, market)


def _nearest_strike(
    leg: OptionEntryLeg,
    strikes: Sequence[Listing],
    market: _Market,
    *,
    target: Fraction,
    tolerance: Fraction,
    tie: Callable[[Listing], Fraction | int],
    meant: str,
) -> Listing:
    """The pick of a rule that names a point on the strike ladder and how far from it a strike may be."""

    def nearness(listing: Listing) -> tuple[Fraction, Fraction | int, int]:
        return (abs(_strike(listing) - target), tie(listing), _strike(listing))

    within = sorted((listing for listing in strikes if abs(_strike(listing) - target) <= tolerance), key=nearness)
    if not within:
        raise _NoPick(
            ResolverRefusal.NOTHING_CLOSE_ENOUGH,
            f"leg {leg.leg_id!r} asks for {meant}; the chain lists no {leg.right} that close for its expiry",
            leg_ids=(leg.leg_id,),
        )
    return _first_usable(leg, within, market)


def _strike_by_delta(leg: OptionEntryLeg, rule: DeltaStrike, strikes: Sequence[Listing], market: _Market) -> Listing:
    searched = [at for at, listing in enumerate(strikes) if market.held(listing) is not None]
    if not searched:
        raise _NoPick(
            ResolverRefusal.SEARCH_INCOMPLETE,
            f"leg {leg.leg_id!r} picks by delta, and no quote is held for any {leg.right} of its expiry",
            leg_ids=(leg.leg_id,),
        )

    def unusable(at: int) -> QuoteFault | AmericanIvStatus | None:
        """Why the model has no delta for a searched contract, or ``None`` when it has one."""
        fault = market.fault(strikes[at], leg)
        if fault is not None:
            return fault
        status = market.price(strikes[at]).status
        return None if status is AmericanIvStatus.SOLVED else status

    sizes: dict[int, float] = {}
    for at in searched:
        if unusable(at) is None:
            delta = market.price(strikes[at]).delta
            assert delta is not None
            sizes[at] = abs(delta)
    if not sizes:
        nearest = min(searched, key=lambda at: (market.from_the_money(strikes[at]), _strike(strikes[at])))
        raise _NoPick(
            ResolverRefusal.NO_USABLE_QUOTE,
            f"leg {leg.leg_id!r} picks by delta, and no {leg.right} of its expiry has a quote the model can turn "
            f"into one; {_symbol(strikes[nearest])}, nearest the money, fails as {unusable(nearest)}",
            leg_ids=(leg.leg_id,),
            fault=unusable(nearest),
        )
    best = min(
        sizes, key=lambda at: (abs(sizes[at] - rule.target), market.from_the_money(strikes[at]), _strike(strikes[at]))
    )
    # A call's delta falls as the strike rises and a put's grows, so the target lies one way along the ladder.
    wants_more = rule.target > sizes[best]
    step = 0 if rule.target == sizes[best] else (-1 if wants_more == (leg.right == "call") else 1)
    toward = best + step if step and 0 <= best + step < len(strikes) else None
    if toward is not None and toward not in searched:
        raise _NoPick(
            ResolverRefusal.SEARCH_INCOMPLETE,
            f"leg {leg.leg_id!r} asks for a delta of {rule.target}; {_symbol(strikes[best])} has {sizes[best]:.4f}, and "
            f"no quote is held for {_symbol(strikes[toward])}, the next strike toward the target",
            leg_ids=(leg.leg_id,),
        )
    if abs(sizes[best] - rule.target) <= rule.tolerance_delta:
        return strikes[best]
    if toward is not None and toward not in sizes:
        raise _NoPick(
            ResolverRefusal.NO_USABLE_QUOTE,
            f"leg {leg.leg_id!r} asks for a delta within {rule.tolerance_delta} of {rule.target}; "
            f"{_symbol(strikes[best])} has {sizes[best]:.4f}, and {_symbol(strikes[toward])}, the next strike toward "
            f"the target, fails as {unusable(toward)}",
            leg_ids=(leg.leg_id,),
            fault=unusable(toward),
        )
    raise _NoPick(
        ResolverRefusal.NOTHING_CLOSE_ENOUGH,
        f"leg {leg.leg_id!r} asks for a delta within {rule.tolerance_delta} of {rule.target}; the nearest is "
        f"{_symbol(strikes[best])} at {sizes[best]:.4f}",
        leg_ids=(leg.leg_id,),
    )


def _pick(leg: OptionEntryLeg, strikes: Sequence[Listing], market: _Market, picked: Mapping[str, Listing]) -> Listing:
    rule = leg.strike
    match rule:
        case AtmStrike() | AtmOffsetStrike() | AbsoluteStrike():
            return _one_strike(leg, strikes, market)
        case DeltaStrike():
            return _strike_by_delta(leg, rule, strikes, market)
        case LegDistanceStrike():
            other = _strike(picked[rule.leg_id])
            return _nearest_strike(
                leg,
                strikes,
                market,
                target=Fraction(other + _signed_thousandths(rule.dollars)),
                tolerance=Fraction(strike_thousandths(rule.tolerance_dollars)),
                # Of two as near, the one that makes the spread narrower.
                tie=lambda listing: abs(_strike(listing) - other),
                meant=f"a strike {rule.dollars:+g} from leg {rule.leg_id!r}'s, give or take {rule.tolerance_dollars:g}",
            )
        case PercentStrike():
            percent, tolerance = Fraction(Decimal(str(rule.percent))), Fraction(Decimal(str(rule.tolerance_percent)))
            return _nearest_strike(
                leg,
                strikes,
                market,
                target=market.money * (100 + percent) / 100,
                tolerance=market.money * tolerance / 100,
                # Of two as near, the one nearer the money.
                tie=market.from_the_money,
                meant=f"a strike {rule.percent:+g}% from the price, give or take {rule.tolerance_percent:g}%",
            )
        case _:
            assert_never(rule)


def _leg_evidence(
    leg: OptionEntryLeg, of_its_kind: Sequence[Listing], market: _Market, expiry: date | None, pick: Listing | None
) -> LegEvidence:
    """Every watched contract of the leg's kind and expiry, as the leg's rule saw it."""
    seen = []
    for listing in sorted(of_its_kind, key=_strike):
        if listing.contract.expiry_date != expiry:
            continue
        watched = market.chain.watched.get(_symbol(listing))
        if watched is None:
            continue
        fault = None if watched.quote is None or not listing.is_standard else market.fault(listing, leg)
        priced = market.priced.get(_symbol(listing)) if fault is None else None
        if listing is pick:
            passed_over = None
        elif not listing.is_standard:
            passed_over = PassedOver.NOT_STANDARD
        elif watched.quote is None:
            passed_over = PassedOver.NO_QUOTE_HELD
        elif fault is not None:
            passed_over = PassedOver.QUOTE_FAULT
        elif priced is not None and priced.status is not AmericanIvStatus.SOLVED:
            passed_over = PassedOver.NOT_SOLVED
        else:
            passed_over = PassedOver.NOT_THE_NEAREST
        seen.append(
            Candidate(
                contract=listing.contract,
                quote=watched.quote,
                vendor_delta=watched.vendor_delta,
                fault=fault,
                priced=priced,
                passed_over=passed_over,
            )
        )
    return LegEvidence(
        leg_id=leg.leg_id,
        expiry_ms=None if expiry is None else option_expiry_anchor_ms(expiry),
        candidates=tuple(seen),
        picked=None if pick is None else pick.contract,
    )


def _require_usable_underlying(
    underlying: UnderlyingObservation, feed: FeedState, limits: ResolverLimits, at_ms: int
) -> HeldQuote:
    if feed.unhealthy is not None:
        raise _NoPick(ResolverRefusal.FEED_UNHEALTHY, f"the quote feed is not healthy: {feed.unhealthy}")
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
    ``app.options.structure_check``'s question.
    """
    legs = [leg for leg in plan.on_enter if isinstance(leg, OptionEntryLeg)]
    if not legs:
        raise ValueError("a plan with no option leg has no contracts to resolve")
    root = legs[0].instrument.underlying.upper()
    resolved: list[LegEvidence] = []
    market: _Market | None = None

    def evidence() -> Evidence:
        return Evidence(
            decision_bar_close_ms=decision_bar_close_ms,
            selection_ms=selection_ms,
            generation=feed.generation,
            money=underlying.pricing_bar_close,
            underlying_quote=underlying.quote,
            model_spot=None if market is None else market.spot,
            model=model,
            limits=limits,
            watched=tuple(sorted(chain.watched)),
            legs=tuple(resolved),
        )

    try:
        underlying_quote = _require_usable_underlying(underlying, feed, limits, selection_ms)
        if underlying.pricing_bar_close is None:
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
            decision_date=et_date_at_ms(decision_bar_close_ms),
            money=_thousandths(underlying.pricing_bar_close),
            spot=float(_mid(underlying_quote)),
            priced={},
        )
        picked: dict[str, Listing] = {}
        for leg in _in_reference_order(legs):
            of_its_kind = [
                listing
                for listing in chain.listings
                if listing.contract.root == root and listing.contract.right == leg.right
            ]
            expiry: date | None = None
            pick: Listing | None = None
            try:
                expiry = _expiry_date(leg, [listing for listing in of_its_kind if listing.is_standard], market)
                strikes = sorted(
                    (listing for listing in of_its_kind if listing.is_standard and listing.contract.expiry_date == expiry),
                    key=_strike,
                )
                pick = _pick(leg, strikes, market, picked)
                # Every pick is priced, by whatever rule it was picked: the evidence and a preview show its delta.
                market.price(pick)
                picked[leg.leg_id] = pick
            finally:
                resolved.append(_leg_evidence(leg, of_its_kind, market, expiry, pick))
        quotes = {leg_id: quote for leg_id, listing in picked.items() if (quote := market.held(listing)) is not None}
        _require_coherent(underlying_quote, quotes, limits)
    except _NoPick as no_pick:
        raise ContractsNotResolved(
            no_pick.reason, no_pick.leg_ids, no_pick.detail, fault=no_pick.fault, evidence=evidence()
        ) from None
    return Resolution(
        picked=MappingProxyType({leg.leg_id: picked[leg.leg_id].contract for leg in legs}),
        quotes=MappingProxyType({leg.leg_id: quotes[leg.leg_id] for leg in legs}),
        evidence=evidence(),
    )


def recheck(
    resolution: Resolution,
    plan: ActionPlan,
    *,
    chain: Chain,
    underlying: UnderlyingObservation,
    feed: FeedState,
    limits: ResolverLimits,
    recheck_ms: int,
) -> Recheck:
    """Whether the picked contracts' quotes still stand, just before the order is sent.

    Nothing is picked again. Raises ``ContractsNotResolved`` when the feed is
    unhealthy, a picked leg's quote or the underlying's is no longer usable,
    or the quotes are now too far apart; nothing is sent then.
    """
    sells = {leg.leg_id: leg.position == "short" for leg in plan.on_enter if isinstance(leg, OptionEntryLeg)}
    held = {
        leg_id: None if (watched := chain.watched.get(contract.occ_symbol)) is None else watched.quote
        for leg_id, contract in resolution.picked.items()
    }
    seen = Recheck(recheck_ms=recheck_ms, underlying_quote=underlying.quote, quotes=MappingProxyType(held))
    try:
        underlying_quote = _require_usable_underlying(underlying, feed, limits, recheck_ms)
        for leg_id, quote in held.items():
            symbol = resolution.picked[leg_id].occ_symbol
            if quote is None:
                raise _NoPick(
                    ResolverRefusal.SEARCH_INCOMPLETE,
                    f"leg {leg_id!r} picked {symbol}, and no quote is held for it now",
                    leg_ids=(leg_id,),
                )
            fault = quote_fault(
                quote,
                at_ms=recheck_ms,
                generation=feed.generation,
                max_age_ms=limits.max_quote_age_ms,
                needs_a_bid=sells[leg_id],
            )
            if fault is not None:
                raise _NoPick(
                    ResolverRefusal.NO_USABLE_QUOTE,
                    f"leg {leg_id!r} picked {symbol}, whose quote now fails as {fault}",
                    leg_ids=(leg_id,),
                    fault=fault,
                )
        _require_coherent(underlying_quote, {leg_id: quote for leg_id, quote in held.items() if quote is not None}, limits)
    except _NoPick as no_pick:
        raise ContractsNotResolved(
            no_pick.reason,
            no_pick.leg_ids,
            no_pick.detail,
            fault=no_pick.fault,
            evidence=resolution.evidence,
            recheck=seen,
        ) from None
    return seen
