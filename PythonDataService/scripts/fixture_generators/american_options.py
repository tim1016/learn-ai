"""Generate AM-001 to AM-005 from QuantLib, without importing the canonical American pricer.

AM-001  price and delta with no dividend, from ``BinomialLRVanillaEngine``.
AM-002  price and delta with cash dividends, and one group with none, from
        ``FdBlackScholesVanillaEngine`` under the escrowed dividend model, on a
        fine grid.
AM-003  implied volatility: a price QuantLib computes at a known volatility,
        which a solver must turn back into that volatility.
AM-004  daily time decay with no dividend: the ``BinomialLRVanillaEngine``
        price of the same contract 24 hours on, less its price now.
AM-005  daily time decay with cash dividends, and one group with none: the
        same difference from ``FdBlackScholesVanillaEngine``. A stock that
        goes ex inside the day is priced a day later at its price less that
        dividend, with the dividend gone from the schedule. One group goes ex
        on day 2, a day ahead of the later price.

QuantLib counts time in whole days. A time to expiry that is not a whole
number of days is the same problem restated over the next whole number up:
with ``f`` the fraction of that span the contract really has, the rate
becomes ``r·f`` and the volatility ``σ·√f``, which leaves ``r·T`` and
``σ²·T``, and so the price and the delta, unchanged.

QuantLib 1.42.1's binomial engines lose the payoff at expiry when the step,
multiplied back by the number of steps, rounds to less than the time to
expiry: an American price then comes out under the European one. No such pair
of span and step count is used; ``_tree_row`` refuses one.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import QuantLib as ql

QUANTLIB_VERSION = "1.42.1"
SPOT = 100.0
VALUATION_DATE = ql.Date(15, 1, 2025)
DAY_COUNT = ql.Actual365Fixed()
DAY_MS = 86_400_000
MS_PER_YEAR = 365 * DAY_MS

# (label, whole days QuantLib is given, fraction of that span the contract really has)
TIMES: list[tuple[str, int, float]] = [
    ("30d", 30, 1.0),
    ("7d", 7, 1.0),
    ("1d", 1, 1.0),
    ("2h", 1, 2.0 / 24.0),
    ("15min", 1, 0.25 / 24.0),
]
VOLATILITIES = (0.15, 0.45)
RATES = (0.045, 0.01)
# The strike's distance from the price, in standard deviations of the price at expiry.
MONEYNESS = (-4.0, -1.0, 0.0, 1.0, 4.0)
RIGHTS = ("call", "put")
TREE_STEPS = (101, 401)

FD_TIME_STEPS = 2000
FD_PRICE_STEPS = 2000
# (label, days to expiry, [(day the stock goes ex, cash per share), ...], strikes)
DIVIDEND_CASES: list[tuple[str, int, list[tuple[int, float]], tuple[float, ...]]] = [
    ("30d_no_dividend", 30, [], (80.0, 90.0, 95.0, 100.0, 105.0, 110.0, 120.0)),
    ("30d_one_dividend", 30, [(12, 1.75)], (80.0, 90.0, 95.0, 100.0, 105.0, 110.0, 120.0)),
    ("7d_one_dividend", 7, [(3, 0.6)], (90.0, 95.0, 100.0, 105.0, 110.0)),
    ("90d_two_dividends", 90, [(20, 0.9), (80, 0.9)], (80.0, 90.0, 100.0, 110.0, 120.0)),
]
DIVIDEND_VOLATILITIES = (0.2, 0.4)
DIVIDEND_RATE = 0.045

# An implied volatility is asked for only where the price holds this much more than exercising now pays.
MIN_TIME_VALUE_FOR_IV = 1e-4

# (label, milliseconds to expiry now). The later price is the same contract 24 hours on.
DECAY_SPANS: list[tuple[str, int]] = [
    ("30d", 30 * DAY_MS),
    ("7d", 7 * DAY_MS),
    ("2d", 2 * DAY_MS),
    ("1d2h", DAY_MS + 2 * 3_600_000),
    ("1d15min", DAY_MS + 15 * 60_000),
]
# (label, days to expiry now, [(day the stock goes ex, cash per share), ...], strikes).
# A stock going ex on day 1 goes ex inside the day the decay is taken over. One going ex on day 2
# is a day ahead of the later price: the canonical tree is at its coarsest there, so the fixture
# holds that class too.
DECAY_DIVIDEND_CASES: list[tuple[str, int, list[tuple[int, float]], tuple[float, ...]]] = [
    ("30d_no_dividend", 30, [], (90.0, 95.0, 100.0, 105.0, 110.0)),
    ("30d_ex_on_day_12", 30, [(12, 1.75)], (90.0, 95.0, 100.0, 105.0, 110.0)),
    ("7d_ex_on_day_3", 7, [(3, 0.6)], (95.0, 100.0, 105.0)),
    ("30d_ex_inside_the_day", 30, [(1, 1.75)], (90.0, 95.0, 100.0, 105.0, 110.0)),
    ("60d_ex_inside_the_day_and_on_day_40", 60, [(1, 0.9), (40, 0.9)], (90.0, 100.0, 110.0)),
    ("60d_ex_on_day_2", 60, [(2, 1.75)], (85.0, 90.0, 95.0, 100.0)),
]
# The step counts AM-005 is compared at, both prime and over the days to any expiry here. An ex-date
# exactly on a tree step is counted before or after that step by the last bit of the two times, so
# no case may put one there, now or a day later. At 2,001 steps, 69 to the day over 29 days, every
# whole day of a 29-day span would be.
DECAY_DIVIDEND_TREE_STEPS = (401, 2003)


def _process(rate: float, volatility: float, spot_price: float = SPOT) -> ql.BlackScholesMertonProcess:
    spot = ql.QuoteHandle(ql.SimpleQuote(spot_price))
    rates = ql.YieldTermStructureHandle(ql.FlatForward(VALUATION_DATE, rate, DAY_COUNT))
    no_yield = ql.YieldTermStructureHandle(ql.FlatForward(VALUATION_DATE, 0.0, DAY_COUNT))
    volatilities = ql.BlackVolTermStructureHandle(
        ql.BlackConstantVol(VALUATION_DATE, ql.NullCalendar(), volatility, DAY_COUNT)
    )
    return ql.BlackScholesMertonProcess(spot, no_yield, rates, volatilities)


def _option(right: str, strike: float, days: int) -> ql.VanillaOption:
    payoff = ql.PlainVanillaPayoff(ql.Option.Call if right == "call" else ql.Option.Put, strike)
    return ql.VanillaOption(payoff, ql.AmericanExercise(VALUATION_DATE, VALUATION_DATE + days))


def _tree_row(right: str, strike: float, days: int, fraction: float, rate: float, volatility: float, steps: int) -> dict[str, float]:
    span_years = days / 365.0
    if (span_years / steps) * steps < span_years:
        raise SystemExit(f"QuantLib {QUANTLIB_VERSION} loses the payoff at expiry at {days} days and {steps} steps")
    option = _option(right, strike, days)
    option.setPricingEngine(
        ql.BinomialLRVanillaEngine(_process(rate * fraction, volatility * math.sqrt(fraction)), steps)
    )
    return {"price": option.NPV(), "delta": option.delta()}


def _no_dividend_cases() -> list[dict[str, Any]]:
    cases = []
    for label, days, fraction in TIMES:
        years = days / 365.0 * fraction
        for volatility in VOLATILITIES:
            for rate in RATES:
                for moneyness in MONEYNESS:
                    strike = round(SPOT * math.exp(moneyness * volatility * math.sqrt(years)), 4)
                    for right in RIGHTS:
                        for steps in TREE_STEPS:
                            cases.append(
                                {
                                    "case": f"{label}_vol{volatility}_rate{rate}_m{moneyness:+.0f}_{right}_{steps}steps",
                                    "input": {
                                        "spot": SPOT,
                                        "strike": strike,
                                        "years_to_expiry": years,
                                        "rate": rate,
                                        "volatility": volatility,
                                        "right": right,
                                        "steps": steps,
                                    },
                                    "quantlib": {"days": days, "fraction_of_span": fraction},
                                    "expected": _tree_row(right, strike, days, fraction, rate, volatility, steps),
                                }
                            )
    return cases


def _escrowed_option(
    right: str, strike: float, days: int, dividends: list[tuple[int, float]], volatility: float, spot_price: float = SPOT
) -> ql.VanillaOption:
    option = _option(right, strike, days)
    option.setPricingEngine(
        ql.FdBlackScholesVanillaEngine(
            _process(DIVIDEND_RATE, volatility, spot_price),
            ql.DividendVector([VALUATION_DATE + day for day, _ in dividends], [cash for _, cash in dividends]),
            FD_TIME_STEPS,
            FD_PRICE_STEPS,
            0,
            ql.FdmSchemeDesc.Douglas(),
            False,
            -ql.nullDouble(),
            ql.FdBlackScholesVanillaEngine.Escrowed,
        )
    )
    return option


def _dividend_cases() -> list[dict[str, Any]]:
    cases = []
    for label, days, dividends, strikes in DIVIDEND_CASES:
        for volatility in DIVIDEND_VOLATILITIES:
            for strike in strikes:
                for right in RIGHTS:
                    option = _escrowed_option(right, strike, days, dividends, volatility)
                    cases.append(
                        {
                            "case": f"{label}_vol{volatility}_k{strike:g}_{right}",
                            "input": {
                                "spot": SPOT,
                                "strike": strike,
                                "years_to_expiry": days / 365.0,
                                "rate": DIVIDEND_RATE,
                                "volatility": volatility,
                                "right": right,
                                "dividends": [{"years_to_ex": day / 365.0, "cash": cash} for day, cash in dividends],
                            },
                            "expected": {"price": option.NPV(), "delta": option.delta()},
                        }
                    )
    return cases


def _implied_volatility_cases(no_dividend_cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cases = []
    for case in no_dividend_cases:
        given = case["input"]
        if given["steps"] != 401:
            continue
        exercise_now = max(
            SPOT - given["strike"] if given["right"] == "call" else given["strike"] - SPOT,
            0.0,
        )
        if case["expected"]["price"] - exercise_now < MIN_TIME_VALUE_FOR_IV:
            continue
        cases.append(
            {
                "case": case["case"],
                "input": {key: value for key, value in given.items() if key != "volatility"}
                | {"price": case["expected"]["price"]},
                "expected": {"volatility": given["volatility"], "delta": case["expected"]["delta"]},
            }
        )
    return cases


def _whole_days(span_ms: int) -> tuple[int, float]:
    """The whole days QuantLib is given for a span, and the fraction of them the span really is."""
    days = -(-span_ms // DAY_MS)
    return days, span_ms / (days * DAY_MS)


def _decay(price_now: float, price_a_day_later: float) -> dict[str, float]:
    return {"price_now": price_now, "price_a_day_later": price_a_day_later, "decay": price_a_day_later - price_now}


def _decay_cases() -> list[dict[str, Any]]:
    cases = []
    for label, span_ms in DECAY_SPANS:
        now_days, now_fraction = _whole_days(span_ms)
        later_days, later_fraction = _whole_days(span_ms - DAY_MS)
        for volatility in VOLATILITIES:
            for rate in RATES:
                for moneyness in MONEYNESS:
                    # Measured at the later price's expiry, the nearer of the two: the strike is then
                    # no more than four standard deviations away at either.
                    later_deviation = volatility * math.sqrt((span_ms - DAY_MS) / MS_PER_YEAR)
                    strike = round(SPOT * math.exp(moneyness * later_deviation), 4)
                    for right in RIGHTS:
                        for steps in TREE_STEPS:
                            now = _tree_row(right, strike, now_days, now_fraction, rate, volatility, steps)
                            later = _tree_row(right, strike, later_days, later_fraction, rate, volatility, steps)
                            cases.append(
                                {
                                    "case": f"{label}_vol{volatility}_rate{rate}_m{moneyness:+.0f}_{right}_{steps}steps",
                                    "input": {
                                        "spot": SPOT,
                                        "strike": strike,
                                        "years_to_expiry": span_ms / MS_PER_YEAR,
                                        "rate": rate,
                                        "volatility": volatility,
                                        "right": right,
                                        "steps": steps,
                                    },
                                    "quantlib": {
                                        "now": {"days": now_days, "fraction_of_span": now_fraction},
                                        "a_day_later": {"days": later_days, "fraction_of_span": later_fraction},
                                    },
                                    "expected": _decay(now["price"], later["price"]),
                                }
                            )
    return cases


def _refuse_an_ex_date_on_a_tree_step(label: str, days: int, dividends: list[tuple[int, float]]) -> None:
    for day, _ in dividends:
        for steps in DECAY_DIVIDEND_TREE_STEPS:
            if (day * steps) % days == 0:
                raise SystemExit(f"{label}: day {day} of {days} is exactly on a step of a {steps}-step tree")


def _dividend_decay_cases() -> list[dict[str, Any]]:
    cases = []
    for label, days, dividends, strikes in DECAY_DIVIDEND_CASES:
        cash_gone_ex = math.fsum(cash for day, cash in dividends if day <= 1)
        later_spot = SPOT - cash_gone_ex
        later_dividends = [(day - 1, cash) for day, cash in dividends if day > 1]
        _refuse_an_ex_date_on_a_tree_step(label, days, dividends)
        _refuse_an_ex_date_on_a_tree_step(label, days - 1, later_dividends)
        for volatility in DIVIDEND_VOLATILITIES:
            for strike in strikes:
                for right in RIGHTS:
                    now = _escrowed_option(right, strike, days, dividends, volatility)
                    later = _escrowed_option(right, strike, days - 1, later_dividends, volatility, later_spot)
                    cases.append(
                        {
                            "case": f"{label}_vol{volatility}_k{strike:g}_{right}",
                            "input": {
                                "spot": SPOT,
                                "strike": strike,
                                "years_to_expiry": days / 365.0,
                                "rate": DIVIDEND_RATE,
                                "volatility": volatility,
                                "right": right,
                                "dividends": [{"years_to_ex": day / 365.0, "cash": cash} for day, cash in dividends],
                            },
                            "quantlib": {
                                "a_day_later": {
                                    "spot": later_spot,
                                    "days": days - 1,
                                    "dividends": [{"day": day, "cash": cash} for day, cash in later_dividends],
                                }
                            },
                            "expected": _decay(now.NPV(), later.NPV()) | {"cash_gone_ex": cash_gone_ex},
                        }
                    )
    return cases


def _write(fixture: str, input_payload: dict[str, Any], output_payload: dict[str, Any]) -> None:
    destination = Path(__file__).parents[2] / "tests/fixtures/golden/options-pricing" / fixture / "v1"
    destination.mkdir(parents=True, exist_ok=True)
    for filename, payload in (("input.json", input_payload), ("output.json", output_payload)):
        (destination / filename).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _split(cases: list[dict[str, Any]], **settings: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    given = {
        "quantlib_version": QUANTLIB_VERSION,
        **settings,
        "cases": [{key: value for key, value in case.items() if key != "expected"} for case in cases],
    }
    return given, {"cases": [{"case": case["case"], **case["expected"]} for case in cases]}


def main() -> None:
    if ql.__version__ != QUANTLIB_VERSION:
        raise SystemExit(f"these fixtures are pinned to QuantLib {QUANTLIB_VERSION}; this is {ql.__version__}")
    ql.Settings.instance().evaluationDate = VALUATION_DATE
    no_dividend = _no_dividend_cases()
    _write("AM-001", *_split(no_dividend, engine="BinomialLRVanillaEngine"))
    _write(
        "AM-002",
        *_split(
            _dividend_cases(),
            engine="FdBlackScholesVanillaEngine",
            cash_dividend_model="Escrowed",
            scheme="Douglas",
            time_steps=FD_TIME_STEPS,
            price_steps=FD_PRICE_STEPS,
        ),
    )
    _write(
        "AM-003",
        *_split(
            _implied_volatility_cases(no_dividend),
            engine="BinomialLRVanillaEngine",
            min_time_value=MIN_TIME_VALUE_FOR_IV,
        ),
    )
    _write("AM-004", *_split(_decay_cases(), engine="BinomialLRVanillaEngine", day_ms=DAY_MS))
    _write(
        "AM-005",
        *_split(
            _dividend_decay_cases(),
            engine="FdBlackScholesVanillaEngine",
            cash_dividend_model="Escrowed",
            scheme="Douglas",
            time_steps=FD_TIME_STEPS,
            price_steps=FD_PRICE_STEPS,
            day_ms=DAY_MS,
        ),
    )


if __name__ == "__main__":
    main()
