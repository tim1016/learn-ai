from __future__ import annotations

import math
from scipy.stats import norm


RISK_FREE_RATE = 0.043  # Fed funds approximation — cancels in IV rank/VRP


def bs_price(
    S: float, K: float, T: float, r: float, sigma: float, option_type: str
) -> float:
    """Standard Black-Scholes European option price.

    Args:
        S: Spot price
        K: Strike price
        T: Time to expiry in years
        r: Risk-free rate
        sigma: Volatility (annualized)
        option_type: "call" or "put"
    """
    if T <= 0 or sigma <= 0:
        return 0.0

    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    if option_type == "call":
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    else:
        return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def bs_vega(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-Scholes vega (derivative w.r.t. sigma) for Newton-Raphson gradient."""
    if T <= 0 or sigma <= 0:
        return 0.0

    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    return S * math.sqrt(T) * norm.pdf(d1)


def implied_volatility(
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    option_type: str,
    max_iterations: int = 100,
    tolerance: float = 1e-8,
) -> float | None:
    """Derive implied volatility via Newton-Raphson.

    Returns None if solver doesn't converge or inputs are invalid.
    Guards: reject if T < 7/365, price <= 0, or no convergence.
    """
    if T < 7 / 365:
        return None
    if market_price <= 0:
        return None
    if S <= 0 or K <= 0:
        return None

    # Intrinsic value check — option price must exceed intrinsic
    intrinsic = max(0.0, S - K) if option_type == "call" else max(0.0, K * math.exp(-r * T) - S)
    if market_price < intrinsic - tolerance:
        return None

    # Initial guess: Brenner-Subrahmanyam approximation
    sigma = math.sqrt(2 * math.pi / T) * market_price / S
    sigma = max(0.01, min(sigma, 5.0))

    for _ in range(max_iterations):
        price = bs_price(S, K, T, r, sigma, option_type)
        vega = bs_vega(S, K, T, r, sigma)

        if vega < 1e-12:
            return None

        diff = price - market_price
        if abs(diff) < tolerance:
            # Reject IV outside [0.05, 3.0] (5%-300% annualized)
            if 0.05 <= sigma <= 3.0:
                return sigma
            return None

        sigma -= diff / vega

        # Keep sigma in a reasonable range during iteration
        if sigma <= 0.001:
            sigma = 0.001
        elif sigma > 5.0:
            return None

    return None
