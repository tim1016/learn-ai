"""The American pricer beyond what golden fixtures AM-001 to AM-003 pin: refusals, and what the step count costs."""

from __future__ import annotations

import math

import numpy as np
import pytest

from app.options.american import (
    DEFAULT_TREE_STEPS,
    AmericanIvStatus,
    CashDividend,
    american_implied_volatility,
    american_value,
)

_THIRTY_DAYS = 30 / 365
_FIFTEEN_MINUTES = 15 / (60 * 24 * 365)
_AT_THE_MONEY_PUT = {"spot": 100.0, "strike": 100.0, "years_to_expiry": _THIRTY_DAYS, "rate": 0.045, "right": "put"}


@pytest.mark.parametrize(
    ("changes", "status"),
    [
        pytest.param(
            {"strike": 130.0, "price": 29.5}, AmericanIvStatus.BELOW_EXERCISE_VALUE, id="a price under what exercising now pays"
        ),
        pytest.param(
            {"strike": 130.0, "price": 30.0},
            AmericanIvStatus.BELOW_VOLATILITY_FLOOR,
            id="a deep put priced at exactly its exercise value, which every low volatility explains",
        ),
        pytest.param({"price": 60.0}, AmericanIvStatus.ABOVE_VOLATILITY_CEILING, id="a price no volatility up to 500% reaches"),
        pytest.param(
            {"years_to_expiry": 30 / (60 * 60 * 24 * 365), "price": 0.05},
            AmericanIvStatus.TIME_TO_EXPIRY_BELOW_FLOOR,
            id="thirty seconds to expiry",
        ),
        pytest.param({"spot": 0.0, "price": 2.0}, AmericanIvStatus.INVALID_INPUT, id="no stock price"),
        pytest.param({"price": 0.0}, AmericanIvStatus.INVALID_INPUT, id="no option price"),
        pytest.param({"price": float("nan")}, AmericanIvStatus.INVALID_INPUT, id="an option price that is no number"),
        pytest.param({"price": 2.0, "steps": 400}, AmericanIvStatus.INVALID_INPUT, id="an even number of steps"),
        pytest.param(
            {"price": 2.0, "dividends": (CashDividend(years_to_ex=10 / 365, cash=150.0),)},
            AmericanIvStatus.INVALID_INPUT,
            id="a dividend worth more than the stock",
        ),
        pytest.param(
            {"price": 2.0, "dividends": (CashDividend(years_to_ex=10 / 365, cash=-1.0),)},
            AmericanIvStatus.INVALID_INPUT,
            id="a negative dividend",
        ),
    ],
)
def test_a_solve_with_no_answer_says_why_and_carries_no_number(changes: dict, status: AmericanIvStatus) -> None:
    """A failure must never look like a volatility: a contract with one is left out, not picked on a guess."""
    result = american_implied_volatility(**(_AT_THE_MONEY_PUT | changes))

    assert result.status is status
    assert result.volatility is None
    assert result.delta is None
    assert result.detail


def test_a_strike_dozens_of_standard_deviations_away_is_not_priced() -> None:
    """Fifteen minutes out, a strike 13% away is over a hundred standard deviations off. The tree's
    branch sizes would be rounding error there, so it answers nothing and never a NaN."""
    far = {"spot": 100.0, "strike": 113.0, "years_to_expiry": _FIFTEEN_MINUTES, "rate": 0.045, "right": "call"}

    assert american_value(**far, volatility=0.2) is None
    # A penny on that call needs more volatility than the model will try.
    assert american_implied_volatility(**far, price=0.01).status is AmericanIvStatus.ABOVE_VOLATILITY_CEILING


def test_a_far_strike_is_still_solved_once_enough_volatility_resolves_it() -> None:
    """The lowest volatility tried rises until the tree resolves the strike; the answer is above it."""
    near = {"spot": 100.0, "strike": 101.0, "years_to_expiry": _FIFTEEN_MINUTES, "rate": 0.045, "right": "call"}

    assert american_value(**near, volatility=0.005) is None
    priced = american_value(**near, volatility=1.2)
    assert priced is not None
    solved = american_implied_volatility(**near, price=priced.price)
    assert solved.status is AmericanIvStatus.SOLVED
    assert solved.volatility == pytest.approx(1.2, abs=1e-6, rel=0)


@pytest.mark.parametrize(
    "dividend",
    [
        pytest.param(CashDividend(years_to_ex=45 / 365, cash=1.75), id="going ex after the option expires"),
        pytest.param(CashDividend(years_to_ex=0.0, cash=1.75), id="already ex"),
        pytest.param(CashDividend(years_to_ex=-3 / 365, cash=1.75), id="ex three days ago"),
    ],
)
def test_a_dividend_outside_the_options_life_changes_nothing(dividend: CashDividend) -> None:
    without = american_value(**_AT_THE_MONEY_PUT, volatility=0.25)
    with_it = american_value(**_AT_THE_MONEY_PUT, volatility=0.25, dividends=(dividend,))

    assert with_it == without


def test_a_deep_call_is_exercised_just_before_the_stock_goes_ex() -> None:
    """Without a dividend an American call is never exercised early: it is worth the stock less the
    strike discounted to expiry. With $1.75 going ex tomorrow a deep call is exercised the moment
    before, to own the stock while it still carries the dividend: it is worth the stock less the
    strike discounted one day, and no more."""
    deep_call = {"spot": 100.0, "strike": 60.0, "years_to_expiry": _THIRTY_DAYS, "rate": 0.045, "right": "call"}
    tomorrow = 1 / 365

    held_to_expiry = american_value(**deep_call, volatility=0.25)
    exercised_before_ex = american_value(
        **deep_call, volatility=0.25, dividends=(CashDividend(years_to_ex=tomorrow, cash=1.75),)
    )

    assert held_to_expiry is not None and exercised_before_ex is not None
    assert held_to_expiry.price == pytest.approx(100.0 - 60.0 * math.exp(-0.045 * _THIRTY_DAYS), abs=1e-6, rel=0)
    # Exercise falls on the tree's last step before the ex-date, so within one step's interest on the strike.
    one_steps_interest = 60.0 * 0.045 * _THIRTY_DAYS / DEFAULT_TREE_STEPS
    assert exercised_before_ex.price == pytest.approx(
        100.0 - 60.0 * math.exp(-0.045 * tomorrow), abs=one_steps_interest, rel=0
    )


def test_the_default_step_count_costs_under_a_thousandth_of_delta() -> None:
    """What 401 steps give up against a tree ten times finer, on contracts with and without a dividend.
    A delta rule's own tolerance is 0.05, so the step count moves a pick by under 2% of it."""
    dividend = (CashDividend(years_to_ex=12 / 365, cash=1.75),)
    gaps = []
    for right in ("call", "put"):
        for strike in (90.0, 95.0, 100.0, 105.0, 110.0):
            for dividends in ((), dividend):
                contract = {
                    "spot": 100.0,
                    "strike": strike,
                    "years_to_expiry": _THIRTY_DAYS,
                    "rate": 0.045,
                    "volatility": 0.25,
                    "right": right,
                    "dividends": dividends,
                }
                coarse = american_value(**contract, steps=DEFAULT_TREE_STEPS)
                fine = american_value(**contract, steps=4001)
                assert coarse is not None and fine is not None
                gaps.append(abs(coarse.delta - fine.delta))

    assert max(gaps) < 1e-3
    assert all(math.isfinite(gap) for gap in gaps)


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"volatility": 0.0}, id="no volatility"),
        pytest.param({"volatility": float("inf")}, id="infinite volatility"),
        pytest.param({"strike": -5.0}, id="a negative strike"),
        pytest.param({"years_to_expiry": 0.0}, id="no time left"),
        pytest.param({"steps": 2}, id="too few steps"),
    ],
)
def test_a_price_is_not_worked_out_from_inputs_the_model_has_no_meaning_for(changes: dict) -> None:
    with pytest.raises(ValueError):
        american_value(**(_AT_THE_MONEY_PUT | {"volatility": 0.25} | changes))


def test_delta_stays_between_no_exposure_and_the_whole_share() -> None:
    """Across strikes from deep in to deep out of the money, a call's delta lies in [0, 1], a put's in [-1, 0]."""
    for right, low, high in (("call", 0.0, 1.0), ("put", -1.0, 0.0)):
        deltas = [
            american_value(spot=100.0, strike=strike, years_to_expiry=_THIRTY_DAYS, rate=0.045, volatility=0.25, right=right)
            for strike in np.arange(70.0, 131.0, 5.0)
        ]
        assert all(value is not None and low - 1e-12 <= value.delta <= high + 1e-12 for value in deltas), right
