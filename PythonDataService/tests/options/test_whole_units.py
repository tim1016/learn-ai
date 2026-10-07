"""How many whole structures a set of legs comes to, and whether the legs are in step (#2913).

Every figure is worked by hand in the test that asserts it. All of it is
whole numbers, so every comparison is exact.
"""

from __future__ import annotations

import pytest

from app.options.whole_units import LegContracts, WholeUnits, in_leg_direction, whole_units


def _legs(*counted: tuple[str, int, int]) -> list[LegContracts]:
    return [LegContracts(leg_id=leg_id, ratio=ratio, contracts=contracts) for leg_id, ratio, contracts in counted]


def test_two_legs_filled_alike_are_that_many_units_in_step() -> None:
    came_to = whole_units(_legs(("bought", 1, 2), ("sold", 1, 2)))

    assert (came_to.units, came_to.in_step, dict(came_to.beyond)) == (2, True, {"bought": 0, "sold": 0})


def test_the_units_are_the_smallest_whole_number_across_the_legs() -> None:
    # An order for two units that filled two of one leg and one of the other holds one unit.
    came_to = whole_units(_legs(("bought", 1, 2), ("sold", 1, 1)))

    assert (came_to.units, came_to.in_step) == (1, False)
    # One contract of the bought leg is beyond the one whole unit.
    assert dict(came_to.beyond) == {"bought": 1, "sold": 0}


def test_a_leg_is_counted_by_its_ratio() -> None:
    # A 1:2 spread. Three of the first leg and six of the second are three units: 3 // 1 = 3, 6 // 2 = 3.
    assert whole_units(_legs(("one", 1, 3), ("two", 2, 6))) == WholeUnits(units=3, in_step=True, beyond={"one": 0, "two": 0})
    # Three and five: 5 // 2 = 2 units. Beyond them: 3 - 2 = 1 of the first, 5 - 4 = 1 of the second.
    uneven = whole_units(_legs(("one", 1, 3), ("two", 2, 5)))
    assert (uneven.units, uneven.in_step, dict(uneven.beyond)) == (2, False, {"one": 1, "two": 1})


def test_a_fill_on_one_leg_only_is_no_unit_and_out_of_step() -> None:
    came_to = whole_units(_legs(("bought", 1, 1), ("sold", 1, 0)))

    assert (came_to.units, came_to.in_step, dict(came_to.beyond)) == (0, False, {"bought": 1, "sold": 0})


def test_nothing_filled_is_no_unit_and_in_step() -> None:
    came_to = whole_units(_legs(("bought", 1, 0), ("sold", 1, 0)))

    assert (came_to.units, came_to.in_step) == (0, True)


def test_a_leg_held_the_other_way_round_counts_below_zero_and_never_as_a_unit() -> None:
    # One leg is in its direction by two; the other is the wrong way round by one.
    came_to = whole_units(_legs(("bought", 1, 2), ("sold", 1, -1)))

    assert (came_to.units, came_to.in_step, dict(came_to.beyond)) == (0, False, {"bought": 2, "sold": -1})
    # With a ratio of two, floor division still never rounds a wrong-way leg up to nothing.
    assert whole_units(_legs(("one", 1, 4), ("two", 2, -1))).units == 0


def test_one_leg_alone_is_its_contracts_over_its_ratio() -> None:
    assert whole_units(_legs(("only", 1, 5))) == WholeUnits(units=5, in_step=True, beyond={"only": 0})


def test_a_bought_leg_counts_what_is_held_and_a_sold_leg_what_is_owed() -> None:
    assert in_leg_direction(2, "long") == 2
    assert in_leg_direction(-2, "short") == 2
    # A bought leg's contract held short, and a sold leg's held long, are the other side.
    assert in_leg_direction(-1, "long") == -1
    assert in_leg_direction(1, "short") == -1


@pytest.mark.parametrize("held", [1.0, True, "1", None])
def test_a_holding_that_is_not_a_whole_number_is_refused(held: object) -> None:
    with pytest.raises(ValueError, match="whole number"):
        in_leg_direction(held, "long")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("ratio", "contracts", "message"),
    [(0, 1, "one or more"), (1.0, 1, "whole number"), (1, 1.5, "whole number"), (True, 1, "whole number")],
)
def test_a_leg_count_that_is_not_a_whole_number_or_a_ratio_below_one_is_refused(
    ratio: object, contracts: object, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        LegContracts(leg_id="leg", ratio=ratio, contracts=contracts)  # type: ignore[arg-type]


def test_no_legs_and_a_leg_named_twice_are_refused() -> None:
    with pytest.raises(ValueError, match="at least one leg"):
        whole_units([])
    with pytest.raises(ValueError, match="counted once"):
        whole_units(_legs(("leg", 1, 1), ("leg", 1, 1)))
