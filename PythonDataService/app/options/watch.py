"""Which contracts a Clerk watches for an option plan, and which of them hold a quote line.

Quotes are held only for the contracts being watched, and a rule picks only
among those (``app.options.resolver``). Two questions are answered here, with
no feed and no clock.

``watched_window``: the contracts a plan's rules can come to while the
underlying stays near where it is. For each leg that is the expiry its rule
picks today, and the listed strikes within a set number of the strike its
rule aims at. A rule by delta aims at the money: where its target lies is not
known until quotes are held, and a window that does not reach it makes the
resolver's search incomplete, never a wrong pick.

``plan_lines``: which wanted contracts hold one of the Clerk's quote lines.
The provider grants the account a number of concurrent lines, shared by every
session under it, and no session can see the whole. So a Clerk keeps to a
budget of its own, and learns a lower ceiling when the provider refuses a
line. One contract is one line however many bots or pages want it. Priority,
highest first:

1. ``HELD``       contracts a bot holds or has a working order in. They
                  count toward the budget, are never given up for a lower
                  class, and are asked for even past it.
3. ``CANDIDATE``  the watched windows of running bots, nearest their centre
                  first.
4. ``PREVIEW``    previews and viewer pages: only what is spare.

Class 2, the pricing and underlying streams of running bots, is stock lines
and belongs to the underlying's own supervisor (ADR 0067).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import IntEnum, StrEnum
from fractions import Fraction
from types import MappingProxyType
from typing import assert_never

from app.options.contract import OptionContract, strike_thousandths
from app.options.resolver import Chain, expiry_a_rule_comes_to
from app.schemas.action_plan import (
    AbsoluteStrike,
    ActionPlan,
    AtmOffsetStrike,
    AtmStrike,
    DeltaStrike,
    LegDistanceStrike,
    OptionEntryLeg,
    PercentStrike,
)

_THOUSANDTHS_PER_DOLLAR = 1000


@dataclass(frozen=True, slots=True)
class WindowContract:
    """One contract of a plan's watched window."""

    contract: OptionContract
    # Listed strikes between it and the strike its nearest rule aims at. The farthest give way first.
    distance: int


def _option_legs(plan: ActionPlan) -> list[OptionEntryLeg]:
    return [leg for leg in plan.on_enter if isinstance(leg, OptionEntryLeg)]


def expiries_wanted(plan: ActionPlan, listed: Sequence[date], today: date) -> frozenset[date]:
    """The expiries ``plan``'s legs come to on ``today``, among the dates ``listed``: the ones whose contracts
    must be known before anything can be watched."""
    came_to = (expiry_a_rule_comes_to(leg, listed, today) for leg in _option_legs(plan))
    return frozenset(expiry for expiry in came_to if expiry is not None)


def _nearest(ladder: Sequence[int], target: Fraction) -> int:
    """Where in ``ladder`` the strike nearest ``target`` is, the lower of two as near."""
    return min(range(len(ladder)), key=lambda at: (abs(ladder[at] - target), ladder[at]))


def _aim(leg: OptionEntryLeg, ladder: Sequence[int], money: Fraction, aims: Mapping[str, int]) -> int | None:
    """Where on ``ladder`` (strikes in thousandths of a dollar) the leg's strike rule aims, counted as a place on
    it. It may lie off either end. None when the rule aims at nothing listed, or is measured from a leg that
    aims at nothing."""
    rule = leg.strike
    match rule:
        case AtmStrike() | DeltaStrike():
            return _nearest(ladder, money)
        case AtmOffsetStrike():
            return _nearest(ladder, money) + rule.offset
        case AbsoluteStrike():
            named = strike_thousandths(rule.strike)
            return ladder.index(named) if named in ladder else None
        case PercentStrike():
            return _nearest(ladder, money * (100 + Fraction(Decimal(str(rule.percent)))) / 100)
        case LegDistanceStrike():
            other = aims.get(rule.leg_id)
            if other is None:
                return None
            dollars = strike_thousandths(abs(rule.dollars)) * (-1 if rule.dollars < 0 else 1)
            return _nearest(ladder, Fraction(other + dollars))
        case _:
            assert_never(rule)


def watched_window(
    plan: ActionPlan, *, chain: Chain, money: Decimal, today: date, strikes_each_side: int
) -> tuple[WindowContract, ...]:
    """The contracts to watch for ``plan`` with the underlying at ``money``, by contract symbol.

    ``strikes_each_side`` is how many listed strikes either side of each
    leg's aim are watched: a tunable of the Clerk's.

    A leg whose expiry rule comes to nothing, or to an expiry whose contracts
    the chain does not hold yet, adds nothing: there is nothing to watch for
    it, and the resolver refuses it by its own reason. A rule that names one
    strike watches that strike alone.
    """
    if strikes_each_side < 0:
        raise ValueError(f"a window cannot reach {strikes_each_side} strikes either side")
    legs = _option_legs(plan)
    if not legs:
        return ()
    root = legs[0].instrument.underlying.upper()
    price = Fraction(money) * _THOUSANDTHS_PER_DOLLAR
    named = chain.expiry_dates
    listed = (
        named
        if named is not None
        else sorted({listing.contract.expiry_date for listing in chain.listings if listing.is_standard})
    )
    # The strike each leg aims at, in thousandths: what a leg measured from it starts from.
    aims: dict[str, int] = {}
    nearest: dict[str, WindowContract] = {}
    waiting = list(legs)
    while waiting:
        # A leg measured from another comes after it. The plan's own validation forbids a loop.
        ready = [leg for leg in waiting if not isinstance(leg.strike, LegDistanceStrike) or leg.strike.leg_id in aims]
        if not ready:
            # Measured from a leg that aims at nothing: nothing to watch for these either.
            break
        waiting = [leg for leg in waiting if leg not in ready]
        for leg in ready:
            expiry = expiry_a_rule_comes_to(leg, listed, today)
            contracts = sorted(
                (
                    listing.contract
                    for listing in chain.listings
                    if listing.is_standard
                    and listing.contract.root == root
                    and listing.contract.right == leg.right
                    and listing.contract.expiry_date == expiry
                ),
                key=lambda contract: contract.strike_thousandths,
            )
            if not contracts:
                continue
            ladder = [contract.strike_thousandths for contract in contracts]
            aim = _aim(leg, ladder, price, aims)
            if aim is None:
                continue
            # A leg measured from this one starts from the listed strike nearest its aim.
            aims[leg.leg_id] = ladder[min(max(aim, 0), len(ladder) - 1)]
            reach = 0 if isinstance(leg.strike, AbsoluteStrike) else strikes_each_side
            for at in range(max(aim - reach, 0), min(aim + reach, len(ladder) - 1) + 1):
                seen = nearest.get(contracts[at].occ_symbol)
                if seen is None or abs(at - aim) < seen.distance:
                    nearest[contracts[at].occ_symbol] = WindowContract(contracts[at], abs(at - aim))
    return tuple(nearest[symbol] for symbol in sorted(nearest))


def lines_a_plan_can_need(plan: ActionPlan, *, strikes_each_side: int) -> int:
    """The most quote lines ``plan``'s watched window can take, before any chain is known: every leg's whole
    window, as if no two legs shared a contract. What a Deploy or a Start is held to."""
    return sum(1 if isinstance(leg.strike, AbsoluteStrike) else 2 * strikes_each_side + 1 for leg in _option_legs(plan))


class LineClass(IntEnum):
    """Who wants a contract watched. A lower number is a higher priority."""

    HELD = 1
    CANDIDATE = 3
    PREVIEW = 4


@dataclass(frozen=True, slots=True)
class LineWant:
    contract: OptionContract
    line_class: LineClass
    # For a candidate or a preview: listed strikes from the centre of the window that wants it.
    distance: int = 0


class NoLine(StrEnum):
    """Why a wanted contract holds no line."""

    # The Clerk's own budget has no line left for it. The budget cannot see what other sessions use.
    OVER_BUDGET = "OPTION_LINE_OVER_BUDGET"
    # The provider refused a line while the Clerk was inside its budget: the account's allowance is in use.
    NO_CAPACITY = "OPTION_LINE_NO_CAPACITY"
    # It is to hold a line, and the chain's contracts for its expiry have not been read yet.
    LISTING_NOT_READ = "OPTION_LINE_LISTING_NOT_READ"
    # It is to hold a line and has not been asked for yet: a refused request waits to be tried again, or
    # too many were asked for at once.
    WAITING_TO_BE_ASKED_FOR = "OPTION_LINE_WAITING_TO_BE_ASKED_FOR"


@dataclass(frozen=True, slots=True)
class LinePlan:
    # Every contract that is to hold a line, highest priority first.
    lines: tuple[OptionContract, ...]
    # Of those, the ones with no line open yet, highest priority first.
    opens: tuple[OptionContract, ...]
    # Contract symbols with a line open that are to give it up.
    closes: tuple[str, ...]
    # Wanted contracts that get no line, by contract symbol.
    without: Mapping[str, NoLine]


def plan_lines(
    wants: Iterable[LineWant], *, budget: int, ceiling: int | None, open_lines: Collection[str]
) -> LinePlan:
    """Which wanted contracts hold a line.

    ``budget`` is the Clerk's own configured allowance. ``ceiling`` is the
    number of lines the provider last showed it would grant, when it has
    refused one since the last reconnect; None when it has refused none.
    ``open_lines`` are the contract symbols with a line open now.

    Held contracts always get a line, past the budget and the ceiling: the
    refusal, if there is one, must be the provider's and seen. Everything
    else shares what is left, a candidate before a preview and the nearest
    its window's centre first.
    """
    if budget < 0:
        raise ValueError(f"a line budget cannot be {budget}")
    best: dict[str, LineWant] = {}
    for want in wants:
        seen = best.get(want.contract.occ_symbol)
        if seen is None or (want.line_class, want.distance) < (seen.line_class, seen.distance):
            best[want.contract.occ_symbol] = want
    ordered = sorted(
        best.values(),
        key=lambda want: (
            want.line_class,
            want.distance,
            want.contract.expiry_date,
            want.contract.strike_thousandths,
            want.contract.right,
        ),
    )
    held = [want for want in ordered if want.line_class is LineClass.HELD]
    others = [want for want in ordered if want.line_class is not LineClass.HELD]
    granted = budget if ceiling is None else min(budget, ceiling)
    spare = max(granted - len(held), 0)
    lines = tuple(want.contract for want in (*held, *others[:spare]))
    within_budget = max(budget - len(held), 0)
    without = {
        want.contract.occ_symbol: NoLine.NO_CAPACITY if place < within_budget else NoLine.OVER_BUDGET
        for place, want in enumerate(others)
        if place >= spare
    }
    to_hold = {contract.occ_symbol for contract in lines}
    opens = tuple(contract for contract in lines if contract.occ_symbol not in open_lines)
    return LinePlan(
        lines=lines,
        opens=opens,
        closes=tuple(sorted(symbol for symbol in open_lines if symbol not in to_hold)),
        without=MappingProxyType(without),
    )
