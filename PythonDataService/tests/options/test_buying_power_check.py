"""The arithmetic of an option entry's comparison with a broker's option buying power (#2913).

Hand-derived cases, in whole cents. Two of them are the PRD's own figures:
the call bought for $1.94 a share (premium $194.00) and the screens' entry
that set aside $193.37 and saw a fall of $193.00.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.options.buying_power_check import (
    BuyingPowerCheckResult,
    judge_fall,
    judge_part_fill,
    requirement_for_filled_cents,
    set_aside_for_filled_cents,
    whole_cents,
)

CONSISTENT = BuyingPowerCheckResult.CONSISTENT
OVER = BuyingPowerCheckResult.OVER_RESERVED
UNDER = BuyingPowerCheckResult.UNDER_RESERVED_UNEXPLAINED
UNKNOWN = BuyingPowerCheckResult.UNKNOWN


@pytest.mark.parametrize(
    ("first", "second", "cost", "set_aside", "expected"),
    [
        # The screens' entry: $193.00 of premium, $193.37 set aside with its fees, a fall of $193.00.
        (10_000_000, 9_980_700, 19_300, 19_337, CONSISTENT),
        # The same entry and the made-up notice: a fall of $250.00.
        (10_000_000, 9_975_000, 19_300, 19_337, UNDER),
        # A call bought at its $1.94 limit: the fall is the premium, and the six cents of fees are not in it.
        (10_000_000, 9_980_600, 19_400, 19_406, CONSISTENT),
        # Alpaca took a cent more than the premium, inside the set-aside.
        (10_000_000, 9_980_599, 19_400, 19_406, OVER),
        # Alpaca took exactly what was set aside: more than the cost, and not more than was set aside.
        (10_000_000, 9_980_594, 19_400, 19_406, OVER),
        # One cent more than was set aside.
        (10_000_000, 9_980_593, 19_400, 19_406, UNDER),
        # Alpaca took a cent less than the premium paid.
        (10_000_000, 9_980_601, 19_400, 19_406, OVER),
        # The figure did not move at all, and it rose: neither is more than was set aside.
        (10_000_000, 10_000_000, 19_400, 19_406, OVER),
        (10_000_000, 10_005_000, 19_400, 19_406, OVER),
        # Nothing filled and nothing fell.
        (10_000_000, 10_000_000, 0, 0, CONSISTENT),
        # A fall that is the cost is consistent even where the cost is above the set-aside: the fills explain it.
        (10_000_000, 9_980_000, 20_000, 19_406, CONSISTENT),
    ],
)
def test_a_fall_is_judged_against_the_cost_of_what_filled_and_against_what_was_set_aside(
    first: int, second: int, cost: int, set_aside: int, expected: BuyingPowerCheckResult
) -> None:
    assert (
        judge_fall(
            first_cents=first, second_cents=second, cost_of_filled_cents=cost, set_aside_for_filled_cents=set_aside
        )
        is expected
    )


def test_a_comparable_pair_is_never_judged_unknown() -> None:
    results = {
        judge_fall(first_cents=1_000, second_cents=second, cost_of_filled_cents=300, set_aside_for_filled_cents=310)
        for second in range(0, 1_200)
    }

    assert results == {CONSISTENT, OVER, UNDER}


@pytest.mark.parametrize(
    ("second", "expected"),
    [
        # Two calls at a $1.94 limit set aside $388.00 and nine cents of fees. One filled; the other was cancelled.
        # Alpaca shows only the contract that filled taken: not called consistent, on a guess that the rest is back.
        (9_980_600, UNKNOWN),
        # It still shows the whole order taken, $388.00: inside everything the entry set aside.
        (9_961_200, UNKNOWN),
        # Exactly everything the entry set aside, $388.09.
        (9_961_191, UNKNOWN),
        # One cent more than that: nothing of the entry explains it.
        (9_961_190, UNDER),
        (9_950_000, UNDER),
        # No fall, and a rise.
        (10_000_000, UNKNOWN),
        (10_005_000, UNKNOWN),
    ],
)
def test_a_part_fill_is_held_to_everything_the_entry_set_aside_and_judged_by_nothing_finer(
    second: int, expected: BuyingPowerCheckResult
) -> None:
    assert judge_part_fill(first_cents=10_000_000, second_cents=second, commitment_cents=38_809) is expected


def test_a_part_fill_is_never_called_consistent_or_over_reserved() -> None:
    results = {
        judge_part_fill(first_cents=100_000, second_cents=second, commitment_cents=38_809)
        for second in range(0, 120_000, 7)
    }

    assert results == {UNKNOWN, UNDER}


@pytest.mark.parametrize("not_cents", [1.0, "100", None, True])
def test_a_part_fills_judgment_refuses_a_reading_that_is_not_whole_cents(not_cents: object) -> None:
    with pytest.raises(ValueError, match="whole cents"):
        judge_part_fill(first_cents=not_cents, second_cents=0, commitment_cents=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="commitment_cents"):
        judge_part_fill(first_cents=0, second_cents=0, commitment_cents=0)


@pytest.mark.parametrize(
    ("commitment", "asked", "filled", "expected"),
    [
        (19_406, 1, 1, 19_406),
        (19_406, 1, 0, 0),
        # Three contracts at $1.94 with eight cents of fees: $582.08. Two of the three: 38,805.33 cents, up to the cent.
        (58_208, 3, 2, 38_806),
        (58_208, 3, 1, 19_403),
        (58_208, 3, 3, 58_208),
        # A share that divides evenly is not moved.
        (58_200, 3, 2, 38_800),
    ],
)
def test_what_was_set_aside_for_a_part_fill_is_its_share_by_contracts_rounded_up_to_the_cent(
    commitment: int, asked: int, filled: int, expected: int
) -> None:
    assert (
        set_aside_for_filled_cents(commitment_cents=commitment, contracts_asked=asked, contracts_filled=filled)
        == expected
    )


def test_more_contracts_filled_than_asked_for_is_refused_not_scaled() -> None:
    with pytest.raises(ValueError, match="2 contracts filled of 1"):
        set_aside_for_filled_cents(commitment_cents=19_406, contracts_asked=1, contracts_filled=2)
    with pytest.raises(ValueError, match="2 structures filled of 1"):
        requirement_for_filled_cents(requirement_cents=50_000, units_asked=1, units_filled=2)


def test_a_requirement_is_shared_by_whole_structures_and_never_rounded() -> None:
    # A bought option has none. A $5-wide spread sold twice has $1,000.00; one of the two is $500.00.
    assert requirement_for_filled_cents(requirement_cents=0, units_asked=3, units_filled=2) == 0
    assert requirement_for_filled_cents(requirement_cents=100_000, units_asked=2, units_filled=1) == 50_000
    assert requirement_for_filled_cents(requirement_cents=100_000, units_asked=2, units_filled=0) == 0
    with pytest.raises(ValueError, match="not a whole number of cents each"):
        requirement_for_filled_cents(requirement_cents=100_001, units_asked=2, units_filled=1)


@pytest.mark.parametrize(
    ("dollars", "cents"),
    [
        (Decimal("100000"), 10_000_000),
        (Decimal("99806.00"), 9_980_600),
        (Decimal("0.01"), 1),
        (Decimal("-12.34"), -1_234),
        (Decimal("99806.005"), None),
        (Decimal("0.001"), None),
        (Decimal("NaN"), None),
        (Decimal("Infinity"), None),
    ],
)
def test_a_figure_with_a_fraction_of_a_cent_is_not_made_into_cents(dollars: Decimal, cents: int | None) -> None:
    assert whole_cents(dollars) == cents


@pytest.mark.parametrize("not_cents", [1.0, "100", None, True])
def test_a_reading_that_is_not_whole_cents_is_refused_by_the_judgment(not_cents: object) -> None:
    with pytest.raises(ValueError, match="whole cents"):
        judge_fall(
            first_cents=not_cents,  # type: ignore[arg-type]
            second_cents=0, cost_of_filled_cents=0, set_aside_for_filled_cents=0,
        )
