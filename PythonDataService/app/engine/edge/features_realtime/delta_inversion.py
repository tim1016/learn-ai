"""Delta-based moneyness — solve for strike given target Black-Scholes delta.

Inverts BS call delta:
    Δ_C = e^(-qT) · N(d1)
    d1  = (ln(S/K) + (r - q + σ²/2)T) / (σ√T)

For a fixed σ this is closed-form:
    K = S · exp[(r - q + σ²/2)T - σ√T · N⁻¹(Δ · e^(qT))]

When σ depends on K via the smile σ(K, T) we run a fixed-point iteration
between this closed form and the surface, falling back to bisection on a
bounded K range if convergence fails.
"""
from __future__ import annotations

from collections.abc import Callable

import numpy as np
from scipy.optimize import brentq
from scipy.stats import norm

VolSurfaceFn = Callable[[float, float], float]  # (K, T) -> sigma


def strike_for_delta_constant_vol(
    *, S: float, T: float, r: float, q: float, sigma: float, target_delta: float,
) -> float:
    """Closed-form K for a fixed σ. delta is the call delta in (0, e^{-qT})."""
    if not 0 < target_delta < np.exp(-q * T):
        raise ValueError(f"target_delta {target_delta} out of (0, e^(-qT)) range")
    d1 = norm.ppf(target_delta * np.exp(q * T))
    return float(S * np.exp((r - q + 0.5 * sigma ** 2) * T - sigma * np.sqrt(T) * d1))


def strike_and_iv_for_delta(
    *, S: float, T: float, r: float, q: float, target_delta: float,
    surface: VolSurfaceFn, max_iter: int = 20, tol: float = 1e-6,
    k_lower_mult: float = 0.3, k_upper_mult: float = 3.0,
) -> tuple[float, float]:
    """Fixed-point iteration: K_i = closed_form(σ(K_{i-1}, T)).

    Returns (K, σ) at the converged point. Falls back to bisection if
    fixed-point oscillates.
    """
    sigma = surface(S, T)
    K_prev = S
    for _ in range(max_iter):
        K = strike_for_delta_constant_vol(
            S=S, T=T, r=r, q=q, sigma=sigma, target_delta=target_delta,
        )
        if abs(K - K_prev) < tol * S:
            return K, sigma
        sigma = surface(K, T)
        K_prev = K

    # Fallback: bisect on |delta(K) - target| = 0
    def signed_diff(K_test: float) -> float:
        s = surface(K_test, T)
        d1 = (np.log(S / K_test) + (r - q + 0.5 * s ** 2) * T) / (s * np.sqrt(T))
        modeled_delta = np.exp(-q * T) * norm.cdf(d1)
        return modeled_delta - target_delta

    K_bisect = brentq(signed_diff, S * k_lower_mult, S * k_upper_mult, xtol=tol * S)
    return float(K_bisect), float(surface(K_bisect, T))
