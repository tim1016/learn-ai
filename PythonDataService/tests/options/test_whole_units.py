"""How many whole structures a set of legs comes to, and whether the legs are in step (#2913).

Every figure is worked by hand in the test that asserts it. All of it is
whole numbers, so every comparison is exact.
"""

from __future__ import annotations

import pytest

from app.options.whole_units import LegContracts, in_leg_direction, whole_units


def _legs(*counted: tuple[str, int, int]) -> list[LegContracts]:
    return [LegContracts(leg_id=leg_id, ratio=ratio, contracts=contracts) for leg_id, ratio, contracts in counted]


@pytest.mark.parametrize(
    ("counted", "units", "in_step", "beyond"),
    [
        pytest.param(
            (("bought", 1, 2), ("sold", 1, 2)), 2, True, (("bought", 0), ("sold", 0)),
            id="two legs filled alike are that many units in step",
        ),
        # An order for two units that filled two of one leg and one of the other holds one unit.
        # One contract of the bought leg is beyond the one whole unit.
        pytest.param(
            (("bought", 1, 2), ("sold", 1, 1)), 1, False, (("bought", 1), ("sold", 0)),
            id="the units are the smallest whole number across the legs",
        ),
        # A 1:2 spread. Three of the first leg and six of the second are three units: 3 // 1 = 3, 6 // 2 = 3.
        pytest.param(
            (("one", 1, 3), ("two", 2, 6)), 3, True, (("one", 0), ("two", 0)),
            id="a leg is counted by its ratio: three and six",
        ),
        # Three and five: 5 // 2 = 2 units. Beyond them: 3 - 2 = 1 of the first, 5 - 4 = 1 of the second.
        pytest.param(
            (("one", 1, 3), ("two", 2, 5)), 2, False, (("one", 1), ("two", 1)),
            id="a leg is counted by its ratio: three and five",
        ),
        pytest.param(
            (("bought", 1, 1), ("sold", 1, 0)), 0, False, (("bought", 1), ("sold", 0)),
            id="a fill on one leg only is no unit and out of step",
        ),
        pytest.param(
            (("bought", 1, 0), ("sold", 1, 0)), 0, True, (("bought", 0), ("sold", 0)),
            id="nothing filled is no unit and in step",
        ),
        # One leg is in its direction by two; the other is the wrong way round by one.
        pytest.param(
            (("bought", 1, 2), ("sold", 1, -1)), 0, False, (("bought", 2), ("sold", -1)),
            id="a leg held the other way round counts below zero and never as a unit",
        ),
        # With a ratio of two, floor division still never rounds a wrong-way leg up to nothing.
        pytest.param(
            (("one", 1, 4), ("two", 2, -1)), 0, False, (("one", 4), ("two", -1)),
            id="a wrong-way leg with a ratio of two is still no unit",
        ),
        pytest.param((("only", 1, 5),), 5, True, (("only", 0),), id="one leg alone is its contracts over its ratio"),
    ],
)
def test_legs_come_to_whole_units(
    counted: tuple[tuple[str, int, int], ...], units: int, in_step: bool, beyond: tuple[tuple[str, int], ...]
) -> None:
    came_to = whole_units(_legs(*counted))

    assert came_to.units == units, "the whole units the legs come to"
    assert came_to.in_step is in_step, "whether the legs are in step"
    assert dict(came_to.beyond) == dict(beyond), "the contracts of each leg beyond the whole units"


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
