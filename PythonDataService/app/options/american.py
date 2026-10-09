"""American option price, delta, implied volatility and daily time decay, with cash dividends, for stock and ETF options.

Formula: a Leisen-Reimer binomial tree of ``n`` (odd) steps over time ``T``.
  With ``dt = T/n`` and ``h(z) = 1/2 + sign(z)·sqrt(1/4 - 1/4·exp(-(z/(n + 1/3 + 0.1/(n+1)))²·(n + 1/6)))``
  (Peizer-Pratt inversion, method 2):
    d2 = (ln(S*/K) + (r - σ²/2)·T) / (σ·√T),  p = h(d2),  p' = h(d2 + σ·√T)
    up = e^{r·dt}·p'/p,  down = (e^{r·dt} - p·up)/(1 - p)
    node (i, j) carries S*·down^{i-j}·up^{j};  V_i = max(exercise value, e^{-r·dt}·((1-p)·V_{i+1,j} + p·V_{i+1,j+1}))
    price = V_0,  delta = (V_{1,1} - V_{1,0}) / (S_{1,1} - S_{1,0})
  Cash dividends are held in escrow: S* = S - Σ D_k·e^{-r·t_k} over the dividends
  still to go ex before expiry, the tree is built on S*, and the stock price a
  node is exercised at is S* at that node plus the present value, at the node's
  time, of the dividends still to go ex.
  Daily time decay = price a day later - price now: the same tree with 24
  hours less to expiry and every dividend 24 hours nearer, at the same stock
  price, volatility and rate.
Reference: Leisen & Reimer (1996), "Binomial models for option valuation -
  examining and improving convergence", Applied Mathematical Finance 3(4),
  319-346, as constructed by QuantLib 1.42.1 ``BinomialLRVanillaEngine``: the
  tree, the exercise test at every step including the first, and delta read
  from the first step. Escrowed cash dividends: Hull, Options, Futures, and
  Other Derivatives, "Binomial model for a dividend-paying stock: known
  dollar dividend"; QuantLib 1.42.1 ``FdBlackScholesVanillaEngine`` with
  ``CashDividendModel.Escrowed`` is the independent check.
Canonical implementation: this file.
Validated against: tests/fixtures/test_american_option_fixtures.py (golden
  fixtures AM-001 to AM-006), tests/options/test_american.py and
  tests/options/test_american_daily_decay.py.

Why a tree and not the closed form the rest of the repo uses: a stock or ETF
option can be exercised early, so Black-Scholes prices a different contract.
Why this tree: with no dividend it reproduces a second pinned engine to
1e-9, so the port can be proven, not argued.

Accepted divergence, with cash dividends. No engine pinned here builds this
tree with cash dividends, so that path is checked against QuantLib's
finite-difference engine for the same escrowed model and agrees with it only
to the tree's own discretization error. On fixture AM-002, 2,001 steps are
held to 1e-3 in price and 2e-4 in delta; 401 steps are held to 5e-3 and
1e-3. Those are fixture tolerances, not bounds on other contracts: AM-006's
call a day before a $3 ex-date missed the second engine by 1.9e-2 at 401.
The default uses 1,009 steps for a call with a dividend before or at expiry,
bringing AM-006's largest price gap to 2.9e-3. An implied volatility
solved from that engine's price is within 5e-4 of its volatility inside one
standard deviation of the money, and looser beyond, where vega is small.

Daily time decay is a difference of two prices from this tree, 24 hours
apart. It is not the theta a tree gives from its own nodes, and not a yearly
rate of decay divided by 365. It is the number an owner means by what a day
costs. It stays a plain number in the last days before expiry, where a rate
of decay runs away. And each of its two prices is one the reference engine
computes too, so the difference is proven the way the price is. The day is
24 hours by the clock. Over a weekend three such days pass before the next
session, and this figure is one of them. A contract that expires inside the
day is worth, a day later, what exercising pays. A stock that goes ex inside
the day is priced a day later at its price less that dividend.

How close the decay is. With no dividend it matches the reference to 1e-9
on fixture AM-004's cases, whose later price is fifteen minutes or more
from expiry. That is true of those cases, not of every contract: outside
the fixture 1.6e-9 was measured with a later price fifteen minutes from
expiry, and 1.7e-9 with one minute. With cash dividends it is checked
against the finite-difference engine's difference and agrees to the tree's
step error, as the price does. On fixture AM-005's cases the largest gap is
9.4e-4 at an explicit 2,003 steps, 8.1e-3 at an explicit 401 steps, and
8.9e-4 with the adaptive default. Those are the fixture's own measured gaps,
not bounds. The gap is largest where one of the two prices is a day before
an ex-date: a call in the money whose stock goes ex one or two days out.

What it does not do. It has no continuous dividend yield: a bot's dividends
are cash amounts on dates, given explicitly, and there is no default of
none. It does not price index options or anything not American. Time is a
fraction of a 365-day year, to the millisecond, so a contract expiring today
has the hours it really has.

Where it is coarse. An up probability within about 1e-8 of one half rounds
to one half, as the reference's does: both take ``1 - exp(-x)`` of a very
small ``x``. That is a strike placed where d2, or d2 + σ·√T, is all but
zero, at any time to expiry. The price there moves in steps of up to about
1e-5 as the volatility changes, and a volatility solved from it is good to
about 2e-7/√T: 1e-6 at thirty days, 1e-4 at one minute.
A dividend whose ex-date falls exactly on a tree step is counted before or
after that step by the last bit of the two times, and the price can move by
a step's error for it. The move measured is 5.6e-3 to 1.1e-2 at an explicit
401 steps.
An ex-date lands on a step only at isolated instants. Whole days to both
the expiry and the ex-date never do under 401 or 1,009 days, because both
step counts are prime.
The daily decay takes a day off each time as a float, so there its later
price can differ that much from a price worked a day later from instants.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

import numpy as np
from scipy.optimize import brentq

from app.options.contract import OptionRight

MS_PER_YEAR = 365 * 86_400_000
# Under a minute the model is not asked: a quote that close to expiry prices exercise, not volatility.
MIN_TIME_TO_EXPIRY_MS = 60_000
_MIN_YEARS_TO_EXPIRY = MIN_TIME_TO_EXPIRY_MS / MS_PER_YEAR
# The day a daily time decay is taken over: 24 hours by the clock, not a trading session.
ONE_DAY_MS = 86_400_000
_ONE_DAY_YEARS = ONE_DAY_MS / MS_PER_YEAR
# A day later a contract must still have the minute the model asks for. With less it is priced as exercised.
_MIN_YEARS_FOR_A_PRICE_A_DAY_LATER = (ONE_DAY_MS + MIN_TIME_TO_EXPIRY_MS) / MS_PER_YEAR
# Odd, as the tree requires. More steps buy accuracy with time, about linearly in both.
DEFAULT_TREE_STEPS = 401
# A call with a cash dividend needs a finer default tree to resolve early exercise.
DIVIDEND_CALL_TREE_STEPS = 1009
MIN_VOLATILITY = 0.005
MAX_VOLATILITY = 5.0
_VOLATILITY_XTOL = 1e-12
_SOLVER_MAX_ITERATIONS = 200
# How fast the lowest volatility tried is raised until the tree can resolve the strike.
_BRACKET_GROWTH = 1.25
# A branch probability this close to zero or one leaves the other branch to rounding error:
# ``down`` divides by one minus the up probability. The strike is then dozens of standard
# deviations from the price, and the tree is not asked.
_UNRESOLVED_PROBABILITY = 1e-6
# A price this close to a bound is at it: far under any quote's increment, far over the tree's rounding.
# Without it a quote at exactly what exercising pays would be told apart from one a rounding error over.
# The tree's rounding is about 1e-13 of the price, so the allowance holds for an option worth thousands a share.
_PRICE_NOISE = 1e-9
# A dollar compounded at the rate to expiry is ``e^(rate·years)``; past this the number cannot be held.
_MAX_RATE_TIMES_YEARS = 700.0


def years_from_ms(duration_ms: int) -> float:
    """A span in milliseconds as the fraction of a 365-day year the model counts time in.

    The one conversion from instants to model time. Two spans equal in
    milliseconds are the same float, so a dividend going ex at the instant an
    option expires is at its expiry in the model too.
    """
    return duration_ms / MS_PER_YEAR


@dataclass(frozen=True, slots=True)
class CashDividend:
    """One cash dividend: when the stock goes ex, in years from the valuation instant, and the amount per share.

    The model's time, as ``years_to_expiry`` is: a span, not an instant. A
    caller holding instants converts each with ``years_from_ms``.
    """

    years_to_ex: float
    cash: float


@dataclass(frozen=True, slots=True)
class AmericanValue:
    """An American option's model price and delta, per share."""

    price: float
    delta: float


@dataclass(frozen=True, slots=True)
class AmericanDailyDecay:
    """What the next 24 hours take from an American option's model price, per share, with its working.

    ``decay`` is the later price less the price now: negative for a contract
    that loses value as time passes. The day is 24 hours by the clock. Over a
    weekend three such days pass before the next session, and this figure is
    one of them.
    """

    price_now: float
    price_a_day_later: float
    # The contract has under a minute left a day later, or none: the later price is what exercising pays.
    later_price_is_exercise_value: bool
    # The dividends going ex inside the day, per share. The later price is worked at the stock price less this.
    cash_gone_ex: float

    @property
    def decay(self) -> float:
        return self.price_a_day_later - self.price_now


class AmericanIvStatus(StrEnum):
    SOLVED = "solved"
    # Less than a minute to expiry.
    TIME_TO_EXPIRY_BELOW_FLOOR = "time_to_expiry_below_floor"
    # The price is under what exercising now pays, so no volatility explains it.
    BELOW_EXERCISE_VALUE = "below_exercise_value"
    # The price is at or over exercise value but no higher than the model's price at the lowest
    # volatility tried. That includes an option priced at exactly what exercising now pays: every
    # volatility up to some level gives that price, so none is implied.
    BELOW_VOLATILITY_FLOOR = "below_volatility_floor"
    # The price is over the model's price at the highest volatility tried.
    ABOVE_VOLATILITY_CEILING = "above_volatility_ceiling"
    # The root search stopped without an answer.
    NO_CONVERGENCE = "no_convergence"
    # An input the model has no meaning for.
    INVALID_INPUT = "invalid_input"


@dataclass(frozen=True, slots=True)
class AmericanIv:
    """An implied-volatility solve: the volatility and delta, or the reason there are none.

    ``volatility`` and ``delta`` are set only when ``status`` is ``SOLVED``. A
    failure never carries a number, so nothing downstream can mistake one for
    an answer.
    """

    status: AmericanIvStatus
    volatility: float | None = None
    delta: float | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        solved = self.status is AmericanIvStatus.SOLVED
        if (self.volatility is not None, self.delta is not None) != (solved, solved):
            raise ValueError("an implied volatility carries its volatility and delta when solved, and neither otherwise")


def _peizer_pratt(z: float, steps: int) -> float:
    scaled = z / (steps + 1.0 / 3.0 + 0.1 / (steps + 1.0))
    tail = math.exp(-scaled * scaled * (steps + 1.0 / 6.0))
    sign = 1.0 if z > 0 else -1.0
    return 0.5 + sign * math.sqrt(0.25 * (1.0 - tail))


@dataclass(frozen=True, slots=True)
class _Tree:
    escrowed_spot: float
    up: float
    down: float
    probability_up: float
    discount: float
    step_years: float


def _escrow(dividends: Sequence[CashDividend], *, rate: float, at_years: float, expiry_years: float) -> float:
    """The present value, at ``at_years``, of the dividends still to go ex by expiry."""
    return sum(
        dividend.cash * math.exp(-rate * (dividend.years_to_ex - at_years))
        for dividend in dividends
        if at_years < dividend.years_to_ex <= expiry_years
    )


def _escrowed_spot(spot: float, dividends: Sequence[CashDividend], *, rate: float, years_to_expiry: float) -> float:
    """The stock price less the present value of the dividends still to go ex by expiry: what the tree is built on."""
    escrowed_spot = spot - _escrow(dividends, rate=rate, at_years=0.0, expiry_years=years_to_expiry)
    if escrowed_spot <= 0:
        raise ValueError("the dividends before expiry are worth as much as the stock itself")
    return escrowed_spot


def _tree(
    *,
    spot: float,
    strike: float,
    years_to_expiry: float,
    rate: float,
    volatility: float,
    dividends: Sequence[CashDividend],
    steps: int,
) -> _Tree | None:
    """The tree for these inputs, or ``None`` when the strike is too far from the price for it to resolve.

    That happens when the strike lies so many standard deviations away that a
    branch probability is all but zero or one. The option then has no time
    value the tree can see, and the branch sizes would be rounding error. A
    volatility too small to have a square has no tree either, nor has a price
    and strike whose ratio is not a number above zero.
    """
    escrowed_spot = _escrowed_spot(spot, dividends, rate=rate, years_to_expiry=years_to_expiry)
    step_years = years_to_expiry / steps
    variance = volatility * volatility * years_to_expiry
    deviation = math.sqrt(variance)
    if not (math.isfinite(deviation) and deviation > 0):
        return None
    moneyness = escrowed_spot / strike
    if not (math.isfinite(moneyness) and moneyness > 0):
        return None
    drift_per_step = (rate - 0.5 * volatility * volatility) * step_years
    growth = math.exp(drift_per_step + 0.5 * variance / steps)
    d2 = (math.log(moneyness) + drift_per_step * steps) / deviation
    probability_up = _peizer_pratt(d2, steps)
    probability_up_tilted = _peizer_pratt(d2 + deviation, steps)
    resolved = min(probability_up, probability_up_tilted) >= _UNRESOLVED_PROBABILITY and max(
        probability_up, probability_up_tilted
    ) <= 1.0 - _UNRESOLVED_PROBABILITY
    if not resolved:
        return None
    up = growth * probability_up_tilted / probability_up
    down = (growth - probability_up * up) / (1.0 - probability_up)
    return _Tree(
        escrowed_spot=escrowed_spot,
        up=up,
        down=down,
        probability_up=probability_up,
        discount=math.exp(-rate * step_years),
        step_years=step_years,
    )


def _require_inputs(
    *, spot: float, strike: float, years_to_expiry: float, rate: float, right: OptionRight, steps: int
) -> None:
    if right not in ("call", "put"):
        # Anything else would be priced as one of the two without saying which.
        raise ValueError(f"an option is a call or a put; got {right!r}")
    for name, value in (("spot", spot), ("strike", strike), ("years to expiry", years_to_expiry)):
        if not (math.isfinite(value) and value > 0):
            raise ValueError(f"{name} must be a finite number above zero; got {value}")
    if not (math.isfinite(rate) and abs(rate) * years_to_expiry <= _MAX_RATE_TIMES_YEARS):
        raise ValueError(
            f"the rate must be finite, and small enough to compound to expiry; got {rate} over {years_to_expiry} years"
        )
    if steps < 3 or steps % 2 == 0:
        raise ValueError(f"the tree takes an odd number of steps, three or more; got {steps}")


def _require_dividends(dividends: Sequence[CashDividend]) -> None:
    for dividend in dividends:
        if not (math.isfinite(dividend.years_to_ex) and math.isfinite(dividend.cash) and dividend.cash > 0):
            raise ValueError(f"a cash dividend is a finite amount above zero at a finite time; got {dividend}")


def _effective_steps(
    steps: int | None, *, right: OptionRight, years_to_expiry: float, dividends: Sequence[CashDividend]
) -> int:
    if steps is not None:
        return steps
    if right == "call" and any(
        0.0 < dividend.years_to_ex <= years_to_expiry
        for dividend in dividends
    ):
        return DIVIDEND_CALL_TREE_STEPS
    return DEFAULT_TREE_STEPS


def _exercise_value(stock: np.ndarray | float, strike: float, right: OptionRight) -> np.ndarray | float:
    return np.maximum(stock - strike, 0.0) if right == "call" else np.maximum(strike - stock, 0.0)


def _roll_back(
    tree: _Tree,
    *,
    strike: float,
    right: OptionRight,
    rate: float,
    years_to_expiry: float,
    dividends: Sequence[CashDividend],
    steps: int,
) -> AmericanValue:
    moves = np.arange(steps + 1, dtype=np.float64)
    down_moves = np.power(tree.down, moves)
    up_moves = np.power(tree.up, moves)

    def stock_prices(step: int) -> np.ndarray:
        # The last layer is expiry itself. The step multiplied back can round to just under it, and a
        # dividend going ex at expiry would then still be in escrow when the option is settled.
        at_years = years_to_expiry if step == steps else tree.step_years * step
        escrow = _escrow(dividends, rate=rate, at_years=at_years, expiry_years=years_to_expiry)
        return tree.escrowed_spot * down_moves[step::-1] * up_moves[: step + 1] + escrow

    probability_down = 1.0 - tree.probability_up
    values = _exercise_value(stock_prices(steps), strike, right)
    first_step_values = values
    for step in range(steps - 1, -1, -1):
        values = (probability_down * values[:-1] + tree.probability_up * values[1:]) * tree.discount
        values = np.maximum(values, _exercise_value(stock_prices(step), strike, right))
        if step == 1:
            first_step_values = values
    first_step_stock = stock_prices(1)
    delta = (first_step_values[1] - first_step_values[0]) / (first_step_stock[1] - first_step_stock[0])
    return AmericanValue(price=float(values[0]), delta=float(delta))


def american_value(
    *,
    spot: float,
    strike: float,
    years_to_expiry: float,
    rate: float,
    volatility: float,
    right: OptionRight,
    dividends: Sequence[CashDividend],
    steps: int | None = None,
) -> AmericanValue | None:
    """The model price and delta of one American option, per share.

    ``dividends`` is every cash dividend the stock may go ex on; it has no
    default, so pricing with none is said, never assumed. With ``steps=None``
    the tree uses 401 steps, or 1,009 for a call with an ex-date before or at
    expiry. An explicit ``steps`` is used exactly. ``None`` when the
    strike is too far from the price, for this time and volatility, for the
    tree to resolve, or the inputs are so extreme its prices overflow: such
    an option is not given a made-up value.
    """
    _require_dividends(dividends)
    effective_steps = _effective_steps(steps, right=right, years_to_expiry=years_to_expiry, dividends=dividends)
    _require_inputs(spot=spot, strike=strike, years_to_expiry=years_to_expiry, rate=rate, right=right, steps=effective_steps)
    if not (math.isfinite(volatility) and volatility > 0):
        raise ValueError(f"volatility must be a finite number above zero; got {volatility}")
    tree = _tree(
        spot=spot,
        strike=strike,
        years_to_expiry=years_to_expiry,
        rate=rate,
        volatility=volatility,
        dividends=dividends,
        steps=effective_steps,
    )
    if tree is None:
        return None
    with np.errstate(over="ignore", invalid="ignore"):
        value = _roll_back(
            tree, strike=strike, right=right, rate=rate, years_to_expiry=years_to_expiry, dividends=dividends, steps=effective_steps
        )
    return value if math.isfinite(value.price) and math.isfinite(value.delta) else None


def american_daily_decay(
    *,
    spot: float,
    strike: float,
    years_to_expiry: float,
    rate: float,
    volatility: float,
    right: OptionRight,
    dividends: Sequence[CashDividend],
    steps: int | None = None,
) -> AmericanDailyDecay | None:
    """What the next 24 hours take from one American option's model price, per share.

    The price a day later less the price now, both from ``american_value``,
    with the stock price, the volatility and the rate held and every dividend
    kept on its date. Negative for a contract that loses value with time. Over
    a weekend three such days pass before the next session, and this figure
    is one of them.

    Each price uses the same default selection as ``american_value``. On
    fixture AM-005 the adaptive default's largest difference from the second
    engine is 8.9e-4, measured across its cases, not a bound.

    A contract with less than a day and a minute left is worth, a day later,
    what exercising pays at the held stock price: nothing when it is out of
    the money. The model is not asked for a price under a minute from expiry.

    A stock that goes ex inside the day is priced a day later at its price
    less that dividend, in full. Holding the quoted price across an ex-date
    would say the stock had risen by the dividend, and that rise, not the
    day, would be most of the number. A dividend going ex exactly a day from
    now has gone ex. For a contract that expires inside the day, the
    dividends taken off are those going ex by its expiry.

    Whatever ``american_value`` refuses, this refuses the same way: its
    ``ValueError`` for an input the model has no meaning for, and ``None``
    when the tree cannot resolve the strike, now or a day later. ``None`` too
    with less than a minute to expiry now. A refusal is never a zero.
    """
    now = american_value(
        spot=spot,
        strike=strike,
        years_to_expiry=years_to_expiry,
        rate=rate,
        volatility=volatility,
        right=right,
        dividends=dividends,
        steps=steps,
    )
    if now is None or years_to_expiry < _MIN_YEARS_TO_EXPIRY:
        return None
    expires_inside_the_day = years_to_expiry < _MIN_YEARS_FOR_A_PRICE_A_DAY_LATER
    gone_ex_by = years_to_expiry if expires_inside_the_day else _ONE_DAY_YEARS
    cash_gone_ex = math.fsum(dividend.cash for dividend in dividends if 0.0 < dividend.years_to_ex <= gone_ex_by)
    spot_a_day_later = spot - cash_gone_ex
    if spot_a_day_later <= 0:
        raise ValueError("the dividends going ex inside the day are worth as much as the stock itself")
    if expires_inside_the_day:
        price_a_day_later = float(_exercise_value(spot_a_day_later, strike, right))
    else:
        later = american_value(
            spot=spot_a_day_later,
            strike=strike,
            years_to_expiry=years_to_expiry - _ONE_DAY_YEARS,
            rate=rate,
            volatility=volatility,
            right=right,
            # One at or under zero has gone ex, and the pricer leaves it out.
            dividends=[CashDividend(dividend.years_to_ex - _ONE_DAY_YEARS, dividend.cash) for dividend in dividends],
            steps=steps,
        )
        if later is None:
            return None
        price_a_day_later = later.price
    return AmericanDailyDecay(
        price_now=now.price,
        price_a_day_later=price_a_day_later,
        later_price_is_exercise_value=expires_inside_the_day,
        cash_gone_ex=cash_gone_ex,
    )


def american_implied_volatility(
    *,
    price: float,
    spot: float,
    strike: float,
    years_to_expiry: float,
    rate: float,
    right: OptionRight,
    dividends: Sequence[CashDividend],
    steps: int | None = None,
) -> AmericanIv:
    """The volatility at which the model prices the option at ``price``, and the delta there.

    Every way of having no answer is a status, never a number: a solve that
    did not converge does not return where it stopped.
    """
    try:
        _require_dividends(dividends)
        effective_steps = _effective_steps(steps, right=right, years_to_expiry=years_to_expiry, dividends=dividends)
        _require_inputs(spot=spot, strike=strike, years_to_expiry=years_to_expiry, rate=rate, right=right, steps=effective_steps)
        _escrowed_spot(spot, dividends, rate=rate, years_to_expiry=years_to_expiry)
        if not (math.isfinite(price) and price > 0):
            raise ValueError(f"an option price must be a finite number above zero; got {price}")
    except ValueError as invalid:
        return AmericanIv(AmericanIvStatus.INVALID_INPUT, detail=str(invalid))
    if years_to_expiry < _MIN_YEARS_TO_EXPIRY:
        return AmericanIv(AmericanIvStatus.TIME_TO_EXPIRY_BELOW_FLOOR, detail="less than a minute to expiry")
    exercise_now = float(_exercise_value(spot, strike, right))
    if price < exercise_now - _PRICE_NOISE:
        return AmericanIv(
            AmericanIvStatus.BELOW_EXERCISE_VALUE,
            detail=f"the price {price} is under the {exercise_now} that exercising now pays",
        )

    def value_at(volatility: float) -> AmericanValue | None:
        return american_value(
            spot=spot,
            strike=strike,
            years_to_expiry=years_to_expiry,
            rate=rate,
            volatility=volatility,
            right=right,
            dividends=dividends,
            steps=effective_steps,
        )

    # The lowest volatility at which the tree can resolve this strike. Below it the option has no
    # time value the model can see, so its price there is no higher than at this one.
    lowest = MIN_VOLATILITY
    at_lowest = value_at(lowest)
    while at_lowest is None and lowest < MAX_VOLATILITY:
        lowest = min(lowest * _BRACKET_GROWTH, MAX_VOLATILITY)
        at_lowest = value_at(lowest)
    at_highest = value_at(MAX_VOLATILITY)
    if at_lowest is None or at_highest is None:
        return AmericanIv(
            AmericanIvStatus.NO_CONVERGENCE, detail="the tree cannot resolve this strike at any volatility it tries"
        )
    if price <= at_lowest.price + _PRICE_NOISE:
        return AmericanIv(
            AmericanIvStatus.BELOW_VOLATILITY_FLOOR,
            detail=f"the price {price} is no higher than the model's {at_lowest.price} at a volatility of {lowest}",
        )
    if price > at_highest.price:
        return AmericanIv(
            AmericanIvStatus.ABOVE_VOLATILITY_CEILING,
            detail=f"the price {price} is over the model's {at_highest.price} at a volatility of {MAX_VOLATILITY}",
        )

    def miss(volatility: float) -> float:
        valued = value_at(volatility)
        if valued is None:
            raise _Unresolved
        return valued.price - price

    try:
        volatility, report = brentq(
            miss, lowest, MAX_VOLATILITY, xtol=_VOLATILITY_XTOL, maxiter=_SOLVER_MAX_ITERATIONS, full_output=True, disp=False
        )
    except _Unresolved:
        return AmericanIv(AmericanIvStatus.NO_CONVERGENCE, detail="the tree stopped resolving the strike during the search")
    solved = value_at(volatility) if report.converged else None
    if solved is None:
        return AmericanIv(AmericanIvStatus.NO_CONVERGENCE, detail=f"no answer after {report.iterations} iterations")
    return AmericanIv(AmericanIvStatus.SOLVED, volatility=float(volatility), delta=solved.delta)


class _Unresolved(Exception):
    """The tree could not resolve the strike at a volatility the root search asked about."""
