"""Which way a structure leans: its net delta in shares of the underlying. Expected values are by hand."""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from app.options.contract import OptionContract
from app.options.lean import ABOUT_FLAT_BELOW_DELTA, LeanDirection, lean
from app.options.requirement import EntrySize
from app.options.structure import ResolvedLeg, ResolvedStructure

_FRIDAY = date(2026, 10, 9)
_ONE = EntrySize(multiplier=100, units=1)


def _leg(leg_id: str, position: str, right: str, strike: int, *, ratio: int = 1) -> ResolvedLeg:
    return ResolvedLeg(
        leg_id=leg_id,
        contract=OptionContract.of(root="SPY", expiry_date=_FRIDAY, right=right, strike=strike),
        position=position,
        ratio=ratio,
    )


def _of(*legs: ResolvedLeg) -> ResolvedStructure:
    return ResolvedStructure(legs=legs)


def _close(actual: float, expected: float) -> None:
    np.testing.assert_allclose(actual, expected, atol=1e-9, rtol=0, equal_nan=False)


@pytest.mark.parametrize(
    ("multiplier", "units", "bought_delta", "sold_delta", "net_delta", "shares_per_structure", "shares"),
    [
        pytest.param(
            100, 1, 0.48, 0.31, 0.17, 17.0, 17.0, id="a call spread with deltas of 0.48 and 0.31 moves like about 17 shares"
        ),
        pytest.param(
            10, 4, 0.5, 0.25, 0.25, 2.5, 10.0, id="the shares a structure moves like are counted at its own contracts size"
        ),
    ],
)
def test_the_shares_a_call_spread_moves_like_are_its_net_delta_at_its_own_contracts_size(
    multiplier: int,
    units: int,
    bought_delta: float,
    sold_delta: float,
    net_delta: float,
    shares_per_structure: float,
    shares: float,
) -> None:
    """Bought 0.48, sold 0.31: a net delta of 0.17, and 0.17 x 100 shares a contract is 17 shares. A net delta of
    0.25 on a contract of ten shares moves like 2.5 shares, not 25; four of them like 10."""
    spread = _of(_leg("bought", "long", "call", 585), _leg("sold", "short", "call", 590))

    leaning = lean(spread, EntrySize(multiplier=multiplier, units=units), {"bought": bought_delta, "sold": sold_delta})

    _close(leaning.net_delta, net_delta)
    _close(leaning.shares_per_structure, shares_per_structure)
    _close(leaning.shares, shares)
    assert leaning.direction is LeanDirection.UP, "a positive net delta leans up"


def test_a_bought_put_leans_down_and_a_sold_put_leans_up() -> None:
    """A put's delta is below zero. Bought at -0.40 it moves like 40 shares sold short; sold, like 40 held."""
    bought = lean(_of(_leg("only", "long", "put", 585)), _ONE, {"only": -0.40})
    # A put spread sold: the 585 put sold at -0.40 and the 580 bought at -0.25 leave +0.40 - 0.25 = +0.15.
    sold = lean(
        _of(_leg("sold", "short", "put", 585), _leg("bought", "long", "put", 580)), _ONE, {"sold": -0.40, "bought": -0.25}
    )

    _close(bought.shares, -40.0)
    assert bought.direction is LeanDirection.DOWN
    _close(sold.shares, 15.0)
    assert sold.direction is LeanDirection.UP


def test_a_legs_ratio_and_the_number_of_structures_both_count() -> None:
    """A butterfly: 0.70 bought, two 0.50 sold, 0.28 bought: 0.70 - 2 x 0.50 + 0.28 = -0.02 a share. Three of
    them move like 3 x 100 x -0.02 = -6 shares."""
    butterfly = _of(
        _leg("lower", "long", "call", 580), _leg("body", "short", "call", 585, ratio=2), _leg("upper", "long", "call", 590)
    )

    leaning = lean(butterfly, EntrySize(multiplier=100, units=3), {"lower": 0.70, "body": 0.50, "upper": 0.28})

    _close(leaning.net_delta, -0.02)
    _close(leaning.shares_per_structure, -2.0)
    _close(leaning.shares, -6.0)
    assert leaning.direction is LeanDirection.ABOUT_FLAT


@pytest.mark.parametrize(
    ("call_delta", "put_delta", "direction"),
    [
        pytest.param(0.50, -0.50, LeanDirection.ABOUT_FLAT, id="a straddle at the money: 0.50 - 0.50"),
        pytest.param(0.5625, -0.5, LeanDirection.UP, id="0.0625 up: over the threshold"),
        pytest.param(0.53125, -0.5, LeanDirection.ABOUT_FLAT, id="0.03125 up: under it"),
        pytest.param(0.5, -0.5625, LeanDirection.DOWN, id="0.0625 down"),
        pytest.param(0.5, -0.53125, LeanDirection.ABOUT_FLAT, id="0.03125 down"),
    ],
)
def test_a_net_delta_under_the_threshold_is_about_flat_either_way(
    call_delta: float, put_delta: float, direction: LeanDirection
) -> None:
    """The deltas are halves, eighths and sixteenths, so each sum is exact."""
    straddle = _of(_leg("call", "long", "call", 585), _leg("put", "long", "put", 585))

    assert ABOUT_FLAT_BELOW_DELTA == 0.05
    assert lean(straddle, _ONE, {"call": call_delta, "put": put_delta}).direction is direction


def test_a_leg_with_no_delta_gives_no_lean_at_all() -> None:
    spread = _of(_leg("bought", "long", "call", 585), _leg("sold", "short", "call", 590))

    with pytest.raises(ValueError, match="one delta for each"):
        lean(spread, _ONE, {"bought": 0.48})
    with pytest.raises(ValueError, match="finite"):
        lean(spread, _ONE, {"bought": 0.48, "sold": float("nan")})
