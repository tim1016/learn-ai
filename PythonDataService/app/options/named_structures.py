"""The named option structures: what each one is made of.

A named structure is a starting point the owner may reshape, not a cage: the
strikes and expiries are theirs to choose. What the name fixes is what it is
made of — how many legs, bought or sold, calls or puts, in what ratio. A plan
that says "bull call spread" and is not two calls, one bought and one sold,
is refused, and the owner builds it as Custom: a plan with no name.

Structures that cannot be covered inside one order, such as a short straddle,
are not here.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from app.options.contract import OptionRight
from app.options.structure import LegPosition


class NamedStructure(StrEnum):
    LONG_CALL = "long_call"
    LONG_PUT = "long_put"
    BULL_CALL_SPREAD = "bull_call_spread"
    BEAR_PUT_SPREAD = "bear_put_spread"
    BULL_PUT_SPREAD = "bull_put_spread"
    BEAR_CALL_SPREAD = "bear_call_spread"
    LONG_STRADDLE = "long_straddle"
    LONG_STRANGLE = "long_strangle"
    IRON_CONDOR = "iron_condor"
    IRON_BUTTERFLY = "iron_butterfly"
    CALL_BUTTERFLY = "call_butterfly"
    PUT_BUTTERFLY = "put_butterfly"

    @property
    def in_words(self) -> str:
        return self.value.replace("_", " ")


@dataclass(frozen=True, slots=True, order=True)
class LegKind:
    """What one leg of a structure is, strike and expiry aside."""

    position: LegPosition
    right: OptionRight
    ratio: int

    @property
    def in_words(self) -> str:
        return f"{self.ratio} {'bought' if self.position == 'long' else 'sold'} {self.right}{'' if self.ratio == 1 else 's'}"


def _bought(right: OptionRight, ratio: int = 1) -> LegKind:
    return LegKind("long", right, ratio)


def _sold(right: OptionRight, ratio: int = 1) -> LegKind:
    return LegKind("short", right, ratio)


_A_SPREAD_OF: dict[OptionRight, tuple[LegKind, ...]] = {
    "call": (_bought("call"), _sold("call")),
    "put": (_bought("put"), _sold("put")),
}
_A_BUTTERFLY_OF: dict[OptionRight, tuple[LegKind, ...]] = {
    "call": (_bought("call"), _sold("call", 2), _bought("call")),
    "put": (_bought("put"), _sold("put", 2), _bought("put")),
}
_FOUR_LEGS = (_bought("put"), _sold("put"), _sold("call"), _bought("call"))

#: The legs each named structure is made of, in no particular order.
MADE_OF: dict[NamedStructure, tuple[LegKind, ...]] = {
    NamedStructure.LONG_CALL: (_bought("call"),),
    NamedStructure.LONG_PUT: (_bought("put"),),
    NamedStructure.BULL_CALL_SPREAD: _A_SPREAD_OF["call"],
    NamedStructure.BEAR_CALL_SPREAD: _A_SPREAD_OF["call"],
    NamedStructure.BEAR_PUT_SPREAD: _A_SPREAD_OF["put"],
    NamedStructure.BULL_PUT_SPREAD: _A_SPREAD_OF["put"],
    NamedStructure.LONG_STRADDLE: (_bought("call"), _bought("put")),
    NamedStructure.LONG_STRANGLE: (_bought("call"), _bought("put")),
    NamedStructure.IRON_CONDOR: _FOUR_LEGS,
    NamedStructure.IRON_BUTTERFLY: _FOUR_LEGS,
    NamedStructure.CALL_BUTTERFLY: _A_BUTTERFLY_OF["call"],
    NamedStructure.PUT_BUTTERFLY: _A_BUTTERFLY_OF["put"],
}


def is_made_as_named(name: NamedStructure, legs: Iterable[LegKind]) -> bool:
    """Whether ``legs`` are exactly what the named structure is made of, in any order."""
    return sorted(legs) == sorted(MADE_OF[name])


def made_of_in_words(name: NamedStructure) -> str:
    """What the named structure is made of, as a refusal says it: "1 bought call and 1 sold call"."""
    return " and ".join(kind.in_words for kind in MADE_OF[name])
