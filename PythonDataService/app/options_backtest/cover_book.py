"""The cover book: what an options backtest holds, what covers it and what it pledges (#3104).

Formula: with ``q_i`` the signed contracts held in contract ``i`` (bought positive, sold negative), a *kind*
    ``c`` = (underlying, call or put, actual expiry), ``K_i`` a strike and ``m`` the contract multiplier:
    covered       for every kind c:  Σ_{i in c} q_i >= 0            (one bought contract covers one sold one)
    collateral    = Σ_underlying max_expiry  m · max(0, − min_{p>=0} Σ_i q_i · exercise_i(p))
    may_leave(b)  = covered(held without b)  and  collateral(held without b) <= collateral(held)
    obligation    = Σ_{q_i<0} |q_i| · K_i · m
    opening group = other side, then second expiry, then cover, then cash, and the cash step is
                    cash + Σ cash effects >= collateral(held plus the group), all of the group or none
  where ``exercise_i(p)`` is ``max(p − K_i, 0)`` for a call and ``max(K_i − p, 0)`` for a put.
Reference: ADR 0076 decision 3 (the requirement is the lowest point of the summed exercise payoff, premiums left
  out) and decision 28 (the portfolio form, largest independently bounded expiry per underlying), from Alpaca,
  "Options Level 3 Trading", https://docs.alpaca.markets/docs/options-level-3-trading, as cited by
  ``app/options/requirement.py``. What covers what, one for one and on the actual expiry, is the owner's rule in
  #3095: you cannot sell something you did not buy.
Canonical implementation: this file for cover over holdings through time: what is held, what a sale needs and
  the order an opening group is judged in. ``app/options/structure_check.py`` is canonical for cover within one
  resolved structure, and the parity test holds the two to one answer. ``app/options/requirement.py`` is
  canonical for the requirement, which is called here and not copied, and ``app/options/event_allocation.py``
  (``assignment_cash_cents``) for the cash an assignment moves, which the obligation sums and the account's
  picture states in cents from the same kernel.
Validated against: tests/options_backtest/test_cover_book.py (scenes worked by hand from the rules) and
  tests/options_backtest/test_cover_book_parity.py (the bots' cover check, ``require_sold_legs_covered``, on every
  case both express; the canonical requirement and an independent exhaustive scan of the payoff, to the cent),
  and tests/broker/alpaca/clerk/test_account_picture.py (the obligation, held to the account's picture over the
  same holdings).

Why this file exists. The replay decides what happens when. Every question about what is held, whether a sale is
covered, what the holdings pledge and which legs must go with a leaving one is asked here, so the replay never
counts contracts, compares expiries or works out a requirement itself. This module reads no market, no clock and
no replay state, and every figure is an exact ``Decimal``.

What a contract is. Holdings are counted by contract: two legs on one contract on the same side add up, and a leg
may not hold the contract another leg holds on the other side. Cover is an identity on contracts that were
picked: underlying, call or put, actual expiry and the standard hundred-share deliverable must all match, and the
strike does not matter. Two legs whose expiry rules agree may still have come to different dates, so a rule's
wording is never evidence of cover. While anything is sold, everything held shares one actual expiry.

The bots' cover check (``app/options/structure_check.py``) lets a bought contract that expires later cover a sold
one. This book does not: the two agree wherever one expiry is held, which is the only case the money path admits
for a sold leg (ADR 0076 decision 4). Only that case is held to parity.

The requirement is asked of the canonical portfolio function over every contract held, however many legs that is.
The four-leg shape of one submitted order is not used and its limit is not raised, and holdings are never split
into groups whose requirements are summed. A loss with no limit is a broken invariant of this module and raises
``CoverInvariantBroken``: it is never estimated.

Minute bars cannot show which of two contracts printed first inside a minute. Judging an opening group as one
simulated atomic group, bought legs standing beside the sold legs they cover, is a modelling choice and not
observed coverage. Which sold legs close with a leaving bought leg follows the listed order of the setup: it is a
deterministic policy, it is not minimal, and it can close more legs than another policy would.

Only the standard contract is traded here (``STANDARD_MULTIPLIER``). A candidate states the multiplier of the
contract that was picked so that the obligation is worked from what was picked, and any other multiplier is a
caller's mistake and is refused.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from functools import partial
from typing import Literal

from app.options.contract import OptionContract
from app.options.event_allocation import assignment_cash_cents
from app.options.exact import exact_sum, require_everyday
from app.options.requirement import EntrySize, StructureMoneyRefused, portfolio_broker_requirement
from app.options.structure import ResolvedLeg
from app.options_backtest.fills import EXACT
from app.options_backtest.market import STANDARD_MULTIPLIER

LegSide = Literal["buy", "sell"]

# Whole cents, the unit the canonical requirement and an assignment's cash are held in.
_DOLLARS_AS_CENTS = -2

# Every held contract is one structure's worth of the recorded multiplier: ratios are the contracts held.
_HELD_SIZE = EntrySize(multiplier=STANDARD_MULTIPLIER, units=1)

# A kind of contract for cover: underlying, call or put, actual expiry.
_Kind = tuple[str, str, int]


class OpeningRefusal(StrEnum):
    """Why an opening fill is not taken. Each refused candidate has exactly one: the first step that refused it."""

    # Another leg already holds this contract, or stands to, on the other side.
    OTHER_SIDE = "other_side"
    # Its expiry differs from one already in play, and a sold contract is in play or it is itself sold.
    SECOND_EXPIRY = "second_expiry"
    # A sold candidate with no bought contract left to cover it.
    NO_COVER = "no_cover"
    # The group, applied together, would pledge more collateral than the cash can carry.
    CASH_SHORT = "cash_short"


class CoverInvariantBroken(RuntimeError):
    """What is held has a loss with no limit: a fault of this module or of its caller, never an answer."""


@dataclass(frozen=True)
class Holding:
    """What one leg holds: a number of one contract, bought or sold."""

    leg_id: str
    contract: OptionContract
    side: LegSide
    contracts: int
    multiplier: int

    def __post_init__(self) -> None:
        if not isinstance(self.leg_id, str) or not self.leg_id:
            raise ValueError(f"a leg is named by a non-empty id; got {self.leg_id!r}")
        if self.side not in ("buy", "sell"):
            raise ValueError(f"leg {self.leg_id!r}: a side is 'buy' or 'sell'; got {self.side!r}")
        if isinstance(self.contracts, bool) or not isinstance(self.contracts, int) or self.contracts < 1:
            raise ValueError(f"leg {self.leg_id!r}: contracts are a whole number, one or more; got {self.contracts!r}")
        if self.multiplier != STANDARD_MULTIPLIER:
            raise ValueError(
                f"leg {self.leg_id!r}: only the standard {STANDARD_MULTIPLIER}-share contract is traded; "
                f"got a multiplier of {self.multiplier!r}"
            )

    @property
    def signed_contracts(self) -> int:
        """Positive when bought, negative when sold."""
        return self.contracts if self.side == "buy" else -self.contracts


@dataclass(frozen=True)
class Candidate(Holding):
    """An opening fill that is offered: what a leg would hold, and the cash its fill would move.

    ``cash_effect`` is signed and fees are in it: a buy takes cash out (so it is not above zero), a sale brings
    cash in. A sale's can still be below zero, when its fee is more than its value.
    """

    cash_effect: Decimal

    def __post_init__(self) -> None:
        Holding.__post_init__(self)
        require_everyday(self.cash_effect, what=f"the cash effect of leg {self.leg_id!r}")
        if self.side == "buy" and self.cash_effect > 0:
            raise ValueError(f"leg {self.leg_id!r}: a buy takes cash out; got a cash effect of {self.cash_effect}")


@dataclass(frozen=True)
class Held:
    """What is held, as a signed count per contract, with which leg holds what, in the setup's listed order.

    A leg holds at most one contract, and no contract is held on both sides. Whether what is held is covered is
    not enforced here: it is what ``may_leave`` and ``leaving_plan`` are asked.
    """

    # The setup's leg ids, in the order the setup lists them.
    listed: tuple[str, ...]
    holdings: tuple[Holding, ...] = ()

    def __post_init__(self) -> None:
        listed = tuple(self.listed)
        holdings = tuple(self.holdings)
        if len(set(listed)) != len(listed) or not all(isinstance(leg, str) and leg for leg in listed):
            raise ValueError(f"a setup lists each leg once, by a non-empty id; got {listed!r}")
        position = {leg_id: place for place, leg_id in enumerate(listed)}
        holders = [holding.leg_id for holding in holdings]
        if len(set(holders)) != len(holders):
            raise ValueError(f"a leg holds one contract; these hold more: {sorted(holders)}")
        if unlisted := sorted(set(holders) - position.keys()):
            raise ValueError(f"legs {unlisted} are not among the setup's legs {list(listed)}")
        sides: dict[OptionContract, LegSide] = {}
        for holding in holdings:
            if sides.setdefault(holding.contract, holding.side) != holding.side:
                raise ValueError(f"{holding.contract.occ_symbol} is held on both sides")
        # Normalised to tuples in listed order, so that two values that hold the same things are equal.
        object.__setattr__(self, "listed", listed)
        object.__setattr__(self, "holdings", tuple(sorted(holdings, key=lambda held: position[held.leg_id])))

    @classmethod
    def empty(cls, listed_leg_ids: Iterable[str]) -> Held:
        """Nothing held, by a setup that lists these legs in this order."""
        return cls(listed=tuple(listed_leg_ids))

    def holding_of(self, leg_id: str) -> Holding | None:
        """What ``leg_id`` holds, or None when it holds nothing."""
        return next((holding for holding in self.holdings if holding.leg_id == leg_id), None)

    def opened(self, taken: Iterable[Candidate]) -> Held:
        """What is held once ``taken`` have filled. Refuses a leg that already holds, and a contract held both ways."""
        opened = tuple(Holding(c.leg_id, c.contract, c.side, c.contracts, c.multiplier) for c in taken)
        return Held(self.listed, (*self.holdings, *opened))

    def without(self, leg_ids: Iterable[str]) -> Held:
        """What is held once these legs are gone. Refuses a leg that holds nothing."""
        gone = set(leg_ids)
        if not_held := sorted(gone - {holding.leg_id for holding in self.holdings}):
            raise ValueError(f"legs {not_held} hold nothing, so they cannot leave")
        return Held(self.listed, tuple(holding for holding in self.holdings if holding.leg_id not in gone))


@dataclass(frozen=True)
class Refused:
    """One opening fill that was not taken, and why."""

    candidate: Candidate
    reason: OpeningRefusal


@dataclass(frozen=True)
class Admission:
    """The judgment of one minute's opening fills: those taken and those refused, each in listed order."""

    taken: tuple[Candidate, ...]
    refused: tuple[Refused, ...]


def _kind(contract: OptionContract) -> _Kind:
    return (contract.root, contract.right, contract.expiry_ms)


def _signed_by_contract(holdings: Iterable[Holding]) -> dict[OptionContract, int]:
    signed: dict[OptionContract, int] = defaultdict(int)
    for holding in holdings:
        signed[holding.contract] += holding.signed_contracts
    return dict(signed)


def _requirement_cents(holdings: Iterable[Holding]) -> int:
    """The canonical portfolio requirement of the contracts held, in whole cents."""
    legs = tuple(
        ResolvedLeg(
            leg_id=contract.occ_symbol,
            contract=contract,
            position="long" if signed > 0 else "short",
            ratio=abs(signed),
        )
        for contract, signed in _signed_by_contract(holdings).items()
    )
    try:
        return portfolio_broker_requirement(legs, _HELD_SIZE)
    except StructureMoneyRefused as refused:
        raise CoverInvariantBroken(f"what is held has a loss with no limit: {refused}") from refused


def _dollars(cents: int) -> Decimal:
    """Whole cents as dollars, exactly."""
    return Decimal(cents).scaleb(_DOLLARS_AS_CENTS, context=EXACT)


def _covered(holdings: Iterable[Holding]) -> bool:
    """Whether, in every kind, the bought contracts are at least the sold ones."""
    net: dict[_Kind, int] = defaultdict(int)
    for holding in holdings:
        net[_kind(holding.contract)] += holding.signed_contracts
    return all(count >= 0 for count in net.values())


def _stands(remaining: Sequence[Holding], *, before: int) -> bool:
    """Whether what remains is covered and pledges no more than ``before`` cents.

    The one test ``may_leave`` and ``leaving_plan`` both apply, so the two cannot disagree.
    """
    return _covered(remaining) and _requirement_cents(remaining) <= before


def _bought_leg(held: Held, leg_id: str, *, asked: str) -> Holding:
    holding = held.holding_of(leg_id)
    if holding is None or holding.side != "buy":
        raise ValueError(f"{asked} is asked of bought legs that are held; leg {leg_id!r} is not one")
    return holding


# ---- Cover ----------------------------------------------------------------------


def covers(bought: OptionContract, sold: OptionContract) -> bool:
    """Whether one bought contract can cover one sold contract: the same underlying, call or put, and actual expiry.

    Cover is an identity on those three, so the strikes do not matter and neither does which is which. The
    standard hundred-share deliverable that cover also needs is held by ``Holding``, which refuses any other
    multiplier. One bought contract covers one sold contract and never two: ``is_covered`` counts it so.
    """
    return _kind(bought) == _kind(sold)


def is_covered(held: Held) -> bool:
    """Whether every sold contract held has a bought contract of its own to cover it.

    In each underlying, call or put and actual expiry, the bought contracts held are at least the sold ones.
    """
    return _covered(held.holdings)


# ---- Collateral and obligation --------------------------------------------------


def collateral(held: Held) -> Decimal:
    """The requirement of what is held, in dollars: the canonical portfolio requirement, exactly.

    Raises ``CoverInvariantBroken`` when what is held has a loss with no limit.
    """
    return _dollars(_requirement_cents(held.holdings))


def assignment_obligation(held: Held) -> Decimal:
    """Sold contracts times strike times multiplier, in dollars. Gross: not a loss, and nothing is netted.

    The cash of one assignment is ``assignment_cash_cents``, which the account's picture states in cents from the
    same kernel (``app/broker/alpaca/clerk/account_picture.py``); each converts the unit at its own edge.
    """
    return _dollars(sum(
        assignment_cash_cents(holding.contract, contracts=holding.contracts, multiplier=holding.multiplier)
        for holding in held.holdings
        if holding.side == "sell"
    ))


# ---- Protection -----------------------------------------------------------------


def may_leave(held: Held, leg_id: str) -> bool:
    """Whether one bought leg can be sold now: with it gone, every sold contract is still covered and the
    collateral is no larger than it is now. Reads no cash.

    Raises ``ValueError`` for a leg that is not a bought leg that is held, and ``CoverInvariantBroken`` when what
    is held has a loss with no limit.
    """
    _bought_leg(held, leg_id, asked="may_leave")
    remaining = tuple(holding for holding in held.holdings if holding.leg_id != leg_id)
    return _stands(remaining, before=_requirement_cents(held.holdings))


def leaving_plan(held: Held, leaving: Iterable[str]) -> tuple[str, ...]:
    """The sold legs that must close with the bought legs that are leaving, in the order to close them.

    Last listed first, the legs of the same kind and expiry as a leaving leg before any other, until what remains
    is covered and its collateral is no larger than before. It always ends, because holdings with no sold leg
    need no collateral. The order is a policy by listed order: it is not minimal and can close more than another
    would. A leaving leg that protects nothing sold gives an empty plan, and so does nothing leaving from holdings
    that are covered.

    Raises ``ValueError`` for a leaving leg that is not a bought leg that is held.
    """
    leaving_ids = set(leaving)
    leaving_kinds = {_kind(_bought_leg(held, leg_id, asked="leaving_plan").contract) for leg_id in leaving_ids}
    before = _requirement_cents(held.holdings)
    remaining = [holding for holding in held.holdings if holding.leg_id not in leaving_ids]
    listed_at = {leg_id: place for place, leg_id in enumerate(held.listed)}
    sold = sorted(
        (holding for holding in remaining if holding.side == "sell"),
        key=lambda holding: (_kind(holding.contract) not in leaving_kinds, -listed_at[holding.leg_id]),
    )
    plan: list[str] = []
    for holding in sold:
        if _stands(remaining, before=before):
            break
        remaining.remove(holding)
        plan.append(holding.leg_id)
    return tuple(plan)


# ---- The opening group ----------------------------------------------------------

_Step = Callable[[list[Candidate]], tuple[list[Candidate], dict[str, OpeningRefusal]]]


def _other_side(held: Held, candidates: list[Candidate]) -> tuple[list[Candidate], dict[str, OpeningRefusal]]:
    """A candidate in a contract held, or standing to be held earlier in the group, on the other side is refused."""
    side_of: dict[OptionContract, LegSide] = {holding.contract: holding.side for holding in held.holdings}
    standing: list[Candidate] = []
    refused: dict[str, OpeningRefusal] = {}
    for candidate in candidates:
        if side_of.setdefault(candidate.contract, candidate.side) == candidate.side:
            standing.append(candidate)
        else:
            refused[candidate.leg_id] = OpeningRefusal.OTHER_SIDE
    return standing, refused


def _second_expiry(held: Held, candidates: list[Candidate]) -> tuple[list[Candidate], dict[str, OpeningRefusal]]:
    """A candidate whose expiry differs from one in play is refused when a sold contract is in play or it is sold.

    Bought-only holdings may span expiries. What is in play is what is held and what has stood so far in this
    step, so a sold candidate that the cover step goes on to refuse is still in play here: it can be the reason a
    bought candidate of another expiry is refused, and its own later refusal does not undo that.
    """
    expiries = {holding.contract.expiry_ms for holding in held.holdings}
    sold_in_play = any(holding.side == "sell" for holding in held.holdings)
    standing: list[Candidate] = []
    refused: dict[str, OpeningRefusal] = {}
    for candidate in candidates:
        expiry = candidate.contract.expiry_ms
        differs = any(in_play != expiry for in_play in expiries)
        if differs and (sold_in_play or candidate.side == "sell"):
            refused[candidate.leg_id] = OpeningRefusal.SECOND_EXPIRY
            continue
        expiries.add(expiry)
        sold_in_play = sold_in_play or candidate.side == "sell"
        standing.append(candidate)
    return standing, refused


def _without_cover(
    held: Held, closing: frozenset[str], candidates: list[Candidate]
) -> tuple[list[Candidate], dict[str, OpeningRefusal]]:
    """Sold candidates take cover in listed order. A sold candidate that cannot be covered whole is refused.

    Cover is the bought contracts held and not closing, plus the bought candidates still standing in this
    group, less every sold contract already held or already covered.
    """
    cover: dict[_Kind, int] = defaultdict(int)
    for holding in held.holdings:
        if holding.side == "sell" or holding.leg_id not in closing:
            cover[_kind(holding.contract)] += holding.signed_contracts
    for candidate in candidates:
        if candidate.side == "buy":
            cover[_kind(candidate.contract)] += candidate.contracts
    standing: list[Candidate] = []
    refused: dict[str, OpeningRefusal] = {}
    for candidate in candidates:
        if candidate.side == "sell":
            kind = _kind(candidate.contract)
            if cover[kind] < candidate.contracts:
                refused[candidate.leg_id] = OpeningRefusal.NO_COVER
                continue
            cover[kind] -= candidate.contracts
        standing.append(candidate)
    return standing, refused


def _short_of_cash(
    held: Held, cash: Decimal, candidates: list[Candidate]
) -> tuple[list[Candidate], dict[str, OpeningRefusal]]:
    """The candidates are applied together: when the cash after them is below the collateral then held, all go."""
    if not candidates:
        return [], {}
    after = exact_sum([(1, cash), *((1, candidate.cash_effect) for candidate in candidates)])
    pledged = _dollars(_requirement_cents((*held.holdings, *candidates)))
    if after < pledged:
        return [], {candidate.leg_id: OpeningRefusal.CASH_SHORT for candidate in candidates}
    return candidates, {}


def admit_opening(held: Held, candidates: Iterable[Candidate], *, cash: Decimal | None, closing: Iterable[str]) -> Admission:
    """Judge the opening fills of one minute together, in four steps, over the candidates in listed order.

    1. Other side: a candidate in a contract another leg holds on the other side is refused.
    2. Second expiry: refused when its expiry differs from one in play and a sold contract is in play or it is
       itself sold.
    3. Cover: sold candidates take cover in listed order, counting bought contracts held and not in ``closing``
       plus bought candidates still standing in this group. One bought contract covers one sold one, so a sold
       contract already held, or covered earlier in this group, has used its own. A sold candidate that cannot be
       covered whole is refused, and takes none.
    4. Cash: the candidates still standing are applied together. When ``cash`` plus their cash effects is less
       than the collateral of what would then be held, every one of them is refused.

    Each step judges what the one before left standing, and each candidate is refused by the first step that
    refuses it. So a refusal at a later step frees nothing an earlier step decided: a sold candidate refused for
    cover was still in play for the second-expiry step. ``cash`` is the cash on hand before the group, and
    ``closing`` names the legs whose close is pending. They are still held and still pledged, and their fills
    come after the opening group: a bought leg in ``closing`` is not cover for a sale to open, and a sold leg in
    ``closing`` keeps using the cover it has until its buy-back fills. For a group with no sold candidate and
    nothing sold held, the last step is exactly "refused when the cost is more than the cash".

    ``cash=None`` asks only the three structural questions: a live Clerk
    then admits cash through its revision-bound deployment budget authority.
    Every research replay supplies its actual cash and asks all four.

    Raises ``ValueError`` for a candidate of a leg the setup does not list, of a leg that already holds, or
    named twice, and for a pending close of a leg that holds nothing.
    """
    listed_at = {leg_id: place for place, leg_id in enumerate(held.listed)}
    ordered = sorted(candidates, key=lambda candidate: listed_at.get(candidate.leg_id, len(listed_at)))
    ids = [candidate.leg_id for candidate in ordered]
    if unlisted := sorted(set(ids) - listed_at.keys()):
        raise ValueError(f"legs {unlisted} are not among the setup's legs {list(held.listed)}")
    if repeated := sorted(leg_id for leg_id, count in Counter(ids).items() if count > 1):
        raise ValueError(f"a leg opens once in a group; these repeat: {repeated}")
    holding_ids = {holding.leg_id for holding in held.holdings}
    if already := sorted(set(ids) & holding_ids):
        raise ValueError(f"legs {already} already hold a contract, so they cannot open another")
    closing_ids = frozenset(closing)
    if not_held := sorted(closing_ids - holding_ids):
        raise ValueError(f"legs {not_held} hold nothing, so no close of theirs can be pending")
    on_hand = None if cash is None else require_everyday(cash, what="the cash an opening group is judged against")

    steps: tuple[_Step, ...] = (
        partial(_other_side, held),
        partial(_second_expiry, held),
        partial(_without_cover, held, closing_ids),
        *((partial(_short_of_cash, held, on_hand),) if on_hand is not None else ()),
    )
    standing = ordered
    reasons: dict[str, OpeningRefusal] = {}
    for step in steps:
        standing, refused_here = step(standing)
        reasons.update(refused_here)
    return Admission(
        taken=tuple(standing),
        refused=tuple(
            Refused(candidate, reasons[candidate.leg_id]) for candidate in ordered if candidate.leg_id in reasons
        ),
    )
