"""py_vollib solver-parity test (Step 5 of IV-RV alignment).

Confirms our BS pricer (`bs_european_price`) and IV solver (`implied_volatility`)
agree with py_vollib's reference Black-Scholes-Merton implementation across a
moneyness × TTM × volatility × rate × dividend grid.

Tolerance: ``price diff < 1e-8`` (numerical noise only) and
``IV diff < 5e-5`` (5 vol points = 0.005% absolute) on contracts with
non-degenerate vega. py_vollib is itself a thin reference implementation,
so any larger divergence indicates a real bug in our pricer or solver, not
a floating-point coincidence.
"""

from __future__ import annotations

import math

import pytest

from py_vollib.black_scholes_merton import black_scholes_merton as pv_bsm
from py_vollib.black_scholes_merton.implied_volatility import (
    implied_volatility as pv_iv,
)

from app.services.bs_greeks import bs_european_price, bs_european_vega
from app.volatility.solver import implied_volatility as our_iv


# Grid factors held tight enough to keep the test < 0.5s but wide enough
# to exercise the moneyness / TTM / vol surface. The full Cartesian product
# is 4 × 4 × 4 × 3 × 3 = 576 cases — pytest parametrize is overkill, so we
# loop inside one test to keep collection cheap.
SPOTS = [100.0]
STRIKES_REL = [0.7, 0.85, 1.0, 1.15, 1.3]   # K / S
TTMS_DAYS = [7, 30, 90, 365]
VOLS = [0.05, 0.20, 0.60, 1.50]
RATES = [0.0, 0.025, 0.07]
DIVS = [0.0, 0.013, 0.03]


def _calls_and_puts():
    yield "c", True
    yield "p", False


class TestPriceParity:
    def test_price_agrees_with_py_vollib_within_1e8(self):
        cases = 0
        max_diff = 0.0
        worst = None
        for s in SPOTS:
            for k_rel in STRIKES_REL:
                k = s * k_rel
                for ttm_d in TTMS_DAYS:
                    ttm = ttm_d / 365.0
                    for sigma in VOLS:
                        for r in RATES:
                            for q in DIVS:
                                for flag, is_call in _calls_and_puts():
                                    ours = bs_european_price(
                                        spot=s, strike=k, ttm_years=ttm,
                                        rate=r, volatility=sigma, is_call=is_call,
                                        dividend=q,
                                    )
                                    theirs = pv_bsm(flag, s, k, ttm, r, sigma, q)
                                    diff = abs(ours - theirs)
                                    if diff > max_diff:
                                        max_diff = diff
                                        worst = (k_rel, ttm_d, sigma, r, q, flag, ours, theirs)
                                    cases += 1
        assert cases > 200
        assert max_diff < 1e-8, f"max price diff {max_diff:.2e}, worst case {worst}"


class TestIvSolverParity:
    """For each grid point, price with sigma_true, then re-solve via both solvers."""

    def test_iv_agrees_with_py_vollib_within_5bps(self):
        # Skip degenerate cases where vega is too small for either solver to recover.
        VEGA_FLOOR = 0.01
        IV_TOL = 5e-5  # 5 bps absolute

        cases = 0
        max_diff = 0.0
        worst = None
        for k_rel in STRIKES_REL:
            k = 100.0 * k_rel
            for ttm_d in TTMS_DAYS:
                ttm = ttm_d / 365.0
                for sigma_true in VOLS:
                    for r in RATES:
                        for q in DIVS:
                            for flag, is_call in _calls_and_puts():
                                vega = bs_european_vega(
                                    spot=100.0, strike=k, ttm_years=ttm,
                                    rate=r, volatility=sigma_true, dividend=q,
                                )
                                if vega < VEGA_FLOOR:
                                    continue
                                price = bs_european_price(
                                    spot=100.0, strike=k, ttm_years=ttm,
                                    rate=r, volatility=sigma_true, is_call=is_call,
                                    dividend=q,
                                )
                                if price <= 0 or not math.isfinite(price):
                                    continue
                                # py_vollib raises on bracket failure; skip those.
                                try:
                                    pv_solved = pv_iv(price, 100.0, k, ttm, r, q, flag)
                                except Exception:
                                    continue
                                ours_res = our_iv(
                                    option_price=price,
                                    spot=100.0,
                                    strike=k,
                                    ttm=ttm,
                                    rate=r,
                                    dividend=q,
                                    is_call=is_call,
                                )
                                if ours_res.iv is None:
                                    continue
                                diff = abs(ours_res.iv - pv_solved)
                                if diff > max_diff:
                                    max_diff = diff
                                    worst = (k_rel, ttm_d, sigma_true, r, q, flag, ours_res.iv, pv_solved)
                                cases += 1
        assert cases > 100, f"too few non-degenerate cases ({cases})"
        assert max_diff < IV_TOL, f"max IV diff {max_diff:.2e} > {IV_TOL}, worst {worst}"

    def test_round_trip_iv_recovers_input_within_5bps(self):
        """Spot-check: BSM(σ_true) → IV → σ_recovered. Picks ATM-ish points
        where vega is substantial; deep-ITM short-dated cases where the option
        price is dominated by intrinsic value are not recoverable by *any* IV
        solver and are excluded.
        """
        cases = [
            (100.0, 100.0, 30 / 365.0, 0.05, 0.02, 0.20, True),    # ATM 30d call
            (100.0, 110.0, 60 / 365.0, 0.04, 0.01, 0.40, False),   # OTM 60d put
            (100.0, 100.0, 7 / 365.0, 0.05, 0.0, 0.30, True),      # ATM 7d call
            (100.0, 95.0, 90 / 365.0, 0.04, 0.015, 0.25, False),   # OTM 90d put
        ]
        for s, k, t, r, q, sigma_true, is_call in cases:
            price = bs_european_price(
                spot=s, strike=k, ttm_years=t, rate=r,
                volatility=sigma_true, is_call=is_call, dividend=q,
            )
            res = our_iv(
                option_price=price, spot=s, strike=k, ttm=t,
                rate=r, dividend=q, is_call=is_call,
            )
            assert res.iv is not None
            assert abs(res.iv - sigma_true) < 5e-5, (
                f"σ={sigma_true} → solved {res.iv:.6f}, diff {abs(res.iv - sigma_true):.2e}"
            )
