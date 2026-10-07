"""Which way an option structure leans: its net delta, counted in shares of the underlying.

Formula: with ``s_i`` = +1 for a bought leg and -1 for a sold one, ``r_i`` its
  ratio, ``Δ_i`` its contract's delta per share, ``m`` the contract multiplier
  and ``N`` the structures opened:
    net delta             = Σ s_i · r_i · Δ_i           (per share of one structure)
    shares, one structure = m · net delta
    shares, the entry     = N · m · net delta
    leans up when net delta ≥ 0.05, down when net delta ≤ -0.05, about flat between
Reference: Hull, Options, Futures, and Other Derivatives, "Delta of a
  portfolio": a portfolio's delta is the sum of its positions' deltas, each
  weighted by its quantity. Calling a net delta under 0.05 "about flat" is
  this repo's own choice (#2913): it is a delta rule's own default tolerance,
  so a smaller lean is inside what the rules that picked the legs control.
Canonical implementation: this file. Each ``Δ_i`` is ``app/options/american.py``'s,
  solved from the contract's own quote; no vendor's delta is used.
Validated against: tests/options/test_lean.py (hand-derived cases).

"Moves like about 17 SPY shares" is this figure: for a small move of the
underlying, now, the structure's value changes as that many shares would. It
says nothing of a large move, of time passing, or of a change in volatility,
and it is not a forecast of profit.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from app.options.requirement import EntrySize
from app.options.structure import ResolvedStructure

# A net delta smaller than this, either way, is called about flat.
ABOUT_FLAT_BELOW_DELTA = 0.05


class LeanDirection(StrEnum):
    # Gains as the underlying rises.
    UP = "up"
    # Gains as the underlying falls.
    DOWN = "down"
    ABOUT_FLAT = "about_flat"


@dataclass(frozen=True, slots=True)
class Lean:
    # Per share of one structure: what the direction is judged by.
    net_delta: float
    # The shares of the underlying one structure moves like. Below zero: like shares sold short.
    shares_per_structure: float
    # The same for every structure of the entry.
    shares: float
    direction: LeanDirection


def lean(structure: ResolvedStructure, size: EntrySize, deltas: Mapping[str, float]) -> Lean:
    """The lean of ``size`` structures, from each leg's contract delta per share, by leg id.

    A put's delta is below zero, so a bought put leans down and a sold one
    up. Every leg must have a delta: a structure with one missing has no net
    delta, and none is made up for it.
    """
    leg_ids = {leg.leg_id for leg in structure.legs}
    if set(deltas) != leg_ids:
        raise ValueError(f"expected one delta for each of the legs {sorted(leg_ids)}; got {sorted(deltas)}")
    for leg_id, delta in deltas.items():
        if isinstance(delta, bool) or not isinstance(delta, float) or not math.isfinite(delta):
            raise ValueError(f"the delta of leg {leg_id!r} must be a finite number; got {delta!r}")
    net_delta = math.fsum(
        (1 if leg.position == "long" else -1) * leg.ratio * deltas[leg.leg_id] for leg in structure.legs
    )
    if net_delta >= ABOUT_FLAT_BELOW_DELTA:
        direction = LeanDirection.UP
    elif net_delta <= -ABOUT_FLAT_BELOW_DELTA:
        direction = LeanDirection.DOWN
    else:
        direction = LeanDirection.ABOUT_FLAT
    per_structure = size.multiplier * net_delta
    return Lean(
        net_delta=net_delta,
        shares_per_structure=per_structure,
        shares=size.units * per_structure,
        direction=direction,
    )
