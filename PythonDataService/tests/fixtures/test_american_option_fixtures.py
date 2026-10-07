"""Golden parity for the American option pricer against QuantLib (AM-001, AM-002, AM-003)."""

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
    AmericanIvStatus,
    AmericanValue,
    CashDividend,
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
# time steps by 2,000 price steps. That grid has converged: doubling it both ways moves its answers
# by 2.6e-5 in price and 3.4e-6 in delta. So the gap below is this tree's own discretization error,
# which is why the tolerance is the measured gap with headroom and not the strict default. No
# engine pinned here builds this tree with cash dividends, so there is nothing to match bit for bit.
# Largest gap over the 96 cases, as the tree's steps rise:
#     401 steps: price 3.2e-3, delta 4.2e-4      1,001: 1.4e-3, 1.5e-4      2,001: 5.9e-4, 7.2e-5
#     4,001: 1.8e-4, 3.9e-5                      8,001: 2.0e-4, 2.0e-5
# The price gap stops falling near 2e-4: the tree's last step before an ex-date sits up to one
# step's interest on the strike away from it. It is the tree's error, not another model: under
# QuantLib's other cash-dividend model the same cases move by up to 8e-2 in price and 3e-3 in delta.
# Five cases are worth under 1e-3 with a delta under 2e-4, so they pass at any step count.
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
