"""Golden parity for the American option pricer against QuantLib (AM-001, AM-002, AM-003)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

_SVC_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_SVC_ROOT))

from golden_support.registry import default as registry  # noqa: E402

from app.options.american import (  # noqa: E402
    AmericanIvStatus,
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


def test_price_and_delta_without_dividends_match_quantlibs_leisen_reimer_tree() -> None:
    """AM-001: the same tree, step for step, so the two agree to rounding."""
    tolerance = registry.get("AM-001").tolerance
    cases = _cases("AM-001")
    values = [american_value(**case["input"]) for case, _ in cases]

    assert all(value is not None for value in values)
    for field in ("price", "delta"):
        np.testing.assert_allclose(
            [getattr(value, field) for value in values],
            [expected[field] for _, expected in cases],
            atol=tolerance.atol,
            rtol=tolerance.rtol,
            equal_nan=False,
            err_msg=field,
        )


# AM-002 compares two different numerical schemes for one model: this tree, and QuantLib's
# finite-difference grid of 2,000 time steps by 2,000 price steps. They agree only as closely as
# both have converged, so the tolerance is the measured gap with headroom, not the strict default.
# Measured on generation, largest gap over the 68 cases, as the tree's steps rise:
#     401 steps: price 3.2e-3, delta 3.8e-4;  2,001: 5.9e-4, 7.2e-5;  4,001: 1.8e-4, 3.9e-5.
# The gap falls roughly in proportion to the steps, which is discretization, not a different model:
# under QuantLib's other dividend model the same cases move by up to 2.5e-2 in price.
_DIVIDEND_TREE_STEPS = 2001
_DIVIDEND_PRICE_ATOL = 1e-3
_DIVIDEND_DELTA_ATOL = 2e-4


def test_price_and_delta_with_cash_dividends_match_quantlibs_escrowed_finite_difference_engine() -> None:
    """AM-002: a second engine's answer for the same model."""
    cases = _cases("AM-002")
    values = []
    for case, _ in cases:
        given = dict(case["input"])
        dividends = tuple(CashDividend(**dividend) for dividend in given.pop("dividends"))
        values.append(american_value(**given, dividends=dividends, steps=_DIVIDEND_TREE_STEPS))

    assert all(value is not None for value in values)
    np.testing.assert_allclose(
        [value.price for value in values],
        [expected["price"] for _, expected in cases],
        atol=_DIVIDEND_PRICE_ATOL,
        rtol=0,
        equal_nan=False,
        err_msg="price",
    )
    np.testing.assert_allclose(
        [value.delta for value in values],
        [expected["delta"] for _, expected in cases],
        atol=_DIVIDEND_DELTA_ATOL,
        rtol=0,
        equal_nan=False,
        err_msg="delta",
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
    solved = [american_implied_volatility(**case["input"]) for case, _ in cases]

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
