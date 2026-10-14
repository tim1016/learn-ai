"""How many whole structures a set of legs comes to, and whether the legs are in step (ADR 0076).

A structure is its legs in their ratios: one unit of a 1:2 spread is one
contract of the first leg and two of the second. An order of several legs is
meant to fill every leg together, and the books still count each leg by
itself, because a broker reports each leg by itself. This is the one place
the legs' counts are turned back into structures: for what an entry filled,
for what a bot holds of an entry, and for what a close may close.

Formula: with each leg's contracts cᵢ counted in the leg's own direction and
  its ratio rᵢ ≥ 1,
    units = max(0, minᵢ ⌊cᵢ / rᵢ⌋)
    beyondᵢ = cᵢ − units · rᵢ
    in step ⇔ beyondᵢ = 0 for every leg.
  A leg counted in its own direction is its signed holding for a bought leg
  and the negative of it for a sold one, so a leg held the wrong way round
  counts below zero and the structure holds no whole unit.
Reference: ADR 0076 decision 27 (units filled is the smallest whole number
  across legs; legs that come to different numbers are out of step). The
  PRD of #2913, "Partial fill" and "out of step".
Canonical implementation: this file.
Validated against: tests/options/test_whole_units.py.

Everything here is whole numbers. A count that is not one is refused, never
rounded: a broker fills an option in whole contracts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from app.options.structure import LegPosition


@dataclass(frozen=True, slots=True)
class LegContracts:
    """One leg's count of contracts, in the leg's own direction."""

    leg_id: str
    # Contracts of this leg in one structure: one or more.
    ratio: int
    # Counted in the leg's own direction; below zero is the other side.
    contracts: int

    def __post_init__(self) -> None:
        for name in ("ratio", "contracts"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"a leg's {name} is a whole number; leg {self.leg_id!r} has {value!r}")
        if self.ratio < 1:
            raise ValueError(f"a leg's ratio is one or more; leg {self.leg_id!r} has {self.ratio}")


@dataclass(frozen=True, slots=True)
class WholeUnits:
    """What a set of legs comes to in whole structures."""

    # max(0, the smallest across legs of contracts // ratio).
    units: int
    # Every leg counts exactly units x ratio.
    in_step: bool
    # By leg id: contracts - units x ratio. All zero when in step.
    beyond: Mapping[str, int]


def whole_units(legs: Sequence[LegContracts]) -> WholeUnits:
    """The whole structures ``legs`` come to, and how far each leg is beyond them.

    Refuses no legs at all, and a leg named twice: neither is a structure.
    """
    if not legs:
        raise ValueError("a structure has at least one leg")
    leg_ids = [leg.leg_id for leg in legs]
    if len(set(leg_ids)) != len(leg_ids):
        raise ValueError(f"each leg of a structure is counted once; these repeat: {sorted(leg_ids)}")
    # Floor division, so a leg held the other way round counts below zero and never as a unit.
    units = max(0, min(leg.contracts // leg.ratio for leg in legs))
    beyond = {leg.leg_id: leg.contracts - units * leg.ratio for leg in legs}
    return WholeUnits(units=units, in_step=not any(beyond.values()), beyond=MappingProxyType(beyond))


def in_leg_direction(held: int, position: LegPosition) -> int:
    """A signed holding counted in its leg's own direction: itself for a bought leg, its negative for a sold one."""
    if isinstance(held, bool) or not isinstance(held, int):
        raise ValueError(f"a holding of option contracts is a whole number; got {held!r}")
    return held if position == "long" else -held


__all__ = ["LegContracts", "WholeUnits", "in_leg_direction", "whole_units"]
