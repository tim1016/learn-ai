"""Options strategy analysis engine.

Formula: POP (Probability of Profit) and EV (Expected Value) under the BS-M lognormal model. Per-leg payoff curves at expiry; max profit, max loss and breakevens of the expiry payoff, read at price 0, at the strikes and from the net long call quantity above the highest strike (the payoff is piecewise linear, so no price window enters); current-time PnL by composing per-leg theoretical values from `bs_greeks.py`; Greek curves per-spot grid; per-leg diagnostic table. Aggregates into a strategy-level response shape consumed by Strategy Lab.
Reference: Hull, Options Futures and Other Derivatives (10e), §11–12 (payoff diagrams), §19 (Greek Letters); composes the canonical Python options authorities (`bs_greeks.py`, `quantlib_pricer.py`, `volatility/solver.py`).
Canonical implementation: this file (`AnalyzeOptionsStrategy`). The Frontend `OptionsStrategyLabComponent` consumes this response and is now a passthrough since Phase 1.2 (commit 451394d).
Validated against: PythonDataService/tests/test_strategy_engine.py (POP, EV, strategy Greeks across spreads, single-legs, time-decay; max profit, max loss and breakevens hand-derived). Cash-adjusted roots are validated by tests/options_backtest/test_position_preview.py. The browser copy of the payoff extremes and breakevens, `Frontend/src/app/utils/strategy-payoff.ts` (ADR 0068 exception 1), is checked against this file by `tests/contracts/test_strategy_payoff_parity_fixture.py`.

All probability math lives here — Black-Scholes lognormal model
for POP (Probability of Profit) and EV (Expected Value).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np
from scipy.stats import lognorm, norm

from app.models.strategy import (
    CurrentCurvePoint,
    GreekCurvePoint,
    GreeksResult,
    LegDiagnostic,
    PayoffPoint,
    StrategyAnalyzeRequest,
    StrategyAnalyzeResponse,
    StrategyLeg,
)
from app.options.expiry import time_to_expiry_years
from app.services.bs_greeks import black_scholes_greeks, bs_european_price
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

MAX_PROFIT_CAP = 999_999.99


def compute_payoff_at_expiry(legs: list[StrategyLeg], price: float) -> float:
    """Compute total P&L per share at a given underlying price at expiration."""
    total = 0.0
    for leg in legs:
        if leg.option_type == "call":
            intrinsic = max(price - leg.strike, 0.0)
        else:
            intrinsic = max(leg.strike - price, 0.0)

        if leg.position == "long":
            pnl = (intrinsic - leg.premium) * leg.quantity
        else:
            pnl = (leg.premium - intrinsic) * leg.quantity

        total += pnl
    return total


def compute_payoff_curve(
    legs: list[StrategyLeg],
    spot_price: float,
    price_range_pct: float,
    num_points: int,
) -> list[PayoffPoint]:
    """Generate payoff curve over price range [spot*(1-pct), spot*(1+pct)]."""
    low = spot_price * (1.0 - price_range_pct)
    high = spot_price * (1.0 + price_range_pct)
    prices = np.linspace(max(low, 0.01), high, num_points)

    return [
        PayoffPoint(
            price=round(float(p), 4),
            pnl=round(compute_payoff_at_expiry(legs, float(p)), 4),
        )
        for p in prices
    ]


# Prices and premiums carry at most four decimals, so a payoff this close to zero
# is float noise on an exact zero (9.70 - 5.0 - 4.70 is 8.9e-16 in doubles).
_PAYOFF_ZERO = 1e-9


def _payoff_knots(legs: list[StrategyLeg]) -> list[tuple[float, float]]:
    """(price, payoff) at 0 and at each distinct strike, ascending.

    The expiry payoff is piecewise linear in the price and turns only at the
    strikes, so between two of these prices it is a straight line. A payoff
    within ``_PAYOFF_ZERO`` of zero is zero.
    """
    prices = sorted({0.0, *(leg.strike for leg in legs)})
    payoffs = [compute_payoff_at_expiry(legs, price) for price in prices]
    return [(price, 0.0 if abs(pnl) <= _PAYOFF_ZERO else pnl) for price, pnl in zip(prices, payoffs, strict=True)]


def _tail_slope(legs: list[StrategyLeg]) -> float:
    """Slope of the expiry payoff above the highest strike: the net long call quantity.

    Puts are worthless up there, so only calls move the payoff.
    """
    return float(sum((1 if leg.position == "long" else -1) * leg.quantity for leg in legs if leg.option_type == "call"))


def find_breakevens(legs: list[StrategyLeg], *, cash_adjustment: float = 0.0) -> list[float]:
    """Prices above zero where the expiry payoff changes sign, over the whole payoff.

    A price where the payoff only touches zero and turns back is not one. Where
    the payoff sits at zero over a stretch and then leaves it on the other side,
    the breakeven is the price where it first reaches zero. A constant per-share
    cash adjustment retains realized cash and fees when inspecting an open position.
    """
    knots = _payoff_knots(legs)
    if cash_adjustment:
        knots = [(price, 0.0 if abs(pnl + cash_adjustment) <= _PAYOFF_ZERO else pnl + cash_adjustment) for price, pnl in knots]
    tail_slope = _tail_slope(legs)

    breakevens: list[float] = []
    last: tuple[float, float] | None = None  # the latest knot whose payoff is not zero
    zero_from: float | None = None  # first price of the zeros since `last`
    for price, pnl in knots:
        if pnl == 0:
            if zero_from is None:
                zero_from = price
            continue
        if last is not None and (last[1] > 0) != (pnl > 0):
            if zero_from is not None:
                breakevens.append(zero_from)
            else:
                breakevens.append(last[0] - last[1] * (price - last[0]) / (pnl - last[1]))
        last, zero_from = (price, pnl), None

    # Above the highest strike the payoff is a line of the tail slope, or holds
    # the highest strike's value when there is none: a flat tail never crosses.
    if last is not None and tail_slope != 0 and (last[1] > 0) != (tail_slope > 0):
        if zero_from is not None:
            breakevens.append(zero_from)
        else:
            breakevens.append(last[0] - last[1] / tail_slope)

    return [round(be, 4) for be in breakevens]


@dataclass(frozen=True, slots=True)
class PayoffExtremes:
    """The most the expiry payoff gains and loses per share at any price at or above zero.

    ``math.inf`` for a gain with no limit, ``-math.inf`` for a loss with none.
    """

    max_profit: float
    max_loss: float


def compute_max_profit_loss(legs: list[StrategyLeg]) -> PayoffExtremes:
    """Max profit and max loss of the expiry payoff over every price at or above zero.

    The payoff is piecewise linear, so its extremes are at zero and at the
    strikes, or without limit above the highest strike when the net long call
    quantity is not zero. No price window enters.
    """
    values = [pnl for _, pnl in _payoff_knots(legs)]
    tail_slope = _tail_slope(legs)

    max_profit = math.inf if tail_slope > 0 else round(max(values), 4)
    max_loss = -math.inf if tail_slope < 0 else round(min(values), 4)
    return PayoffExtremes(max_profit=max_profit, max_loss=max_loss)


def weighted_iv(legs: list[StrategyLeg]) -> float:
    """Compute premium-weighted average IV across all legs.

    Skips legs with zero IV. Falls back to simple average if all
    premiums are zero.
    """
    valid_legs = [leg for leg in legs if leg.iv > 0]
    if not valid_legs:
        return 0.20  # fallback default

    total_weight = sum(leg.premium * leg.quantity for leg in valid_legs)
    if total_weight <= 0:
        return sum(leg.iv for leg in valid_legs) / len(valid_legs)

    return sum(leg.iv * leg.premium * leg.quantity for leg in valid_legs) / total_weight


def interpolate_iv_at_price(legs: list[StrategyLeg], price: float) -> float:
    """Interpolate IV at a given underlying price using leg strikes.

    Sorts legs by strike and linearly interpolates between the two
    nearest strikes. Falls back to nearest leg IV if price is outside
    all strikes, or to weighted_iv() if no legs have positive IV.
    """
    valid = sorted(
        [leg for leg in legs if leg.iv > 0],
        key=lambda leg: leg.strike,
    )
    if not valid:
        return weighted_iv(legs)

    if len(valid) == 1:
        return valid[0].iv

    # Below all strikes
    if price <= valid[0].strike:
        return valid[0].iv

    # Above all strikes
    if price >= valid[-1].strike:
        return valid[-1].iv

    # Find bracketing strikes and interpolate
    for i in range(len(valid) - 1):
        low_leg, high_leg = valid[i], valid[i + 1]
        if low_leg.strike <= price <= high_leg.strike:
            span = high_leg.strike - low_leg.strike
            if span <= 0:
                return low_leg.iv
            frac = (price - low_leg.strike) / span
            return low_leg.iv + frac * (high_leg.iv - low_leg.iv)

    return weighted_iv(legs)


def compute_d2(
    spot: float,
    strike: float,
    r: float,
    sigma: float,
    t: float,
) -> float:
    """Black-Scholes d2: [ln(S0/K) + (r - 0.5*sigma^2)*T] / (sigma*sqrt(T))."""
    if sigma <= 0 or t <= 0:
        return 0.0
    return (math.log(spot / strike) + (r - 0.5 * sigma**2) * t) / (sigma * math.sqrt(t))


def compute_pop(
    legs: list[StrategyLeg],
    spot_price: float,
    risk_free_rate: float,
    ttm_years: float,
) -> float:
    """Probability of Profit using Black-Scholes lognormal model.

    Determines profit regions from the payoff function, then computes
    probability mass in those regions using N(d2).

    Uses per-boundary IV interpolation to capture IV skew effects
    for multi-leg strategies (iron condors, butterflies, etc.).
    """
    if ttm_years <= 0:
        # At expiration, POP is binary based on current spot
        pnl_now = compute_payoff_at_expiry(legs, spot_price)
        return 1.0 if pnl_now > _PAYOFF_ZERO else 0.0

    t = ttm_years
    # A zero payoff can extend between strikes without changing sign. Strikes
    # split those stretches from the profitable regions beside them.
    boundaries = [
        0.0,
        *sorted({leg.strike for leg in legs} | set(find_breakevens(legs))),
        float("inf"),
    ]
    pop = 0.0

    for i in range(len(boundaries) - 1):
        low_b, high_b = boundaries[i], boundaries[i + 1]

        # Test inside each region; payoff is linear between these boundaries.
        if high_b == float("inf"):
            test_price = low_b + max(1.0, low_b * 0.01)
        else:
            test_price = (low_b + high_b) / 2.0

        pnl = compute_payoff_at_expiry(legs, test_price)
        if pnl <= _PAYOFF_ZERO:
            continue

        # This region is profitable — compute its probability
        # Use per-boundary IV interpolation to capture skew effects
        if low_b == 0.0 and high_b < float("inf"):
            # P(S < BE) = 1 - N(d2)
            sigma = interpolate_iv_at_price(legs, high_b)
            d2 = compute_d2(spot_price, high_b, risk_free_rate, sigma, t)
            pop += 1.0 - norm.cdf(d2)
        elif low_b > 0.0 and high_b == float("inf"):
            # P(S > BE) = N(d2)
            sigma = interpolate_iv_at_price(legs, low_b)
            d2 = compute_d2(spot_price, low_b, risk_free_rate, sigma, t)
            pop += norm.cdf(d2)
        elif low_b > 0.0 and high_b < float("inf"):
            # P(low < S < high) = N(d2_low) - N(d2_high)
            sigma_low = interpolate_iv_at_price(legs, low_b)
            sigma_high = interpolate_iv_at_price(legs, high_b)
            d2_low = compute_d2(spot_price, low_b, risk_free_rate, sigma_low, t)
            d2_high = compute_d2(spot_price, high_b, risk_free_rate, sigma_high, t)
            pop += norm.cdf(d2_low) - norm.cdf(d2_high)

    return round(max(0.0, min(1.0, pop)), 6)


def compute_expected_value(
    legs: list[StrategyLeg],
    spot_price: float,
    risk_free_rate: float,
    ttm_years: float,
    num_points: int = 1000,
) -> float:
    """Expected value via numerical integration of PnL(S) * lognormal_PDF(S) dS."""
    if ttm_years <= 0:
        return compute_payoff_at_expiry(legs, spot_price)

    t = ttm_years
    sigma = weighted_iv(legs)

    if sigma <= 0:
        return compute_payoff_at_expiry(legs, spot_price)

    # Lognormal parameters
    mu = math.log(spot_price) + (risk_free_rate - 0.5 * sigma**2) * t
    s_param = sigma * math.sqrt(t)
    scale = math.exp(mu)

    # Integration range: covers ~99.9% of the distribution
    low = lognorm.ppf(0.001, s=s_param, scale=scale)
    high = lognorm.ppf(0.999, s=s_param, scale=scale)
    prices = np.linspace(max(low, 0.01), high, num_points)
    step = float(prices[1] - prices[0])

    ev = 0.0
    for p in prices:
        pnl = compute_payoff_at_expiry(legs, float(p))
        pdf_val = lognorm.pdf(float(p), s=s_param, scale=scale)
        ev += pnl * pdf_val * step

    return round(ev, 4)


def _bs_d1(spot: float, strike: float, r: float, sigma: float, t: float) -> float:
    """Black-Scholes d1."""
    if sigma <= 0 or t <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    return (math.log(spot / strike) + (r + 0.5 * sigma**2) * t) / (sigma * math.sqrt(t))


def compute_leg_greeks(
    leg: StrategyLeg,
    spot: float,
    r: float,
    t: float,
) -> tuple[float, float, float, float]:
    """Compute Black-Scholes Greeks for a single leg.

    Returns (delta, gamma, theta, vega) scaled by quantity and position sign.
    Theta is per-calendar-day. Vega is per 1% move in IV.
    """
    sigma = leg.iv
    if sigma <= 0 or t <= 0:
        # At/past expiration or no IV — only delta makes sense
        if t <= 0:
            if leg.option_type == "call":
                d = 1.0 if spot > leg.strike else 0.0
            else:
                d = -1.0 if spot < leg.strike else 0.0
            sign = 1 if leg.position == "long" else -1
            return (d * sign * leg.quantity, 0.0, 0.0, 0.0)
        return (0.0, 0.0, 0.0, 0.0)

    sqrt_t = math.sqrt(t)
    d1 = _bs_d1(spot, leg.strike, r, sigma, t)
    d2 = d1 - sigma * sqrt_t
    n_d1 = norm.cdf(d1)
    n_prime_d1 = norm.pdf(d1)
    discount = math.exp(-r * t)

    if leg.option_type == "call":
        delta = n_d1
        theta = (-(spot * n_prime_d1 * sigma) / (2 * sqrt_t) - r * leg.strike * discount * norm.cdf(d2)) / 365.0
    else:
        delta = n_d1 - 1.0
        theta = (-(spot * n_prime_d1 * sigma) / (2 * sqrt_t) + r * leg.strike * discount * norm.cdf(-d2)) / 365.0

    gamma = n_prime_d1 / (spot * sigma * sqrt_t)
    vega = spot * n_prime_d1 * sqrt_t / 100.0  # per 1% IV move

    sign = 1 if leg.position == "long" else -1
    qty = leg.quantity

    return (
        round(delta * sign * qty, 6),
        round(gamma * sign * qty, 6),
        round(theta * sign * qty, 6),
        round(vega * sign * qty, 6),
    )


def compute_strategy_greeks(
    legs: list[StrategyLeg],
    spot: float,
    r: float,
    ttm_years: float,
) -> GreeksResult:
    """Sum Greeks across all legs."""
    t = ttm_years
    total_delta = 0.0
    total_gamma = 0.0
    total_theta = 0.0
    total_vega = 0.0

    for leg in legs:
        d, g, th, v = compute_leg_greeks(leg, spot, r, t)
        total_delta += d
        total_gamma += g
        total_theta += th
        total_vega += v

    return GreeksResult(
        delta=round(total_delta, 4),
        gamma=round(total_gamma, 4),
        theta=round(total_theta, 4),
        vega=round(total_vega, 4),
    )


# ---------------------------------------------------------------------------
# Phase 1.1: current-time curves and per-leg diagnostics.
#
# These functions compute theoretical values at *today's* vol surface (or
# a what-if shifted vol/time), not at expiry. They exist so OptionsStrategyLabComponent
# can stop computing the same numbers in TypeScript via `black-scholes.ts`.
# Math authority: `app/services/bs_greeks.py` (closed-form, continuous time).
# ---------------------------------------------------------------------------


def _per_share_leg_value(leg: StrategyLeg, spot: float, ttm_years: float, r: float, iv: float) -> float:
    """Theoretical per-share value of one leg at given (spot, ttm, iv).

    Sign matches `position`: long contributes +value, short contributes -value.
    Quantity is *not* applied here — caller multiplies if needed.
    """
    if ttm_years <= 0 or iv <= 0:
        # Intrinsic only.
        if leg.option_type == "call":
            value = max(spot - leg.strike, 0.0)
        else:
            value = max(leg.strike - spot, 0.0)
    else:
        value = bs_european_price(
            spot=spot,
            strike=leg.strike,
            ttm_years=ttm_years,
            rate=r,
            volatility=iv,
            is_call=leg.option_type == "call",
            dividend=0.0,
        )

    sign = 1.0 if leg.position == "long" else -1.0
    return sign * value


def compute_current_curve(
    legs: list[StrategyLeg],
    spot_price: float,
    price_range_pct: float,
    num_points: int,
    risk_free_rate: float,
    ttm_years: float,
    iv_shift: float = 0.0,
) -> list[CurrentCurvePoint]:
    """Theoretical per-share strategy value at today's vol surface, over a spot grid.

    Distinct from `compute_payoff_curve` (which is at expiry, intrinsic-only).
    `theoretical_pnl` = theoretical_value - strategy_cost, both per share.
    """
    low = spot_price * (1.0 - price_range_pct)
    high = spot_price * (1.0 + price_range_pct)
    prices = np.linspace(max(low, 0.01), high, num_points)
    cost = compute_strategy_cost(legs)

    points: list[CurrentCurvePoint] = []
    for p in prices:
        spot = float(p)
        value = 0.0
        for leg in legs:
            iv = max(leg.iv + iv_shift, 0.0)
            value += _per_share_leg_value(leg, spot, ttm_years, risk_free_rate, iv) * leg.quantity
        points.append(
            CurrentCurvePoint(
                price=round(spot, 4),
                theoretical_value=round(value, 4),
                theoretical_pnl=round(value - cost, 4),
            )
        )
    return points


def compute_greek_curves(
    legs: list[StrategyLeg],
    spot_price: float,
    price_range_pct: float,
    num_points: int,
    risk_free_rate: float,
    ttm_years: float,
) -> list[GreekCurvePoint]:
    """Aggregate Greek values at each spot grid point.

    Per-spot delta/gamma/theta/vega summed over legs, with sign and quantity applied.
    Vega is per 1% IV move; theta is per calendar day (matching the rest of the response).
    """
    low = spot_price * (1.0 - price_range_pct)
    high = spot_price * (1.0 + price_range_pct)
    prices = np.linspace(max(low, 0.01), high, num_points)

    points: list[GreekCurvePoint] = []
    for p in prices:
        spot = float(p)
        agg_delta = 0.0
        agg_gamma = 0.0
        agg_theta = 0.0
        agg_vega = 0.0
        for leg in legs:
            iv = leg.iv
            sign = 1.0 if leg.position == "long" else -1.0
            qty = leg.quantity
            if ttm_years <= 0 or iv <= 0 or spot <= 0:
                # At expiry: delta jumps to ±1 if ITM, 0 otherwise; other
                # Greeks vanish. Mirror compute_leg_greeks() so the same
                # response doesn't disagree with itself between greeks
                # (intrinsic-aware) and greek_curves (was zeroing all).
                if ttm_years <= 0 and spot > 0:
                    if leg.option_type == "call":
                        d = 1.0 if spot > leg.strike else 0.0
                    else:
                        d = -1.0 if spot < leg.strike else 0.0
                    agg_delta += d * sign * qty
                continue
            g = black_scholes_greeks(
                spot=spot,
                strike=leg.strike,
                ttm_years=ttm_years,
                volatility=iv,
                rate=risk_free_rate,
                dividend=0.0,
                is_call=leg.option_type == "call",
            )
            agg_delta += g.delta * sign * qty
            agg_gamma += g.gamma * sign * qty
            agg_theta += g.theta * sign * qty
            agg_vega += g.vega * sign * qty
        points.append(
            GreekCurvePoint(
                price=round(spot, 4),
                delta=round(agg_delta, 6),
                gamma=round(agg_gamma, 6),
                theta=round(agg_theta, 6),
                vega=round(agg_vega, 6),
            )
        )
    return points


def compute_leg_diagnostics(
    legs: list[StrategyLeg],
    spot_price: float,
    risk_free_rate: float,
    ttm_years: float,
) -> list[LegDiagnostic]:
    """Per-leg current theoretical value and per-leg Greeks at the request spot.

    Used by the Strategy Lab diagnostic table that historically computed these
    values in TypeScript via `black-scholes.ts`. Sign convention: per-share
    *unsigned* values (long/short reflected in the `position` field, not the
    numbers) so the UI can format independently. Use the request quantity as-is.
    """
    rows: list[LegDiagnostic] = []
    for leg in legs:
        if ttm_years > 0 and leg.iv > 0:
            theo = bs_european_price(
                spot=spot_price,
                strike=leg.strike,
                ttm_years=ttm_years,
                rate=risk_free_rate,
                volatility=leg.iv,
                is_call=leg.option_type == "call",
                dividend=0.0,
            )
            g = black_scholes_greeks(
                spot=spot_price,
                strike=leg.strike,
                ttm_years=ttm_years,
                volatility=leg.iv,
                rate=risk_free_rate,
                dividend=0.0,
                is_call=leg.option_type == "call",
            )
            delta, gamma, theta, vega = g.delta, g.gamma, g.theta, g.vega
        else:
            # Expired or zero-IV: intrinsic value, delta = ±1 if ITM (mirrors
            # compute_leg_greeks() so greeks/greek_curves/leg_diagnostics agree
            # at expiry). Other Greeks vanish.
            if leg.option_type == "call":
                theo = max(spot_price - leg.strike, 0.0)
                delta = 1.0 if (ttm_years <= 0 and spot_price > leg.strike) else 0.0
            else:
                theo = max(leg.strike - spot_price, 0.0)
                delta = -1.0 if (ttm_years <= 0 and spot_price < leg.strike) else 0.0
            gamma = theta = vega = 0.0

        # Per-leg P&L. Long: theoretical - premium (gain when theo rises above
        # what we paid). Short: premium - theoretical (gain when theo falls
        # below what we collected). Both scaled by quantity. Per-share — a
        # contract multiplier is the caller's display choice (matches UI
        # convention of showing per-share entry/theo).
        per_share_signed = (theo - leg.premium) if leg.position == "long" else (leg.premium - theo)
        leg_pnl = per_share_signed * leg.quantity

        rows.append(
            LegDiagnostic(
                leg_id=leg.leg_id,
                strike=leg.strike,
                option_type=leg.option_type,
                position=leg.position,
                quantity=leg.quantity,
                iv=leg.iv,
                entry_premium=leg.premium,
                current_theoretical=round(theo, 6),
                current_delta=round(delta, 6),
                current_gamma=round(gamma, 6),
                current_theta=round(theta, 6),
                current_vega=round(vega, 6),
                leg_pnl=round(leg_pnl, 6),
            )
        )
    return rows


def compute_strategy_cost(legs: list[StrategyLeg]) -> float:
    """Net cost of the strategy. Positive = debit, negative = credit."""
    cost = 0.0
    for leg in legs:
        if leg.position == "long":
            cost += leg.premium * leg.quantity
        else:
            cost -= leg.premium * leg.quantity
    return round(cost, 4)


def analyze_strategy(request: StrategyAnalyzeRequest) -> StrategyAnalyzeResponse:
    """Main orchestrator: compute all strategy metrics."""
    try:
        logger.info(
            "[Strategy Engine] Analyzing %d-leg strategy for %s, spot=%.2f, expiry_ms=%s",
            len(request.legs),
            request.symbol,
            request.spot_price,
            request.expiration_ms,
        )

        # Time to expiry is measured to the expiry anchor instant (16:00 New
        # York time on the expiry date), the same instant the chain snapshot's
        # rate tenor is measured to (#2789, #2934).
        ttm_years = time_to_expiry_years(request.expiration_ms, now_ms_utc())

        strategy_cost = compute_strategy_cost(request.legs)

        curve = compute_payoff_curve(
            request.legs,
            request.spot_price,
            request.price_range_pct,
            request.curve_points,
        )

        breakevens = find_breakevens(request.legs)

        extremes = compute_max_profit_loss(request.legs)
        # The response is a finite number: a side with no limit is carried as the cap.
        max_profit = MAX_PROFIT_CAP if math.isinf(extremes.max_profit) else extremes.max_profit
        max_loss = -MAX_PROFIT_CAP if math.isinf(extremes.max_loss) else extremes.max_loss

        pop = compute_pop(request.legs, request.spot_price, request.risk_free_rate, ttm_years)

        ev = compute_expected_value(request.legs, request.spot_price, request.risk_free_rate, ttm_years)

        greeks = compute_strategy_greeks(request.legs, request.spot_price, request.risk_free_rate, ttm_years)

        logger.info(
            "[Strategy Engine] Result: POP=%.2f%%, EV=%.2f, MaxProfit=%.2f, MaxLoss=%.2f, Greeks=%s",
            pop * 100,
            ev,
            max_profit,
            max_loss,
            greeks,
        )

        # Phase 1.1 opt-in extensions: compute only when requested so existing
        # callers don't pay the cost.
        current_curve = None
        if request.include_current_curve:
            shifted_years = max(ttm_years - request.what_if_time_shift_days / 365.0, 0.0)
            current_curve = compute_current_curve(
                request.legs,
                request.spot_price,
                request.price_range_pct,
                request.curve_points,
                request.risk_free_rate,
                shifted_years,
                iv_shift=request.what_if_iv_shift,
            )

        greek_curves = None
        if request.include_greek_curves:
            greek_curves = compute_greek_curves(
                request.legs,
                request.spot_price,
                request.price_range_pct,
                request.curve_points,
                request.risk_free_rate,
                ttm_years,
            )

        leg_diagnostics = None
        if request.include_leg_diagnostics:
            leg_diagnostics = compute_leg_diagnostics(
                request.legs,
                request.spot_price,
                request.risk_free_rate,
                ttm_years,
            )

        return StrategyAnalyzeResponse(
            success=True,
            symbol=request.symbol,
            spot_price=request.spot_price,
            strategy_cost=strategy_cost,
            pop=pop,
            expected_value=ev,
            max_profit=max_profit,
            max_loss=max_loss,
            breakevens=breakevens,
            curve=curve,
            greeks=greeks,
            current_curve=current_curve,
            greek_curves=greek_curves,
            leg_diagnostics=leg_diagnostics,
        )

    except Exception as e:
        logger.error("[Strategy Engine] Error: %s", str(e), exc_info=True)
        return StrategyAnalyzeResponse(
            success=False,
            symbol=request.symbol,
            error=str(e),
        )
