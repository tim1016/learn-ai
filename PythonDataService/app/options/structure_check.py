"""The structure, checked after its rules have picked contracts.

A plan's legs are rules, and independent rules can land on a shape nobody
meant: two legs on one contract, a spread whose strikes come out reversed, a
butterfly with uneven wings. A bounded loss does not prove the result is the
structure the owner named, so the picked contracts are checked before any
order is built from them, and again wherever a plan is previewed.

Two questions, asked in order:

``resolved_structure``  whether the picked contracts are the plan's legs at
    all: one contract per leg, of the leg's kind, on the plan's underlying,
    no contract twice, and every sold leg covered by a bought leg of its
    kind that lasts as long.
``require_named_shape``  for a plan that carries a structure's name, whether
    the contracts have that structure's shape.

Whether the loss is bounded is the money's question
(``app.options.requirement``), and whether the broker lists each contract is
the broker's. Neither is asked here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise

from app.options.contract import OptionContract, OptionRight
from app.options.exact import require_everyday
from app.options.named_structures import LegKind, NamedStructure, is_made_as_named, made_of_in_words
from app.options.structure import LegPosition, ResolvedLeg, ResolvedStructure
from app.schemas.action_plan import ActionPlan, OptionEntryLeg

_THOUSANDTHS_PER_DOLLAR = 1000


class StructureRefusal(StrEnum):
    # The picked contracts are not the plan's legs: one missing, one extra, or one of another kind.
    NOT_THE_PLANS_LEGS = "not_the_plans_legs"
    # Two legs came to one contract.
    SAME_CONTRACT = "same_contract"
    # No bought leg of a sold leg's kind lasts as long as it does.
    SOLD_LEG_NOT_COVERED = "sold_leg_not_covered"
    # The legs are not what the named structure is made of.
    NOT_MADE_AS_NAMED = "not_made_as_named"
    # A named structure's legs expire on more than one date.
    MORE_THAN_ONE_EXPIRY = "more_than_one_expiry"
    # The strikes are not in the order the name means, or not equal where it means them equal.
    STRIKES_NOT_AS_NAMED = "strikes_not_as_named"
    # A butterfly's two wings are not the same distance from its body.
    WINGS_UNEVEN = "wings_uneven"
    # A structure named for paying premium would not pay any.
    PREMIUM_NOT_PAID = "premium_not_paid"
    # A structure named for receiving premium would not receive any.
    PREMIUM_NOT_RECEIVED = "premium_not_received"
    # A spread would receive as much as its width or more, which no real market pays.
    CREDIT_NOT_UNDER_WIDTH = "credit_not_under_width"


class StructureRefused(ValueError):
    """The picked contracts are not the structure the plan means. Names the rule and the legs."""

    def __init__(self, refusal: StructureRefusal, leg_ids: tuple[str, ...], message: str) -> None:
        super().__init__(message)
        self.refusal = refusal
        self.leg_ids = leg_ids


def resolved_structure(plan: ActionPlan, picked: Mapping[str, OptionContract]) -> ResolvedStructure:
    """The structure ``plan``'s rules came to: each option leg with the contract picked for it.

    Position and ratio are read from the plan, never restated, so a resolved
    structure cannot differ from what was declared. Refuses picks that are
    not the plan's legs, two legs on one contract, and a sold leg with no
    bought leg of its kind that lasts as long.
    """
    legs = [leg for leg in plan.on_enter if isinstance(leg, OptionEntryLeg)]
    if not legs:
        raise ValueError("a plan with no option leg has no structure to resolve")
    declared = {leg.leg_id for leg in legs}
    if set(picked) != declared:
        raise StructureRefused(
            StructureRefusal.NOT_THE_PLANS_LEGS,
            tuple(sorted(declared ^ set(picked))),
            f"the plan's legs are {sorted(declared)}; contracts were picked for {sorted(picked)}",
        )
    for leg in legs:
        contract = picked[leg.leg_id]
        if contract.right != leg.right or contract.root != leg.instrument.underlying.upper():
            raise StructureRefused(
                StructureRefusal.NOT_THE_PLANS_LEGS,
                (leg.leg_id,),
                f"leg {leg.leg_id!r} is a {leg.right} on {leg.instrument.underlying.upper()}; "
                f"{contract.occ_symbol} is not",
            )
    _require_one_leg_a_contract(picked)
    structure = ResolvedStructure(
        legs=tuple(
            ResolvedLeg(leg_id=leg.leg_id, contract=picked[leg.leg_id], position=leg.position, ratio=leg.qty_ratio)
            for leg in legs
        )
    )
    _require_sold_legs_covered(structure)
    return structure


def _require_one_leg_a_contract(picked: Mapping[str, OptionContract]) -> None:
    legs_on: dict[str, list[str]] = {}
    for leg_id, contract in sorted(picked.items()):
        legs_on.setdefault(contract.occ_symbol, []).append(leg_id)
    for symbol, leg_ids in legs_on.items():
        if len(leg_ids) > 1:
            raise StructureRefused(
                StructureRefusal.SAME_CONTRACT,
                tuple(leg_ids),
                f"legs {' and '.join(repr(leg_id) for leg_id in leg_ids)} both came to {symbol}",
            )


def _require_sold_legs_covered(structure: ResolvedStructure) -> None:
    """Every sold contract has a bought contract of its kind, one for one, that does not expire before it."""
    for right in ("call", "put"):
        bought = _legs(structure, "long", right)
        sold = sorted(_legs(structure, "short", right), key=lambda leg: leg.contract.expiry_ms, reverse=True)
        to_cover = 0
        # Longest-lived first: what covers a later expiry also covers every earlier one.
        for leg in sold:
            to_cover += leg.ratio
            lasting = sum(cover.ratio for cover in bought if cover.contract.expiry_ms >= leg.contract.expiry_ms)
            if to_cover > lasting:
                raise StructureRefused(
                    StructureRefusal.SOLD_LEG_NOT_COVERED,
                    (leg.leg_id,),
                    f"sold leg {leg.leg_id!r} expires {leg.contract.expiry_date}; "
                    f"the bought {right}s that last that long cover {lasting} of the {to_cover} sold",
                )


def require_named_shape(name: NamedStructure, structure: ResolvedStructure, *, net_price: Decimal) -> None:
    """Refuse a resolved structure that does not have the shape ``name`` means.

    ``net_price`` is the structure's price per share as it would be sent,
    positive when paid: some names mean premium paid, others received.
    """
    require_everyday(net_price, what="a net price")
    if not is_made_as_named(name, (LegKind(leg.position, leg.contract.right, leg.ratio) for leg in structure.legs)):
        raise StructureRefused(
            StructureRefusal.NOT_MADE_AS_NAMED,
            _leg_ids(structure),
            f"a {name.in_words} is {made_of_in_words(name)}; these legs are not",
        )
    if not structure.has_one_expiry:
        expiries = sorted({str(leg.contract.expiry_date) for leg in structure.legs})
        raise StructureRefused(
            StructureRefusal.MORE_THAN_ONE_EXPIRY,
            _leg_ids(structure),
            f"a {name.in_words} has one expiry; these legs expire {' and '.join(expiries)}",
        )
    _SHAPE_OF[name](name, structure, net_price)


def _legs(structure: ResolvedStructure, position: LegPosition, right: OptionRight) -> list[ResolvedLeg]:
    return [leg for leg in structure.legs if leg.position == position and leg.contract.right == right]


def _leg_ids(structure: ResolvedStructure) -> tuple[str, ...]:
    return tuple(leg.leg_id for leg in structure.legs)


def _the(structure: ResolvedStructure, position: LegPosition, right: OptionRight) -> ResolvedLeg:
    (leg,) = _legs(structure, position, right)
    return leg


def _require_rising(name: NamedStructure, *legs: ResolvedLeg) -> None:
    """Each leg's strike is below the next one's."""
    for lower, higher in pairwise(legs):
        if lower.contract.strike_thousandths >= higher.contract.strike_thousandths:
            raise StructureRefused(
                StructureRefusal.STRIKES_NOT_AS_NAMED,
                (lower.leg_id, higher.leg_id),
                f"in a {name.in_words} leg {lower.leg_id!r} sits below leg {higher.leg_id!r}; "
                f"they came to {lower.contract.strike} and {higher.contract.strike}",
            )


def _require_one_strike(name: NamedStructure, first: ResolvedLeg, second: ResolvedLeg) -> None:
    if first.contract.strike_thousandths != second.contract.strike_thousandths:
        raise StructureRefused(
            StructureRefusal.STRIKES_NOT_AS_NAMED,
            (first.leg_id, second.leg_id),
            f"in a {name.in_words} legs {first.leg_id!r} and {second.leg_id!r} share a strike; "
            f"they came to {first.contract.strike} and {second.contract.strike}",
        )


def _in_words(net_price: Decimal) -> str:
    if net_price == 0:
        return "costs nothing"
    return f"costs {net_price} a share" if net_price > 0 else f"pays {-net_price} a share"


def _require_paid(name: NamedStructure, structure: ResolvedStructure, net_price: Decimal) -> None:
    if net_price <= 0:
        raise StructureRefused(
            StructureRefusal.PREMIUM_NOT_PAID,
            _leg_ids(structure),
            f"a {name.in_words} is paid for; at these prices it {_in_words(net_price)}",
        )


def _require_received(name: NamedStructure, structure: ResolvedStructure, net_price: Decimal) -> None:
    if net_price >= 0:
        raise StructureRefused(
            StructureRefusal.PREMIUM_NOT_RECEIVED,
            _leg_ids(structure),
            f"a {name.in_words} receives premium; at these prices it {_in_words(net_price)}",
        )


def _require_credit_under_width(
    name: NamedStructure, structure: ResolvedStructure, net_price: Decimal, lower: ResolvedLeg, higher: ResolvedLeg
) -> None:
    _require_received(name, structure, net_price)
    width_thousandths = higher.contract.strike_thousandths - lower.contract.strike_thousandths
    if -net_price * _THOUSANDTHS_PER_DOLLAR >= width_thousandths:
        raise StructureRefused(
            StructureRefusal.CREDIT_NOT_UNDER_WIDTH,
            (lower.leg_id, higher.leg_id),
            f"a {name.in_words} {higher.contract.strike - lower.contract.strike} wide cannot be sold for "
            f"{-net_price} a share; the prices do not belong together",
        )


def _nothing_more(name: NamedStructure, structure: ResolvedStructure, net_price: Decimal) -> None:
    """One bought option is its own shape."""


def _bull_call_spread(name: NamedStructure, structure: ResolvedStructure, net_price: Decimal) -> None:
    _require_rising(name, _the(structure, "long", "call"), _the(structure, "short", "call"))
    _require_paid(name, structure, net_price)


def _bear_put_spread(name: NamedStructure, structure: ResolvedStructure, net_price: Decimal) -> None:
    _require_rising(name, _the(structure, "short", "put"), _the(structure, "long", "put"))
    _require_paid(name, structure, net_price)


def _bull_put_spread(name: NamedStructure, structure: ResolvedStructure, net_price: Decimal) -> None:
    bought, sold = _the(structure, "long", "put"), _the(structure, "short", "put")
    _require_rising(name, bought, sold)
    _require_credit_under_width(name, structure, net_price, bought, sold)


def _bear_call_spread(name: NamedStructure, structure: ResolvedStructure, net_price: Decimal) -> None:
    sold, bought = _the(structure, "short", "call"), _the(structure, "long", "call")
    _require_rising(name, sold, bought)
    _require_credit_under_width(name, structure, net_price, sold, bought)


def _long_straddle(name: NamedStructure, structure: ResolvedStructure, net_price: Decimal) -> None:
    _require_one_strike(name, _the(structure, "long", "put"), _the(structure, "long", "call"))


def _long_strangle(name: NamedStructure, structure: ResolvedStructure, net_price: Decimal) -> None:
    _require_rising(name, _the(structure, "long", "put"), _the(structure, "long", "call"))


def _iron_condor(name: NamedStructure, structure: ResolvedStructure, net_price: Decimal) -> None:
    _require_rising(
        name,
        _the(structure, "long", "put"),
        _the(structure, "short", "put"),
        _the(structure, "short", "call"),
        _the(structure, "long", "call"),
    )
    _require_received(name, structure, net_price)


def _iron_butterfly(name: NamedStructure, structure: ResolvedStructure, net_price: Decimal) -> None:
    sold_put, sold_call = _the(structure, "short", "put"), _the(structure, "short", "call")
    _require_one_strike(name, sold_put, sold_call)
    _require_rising(name, _the(structure, "long", "put"), sold_put)
    _require_rising(name, sold_call, _the(structure, "long", "call"))
    _require_received(name, structure, net_price)


def _butterfly_of(right: OptionRight) -> Callable[[NamedStructure, ResolvedStructure, Decimal], None]:
    def butterfly(name: NamedStructure, structure: ResolvedStructure, net_price: Decimal) -> None:
        lower, upper = sorted(_legs(structure, "long", right), key=lambda leg: leg.contract.strike_thousandths)
        body = _the(structure, "short", right)
        _require_rising(name, lower, body, upper)
        below = body.contract.strike_thousandths - lower.contract.strike_thousandths
        above = upper.contract.strike_thousandths - body.contract.strike_thousandths
        if below != above:
            raise StructureRefused(
                StructureRefusal.WINGS_UNEVEN,
                (lower.leg_id, upper.leg_id),
                f"a {name.in_words} has wings the same width; leg {lower.leg_id!r} is "
                f"{body.contract.strike - lower.contract.strike} below the body and leg {upper.leg_id!r} "
                f"{upper.contract.strike - body.contract.strike} above it",
            )

    return butterfly


_SHAPE_OF: dict[NamedStructure, Callable[[NamedStructure, ResolvedStructure, Decimal], None]] = {
    NamedStructure.LONG_CALL: _nothing_more,
    NamedStructure.LONG_PUT: _nothing_more,
    NamedStructure.BULL_CALL_SPREAD: _bull_call_spread,
    NamedStructure.BEAR_PUT_SPREAD: _bear_put_spread,
    NamedStructure.BULL_PUT_SPREAD: _bull_put_spread,
    NamedStructure.BEAR_CALL_SPREAD: _bear_call_spread,
    NamedStructure.LONG_STRADDLE: _long_straddle,
    NamedStructure.LONG_STRANGLE: _long_strangle,
    NamedStructure.IRON_CONDOR: _iron_condor,
    NamedStructure.IRON_BUTTERFLY: _iron_butterfly,
    NamedStructure.CALL_BUTTERFLY: _butterfly_of("call"),
    NamedStructure.PUT_BUTTERFLY: _butterfly_of("put"),
}
