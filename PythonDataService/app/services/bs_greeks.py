"""Closed-form Black-Scholes-Merton Greeks for European options.

Computes delta / gamma / theta / vega / rho directly from a continuous
time-to-expiry in years — no QuantLib ``Date`` arithmetic, so the math
works at any sub-day resolution. The data-lab options companion uses
this for per-bar Greeks on 0DTE contracts where the date-based engine
in ``app.services.quantlib_pricer`` collapses to ``t_years = 0``.

Sign and scaling conventions match ``quantlib_pricer.GreeksResult`` so
downstream consumers see identical units regardless of which engine
produced the row:

- ``theta`` per **calendar day** (annual / 365)
- ``vega``  per **1% IV move** (raw / 100)
- ``rho``   per **1% rate move** (raw / 100)

See ``docs/references/options-bs-greeks-2026-04-24.md`` for source
attribution and tolerance notes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from scipy.stats import norm


@dataclass(frozen=True)
class BSGreeks:
    delta: float
    gamma: float
    theta: float  # per calendar day
    vega: float  # per 1% IV move
    rho: float  # per 1% rate move


def black_scholes_greeks(
    spot: float,
    strike: float,
    ttm_years: float,
    volatility: float,
    rate: float,
    dividend: float,
    is_call: bool,
) -> BSGreeks:
    """Compute closed-form Greeks for a European option (Hull 11e, Ch 19).

    All inputs in continuous-time, annualized units. Caller is responsible
    for guarding degenerate inputs — at boundaries (``spot``, ``strike``,
    ``ttm_years``, ``volatility`` ≤ 0) the formulas would produce
    ``inf``/``nan``, so this function asserts strict positivity.
    """
    if spot <= 0 or strike <= 0 or ttm_years <= 0 or volatility <= 0:
        raise ValueError(
            f"black_scholes_greeks requires positive spot/strike/ttm/vol; "
            f"got spot={spot}, strike={strike}, ttm={ttm_years}, vol={volatility}"
        )

    sqrt_t = math.sqrt(ttm_years)
    sigma_sqrt_t = volatility * sqrt_t
    d1 = (math.log(spot / strike) + (rate - dividend + 0.5 * volatility * volatility) * ttm_years) / sigma_sqrt_t
    d2 = d1 - sigma_sqrt_t

    nd1 = float(norm.cdf(d1))
    nd2 = float(norm.cdf(d2))
    pd1 = float(norm.pdf(d1))
    disc_q = math.exp(-dividend * ttm_years)
    disc_r = math.exp(-rate * ttm_years)

    gamma = disc_q * pd1 / (spot * sigma_sqrt_t)
    vega_pct = (spot * disc_q * pd1 * sqrt_t) / 100.0

    if is_call:
        delta = disc_q * nd1
        theta_annual = (
            -spot * disc_q * pd1 * volatility / (2.0 * sqrt_t)
            - rate * strike * disc_r * nd2
            + dividend * spot * disc_q * nd1
        )
        rho_pct = (strike * ttm_years * disc_r * nd2) / 100.0
    else:
        delta = disc_q * (nd1 - 1.0)
        theta_annual = (
            -spot * disc_q * pd1 * volatility / (2.0 * sqrt_t)
            + rate * strike * disc_r * (1.0 - nd2)
            - dividend * spot * disc_q * (1.0 - nd1)
        )
        rho_pct = -(strike * ttm_years * disc_r * (1.0 - nd2)) / 100.0

    return BSGreeks(
        delta=delta,
        gamma=gamma,
        theta=theta_annual / 365.0,
        vega=vega_pct,
        rho=rho_pct,
    )
