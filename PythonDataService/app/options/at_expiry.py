"""What a resolved structure comes to at expiry: the most it gains, where its lowest and highest results
hold, and the prices it breaks even at.

Formula: with ``payoff(p)`` the structure's exercise payoff per share at an
  underlying price ``p ≥ 0``, ``c`` the net price per share (positive is
  paid), ``m`` the contract multiplier and ``N`` the structures opened:
    result(p)        = N · m · (payoff(p) - c)
    gain at expiry   = max(0, N · m · (max over p ≥ 0 of payoff(p) - c)),
                       and without limit when payoff still rises above the highest strike
    the lowest holds   on every p with payoff(p) = min over p ≥ 0 of payoff
    the highest holds  on every p with payoff(p) = max over p ≥ 0 of payoff
    break-even       every p with payoff(p) = c on a stretch where payoff is not level
  payoff is piecewise linear and turns only at zero and at the strikes, so
  each of these is read at those turning points and from the slope above the
  highest strike.
Reference: ADR 0076 decision 3 for the payoff and where its lowest point is
  found. The gain, the ranges and the break-evens are this repo's own
  definitions (#2913): the mirror of its loss-at-expiry estimate, under the
  same assumptions.
Canonical implementation: this file. The payoff is
  ``app/options/requirement.py``'s, and nothing here states it again.
Validated against: tests/options/test_at_expiry.py (hand-derived cases, exact).

Every figure assumes what the loss-at-expiry estimate assumes: every leg has
one expiry, is held to it, is never exercised or assigned early, and settles
at its exercise value, before fees. None of them is a bound on what a market
exit before expiry can bring. A structure whose legs do not share an expiry
has no such figures, and the answer is ``None``: not estimated.

Money is whole cents and no step rounds. A break-even is an exact fraction of
a dollar, because a leg ratio of three puts one where no cent is.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

from app.options.exact import exact_sum, require_everyday, whole_units
from app.options.requirement import (
    EntrySize,
    StructureMoneyRefusal,
    StructureMoneyRefused,
    exercise_payoff_thousandths,
)
from app.options.structure import ResolvedStructure

_THOUSANDTHS_PER_DOLLAR = 1000
_CENTS_PER_DOLLAR = 100
_ONE_THOUSANDTH = Decimal("0.001")


@dataclass(frozen=True, slots=True)
class PriceRange:
    """Underlying prices from ``low`` to ``high``, both included, in dollars."""

    low: Decimal
    # None: every price from ``low`` up.
    high: Decimal | None


@dataclass(frozen=True, slots=True)
class AtExpiry:
    """One entry's figures at expiry. Each is an estimate under the module's assumptions."""

    # The most the entry gains, in cents. None when the gain grows without limit as the price rises.
    gain_cents: int | None
    # Where the highest result holds, lowest prices first. Empty when the gain has no limit.
    gain_holds: tuple[PriceRange, ...]
    # Where the lowest result holds: the prices the loss-at-expiry estimate is the result at.
    loss_holds: tuple[PriceRange, ...]
    # The underlying prices the entry neither gains nor loses at, in dollars, lowest first.
    break_evens: tuple[Fraction, ...]


def _dollars(thousandths: int) -> Decimal:
    return Decimal(thousandths) / _THOUSANDTHS_PER_DOLLAR


def _holds(points: Sequence[int], payoffs: Sequence[int], slope_above: int, extreme: int) -> tuple[PriceRange, ...]:
    """The stretches of price on which the payoff is ``extreme``. Between two neighbouring turning points
    the payoff is a straight line, so it is at ``extreme`` all the way when it is at both ends."""
    stretches: list[tuple[int, int | None]] = []
    for at, (point, payoff) in enumerate(zip(points, payoffs, strict=True)):
        if payoff != extreme:
            continue
        if stretches and payoffs[at - 1] == extreme:
            stretches[-1] = (stretches[-1][0], point)
        else:
            stretches.append((point, point))
    if stretches and payoffs[-1] == extreme and slope_above == 0:
        stretches[-1] = (stretches[-1][0], None)
    return tuple(PriceRange(_dollars(low), None if high is None else _dollars(high)) for low, high in stretches)


def _break_evens(points: Sequence[int], payoffs: Sequence[int], slope_above: int, net_price: Decimal) -> tuple[Fraction, ...]:
    """Every price the payoff equals the net price at, on a stretch where the payoff is not level."""
    price_paid = Fraction(net_price) * _THOUSANDTHS_PER_DOLLAR
    crossings: set[Fraction] = set()
    for at in range(len(points) - 1):
        low, high = points[at], points[at + 1]
        rise = payoffs[at + 1] - payoffs[at]
        if rise == 0:
            continue
        crossing = low + (price_paid - payoffs[at]) * Fraction(high - low, rise)
        if low <= crossing <= high:
            crossings.add(crossing)
    if slope_above != 0:
        crossing = points[-1] + (price_paid - payoffs[-1]) / slope_above
        if crossing >= points[-1]:
            crossings.add(crossing)
    return tuple(crossing / _THOUSANDTHS_PER_DOLLAR for crossing in sorted(crossings))


def at_expiry(structure: ResolvedStructure, size: EntrySize, *, net_price: Decimal) -> AtExpiry | None:
    """The figures at expiry of entering ``size`` at ``net_price`` a share for one structure (positive is paid).

    ``None`` when the legs do not share an expiry: a leg that outlives
    another is worth something no exercise payoff describes. Refuses, as the
    requirement does, a structure whose loss grows without limit as the price
    rises: it has no lowest result to hold anywhere.
    """
    require_everyday(net_price, what="a net price")
    if not structure.has_one_expiry:
        return None
    points = sorted({0, *(leg.contract.strike_thousandths for leg in structure.legs)})
    payoffs = [exercise_payoff_thousandths(structure, point) for point in points]
    # Above the highest strike the payoff is one straight line: its rise over one thousandth is its slope.
    slope_above = exercise_payoff_thousandths(structure, points[-1] + 1) - payoffs[-1]
    if slope_above < 0:
        raise StructureMoneyRefused(
            StructureMoneyRefusal.LOSS_UNBOUNDED,
            "The structure sells more calls than it buys, so its loss grows without limit as the price rises.",
        )
    gain_cents: int | None = None
    gain_holds: tuple[PriceRange, ...] = ()
    if slope_above == 0:
        highest = max(payoffs)
        per_share = exact_sum([(highest, _ONE_THOUSANDTH), (-1, net_price)])
        gain_cents = max(
            0,
            size.units
            * whole_units(
                per_share,
                units_per_one=size.multiplier * _CENTS_PER_DOLLAR,
                what=f"a gain at expiry at {size.multiplier} shares a contract",
                unit="cents",
            ),
        )
        gain_holds = _holds(points, payoffs, slope_above, highest)
    return AtExpiry(
        gain_cents=gain_cents,
        gain_holds=gain_holds,
        loss_holds=_holds(points, payoffs, slope_above, min(payoffs)),
        break_evens=_break_evens(points, payoffs, slope_above, net_price),
    )
