"""A preview request refused for a number outside its range, read into words (#2913).

The builder is served the range of every number an owner can type: each
rule's value and its "Within", a leg's quantity, the structures, and the four
order terms. A form can refuse a number outside its range before asking. One
that reaches the preview all the same is refused by the request's own
validation, and what that validation says is no sentence an owner can use.

Here that refusal is read back against the same ranges the catalogue serves.
When every fault in the request is a number that lies outside the range of
the box it was typed in, the request is said to be that, box by box, with the
leg named by its place. Anything else is left as it was: a missing field, a
wrong type, a name nobody defined, a number that is inside its range and
refused for another reason. Those are a page's mistakes, not an owner's, and
stay a malformed request.

Nothing here decides what a number may be. Each range is the one its
validator holds, read through the catalogue's own source
(``app.options.structure_catalogue``, ``app.options.order_terms``).

What was sent is read here a second time, after its validation refused it,
so nothing here may fail on what that validation guards against. A number is
only ever constructed, compared and read digit by digit
(``app.options.bounds.Bounds.fault``): it is never rounded, written out in
full or computed with, whatever its size.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from pydantic import TypeAdapter, ValidationError

from app.options.bounds import Bounds, bounds_of
from app.options.named_structures import NamedStructure
from app.options.structure import MAX_OPTION_LEGS
from app.options.structure_catalogue import EXPIRY_RULES, STRIKE_RULES, RuleOffered, order_term_starts
from app.schemas.option_preview import OptionLegRules, OptionStructurePreviewRequest
from app.services.option_preview_words import fixed_dates_taken_in_words, out_of_range_sentence

_LEGS_A_STRUCTURE_HAS = Bounds(Decimal(1), True, Decimal(MAX_OPTION_LEGS), True, Decimal(1))


@dataclass(frozen=True, slots=True)
class RequestSaidBack:
    """The parts of a preview request that every answer says back, whether or not the request stood.

    A request that validated gives its own (``of``). One refused for a
    number outside its range is no request, and is never typed as one: each
    part here is read from what was sent, as asked where that part is valid
    by itself and as the request's own default where it is the number
    refused. Its legs and its order terms are not said back at all.
    """

    preview_key: str
    underlying: str
    structure: NamedStructure | None
    structures: int
    gap_limit_percent: Decimal
    cost_limit_percent: Decimal

    @classmethod
    def of(cls, request: OptionStructurePreviewRequest) -> RequestSaidBack:
        return cls(
            preview_key=request.preview_key,
            underlying=request.underlying,
            structure=request.structure,
            structures=request.structures,
            gap_limit_percent=request.gap_limit_percent,
            cost_limit_percent=request.cost_limit_percent,
        )


@dataclass(frozen=True, slots=True)
class OutOfRange:
    """A request refused only for numbers outside their ranges."""

    said_back: RequestSaidBack
    # One sentence for each number refused, in the request's order.
    sentences: tuple[str, ...]
    # The ids of the legs a refused number belongs to, where the leg has one that can be read.
    leg_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Box:
    """One number in a request: what to call it, and the range it is held to."""

    what: str
    bounds: Bounds
    # A date is not said in milliseconds: its range is said in dates, and what was typed is not said back.
    is_a_date: bool = False
    # Why the range ends where it does, where the term's own module gives a reason. Said after the range
    # of a number above it: the reason is about the top, and says nothing true of a number below or between.
    why: str | None = None


def _number(typed: object) -> Decimal | None:
    """What was typed, as a finite number. None for anything else: a word, nothing, a truth value."""
    if isinstance(typed, bool):
        return None
    if isinstance(typed, int):
        return Decimal(typed)
    if isinstance(typed, float):
        return Decimal(repr(typed)) if math.isfinite(typed) else None
    if isinstance(typed, str):
        try:
            number = Decimal(typed)
        except InvalidOperation:
            return None
        return number if number.is_finite() else None
    return None


def _of_a_rule(rules: Sequence[RuleOffered], leg: str, selector: object, field: object) -> _Box | None:
    offered = next((rule for rule in rules if rule.selector == selector), None)
    if offered is None:
        return None
    if field == offered.value_field and offered.value_bounds is not None:
        return _Box(f"{leg}: {offered.label}", offered.value_bounds, is_a_date=offered.value_unit == "date")
    if field == offered.tolerance_field and offered.tolerance_bounds is not None:
        return _Box(f'{leg}: the "Within" beside {offered.label}', offered.tolerance_bounds)
    return None


def _box_at(where: Sequence[object]) -> _Box | None:
    """The number a validation error points at, when it is one the catalogue gives a range for."""
    match list(where):
        case ["body", "structures"]:
            return _Box("The number of structures", bounds_of(OptionStructurePreviewRequest, "structures"))
        case ["body", "legs"]:
            return _Box("The number of legs", _LEGS_A_STRUCTURE_HAS)
        case ["body", str(term)] if term in (terms := order_term_starts()):
            return _Box(terms[term].label, terms[term].bounds, why=terms[term].range_reason)
        case ["body", "legs", int(at), "qty_ratio"]:
            return _Box(f"Leg {at + 1}: the quantity", bounds_of(OptionLegRules, "qty_ratio"))
        case ["body", "legs", int(at), "strike", selector, field]:
            return _of_a_rule(STRIKE_RULES, f"Leg {at + 1}", selector, field)
        case ["body", "legs", int(at), "expiry", selector, field]:
            return _of_a_rule(EXPIRY_RULES, f"Leg {at + 1}", selector, field)
        case _:
            return None


def _as_asked_or_the_default(body: Mapping[str, Any], name: str) -> Any:
    """A part of the request as it was asked, when that part is valid by itself; the request's default when not."""
    info = OptionStructurePreviewRequest.model_fields[name]
    if name not in body:
        return info.default
    held_to = Annotated[info.annotation, *info.metadata] if info.metadata else info.annotation
    try:
        return TypeAdapter(held_to).validate_python(body[name])
    except ValidationError:
        return info.default


def numbers_out_of_range(body: object, errors: Sequence[Mapping[str, Any]]) -> OutOfRange | None:
    """``errors`` read as numbers outside their ranges, or ``None`` when any of them is something else.

    ``body`` is the request as it was sent and ``errors`` what its
    validation said of it. Every error must point at a number the catalogue
    gives a range for, and that number must lie outside that range by the
    range's own test. One error that does not, and the whole request is
    malformed as before.
    """
    if not isinstance(body, dict) or not errors:
        return None
    sentences: list[str] = []
    leg_ids: list[str] = []
    for error in errors:
        where = error.get("loc", ())
        box = _box_at(where)
        if box is None:
            return None
        typed = error.get("input")
        number = Decimal(len(typed)) if list(where) == ["body", "legs"] and isinstance(typed, list) else _number(typed)
        if number is None:
            return None
        fault = box.bounds.fault(number)
        if fault is None:
            # Inside its range and refused all the same: not a range's refusal. A date that is no expiry is one.
            return None
        if box.is_a_date:
            sentences.append(f"{box.what} was given a date no contract can carry. It takes {fixed_dates_taken_in_words()}.")
        else:
            said = number if list(where) == ["body", "legs"] else typed
            refused = out_of_range_sentence(box.what, said, box.bounds, fault)
            sentences.append(f"{refused} {box.why}" if box.why is not None and fault == "above" else refused)
        if len(where) > 2 and where[1] == "legs" and isinstance(where[2], int):
            legs = body.get("legs")
            leg = legs[where[2]] if isinstance(legs, list) and where[2] < len(legs) else None
            if isinstance(leg, dict) and isinstance(leg.get("leg_id"), str) and leg["leg_id"] not in leg_ids:
                leg_ids.append(leg["leg_id"])
    said_back = RequestSaidBack(
        # Neither was refused, or the request would not be here: every error was a number in a box.
        preview_key=body["preview_key"],
        underlying=body["underlying"],
        structure=_as_asked_or_the_default(body, "structure"),
        structures=_as_asked_or_the_default(body, "structures"),
        gap_limit_percent=_as_asked_or_the_default(body, "gap_limit_percent"),
        cost_limit_percent=_as_asked_or_the_default(body, "cost_limit_percent"),
    )
    return OutOfRange(said_back=said_back, sentences=tuple(sentences), leg_ids=tuple(leg_ids))


__all__ = ["OutOfRange", "RequestSaidBack", "numbers_out_of_range"]
