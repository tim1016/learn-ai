"""Where each named structure starts: the legs and rules filled in when the owner picks one.

Every value here is a starting point the owner may change before deploying,
and none is read after that: a deployed plan carries its own rules. What a
name fixes is in ``app.options.named_structures``; what its picked contracts
must look like is in ``app.options.structure_check``.

Most structures hang on one leg picked by delta and place the others a
dollar distance from it, so the shape holds wherever the price is. The
strangle and the iron condor each pick two legs by delta, a put and a call
out of the money, and their shape holds while those two deltas land on
either side of the price.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.options.contract import OptionRight
from app.options.named_structures import NamedStructure
from app.options.structure import LegPosition
from app.schemas.action_plan import (
    ActionPlan,
    CloseLegExit,
    DeltaStrike,
    LegDistanceStrike,
    NearestWeeklyExpiry,
    OptionEntryLeg,
    OptionInstrument,
    StrikeSelector,
)

# The starting delta of a leg at the money, and of one out of it.
_AT_THE_MONEY = 0.50
_OUT_OF_THE_MONEY = 0.30
# The starting distance between neighbouring strikes, in dollars.
_WIDTH = 5.0


@dataclass(frozen=True, slots=True)
class _Start:
    leg_id: str
    position: LegPosition
    right: OptionRight
    strike: StrikeSelector
    ratio: int = 1


def _by_delta(target: float) -> DeltaStrike:
    return DeltaStrike(selector="delta", target=target)


def _from(leg_id: str, dollars: float) -> LegDistanceStrike:
    return LegDistanceStrike(selector="leg_distance", leg_id=leg_id, dollars=dollars)


def _butterfly(right: OptionRight) -> tuple[_Start, ...]:
    return (
        _Start("lower_wing", "long", right, _from("body", -_WIDTH)),
        _Start("body", "short", right, _by_delta(_AT_THE_MONEY), ratio=2),
        _Start("upper_wing", "long", right, _from("body", _WIDTH)),
    )


_STARTS: dict[NamedStructure, tuple[_Start, ...]] = {
    NamedStructure.LONG_CALL: (_Start("long_call", "long", "call", _by_delta(_AT_THE_MONEY)),),
    NamedStructure.LONG_PUT: (_Start("long_put", "long", "put", _by_delta(_AT_THE_MONEY)),),
    NamedStructure.BULL_CALL_SPREAD: (
        _Start("long_call", "long", "call", _by_delta(_AT_THE_MONEY)),
        _Start("short_call", "short", "call", _from("long_call", _WIDTH)),
    ),
    NamedStructure.BEAR_PUT_SPREAD: (
        _Start("long_put", "long", "put", _by_delta(_AT_THE_MONEY)),
        _Start("short_put", "short", "put", _from("long_put", -_WIDTH)),
    ),
    NamedStructure.BULL_PUT_SPREAD: (
        _Start("short_put", "short", "put", _by_delta(_OUT_OF_THE_MONEY)),
        _Start("long_put", "long", "put", _from("short_put", -_WIDTH)),
    ),
    NamedStructure.BEAR_CALL_SPREAD: (
        _Start("short_call", "short", "call", _by_delta(_OUT_OF_THE_MONEY)),
        _Start("long_call", "long", "call", _from("short_call", _WIDTH)),
    ),
    NamedStructure.LONG_STRADDLE: (
        _Start("long_call", "long", "call", _by_delta(_AT_THE_MONEY)),
        _Start("long_put", "long", "put", _from("long_call", 0.0)),
    ),
    NamedStructure.LONG_STRANGLE: (
        _Start("long_put", "long", "put", _by_delta(_OUT_OF_THE_MONEY)),
        _Start("long_call", "long", "call", _by_delta(_OUT_OF_THE_MONEY)),
    ),
    NamedStructure.IRON_CONDOR: (
        _Start("long_put", "long", "put", _from("short_put", -_WIDTH)),
        _Start("short_put", "short", "put", _by_delta(_OUT_OF_THE_MONEY)),
        _Start("short_call", "short", "call", _by_delta(_OUT_OF_THE_MONEY)),
        _Start("long_call", "long", "call", _from("short_call", _WIDTH)),
    ),
    NamedStructure.IRON_BUTTERFLY: (
        _Start("long_put", "long", "put", _from("short_call", -_WIDTH)),
        _Start("short_put", "short", "put", _from("short_call", 0.0)),
        _Start("short_call", "short", "call", _by_delta(_AT_THE_MONEY)),
        _Start("long_call", "long", "call", _from("short_call", _WIDTH)),
    ),
    NamedStructure.CALL_BUTTERFLY: _butterfly("call"),
    NamedStructure.PUT_BUTTERFLY: _butterfly("put"),
}


def starting_plan(name: NamedStructure, underlying: str) -> ActionPlan:
    """The plan the builder shows when the owner picks ``name`` for ``underlying``: every leg, closed by the exit."""
    starts = _STARTS[name]
    return ActionPlan(
        structure=name,
        on_enter=[
            OptionEntryLeg(
                leg_id=start.leg_id,
                instrument=OptionInstrument(kind="option", underlying=underlying),
                position=start.position,
                qty_ratio=start.ratio,
                right=start.right,
                strike=start.strike,
                expiry=NearestWeeklyExpiry(selector="nearest_weekly"),
            )
            for start in starts
        ],
        on_exit=[CloseLegExit(kind="close_leg", entry_leg_id=start.leg_id) for start in starts],
    )


# How the builder lays the names out, and the one line it says of each, in the owner's words.
GROUPS: tuple[tuple[str, tuple[NamedStructure, ...]], ...] = (
    ("One leg", (NamedStructure.LONG_CALL, NamedStructure.LONG_PUT)),
    (
        "Two legs",
        (
            NamedStructure.BULL_CALL_SPREAD,
            NamedStructure.BEAR_PUT_SPREAD,
            NamedStructure.BULL_PUT_SPREAD,
            NamedStructure.BEAR_CALL_SPREAD,
            NamedStructure.LONG_STRADDLE,
            NamedStructure.LONG_STRANGLE,
        ),
    ),
    (
        "Three or four",
        (
            NamedStructure.CALL_BUTTERFLY,
            NamedStructure.PUT_BUTTERFLY,
            NamedStructure.IRON_CONDOR,
            NamedStructure.IRON_BUTTERFLY,
        ),
    ),
)

_DESCRIPTIONS: dict[NamedStructure, str] = {
    NamedStructure.LONG_CALL: "Buy a call. You pay up front.",
    NamedStructure.LONG_PUT: "Buy a put. You pay up front.",
    NamedStructure.BULL_CALL_SPREAD: "Buy a call, sell a higher call. You pay up front.",
    NamedStructure.BEAR_PUT_SPREAD: "Buy a put, sell a lower put. You pay up front.",
    NamedStructure.BULL_PUT_SPREAD: "Sell a put, buy a lower put. You are paid up front.",
    NamedStructure.BEAR_CALL_SPREAD: "Sell a call, buy a higher call. You are paid up front.",
    NamedStructure.LONG_STRADDLE: "Buy a call and a put at one strike. You pay up front.",
    NamedStructure.LONG_STRANGLE: "Buy a put, and a call at a higher strike. You pay up front.",
    NamedStructure.CALL_BUTTERFLY: "Buy a call, sell two at a higher strike, buy one the same distance higher again.",
    NamedStructure.PUT_BUTTERFLY: "Buy a put, sell two at a higher strike, buy one the same distance higher again.",
    NamedStructure.IRON_CONDOR: "Sell a put and a higher call, and buy one further out on each side. You are paid up front.",
    NamedStructure.IRON_BUTTERFLY: "Sell a put and a call at one strike, and buy one further out on each side. You are paid up front.",
}


def label(name: NamedStructure) -> str:
    """The name as a screen writes it: "Bull call spread"."""
    return name.in_words.capitalize()


def with_its_article(name: NamedStructure) -> str:
    """The name as a sentence takes it: "a bull call spread", "an iron condor"."""
    return f"{'an' if name.in_words[0] in 'aeiou' else 'a'} {name.in_words}"


def description(name: NamedStructure) -> str:
    """What the named structure is, in one line."""
    return _DESCRIPTIONS[name]
