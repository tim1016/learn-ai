"""The daily time decay beyond what golden fixtures AM-004 and AM-005 pin: its edges, its refusals and what must hold of it."""

from __future__ import annotations

import math
from itertools import pairwise

import pytest

from app.options.american import (
    DEFAULT_TREE_STEPS,
    MIN_TIME_TO_EXPIRY_MS,
    ONE_DAY_MS,
    CashDividend,
    american_daily_decay,
    american_value,
    years_from_ms,
)
from app.services.bs_greeks import bs_european_price

_HOUR_MS = 3_600_000
_NO_DIVIDENDS: tuple[CashDividend, ...] = ()
_AT_THE_MONEY = {
    "spot": 100.0,
    "strike": 100.0,
    "years_to_expiry": years_from_ms(30 * ONE_DAY_MS),
    "rate": 0.045,
    "volatility": 0.25,
    "right": "put",
    "dividends": _NO_DIVIDENDS,
}
# The repository's strict default. Two prices of one contract from times that differ in their last
# bits agree far inside it: the largest such gap measured over fixture AM-004's cases is 3.6e-12.
_SAME_PRICE_ATOL = 1e-9


def _ex_in(span_ms: int, cash: float) -> CashDividend:
    return CashDividend(years_to_ex=years_from_ms(span_ms), cash=cash)


# A call on a stock that pays nothing is never exercised early, so the tree prices the contract the
# closed form does and differs from it by its own step error. That error falls with the square of
# the step count: over these 48 contracts the largest gap in the decay is 2.4e-6 at 101 steps,
# 1.6e-7 at 401 and 6.4e-9 at 2,001.
@pytest.mark.parametrize(
    ("steps", "atol"),
    [
        pytest.param(DEFAULT_TREE_STEPS, 1e-6, id="the default 401 steps"),
        pytest.param(2001, 5e-8, id="2,001 steps, where the gap is twenty-five times smaller"),
    ],
)
def test_a_call_never_exercised_early_decays_as_the_black_scholes_price_does(steps: int, atol: float) -> None:
    """Against ``bs_european_price`` in app/services/bs_greeks.py, a day apart."""
    gaps: dict[tuple[float, float, float, float], float] = {}
    for days in (30.0, 7.0, 2.0, 1.5):
        span_ms = round(days * ONE_DAY_MS)
        for strike in (90.0, 100.0, 110.0):
            for volatility in (0.15, 0.45):
                for rate in (0.0, 0.045):
                    worked = american_daily_decay(
                        spot=100.0,
                        strike=strike,
                        years_to_expiry=years_from_ms(span_ms),
                        rate=rate,
                        volatility=volatility,
                        right="call",
                        dividends=_NO_DIVIDENDS,
                        steps=steps,
                    )
                    assert worked is not None
                    closed_form = bs_european_price(
                        100.0, strike, years_from_ms(span_ms - ONE_DAY_MS), rate, volatility, is_call=True
                    ) - bs_european_price(100.0, strike, years_from_ms(span_ms), rate, volatility, is_call=True)
                    gap = abs(worked.decay - closed_form)
                    if not gap <= atol:
                        gaps[(days, strike, volatility, rate)] = gap

    assert gaps == {}


@pytest.mark.parametrize(
    "hours_left",
    [
        pytest.param(30 * 24, id="thirty days out"),
        pytest.param(7 * 24, id="seven days out"),
        pytest.param(27, id="a day and three hours out"),
        pytest.param(12, id="expiring inside the day"),
    ],
)
def test_a_deep_put_worth_what_exercising_pays_today_and_tomorrow_has_no_decay(hours_left: int) -> None:
    """A put $10 in the money, at this rate and volatility, is exercised at once: it is worth $10 today
    and $10 a day later, so the day takes nothing. Exactly nothing, not a rounding error."""
    deep_put = _AT_THE_MONEY | {
        "strike": 110.0,
        "volatility": 0.15,
        "years_to_expiry": years_from_ms(hours_left * _HOUR_MS),
    }

    worked = american_daily_decay(**deep_put)

    assert worked is not None
    assert (worked.price_now, worked.price_a_day_later, worked.decay) == (10.0, 10.0, 0.0)


@pytest.mark.parametrize("right", ["call", "put"])
@pytest.mark.parametrize("hours_left", [30 * 24, 7 * 24, 48, 25, 12, 1])
def test_a_contract_at_the_money_loses_value_over_the_day(right: str, hours_left: int) -> None:
    worked = american_daily_decay(
        **(_AT_THE_MONEY | {"right": right, "years_to_expiry": years_from_ms(hours_left * _HOUR_MS)})
    )

    assert worked is not None
    assert worked.decay < 0


@pytest.mark.parametrize(
    ("right", "strike", "ex_after_ms", "cash"),
    [
        pytest.param("put", 101.0, None, 0.0, id="a put that ends in the money"),
        pytest.param("call", 101.0, None, 0.0, id="a call that ends worthless"),
        pytest.param("call", 97.0, 2 * ONE_DAY_MS + 6 * _HOUR_MS, 1.75, id="a call whose stock goes ex on the third day"),
        pytest.param("put", 99.0, 2 * ONE_DAY_MS + 6 * _HOUR_MS, 1.75, id="a put whose stock goes ex on the third day"),
    ],
)
def test_each_days_later_price_is_the_next_days_price_and_the_days_add_up_to_the_whole_loss(
    right: str, strike: float, ex_after_ms: int | None, cash: float
) -> None:
    """Five and a half days out, a day at a time, to expiry.

    Each day is asked afresh from instants: the time left, the time to the
    ex-date, and the stock price less a dividend once it has gone ex. What
    holds: a day's later price is the next day's price now, and the decays
    add up to what exercising pays at the end less the price at the start.
    No ex-date here falls on a tree step, where the last bit of the times
    would decide the price.
    """
    span_ms = 5 * ONE_DAY_MS + 12 * _HOUR_MS
    days = []
    for day in range(6):
        elapsed_ms = day * ONE_DAY_MS
        gone_ex = ex_after_ms is not None and ex_after_ms <= elapsed_ms
        dividends = () if ex_after_ms is None or gone_ex else (_ex_in(ex_after_ms - elapsed_ms, cash),)
        worked = american_daily_decay(
            **(
                _AT_THE_MONEY
                | {
                    "right": right,
                    "strike": strike,
                    "spot": 100.0 - cash if gone_ex else 100.0,
                    "years_to_expiry": years_from_ms(span_ms - elapsed_ms),
                    "dividends": dividends,
                }
            )
        )
        assert worked is not None
        days.append(worked)

    for today, tomorrow in pairwise(days):
        assert today.price_a_day_later == pytest.approx(tomorrow.price_now, abs=_SAME_PRICE_ATOL, rel=0)
    assert [day.later_price_is_exercise_value for day in days] == [False, False, False, False, False, True]
    assert [day.cash_gone_ex for day in days] == [0.0, 0.0, cash, 0.0, 0.0, 0.0]
    stock_at_expiry = 100.0 - cash
    paid_at_expiry = max(stock_at_expiry - strike, 0.0) if right == "call" else max(strike - stock_at_expiry, 0.0)
    assert days[-1].price_a_day_later == paid_at_expiry
    assert sum(day.decay for day in days) == pytest.approx(paid_at_expiry - days[0].price_now, abs=_SAME_PRICE_ATOL, rel=0)


@pytest.mark.parametrize(
    ("right", "strike", "paid"),
    [
        pytest.param("call", 101.0, 0.0, id="a call out of the money, worth nothing"),
        pytest.param("put", 101.0, 1.0, id="a put a dollar in the money"),
        pytest.param("call", 99.5, 0.5, id="a call fifty cents in the money"),
        pytest.param("put", 99.5, 0.0, id="a put out of the money, worth nothing"),
    ],
)
def test_a_contract_that_expires_inside_the_day_is_worth_what_exercising_pays_a_day_later(
    right: str, strike: float, paid: float
) -> None:
    six_hours = _AT_THE_MONEY | {"right": right, "strike": strike, "years_to_expiry": years_from_ms(6 * _HOUR_MS)}
    priced = american_value(**six_hours)

    worked = american_daily_decay(**six_hours)

    assert priced is not None and worked is not None
    assert worked.later_price_is_exercise_value
    assert (worked.price_now, worked.price_a_day_later) == (priced.price, paid)
    assert worked.decay == paid - priced.price


def test_a_day_and_a_minute_left_is_priced_by_the_model_a_day_later_and_a_millisecond_less_is_not() -> None:
    """With a minute left a day later the model still has a price: at the money it is over a cent.
    With a millisecond less it is not asked, and the contract is worth what exercising pays: nothing."""
    a_day_and_a_minute_ms = ONE_DAY_MS + MIN_TIME_TO_EXPIRY_MS
    with_a_minute_left = american_value(**(_AT_THE_MONEY | {"years_to_expiry": years_from_ms(MIN_TIME_TO_EXPIRY_MS)}))

    at_the_floor = american_daily_decay(**(_AT_THE_MONEY | {"years_to_expiry": years_from_ms(a_day_and_a_minute_ms)}))
    under_it = american_daily_decay(**(_AT_THE_MONEY | {"years_to_expiry": years_from_ms(a_day_and_a_minute_ms - 1)}))

    assert with_a_minute_left is not None and at_the_floor is not None and under_it is not None
    assert with_a_minute_left.price > 0.01
    assert not at_the_floor.later_price_is_exercise_value
    assert at_the_floor.price_a_day_later == pytest.approx(with_a_minute_left.price, abs=_SAME_PRICE_ATOL, rel=0)
    assert under_it.later_price_is_exercise_value
    assert under_it.price_a_day_later == 0.0


def test_exactly_a_minute_to_expiry_has_a_decay_and_a_millisecond_less_has_none() -> None:
    a_minute = american_daily_decay(**(_AT_THE_MONEY | {"years_to_expiry": years_from_ms(MIN_TIME_TO_EXPIRY_MS)}))
    under_it = american_daily_decay(**(_AT_THE_MONEY | {"years_to_expiry": years_from_ms(MIN_TIME_TO_EXPIRY_MS - 1)}))

    assert a_minute is not None
    assert a_minute.later_price_is_exercise_value
    assert a_minute.decay == -a_minute.price_now < 0
    # The pricer itself would still price it; the decay is not worked from a price the model is not asked for.
    assert american_value(**(_AT_THE_MONEY | {"years_to_expiry": years_from_ms(MIN_TIME_TO_EXPIRY_MS - 1)})) is not None
    assert under_it is None


def test_a_stock_that_goes_ex_inside_the_day_is_priced_a_day_later_at_its_price_less_the_dividend() -> None:
    """$1.75 goes ex in twelve hours. A day later the stock is priced at 98.25 with no dividend ahead:
    the cash in full, not its present value now, and not the quoted 100 as if the stock had risen."""
    goes_ex_tonight = _AT_THE_MONEY | {"right": "call", "dividends": (_ex_in(12 * _HOUR_MS, 1.75),)}
    a_day_on = {"years_to_expiry": years_from_ms(29 * ONE_DAY_MS), "dividends": _NO_DIVIDENDS, "right": "call"}
    at_the_price_less_the_cash = american_value(**(_AT_THE_MONEY | a_day_on | {"spot": 98.25}))
    at_the_quoted_price = american_value(**(_AT_THE_MONEY | a_day_on))
    at_the_price_less_its_present_value = american_value(
        **(_AT_THE_MONEY | a_day_on | {"spot": 100.0 - 1.75 * math.exp(-0.045 * years_from_ms(12 * _HOUR_MS))})
    )

    worked = american_daily_decay(**goes_ex_tonight)

    assert worked is not None and at_the_price_less_the_cash is not None
    assert at_the_quoted_price is not None and at_the_price_less_its_present_value is not None
    assert worked.cash_gone_ex == 1.75
    assert worked.price_a_day_later == pytest.approx(at_the_price_less_the_cash.price, abs=_SAME_PRICE_ATOL, rel=0)
    # The two other readings are far outside that tolerance, so this test tells them apart.
    assert abs(at_the_quoted_price.price - at_the_price_less_the_cash.price) > 0.5
    assert abs(at_the_price_less_its_present_value.price - at_the_price_less_the_cash.price) > 1e-5


@pytest.mark.parametrize(
    "ex_after_ms",
    [
        pytest.param(12 * _HOUR_MS, id="going ex inside the day"),
        pytest.param(ONE_DAY_MS, id="going ex exactly a day from now"),
        pytest.param(12 * ONE_DAY_MS, id="going ex on day twelve"),
        pytest.param(30 * ONE_DAY_MS, id="going ex at the moment the option expires"),
    ],
)
def test_with_no_interest_a_put_decays_as_the_same_put_on_a_stock_lower_by_the_dividend(ex_after_ms: int) -> None:
    """With no interest a put is never exercised early, and a dividend is worth its cash whenever it
    goes ex. So the put on a stock at 100 that goes ex $1.75 before expiry is the put on a stock at
    98.25 that pays nothing, today and a day later, wherever the ex-date falls. The decay does not
    jump because the ex-date is inside the day."""
    no_interest = _AT_THE_MONEY | {"rate": 0.0}

    with_the_dividend = american_daily_decay(**(no_interest | {"dividends": (_ex_in(ex_after_ms, 1.75),)}))
    on_the_lower_stock = american_daily_decay(**(no_interest | {"spot": 98.25}))

    assert with_the_dividend is not None and on_the_lower_stock is not None
    assert with_the_dividend.price_now == pytest.approx(on_the_lower_stock.price_now, abs=_SAME_PRICE_ATOL, rel=0)
    assert with_the_dividend.decay == pytest.approx(on_the_lower_stock.decay, abs=_SAME_PRICE_ATOL, rel=0)
    assert on_the_lower_stock.decay < -0.01


def test_holding_a_deep_call_through_the_ex_date_costs_the_dividend() -> None:
    """A call deep in the money is exercised the moment before the stock goes ex $1.75. Held a day
    instead, it is a call on a stock $1.75 lower, worth that stock less the strike discounted to
    expiry. The day costs the dividend, less the interest gained on the strike."""
    strike, rate = 60.0, 0.045
    ex_years = years_from_ms(12 * _HOUR_MS)
    deep_call = _AT_THE_MONEY | {"right": "call", "strike": strike, "dividends": (_ex_in(12 * _HOUR_MS, 1.75),)}

    worked = american_daily_decay(**deep_call)

    assert worked is not None
    exercised_before_ex = 100.0 - strike * math.exp(-rate * ex_years)
    held_a_day = 98.25 - strike * math.exp(-rate * years_from_ms(29 * ONE_DAY_MS))
    # Exercise falls on the tree's last step before the ex-date, so within one step's interest on the strike.
    one_steps_interest = strike * rate * years_from_ms(30 * ONE_DAY_MS) / DEFAULT_TREE_STEPS
    assert worked.decay == pytest.approx(held_a_day - exercised_before_ex, abs=one_steps_interest, rel=0)
    assert worked.cash_gone_ex == 1.75


def test_a_dividend_going_ex_exactly_a_day_from_now_has_gone_ex_and_a_millisecond_later_has_not() -> None:
    a_day_on = _AT_THE_MONEY | {"years_to_expiry": years_from_ms(29 * ONE_DAY_MS)}
    with_it_a_millisecond_ahead = american_value(**(a_day_on | {"dividends": (_ex_in(1, 1.75),)}))

    at_a_day = american_daily_decay(**(_AT_THE_MONEY | {"dividends": (_ex_in(ONE_DAY_MS, 1.75),)}))
    just_after = american_daily_decay(**(_AT_THE_MONEY | {"dividends": (_ex_in(ONE_DAY_MS + 1, 1.75),)}))

    assert at_a_day is not None and just_after is not None and with_it_a_millisecond_ahead is not None
    assert (at_a_day.cash_gone_ex, just_after.cash_gone_ex) == (1.75, 0.0)
    # Not yet ex a day later: the stock is still priced at 100, with the dividend a millisecond ahead.
    assert just_after.price_a_day_later == pytest.approx(with_it_a_millisecond_ahead.price, abs=_SAME_PRICE_ATOL, rel=0)
    # Either side of that line the put is worth the same: the two differ by a millisecond's interest on $1.75.
    assert at_a_day.price_a_day_later == pytest.approx(just_after.price_a_day_later, abs=_SAME_PRICE_ATOL, rel=0)


def test_an_ex_date_exactly_on_a_tree_step_moves_the_later_price_by_no_more_than_a_steps_error() -> None:
    """A day later this call has 401 hours left and its stock goes ex 100 hours in: exactly on step
    100 of the tree's 401. Whether that step is before the ex-date is decided by the last bit of the
    two times. The decay takes a day off each time as a float and lands on one side; the same
    contract priced a day later from instants lands on the other. The two prices differ by the
    tree's step error, the figure fixture AM-005 holds the default tree to, and no more."""
    on_a_step = _AT_THE_MONEY | {"right": "call", "strike": 95.0, "volatility": 0.4}

    worked = american_daily_decay(
        **(
            on_a_step
            | {
                "years_to_expiry": years_from_ms(ONE_DAY_MS + 401 * _HOUR_MS),
                "dividends": (_ex_in(ONE_DAY_MS + 100 * _HOUR_MS, 1.75),),
            }
        )
    )
    from_instants = american_value(
        **(on_a_step | {"years_to_expiry": years_from_ms(401 * _HOUR_MS), "dividends": (_ex_in(100 * _HOUR_MS, 1.75),)})
    )

    assert worked is not None and from_instants is not None
    # Measured 5.6e-3. Under 1e-9 the two fell on the same side and this case no longer shows the edge.
    assert 1e-9 < abs(worked.price_a_day_later - from_instants.price) <= 1e-2


@pytest.mark.parametrize(
    "dividend",
    [
        pytest.param(_ex_in(45 * ONE_DAY_MS, 1.75), id="going ex after the option expires"),
        pytest.param(CashDividend(years_to_ex=0.0, cash=1.75), id="already ex"),
        pytest.param(_ex_in(-3 * ONE_DAY_MS, 1.75), id="ex three days ago"),
    ],
)
def test_a_dividend_outside_the_options_life_changes_no_decay(dividend: CashDividend) -> None:
    assert american_daily_decay(**(_AT_THE_MONEY | {"dividends": (dividend,)})) == american_daily_decay(**_AT_THE_MONEY)


def test_a_contract_expiring_inside_the_day_loses_only_the_dividends_that_go_ex_before_it_expires() -> None:
    """Twelve hours left. $1.75 going ex in six hours is off the stock when the call is settled, so it
    pays 100 - 1.75 - 95. $1.75 going ex in eighteen hours is after expiry and takes nothing."""
    twelve_hours = _AT_THE_MONEY | {"right": "call", "strike": 95.0, "years_to_expiry": years_from_ms(12 * _HOUR_MS)}

    ex_before_expiry = american_daily_decay(**(twelve_hours | {"dividends": (_ex_in(6 * _HOUR_MS, 1.75),)}))
    ex_after_expiry = american_daily_decay(**(twelve_hours | {"dividends": (_ex_in(18 * _HOUR_MS, 1.75),)}))

    assert ex_before_expiry is not None and ex_after_expiry is not None
    assert (ex_before_expiry.cash_gone_ex, ex_before_expiry.price_a_day_later) == (1.75, 3.25)
    assert (ex_after_expiry.cash_gone_ex, ex_after_expiry.price_a_day_later) == (0.0, 5.0)
    assert ex_after_expiry == american_daily_decay(**twelve_hours)


def test_a_dividend_inside_the_day_worth_the_whole_stock_is_refused() -> None:
    """A $1 stock going ex $1 in twelve hours. Today the dividend's present value is a hair under the
    price, so the pricer has an answer. A day later the stock would be worth nothing: no price is
    worked from that."""
    penny_stock = _AT_THE_MONEY | {"spot": 1.0, "strike": 6e-5, "dividends": (_ex_in(12 * _HOUR_MS, 1.0),)}

    assert american_value(**penny_stock) is not None
    with pytest.raises(ValueError, match="going ex inside the day are worth as much as the stock"):
        american_daily_decay(**penny_stock)


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"volatility": 0.0}, id="no volatility"),
        pytest.param({"volatility": float("inf")}, id="infinite volatility"),
        pytest.param({"strike": -5.0}, id="a negative strike"),
        pytest.param({"spot": float("nan")}, id="a stock price that is no number"),
        pytest.param({"years_to_expiry": 0.0}, id="no time left"),
        pytest.param({"years_to_expiry": -1 / 365}, id="already expired"),
        pytest.param({"rate": 9000.0}, id="a rate no number can compound to expiry"),
        pytest.param({"steps": 400}, id="an even number of steps"),
        pytest.param({"right": "C"}, id="a call written the market-data provider's way"),
        pytest.param({"dividends": (CashDividend(years_to_ex=10 / 365, cash=-1.0),)}, id="a negative dividend"),
        pytest.param({"dividends": (CashDividend(years_to_ex=10 / 365, cash=150.0),)}, id="a dividend worth more than the stock"),
    ],
)
def test_inputs_the_pricer_refuses_are_refused_with_the_pricers_own_error(changes: dict) -> None:
    with pytest.raises(ValueError) as from_the_pricer:
        american_value(**(_AT_THE_MONEY | changes))

    with pytest.raises(ValueError) as from_the_decay:
        american_daily_decay(**(_AT_THE_MONEY | changes))

    assert str(from_the_decay.value) == str(from_the_pricer.value)


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param(
            {"strike": 500.0, "years_to_expiry": years_from_ms(2 * ONE_DAY_MS)},
            id="two days out, a strike at five times the price",
        ),
        pytest.param(
            {"strike": 113.0, "volatility": 0.2, "years_to_expiry": years_from_ms(15 * 60_000)},
            id="expiring inside the day, a strike 13% away",
        ),
    ],
)
def test_a_strike_the_tree_cannot_resolve_today_has_no_decay(changes: dict) -> None:
    """Over eighty standard deviations away in the first case, over a hundred in the second."""
    far = _AT_THE_MONEY | {"right": "call"} | changes

    assert american_value(**far) is None
    assert american_daily_decay(**far) is None


def test_a_strike_the_tree_resolves_today_but_not_a_day_later_has_no_decay() -> None:
    """A day and fifteen minutes out the tree prices a strike 13% away. Fifteen minutes out it cannot:
    the strike is then over a hundred standard deviations off. The contract will be all but
    worthless, but the model has no price to take the difference from, and a guess is not given."""
    strike_13_percent_away = _AT_THE_MONEY | {"right": "call", "strike": 113.0, "volatility": 0.2}
    today = strike_13_percent_away | {"years_to_expiry": years_from_ms(ONE_DAY_MS + 15 * 60_000)}
    a_day_later = strike_13_percent_away | {"years_to_expiry": years_from_ms(15 * 60_000)}

    assert american_value(**today) is not None
    assert american_value(**a_day_later) is None
    assert american_daily_decay(**today) is None
