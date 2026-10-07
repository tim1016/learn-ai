"""The American pricer beyond what golden fixtures AM-001 to AM-003 pin: refusals, bounds and the solver's statuses."""

from __future__ import annotations

import math
import warnings

import numpy as np
import pytest

from app.options import american
from app.options.american import (
    DEFAULT_TREE_STEPS,
    MIN_TIME_TO_EXPIRY_MS,
    AmericanIv,
    AmericanIvStatus,
    AmericanValue,
    CashDividend,
    american_implied_volatility,
    american_value,
    years_from_ms,
)

_THIRTY_DAYS = 30 / 365
_FIFTEEN_MINUTES = years_from_ms(15 * 60_000)
_NO_DIVIDENDS: tuple[CashDividend, ...] = ()
_AT_THE_MONEY_PUT = {
    "spot": 100.0,
    "strike": 100.0,
    "years_to_expiry": _THIRTY_DAYS,
    "rate": 0.045,
    "right": "put",
    "dividends": _NO_DIVIDENDS,
}


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
            {"years_to_expiry": years_from_ms(30_000), "price": 0.05},
            AmericanIvStatus.TIME_TO_EXPIRY_BELOW_FLOOR,
            id="thirty seconds to expiry",
        ),
        pytest.param(
            {"right": "call", "strike": 1e-300, "price": 100.0},
            AmericanIvStatus.NO_CONVERGENCE,
            id="a strike no volatility the model tries brings within reach",
        ),
        pytest.param(
            {"years_to_expiry": 200.0, "price": 50.0},
            AmericanIvStatus.NO_CONVERGENCE,
            id="two hundred years, where the highest volatility tried overflows the tree",
        ),
        pytest.param({"spot": 0.0, "price": 2.0}, AmericanIvStatus.INVALID_INPUT, id="no stock price"),
        pytest.param({"price": 0.0}, AmericanIvStatus.INVALID_INPUT, id="no option price"),
        pytest.param({"price": float("nan")}, AmericanIvStatus.INVALID_INPUT, id="an option price that is no number"),
        pytest.param({"price": 2.0, "steps": 400}, AmericanIvStatus.INVALID_INPUT, id="an even number of steps"),
        pytest.param(
            {"price": 2.0, "rate": -9000.0},
            AmericanIvStatus.INVALID_INPUT,
            id="a rate no number can compound to expiry, with or without a dividend to discount",
        ),
        pytest.param(
            {"price": 2.0, "rate": 9000.0, "dividends": (CashDividend(years_to_ex=10 / 365, cash=0.5),)},
            AmericanIvStatus.INVALID_INPUT,
            id="the same rate the other way, with a dividend",
        ),
        pytest.param(
            {"right": "call", "spot": 1e-200, "strike": 1e200, "price": 2.0},
            AmericanIvStatus.NO_CONVERGENCE,
            id="a price and strike whose ratio is nothing",
        ),
        pytest.param(
            {"price": 2.0, "right": "P"},
            AmericanIvStatus.INVALID_INPUT,
            id="a put written the market-data provider's way, which is not this model's",
        ),
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


def test_a_put_quoted_at_exactly_what_exercising_pays_has_no_volatility_whichever_way_its_cents_round() -> None:
    """A strike less a price in cents is rarely exact in binary: 227.5 - 158.02 comes out a hair under
    69.48 and 715.5 - 505.15 a hair over 210.35. Neither hair is time value, so neither quote is solved,
    and both get the one status."""
    statuses = set()
    for index in range(60):
        spot = round(150.01 + 7.37 * index, 2)
        strike = round(spot * 2.8) / 2
        at_exercise_value = round(strike - spot, 2)
        statuses.add(
            american_implied_volatility(
                **(_AT_THE_MONEY_PUT | {"spot": spot, "strike": strike}), price=at_exercise_value
            ).status
        )
    for spot, strike, price in ((158.02, 227.5, 69.48), (505.15, 715.5, 210.35)):
        statuses.add(american_implied_volatility(**(_AT_THE_MONEY_PUT | {"spot": spot, "strike": strike}), price=price).status)

    assert statuses == {AmericanIvStatus.BELOW_VOLATILITY_FLOOR}


def test_a_price_no_volatility_moves_until_it_is_high_is_not_solved_to_the_lowest_one_tried() -> None:
    """A put just in the money with $1.49 going ex inside its fifteen minutes is worth the same at every
    volatility up to some level. A price from that level has no one volatility behind it."""
    put = {
        "spot": 100.0,
        "strike": 100.04,
        "years_to_expiry": _FIFTEEN_MINUTES,
        "rate": 0.08,
        "right": "put",
        "dividends": (CashDividend(years_to_ex=years_from_ms(5 * 60_000), cash=1.49),),
    }
    priced = american_value(**put, volatility=0.3848)

    assert priced is not None
    assert american_implied_volatility(**put, price=priced.price).status is AmericanIvStatus.BELOW_VOLATILITY_FLOOR


def test_a_search_that_runs_out_of_iterations_does_not_return_where_it_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(american, "_SOLVER_MAX_ITERATIONS", 1)

    result = american_implied_volatility(**_AT_THE_MONEY_PUT, price=2.5)

    assert (result.status, result.volatility, result.delta) == (AmericanIvStatus.NO_CONVERGENCE, None, None)


def test_a_search_the_tree_stops_answering_part_way_through_has_no_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both ends of the search price; a volatility between them does not. The search stops there."""
    price_it = american.american_value

    def priced_only_at_the_ends(*, volatility: float, **contract: object) -> AmericanValue | None:
        if 0.01 < volatility < 4.9:
            return None
        return price_it(volatility=volatility, **contract)

    monkeypatch.setattr(american, "american_value", priced_only_at_the_ends)

    result = american_implied_volatility(**_AT_THE_MONEY_PUT, price=2.5)

    assert (result.status, result.volatility, result.delta) == (AmericanIvStatus.NO_CONVERGENCE, None, None)


@pytest.mark.parametrize(
    "result",
    [
        pytest.param({"status": AmericanIvStatus.SOLVED}, id="solved, with no numbers"),
        pytest.param({"status": AmericanIvStatus.SOLVED, "volatility": 0.2}, id="solved, with no delta"),
        pytest.param({"status": AmericanIvStatus.NO_CONVERGENCE, "volatility": 0.2, "delta": 0.5}, id="a failure with numbers"),
        pytest.param({"status": AmericanIvStatus.BELOW_VOLATILITY_FLOOR, "delta": -1.0}, id="a failure with a delta"),
    ],
)
def test_an_implied_volatility_cannot_be_built_with_numbers_its_status_denies(result: dict) -> None:
    with pytest.raises(ValueError, match="when solved, and neither otherwise"):
        AmericanIv(**result)


def test_exactly_a_minute_to_expiry_is_solved_and_a_millisecond_less_is_not() -> None:
    a_minute = years_from_ms(MIN_TIME_TO_EXPIRY_MS)
    priced = american_value(**(_AT_THE_MONEY_PUT | {"years_to_expiry": a_minute}), volatility=0.25)
    assert priced is not None

    at_the_floor = american_implied_volatility(**(_AT_THE_MONEY_PUT | {"years_to_expiry": a_minute}), price=priced.price)
    under_it = american_implied_volatility(
        **(_AT_THE_MONEY_PUT | {"years_to_expiry": years_from_ms(MIN_TIME_TO_EXPIRY_MS - 1)}), price=priced.price
    )

    assert at_the_floor.status is AmericanIvStatus.SOLVED
    assert at_the_floor.volatility == pytest.approx(0.25, abs=1e-6, rel=0)
    assert under_it.status is AmericanIvStatus.TIME_TO_EXPIRY_BELOW_FLOOR


def test_a_strike_dozens_of_standard_deviations_away_is_not_priced() -> None:
    """Fifteen minutes out, a strike 13% away is over a hundred standard deviations off. The tree's
    branch sizes would be rounding error there, so it answers nothing and never a NaN."""
    far = {
        "spot": 100.0,
        "strike": 113.0,
        "years_to_expiry": _FIFTEEN_MINUTES,
        "rate": 0.045,
        "right": "call",
        "dividends": _NO_DIVIDENDS,
    }

    assert american_value(**far, volatility=0.2) is None
    # A penny on that call needs more volatility than the model will try.
    assert american_implied_volatility(**far, price=0.01).status is AmericanIvStatus.ABOVE_VOLATILITY_CEILING


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param(
            {"years_to_expiry": 1e-7, "rate": 0.0, "volatility": 1e-4}, id="so little volatility the two branches are one"
        ),
        pytest.param({"volatility": 1e-170}, id="a volatility whose square is nothing"),
        pytest.param({"years_to_expiry": 200.0, "volatility": 5.0}, id="so much that the tree's prices overflow"),
        pytest.param({"spot": 1e-200, "strike": 1e200}, id="a price and strike whose ratio is nothing"),
    ],
)
def test_inputs_too_extreme_for_the_tree_are_not_priced_and_never_come_back_as_a_nan(changes: dict) -> None:
    with warnings.catch_warnings():
        # An overflow the tree expects is not left for the caller to see as a warning.
        warnings.simplefilter("error")
        assert american_value(**(_AT_THE_MONEY_PUT | {"volatility": 0.25} | changes)) is None


def test_a_far_strike_is_still_solved_once_enough_volatility_resolves_it() -> None:
    """The lowest volatility tried rises until the tree resolves the strike; the answer is above it."""
    near = {
        "spot": 100.0,
        "strike": 101.0,
        "years_to_expiry": _FIFTEEN_MINUTES,
        "rate": 0.045,
        "right": "call",
        "dividends": _NO_DIVIDENDS,
    }

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
    with_it = american_value(**(_AT_THE_MONEY_PUT | {"dividends": (dividend,)}), volatility=0.25)

    assert with_it == without


def test_a_dividend_going_ex_at_the_moment_of_expiry_has_gone_ex_when_the_option_settles() -> None:
    """With no interest a put is never exercised early, so one whose stock goes ex $1.75 as it expires
    is worth what the same put is worth on a stock $1.75 lower that pays nothing.

    Every whole-day expiry in a year is tried: on about one in twelve the tree's step, multiplied back
    by the number of steps, rounds to just under the time to expiry."""
    no_interest = _AT_THE_MONEY_PUT | {"rate": 0.0}
    off_by: dict[int, float] = {}
    for days in range(1, 366):
        expiry = days / 365
        ex_at_expiry = american_value(
            **(no_interest | {"years_to_expiry": expiry, "dividends": (CashDividend(years_to_ex=expiry, cash=1.75),)}),
            volatility=0.25,
        )
        on_the_lower_stock = american_value(**(no_interest | {"years_to_expiry": expiry, "spot": 98.25}), volatility=0.25)
        assert ex_at_expiry is not None and on_the_lower_stock is not None
        gap = max(
            abs(ex_at_expiry.price - on_the_lower_stock.price), abs(ex_at_expiry.delta - on_the_lower_stock.delta)
        )
        if gap > 1e-9:
            off_by[days] = gap

    assert off_by == {}


def test_a_deep_call_is_exercised_just_before_the_stock_goes_ex() -> None:
    """Without a dividend an American call is never exercised early: it is worth the stock less the
    strike discounted to expiry. With $1.75 going ex tomorrow a deep call is exercised the moment
    before, to own the stock while it still carries the dividend: it is worth the stock less the
    strike discounted one day, and no more."""
    deep_call = {"spot": 100.0, "strike": 60.0, "years_to_expiry": _THIRTY_DAYS, "rate": 0.045, "right": "call"}
    tomorrow = 1 / 365

    held_to_expiry = american_value(**deep_call, volatility=0.25, dividends=_NO_DIVIDENDS)
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


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"volatility": 0.0}, id="no volatility"),
        pytest.param({"volatility": float("inf")}, id="infinite volatility"),
        pytest.param({"strike": -5.0}, id="a negative strike"),
        pytest.param({"years_to_expiry": 0.0}, id="no time left"),
        pytest.param({"steps": 2}, id="too few steps"),
        pytest.param({"right": "C"}, id="a call written the market-data provider's way"),
    ],
)
def test_a_price_is_not_worked_out_from_inputs_the_model_has_no_meaning_for(changes: dict) -> None:
    with pytest.raises(ValueError):
        american_value(**(_AT_THE_MONEY_PUT | {"volatility": 0.25} | changes))


def test_delta_stays_between_no_exposure_and_the_whole_share() -> None:
    """Across strikes from deep in to deep out of the money, a call's delta lies in [0, 1], a put's in [-1, 0]."""
    for right, low, high in (("call", 0.0, 1.0), ("put", -1.0, 0.0)):
        deltas = [
            american_value(
                spot=100.0,
                strike=strike,
                years_to_expiry=_THIRTY_DAYS,
                rate=0.045,
                volatility=0.25,
                right=right,
                dividends=_NO_DIVIDENDS,
            )
            for strike in np.arange(70.0, 131.0, 5.0)
        ]
        assert all(value is not None and low - 1e-12 <= value.delta <= high + 1e-12 for value in deltas), right
