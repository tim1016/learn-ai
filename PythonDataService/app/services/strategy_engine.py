"""Options strategy analysis engine.

All probability math lives here — Black-Scholes lognormal model
for POP (Probability of Profit) and EV (Expected Value).
"""
from __future__ import annotations

import math
import logging
from datetime import date, datetime

import numpy as np
from scipy.stats import norm, lognorm

from app.models.strategy import (
    StrategyLeg,
    StrategyAnalyzeRequest,
    StrategyAnalyzeResponse,
    PayoffPoint,
    GreeksResult,
)

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


def find_breakevens(
    legs: list[StrategyLeg],
    spot_price: float,
    price_range_pct: float,
) -> list[float]:
    """Find prices where payoff crosses zero using sign-change detection."""
    low = spot_price * (1.0 - price_range_pct)
    high = spot_price * (1.0 + price_range_pct)
    prices = np.linspace(max(low, 0.01), high, 2000)
    payoffs = np.array([compute_payoff_at_expiry(legs, float(p)) for p in prices])

    breakevens: list[float] = []
    for i in range(len(payoffs) - 1):
        if payoffs[i] * payoffs[i + 1] < 0:
            # Linear interpolation between adjacent points
            p1, p2 = float(prices[i]), float(prices[i + 1])
            y1, y2 = payoffs[i], payoffs[i + 1]
            be = p1 - y1 * (p2 - p1) / (y2 - y1)
            breakevens.append(round(be, 4))

    return sorted(breakevens)


def compute_max_profit_loss(
    legs: list[StrategyLeg],
    spot_price: float,
    price_range_pct: float,
) -> tuple[float, float]:
    """Compute max profit and max loss over the price range."""
    low = spot_price * (1.0 - price_range_pct)
    high = spot_price * (1.0 + price_range_pct)
    prices = np.linspace(max(low, 0.01), high, 2000)
    payoffs = [compute_payoff_at_expiry(legs, float(p)) for p in prices]

    max_profit = max(payoffs)
    max_loss = min(payoffs)

    if max_profit > MAX_PROFIT_CAP:
        max_profit = MAX_PROFIT_CAP

    return round(max_profit, 4), round(max_loss, 4)


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

    return sum(
        leg.iv * leg.premium * leg.quantity for leg in valid_legs
    ) / total_weight


def interpolate_iv_at_price(legs: list[StrategyLeg], price: float) -> float:
    """Interpolate IV at a given underlying price using leg strikes.

    Sorts legs by strike and linearly interpolates between the two
    nearest strikes. Falls back to nearest leg IV if price is outside
    all strikes, or to weighted_iv() if no legs have positive IV.
    """
    valid = sorted(
        [leg for leg in legs if leg.iv > 0],
        key=lambda l: l.strike,
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
    days_to_expiry: int,
) -> float:
    """Probability of Profit using Black-Scholes lognormal model.

    Determines profit regions from the payoff function, then computes
    probability mass in those regions using N(d2).

    Uses per-boundary IV interpolation to capture IV skew effects
    for multi-leg strategies (iron condors, butterflies, etc.).
    """
    if days_to_expiry <= 0:
        # At expiration, POP is binary based on current spot
        pnl_now = compute_payoff_at_expiry(legs, spot_price)
        return 1.0 if pnl_now > 0 else 0.0

    t = days_to_expiry / 365.0
    breakevens = find_breakevens(legs, spot_price, 0.50)

    if not breakevens:
        # No breakevens: entire range is profit or loss
        pnl_at_spot = compute_payoff_at_expiry(legs, spot_price)
        return 1.0 if pnl_at_spot > 0 else 0.0

    # Determine the profit structure by sampling just above/below each breakeven
    # and at the extremes of the range
    eps = 0.01

    # Build regions: (-inf, BE1), (BE1, BE2), ..., (BEn, +inf)
    # Determine which regions are profitable
    boundaries = [0.0] + breakevens + [float("inf")]
    pop = 0.0

    for i in range(len(boundaries) - 1):
        low_b, high_b = boundaries[i], boundaries[i + 1]

        # Test midpoint of region
        if high_b == float("inf"):
            test_price = breakevens[-1] + eps
        elif low_b == 0.0:
            test_price = breakevens[0] - eps
        else:
            test_price = (low_b + high_b) / 2.0

        if test_price <= 0:
            test_price = eps

        pnl = compute_payoff_at_expiry(legs, test_price)
        if pnl <= 0:
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
    days_to_expiry: int,
    num_points: int = 1000,
) -> float:
    """Expected value via numerical integration of PnL(S) * lognormal_PDF(S) dS."""
    if days_to_expiry <= 0:
        return compute_payoff_at_expiry(legs, spot_price)

    t = days_to_expiry / 365.0
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
        theta = (-(spot * n_prime_d1 * sigma) / (2 * sqrt_t)
                 - r * leg.strike * discount * norm.cdf(d2)) / 365.0
    else:
        delta = n_d1 - 1.0
        theta = (-(spot * n_prime_d1 * sigma) / (2 * sqrt_t)
                 + r * leg.strike * discount * norm.cdf(-d2)) / 365.0

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
    days_to_expiry: int,
) -> GreeksResult:
    """Sum Greeks across all legs."""
    t = days_to_expiry / 365.0
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
            "[Strategy Engine] Analyzing %d-leg strategy for %s, spot=%.2f, expiry=%s",
            len(request.legs), request.symbol, request.spot_price, request.expiration_date,
        )

        # Days to expiry
        exp_date = datetime.strptime(request.expiration_date, "%Y-%m-%d").date()
        today = date.today()
        days_to_expiry = max((exp_date - today).days, 0)

        strategy_cost = compute_strategy_cost(request.legs)

        curve = compute_payoff_curve(
            request.legs,
            request.spot_price,
            request.price_range_pct,
            request.curve_points,
        )

        breakevens = find_breakevens(
            request.legs, request.spot_price, request.price_range_pct
        )

        max_profit, max_loss = compute_max_profit_loss(
            request.legs, request.spot_price, request.price_range_pct
        )

        pop = compute_pop(
            request.legs, request.spot_price, request.risk_free_rate, days_to_expiry
        )

        ev = compute_expected_value(
            request.legs, request.spot_price, request.risk_free_rate, days_to_expiry
        )

        greeks = compute_strategy_greeks(
            request.legs, request.spot_price, request.risk_free_rate, days_to_expiry
        )

        logger.info(
            "[Strategy Engine] Result: POP=%.2f%%, EV=%.2f, MaxProfit=%.2f, MaxLoss=%.2f, Greeks=%s",
            pop * 100, ev, max_profit, max_loss, greeks,
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
        )

    except Exception as e:
        logger.error("[Strategy Engine] Error: %s", str(e), exc_info=True)
        return StrategyAnalyzeResponse(
            success=False,
            symbol=request.symbol,
            error=str(e),
        )
