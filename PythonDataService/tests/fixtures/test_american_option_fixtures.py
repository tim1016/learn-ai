"""Golden parity for the American option pricer against QuantLib (AM-001 to AM-006)."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np

_SVC_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_SVC_ROOT))

from golden_support.registry import default as registry  # noqa: E402

from app.options.american import (  # noqa: E402
    DEFAULT_TREE_STEPS,
    AmericanDailyDecay,
    AmericanIvStatus,
    AmericanValue,
    CashDividend,
    american_daily_decay,
    american_implied_volatility,
    american_value,
)


def _cases(fixture_id: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    given = json.loads(registry.resolve(fixture_id, "input").read_text(encoding="utf-8"))["cases"]
    expected = {
        case["case"]: case
        for case in json.loads(registry.resolve(fixture_id, "output").read_text(encoding="utf-8"))["cases"]
    }
    return [(case, expected[case["case"]]) for case in given]


def _without_dividends(given: dict[str, Any]) -> dict[str, Any]:
    """AM-001 and AM-003 hold contracts on a stock that pays nothing; the pricer is told so."""
    return given | {"dividends": ()}


def _with_its_dividends(given: dict[str, Any]) -> dict[str, Any]:
    return given | {"dividends": tuple(CashDividend(**dividend) for dividend in given["dividends"])}


def _assert_close(values: list[AmericanValue | None], expected: list[dict[str, Any]], *, price: float, delta: float) -> None:
    assert all(value is not None for value in values)
    for field, atol in (("price", price), ("delta", delta)):
        np.testing.assert_allclose(
            [getattr(value, field) for value in values],
            [case[field] for case in expected],
            atol=atol,
            rtol=0,
            equal_nan=False,
            err_msg=field,
        )


def test_price_and_delta_without_dividends_match_quantlibs_leisen_reimer_tree() -> None:
    """AM-001: the same tree, step for step, so the two agree to rounding.

    The cases a day or less from expiry are the sensitive ones: the tree
    takes ``1 - exp(-x)`` of a very small ``x`` there, as QuantLib does, so
    the last bit of ``exp`` moves their price by up to 2e-9 at one day and
    2e-8 at fifteen minutes. If only those cases fail on another platform,
    that is why, and ``expm1`` is not the cure: it would stop matching the
    reference's own rounding.
    """
    tolerance = registry.get("AM-001").tolerance
    assert (tolerance.atol, tolerance.rtol) == (1e-9, 0)
    cases = _cases("AM-001")
    values = [american_value(**_without_dividends(case["input"])) for case, _ in cases]

    _assert_close(values, [expected for _, expected in cases], price=tolerance.atol, delta=tolerance.atol)


# AM-002 is a second engine's answer for the same model: QuantLib's finite-difference grid of 2,000
# time steps by 2,000 price steps. That grid is good to about 5e-5: doubling it both ways moves its
# answers by under 3e-5 in price and 3.4e-6 in delta, and on its 28 cases with no dividend it settles
# about 4e-5 from this tree. So the gap below is this tree's own discretization error, which is why
# the tolerance is the measured gap with headroom and not the strict default. No engine pinned here
# builds this tree with cash dividends, so there is nothing to match bit for bit.
# Largest gap over the 96 cases, as the tree's steps rise:
#     401 steps: price 3.2e-3, delta 4.2e-4      1,001: 1.4e-3, 1.5e-4      2,001: 5.9e-4, 7.2e-5
#     4,001: 1.8e-4, 3.9e-5      8,001: 2.0e-4, 2.0e-5      16,001: 5.8e-5, 1.2e-5
# The delta gap falls steadily. The price gap falls unevenly (1.3e-4 at 6,001, back up at 8,001)
# until it reaches what the grid itself is good to. It is the tree's error, not another model: under
# QuantLib's other cash-dividend model the same cases move by up to 8e-2 in price and 3e-3 in delta.
# Five cases are worth under 1e-3, four of them with a delta under 2e-4, so those pass at any step count.
_FINE_TREE_STEPS = 2001
_FINE_TREE_PRICE_ATOL = 1e-3
_FINE_TREE_DELTA_ATOL = 2e-4
# What the default step count gives up, against the same engine: half a cent, and a thousandth of
# delta. A delta rule's own tolerance is 0.05, so the step count moves a pick by under 2% of it.
_DEFAULT_STEPS_PRICE_ATOL = 5e-3
_DEFAULT_STEPS_DELTA_ATOL = 1e-3


def test_price_and_delta_with_cash_dividends_match_quantlibs_escrowed_finite_difference_engine() -> None:
    """AM-002 at 2,001 steps: as close as the tree comes at a step count a test can afford."""
    cases = _cases("AM-002")
    values = [american_value(**_with_its_dividends(case["input"]), steps=_FINE_TREE_STEPS) for case, _ in cases]

    _assert_close(
        values, [expected for _, expected in cases], price=_FINE_TREE_PRICE_ATOL, delta=_FINE_TREE_DELTA_ATOL
    )


def test_the_default_step_count_stays_within_a_thousandth_of_delta_of_the_second_engine() -> None:
    """AM-002 at the step count a bot prices with, with and without a dividend."""
    cases = _cases("AM-002")
    values = [american_value(**_with_its_dividends(case["input"]), steps=DEFAULT_TREE_STEPS) for case, _ in cases]

    _assert_close(
        values, [expected for _, expected in cases], price=_DEFAULT_STEPS_PRICE_ATOL, delta=_DEFAULT_STEPS_DELTA_ATOL
    )


def test_near_ex_dividend_price_and_delta_match_the_second_engine_at_fine_steps() -> None:
    """AM-006: the day-one dividend call that AM-002 did not cover, plus nearby cases."""
    cases = _cases("AM-006")
    values = [american_value(**_with_its_dividends(case["input"]), steps=2003) for case, _ in cases]

    _assert_close(values, [expected for _, expected in cases], price=3e-3, delta=1e-4)


def test_near_ex_dividend_default_price_stays_within_half_a_cent_of_the_second_engine() -> None:
    """AM-006: the default must price the call a day before ex-date more closely."""
    cases = _cases("AM-006")
    values = [american_value(**_with_its_dividends(case["input"])) for case, _ in cases]

    _assert_close(values, [expected for _, expected in cases], price=5e-3, delta=1e-3)


def test_near_ex_dividend_default_solves_the_second_engines_price_back_to_its_volatility() -> None:
    """AM-006: the improved price reaches implied volatility and the delta used to pick a leg."""
    cases = _cases("AM-006")
    solved = [
        american_implied_volatility(
            **{key: value for key, value in _with_its_dividends(given["input"]).items() if key != "volatility"},
            price=expected["price"],
        )
        for given, expected in cases
    ]

    assert [result.status for result in solved] == [AmericanIvStatus.SOLVED] * len(cases)
    np.testing.assert_allclose(
        [result.volatility for result in solved],
        [given["input"]["volatility"] for given, _ in cases],
        atol=5e-4,
        rtol=0,
    )
    np.testing.assert_allclose(
        [result.delta for result in solved],
        [expected["delta"] for _, expected in cases],
        atol=1e-3,
        rtol=0,
    )


# The tree and the engine that priced the fixture agree to about 4e-10 in price. Turned back into a
# volatility that is 4e-10 divided by the option's vega, which far from the money is small: the
# largest gap measured over the 140 cases is 7.2e-8. The delta at the solved volatility is then
# as close as the tree itself.
_IMPLIED_VOLATILITY_ATOL = 1e-6
_DELTA_AT_SOLVED_VOLATILITY_ATOL = 1e-9


def test_a_price_quantlib_computed_is_solved_back_to_the_volatility_it_was_computed_at() -> None:
    """AM-003: implied volatility and the delta that goes with it."""
    cases = _cases("AM-003")
    solved = [american_implied_volatility(**_without_dividends(case["input"])) for case, _ in cases]

    assert [result.status for result in solved] == [AmericanIvStatus.SOLVED] * len(cases)
    np.testing.assert_allclose(
        [result.volatility for result in solved],
        [expected["volatility"] for _, expected in cases],
        atol=_IMPLIED_VOLATILITY_ATOL,
        rtol=0,
        equal_nan=False,
        err_msg="volatility",
    )
    np.testing.assert_allclose(
        [result.delta for result in solved],
        [expected["delta"] for _, expected in cases],
        atol=_DELTA_AT_SOLVED_VOLATILITY_ATOL,
        rtol=0,
        equal_nan=False,
        err_msg="delta",
    )


# With dividends the price comes from the other engine, so the volatility found is off by the two
# engines' price gap divided by the option's vega. Within one standard deviation of the money, where
# a delta rule picks and vega is large, the largest gap over the 52 cases is 3.0e-4 in volatility
# and 5.0e-4 in the delta found. Farther out vega falls away and the same price gap is worth a
# volatility of 0.1; those cases are left to the price test above. Solving the dividend cases as if
# the stock paid nothing misses the volatility by 2e-2 or more, forty times this tolerance.
_VOLATILITY_FROM_THE_SECOND_ENGINE_ATOL = 5e-4
_DELTA_FROM_THE_SECOND_ENGINE_ATOL = 1e-3


def test_a_price_the_second_engine_computed_with_dividends_is_solved_back_to_its_volatility() -> None:
    """AM-002 through the solver: the dividends reach the implied volatility, not only the price."""
    near_the_money = [
        (case["input"], expected)
        for case, expected in _cases("AM-002")
        if abs(math.log(case["input"]["strike"] / case["input"]["spot"]))
        <= case["input"]["volatility"] * math.sqrt(case["input"]["years_to_expiry"])
    ]
    assert len(near_the_money) == 52
    solved = [
        american_implied_volatility(
            **{key: value for key, value in _with_its_dividends(given).items() if key != "volatility"},
            price=expected["price"],
        )
        for given, expected in near_the_money
    ]

    assert [result.status for result in solved] == [AmericanIvStatus.SOLVED] * len(near_the_money)
    np.testing.assert_allclose(
        [result.volatility for result in solved],
        [given["volatility"] for given, _ in near_the_money],
        atol=_VOLATILITY_FROM_THE_SECOND_ENGINE_ATOL,
        rtol=0,
        equal_nan=False,
        err_msg="volatility",
    )
    np.testing.assert_allclose(
        [result.delta for result in solved],
        [expected["delta"] for _, expected in near_the_money],
        atol=_DELTA_FROM_THE_SECOND_ENGINE_ATOL,
        rtol=0,
        equal_nan=False,
        err_msg="delta",
    )


def _assert_decay_close(
    decays: list[AmericanDailyDecay | None], expected: list[dict[str, Any]], *, atol: float, group: str = ""
) -> None:
    """The decay, and the two prices it was worked from, against the reference engine's."""
    assert all(decay is not None for decay in decays)
    # Every contract here has over a day and a minute left, so both prices are the model's.
    assert not any(decay.later_price_is_exercise_value for decay in decays)
    for field in ("decay", "price_now", "price_a_day_later"):
        np.testing.assert_allclose(
            [getattr(decay, field) for decay in decays],
            [case[field] for case in expected],
            atol=atol,
            rtol=0,
            equal_nan=False,
            err_msg=f"{group} {field}".strip(),
        )


def test_a_days_decay_without_dividends_matches_the_difference_of_two_quantlib_tree_prices() -> None:
    """AM-004: QuantLib's price of the same contract 24 hours on, less its price now.

    Each price is the same tree as QuantLib's, step for step, so on these
    cases the difference holds the strict default: the largest gap measured
    is 4.7e-10, at a day and fifteen minutes to expiry, and 8.9e-11 with a
    day and two hours or more. That is these cases' figure, not a bound:
    another contract a day and fifteen minutes out was measured at 1.6e-9.
    The cases whose later price is a day or less from expiry are the
    sensitive ones, for the reason AM-001's test gives.
    """
    tolerance = registry.get("AM-004").tolerance
    assert (tolerance.atol, tolerance.rtol) == (1e-9, 0)
    cases = _cases("AM-004")
    decays = [american_daily_decay(**_without_dividends(case["input"])) for case, _ in cases]

    _assert_decay_close(decays, [expected for _, expected in cases], atol=tolerance.atol)
    assert {decay.cash_gone_ex for decay in decays} == {0.0}


# AM-005 is the second engine's decay: its price of the same contract a day on, less its price now,
# with cash dividends. As with AM-002 the gap is this tree's own discretization error, not rounding,
# and no engine pinned here builds this tree with cash dividends. Every figure below is a gap
# measured on this fixture's 100 cases. None is a bound on the tree: the attribution records a call
# outside the fixture that is 2e-2 out at 401 steps.
# Largest gap in the decay over the 100 cases, as the tree's steps rise:
#     401 steps: 8.1e-3      1,009: 8.9e-4      2,003: 9.4e-4      4,001: 2.2e-4
# At 2,003 steps one figure holds the decay and its two prices: 2e-3, twice the 9.5e-4 measured and
# twice what AM-002 holds one price to at 2,001. Half of the cases are within 2e-6 there.
# At the default 401 steps half of the cases are within 1e-5 and a few are far out, so each group
# of cases is held to about twice its own largest gap, in the decay or in either of its prices:
#     group                                  decay     price now   a day later   held to
#     30 days, no dividend                   1.3e-4    2.5e-4      2.4e-4        5e-4
#     30 days, ex on day 12                  8.0e-4    8.1e-4      8.5e-4        2e-3
#     7 days, ex on day 3                    6.7e-4    8.9e-4      3.8e-4        2e-3
#     60 days, ex on day 1 and day 40        1.3e-3    1.5e-3      1.3e-3        3e-3
#     30 days, ex on day 1                   5.0e-3    5.0e-3      2.6e-4        1e-2
#     60 days, ex on day 2                   8.1e-3    7.2e-4      8.0e-3        1e-2
# The last two keep the looser figure because one of their two prices is a day before an ex-date:
# the price now when the stock goes ex on day 1, the later price when it goes ex on day 2. This tree
# is at its coarsest there for a call in the money. That is the tree's step error against another
# method's answer, the accepted divergence ADR 0076 decision 6 grants this tree with dividends. It
# is not a figure loosened to pass: at 2,003 steps the same two groups are within 1e-3. The day-2
# group is held to 1e-2, 1.2 times its gap, and not to twice it: 1e-2 is the most this fixture has
# been held to, and adding its worst kind of case did not raise it.
# What these tolerances tell apart: holding the quoted price across an ex-date moves the 32 cases
# that go ex inside the day by 6.4e-2 to 1.75, and leaving the other dividends where they were, not
# a day nearer, moves 29 of 60 cases by over 2e-3, up to 6.2e-2. What they do not: taking off the
# dividend's present value in place of the cash in full moves the later price by at most 2.2e-4.
# tests/options/test_american_daily_decay.py pins that choice directly.
# The step counts are prime. An ex-date exactly on a tree step is counted before or after that step
# by the last bit of the two times, and at AM-002's 2,001 steps, 69 to the day over 29 days, every
# whole day of a 29-day span is on one. A prime count over the days to expiry has no whole day on a step.
_FINE_DECAY_TREE_STEPS = 2003
_FINE_TREE_DECAY_ATOL = 2e-3
_DEFAULT_STEPS_DECAY_ATOL_BY_GROUP = {
    "30d_no_dividend": 5e-4,
    "30d_ex_on_day_12": 2e-3,
    "7d_ex_on_day_3": 2e-3,
    "60d_ex_inside_the_day_and_on_day_40": 3e-3,
    "30d_ex_inside_the_day": 1e-2,
    "60d_ex_on_day_2": 1e-2,
}


def _decays_with_dividends(steps: int) -> tuple[list[AmericanDailyDecay | None], list[dict[str, Any]]]:
    cases = _cases("AM-005")
    decays = [american_daily_decay(**_with_its_dividends(case["input"]), steps=steps) for case, _ in cases]
    return decays, [expected for _, expected in cases]


def _group(case: dict[str, Any]) -> str:
    """The group a case of AM-005 belongs to: its name up to the volatility."""
    return case["case"].split("_vol")[0]


def test_a_days_decay_with_cash_dividends_matches_the_second_engines_difference() -> None:
    """AM-005 at 2,003 steps, with the dividend that goes ex inside the day taken off the stock price."""
    decays, expected = _decays_with_dividends(_FINE_DECAY_TREE_STEPS)

    _assert_decay_close(decays, expected, atol=_FINE_TREE_DECAY_ATOL)
    assert [decay.cash_gone_ex for decay in decays] == [case["cash_gone_ex"] for case in expected]


def test_a_days_decay_at_the_default_step_count_stays_close_to_the_second_engine_group_by_group() -> None:
    """AM-005 at the step count a bot prices with, each group of cases held to its own figure."""
    decays, expected = _decays_with_dividends(DEFAULT_TREE_STEPS)
    cases_of: dict[str, list[int]] = {}
    for index, case in enumerate(expected):
        cases_of.setdefault(_group(case), []).append(index)

    assert cases_of.keys() == _DEFAULT_STEPS_DECAY_ATOL_BY_GROUP.keys()
    for group, atol in _DEFAULT_STEPS_DECAY_ATOL_BY_GROUP.items():
        _assert_decay_close(
            [decays[index] for index in cases_of[group]],
            [expected[index] for index in cases_of[group]],
            atol=atol,
            group=group,
        )


def test_a_days_decay_with_adaptive_default_stays_close_near_an_ex_date() -> None:
    """AM-005: default call pricing uses more steps with cash dividends."""
    cases = _cases("AM-005")
    decays = [american_daily_decay(**_with_its_dividends(case["input"])) for case, _ in cases]

    _assert_decay_close(decays, [expected for _, expected in cases], atol=2e-3)
