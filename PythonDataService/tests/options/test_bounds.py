"""What a number may be: a range read off the field that validates it, and each way a number falls outside one."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

import pytest
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from app.options.bounds import Bounds, bounds_of
from app.schemas.action_plan import AtmOffsetStrike, DeltaStrike, LegDistanceStrike, MinDteExpiry, PercentStrike

_PERCENT = Bounds(Decimal(0), False, Decimal(100), True, Decimal("0.000001"))


@pytest.mark.parametrize(
    ("number", "fault"),
    [
        pytest.param("0.000001", None, id="the finest step above the bottom"),
        pytest.param("100", None, id="the top, which is taken"),
        pytest.param("1e1", None, id="ten, written with an exponent"),
        pytest.param("7.50", None, id="trailing zeros are not finer digits"),
        pytest.param("0", "below", id="the bottom, which is not taken"),
        pytest.param("-0", "below", id="nothing, signed"),
        pytest.param("0E-30", "below", id="nothing, written to thirty places: still nothing, and not too fine"),
        pytest.param("-1", "below", id="under the bottom"),
        pytest.param("100.000001", "above", id="a step over the top"),
        pytest.param("1e999999999", "above", id="a number too large to write out"),
        pytest.param("3.3333333", "finer", id="finer than the step"),
        pytest.param("1e-7", "finer", id="finer, written with an exponent"),
        # Out of range wins over too fine: the range is what the owner needs to hear first.
        pytest.param("-0.0000001", "below", id="under the bottom and too fine"),
    ],
)
def test_a_number_is_inside_a_range_or_outside_it_one_way(number: str, fault: str | None) -> None:
    assert _PERCENT.fault(Decimal(number)) == fault


def test_a_range_with_no_bound_on_a_side_refuses_nothing_there_and_a_whole_number_range_refuses_a_part() -> None:
    whole, any_number = Bounds(step=Decimal(1)), Bounds(Decimal(0), True)

    assert [whole.fault(Decimal(number)) for number in ("-1000000000000", "0", "0.000", "7", "2.5", "1e30")] == [
        None, None, None, None, "finer", None,
    ]
    assert [any_number.fault(Decimal(number)) for number in ("0", "0.0000000001", "1e30", "-0.1")] == [None, None, None, "below"]


@pytest.mark.parametrize(
    ("bounds", "number", "fault"),
    [
        # A whole number with no bound at all, and one with a bottom and no top: nothing stops a huge number
        # before its step is read, and the step is read off its digits.
        pytest.param(Bounds(step=Decimal(1)), "1e999999999", None, id="no bounds: a whole number too large to write out"),
        pytest.param(Bounds(step=Decimal(1)), "-1e999999999", None, id="no bounds: as far below nothing"),
        pytest.param(Bounds(step=Decimal(1)), "1e1000000", None, id="no bounds: one past the largest exponent the context holds"),
        pytest.param(Bounds(Decimal(1), True, step=Decimal(1)), "1e999999999", None, id="a bottom and no top"),
        pytest.param(Bounds(Decimal(1), True, step=Decimal(1)), "1e1000000", None, id="a bottom and no top, one past the context"),
        pytest.param(Bounds(Decimal(1), True, step=Decimal(1)), "-1e999999999", "below", id="a bottom: far below it"),
        pytest.param(Bounds(step=Decimal(1)), "1e-999999999", "finer", id="too small to write out is finer than one"),
        pytest.param(Bounds(step=Decimal(1)), "1.5e1000000", None, id="fifteen followed by zeros is whole"),
        pytest.param(Bounds(step=Decimal("0.001")), "1.2345e-999999", "finer", id="finer than a thousandth by a long way"),
    ],
)
def test_a_number_of_any_size_is_judged_at_once_whether_or_not_the_range_has_a_top(
    bounds: Bounds, number: str, fault: str | None
) -> None:
    """Each of these is a number the decimal context cannot round: ``Decimal.normalize`` raises on it. A range
    with a top calls a huge number "above" before its step is asked. One without a top must read the step."""
    assert bounds.fault(Decimal(number)) == fault


@pytest.mark.parametrize(
    ("bounds", "number", "fault"),
    [
        pytest.param(Bounds(step=Decimal(1)), "12345678901234567890123456789.5", "finer", id="thirty digits and a half"),
        pytest.param(Bounds(step=Decimal(1)), "1234567890123456789012345678901234567890", None, id="forty digits, whole"),
        pytest.param(_PERCENT, "5.0000000000000000000000000001", "finer", id="five and a part in the twenty-eighth place"),
        pytest.param(_PERCENT, "5.0000000000000000000000000000", None, id="five, written to twenty-eight places"),
        pytest.param(Bounds(step=Decimal("0.001")), "99999.99900000000000000000000000001", "finer", id="a thousandth and a hair"),
    ],
)
def test_a_number_of_more_digits_than_the_decimal_context_keeps_is_read_whole_and_never_rounded_first(
    bounds: Bounds, number: str, fault: str | None
) -> None:
    """The decimal context keeps 28 digits. ``Decimal.normalize`` rounds a longer number to that before anything
    reads it, so a part in the twenty-ninth place was lost and the number was called a whole one."""
    assert bounds.fault(Decimal(number)) == fault


def test_only_a_finite_number_is_held_to_a_range() -> None:
    for no_number in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(ValueError, match="finite"):
            _PERCENT.fault(Decimal(no_number))


def test_a_range_is_stated_from_its_lowest_to_its_highest_in_a_step_that_is_a_power_of_ten() -> None:
    with pytest.raises(ValueError, match="power of ten"):
        Bounds(step=Decimal("0.25"))
    with pytest.raises(ValueError, match="lowest to its highest"):
        Bounds(Decimal(5), True, Decimal(1), True)


@pytest.mark.parametrize(
    ("model", "field", "read"),
    [
        pytest.param(DeltaStrike, "target", Bounds(Decimal(0), False, Decimal(1), False), id="above nothing and below one"),
        pytest.param(PercentStrike, "percent", Bounds(Decimal(-100), False, Decimal(1000), True), id="open below, closed above"),
        pytest.param(PercentStrike, "tolerance_percent", Bounds(Decimal(0), False, Decimal(100), True), id="a tolerance above nothing"),
        pytest.param(LegDistanceStrike, "tolerance_dollars", Bounds(Decimal(0), True), id="nothing or more"),
        pytest.param(LegDistanceStrike, "dollars", Bounds(), id="no bound on the field itself"),
        pytest.param(MinDteExpiry, "days", Bounds(Decimal(1), True, step=Decimal(1)), id="a whole number, one or more"),
        pytest.param(AtmOffsetStrike, "offset", Bounds(step=Decimal(1)), id="any whole number"),
    ],
)
def test_a_range_is_read_off_the_field_that_validates_the_number(model: type[BaseModel], field: str, read: Bounds) -> None:
    assert bounds_of(model, field) == read


class _Typed(BaseModel):
    waited: Annotated[int, Field(ge=1, le=300)]


@pytest.mark.parametrize("kind", [int, Decimal])
def test_a_range_handed_to_a_field_holds_that_field_to_it_and_reads_back_as_itself(kind: type) -> None:
    stated = Bounds(Decimal(1), True, Decimal(300), False, Decimal(1))
    held = TypeAdapter(Annotated[kind, *stated.held_by(kind)])

    assert [held.validate_python(number) for number in (1, 299)] == [1, 299]
    for outside in (0, 300):
        with pytest.raises(ValidationError):
            held.validate_python(outside)
    assert bounds_of(_Typed, "waited") == Bounds(Decimal(1), True, Decimal(300), True, Decimal(1))
