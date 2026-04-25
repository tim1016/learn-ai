"""Tests for closed-form Black-Scholes Greeks (bs_greeks.py).

Anchor cases:
- The reconciliation spot-check (SPY 0DTE call, 14:00 ET, 2hr to expiry).
- Self-consistency: BS price → IV solver → Greeks roundtrips.
- Sub-day TTM regression: the bug that produced 100% NaN in the data-lab
  export was a 1/365 floor on TTM; these tests pin that fix.
"""

from __future__ import annotations

import math

import pytest
from scipy.stats import norm

from app.services.bs_greeks import black_scholes_greeks
from app.volatility.solver import implied_volatility


def _bs_price(spot: float, strike: float, ttm: float, vol: float, rate: float, dividend: float, is_call: bool) -> float:
    """Closed-form Black-Scholes-Merton price for a European option."""
    sqrt_t = math.sqrt(ttm)
    d1 = (math.log(spot / strike) + (rate - dividend + 0.5 * vol * vol) * ttm) / (vol * sqrt_t)
    d2 = d1 - vol * sqrt_t
    disc_q = math.exp(-dividend * ttm)
    disc_r = math.exp(-rate * ttm)
    if is_call:
        return spot * disc_q * float(norm.cdf(d1)) - strike * disc_r * float(norm.cdf(d2))
    return strike * disc_r * float(norm.cdf(-d2)) - spot * disc_q * float(norm.cdf(-d1))


class TestSpotCheckFromReconciliation:
    """Pin against the manual spot-check in
    docs/references/reconciliations/data-lab-spy-2026-04-17-to-2026-04-24.md
    § Finding 3.1 (SPY 0DTE call, 2hr to expiry).
    """

    def test_call_delta_matches_reconciliation(self):
        greeks = black_scholes_greeks(
            spot=710.109,
            strike=709.0,
            ttm_years=2.0 / (365.0 * 24.0),
            volatility=0.1789,
            rate=0.05,
            dividend=0.0,
            is_call=True,
        )
        # Reconciliation report: delta = 0.7203
        assert greeks.delta == pytest.approx(0.7203, abs=1e-3)
        # ITM call: positive gamma, negative theta (decay), positive vega
        assert greeks.gamma > 0
        assert greeks.theta < 0
        assert greeks.vega > 0


class TestPutCallParity:
    """Greek-level parity identities — independent of solver, exact for BS."""

    def test_call_minus_put_delta_equals_exp_minus_qT(self):
        S, K, T, vol, r, q = 100.0, 100.0, 0.25, 0.20, 0.05, 0.02
        call = black_scholes_greeks(S, K, T, vol, r, q, is_call=True)
        put = black_scholes_greeks(S, K, T, vol, r, q, is_call=False)
        assert call.delta - put.delta == pytest.approx(math.exp(-q * T), abs=1e-12)

    def test_gamma_call_equals_gamma_put(self):
        S, K, T, vol, r, q = 100.0, 100.0, 0.25, 0.20, 0.05, 0.02
        call = black_scholes_greeks(S, K, T, vol, r, q, is_call=True)
        put = black_scholes_greeks(S, K, T, vol, r, q, is_call=False)
        assert call.gamma == pytest.approx(put.gamma, abs=1e-12)

    def test_vega_call_equals_vega_put(self):
        S, K, T, vol, r, q = 100.0, 100.0, 0.25, 0.20, 0.05, 0.02
        call = black_scholes_greeks(S, K, T, vol, r, q, is_call=True)
        put = black_scholes_greeks(S, K, T, vol, r, q, is_call=False)
        assert call.vega == pytest.approx(put.vega, abs=1e-12)


class TestSelfConsistency:
    """BS price → IV solver → output IV must round-trip cleanly."""

    @pytest.mark.parametrize(
        "S,K,T,sigma,r,is_call",
        [
            (100.0, 100.0, 0.25, 0.20, 0.05, True),  # ATM call, 3 mo
            (100.0, 100.0, 0.25, 0.20, 0.05, False),  # ATM put
            (100.0, 110.0, 0.5, 0.30, 0.03, True),  # OTM call, 6 mo
            (100.0, 90.0, 0.5, 0.30, 0.03, False),  # OTM put
            (100.0, 99.0, 1.0 / 365.0, 0.40, 0.05, True),  # 1-day slightly-ITM call
        ],
    )
    def test_iv_round_trip(self, S, K, T, sigma, r, is_call):
        price = _bs_price(S, K, T, sigma, r, 0.0, is_call)
        result = implied_volatility(
            option_price=price, spot=S, strike=K, ttm=T, rate=r, dividend=0.0, is_call=is_call
        )
        assert result.iv is not None, f"solver returned status={result.status}"
        # QuantLib's serial-day arithmetic rounds ttm to whole days, which
        # introduces up to ~0.5/365 error in T and a corresponding error in
        # the recovered IV. The Brent fallback (sub-day TTM) is tighter.
        assert result.iv == pytest.approx(sigma, abs=1e-3)


class TestSubDayTtm:
    """The 0DTE bug: TTM well below 1 day must solve, not return EXPIRED."""

    def test_two_hour_ttm_resolves(self):
        S, K, sigma, r = 710.0, 709.0, 0.18, 0.05
        T = 2.0 / (365.0 * 24.0)
        price = _bs_price(S, K, T, sigma, r, 0.0, is_call=True)
        result = implied_volatility(
            option_price=price, spot=S, strike=K, ttm=T, rate=r, dividend=0.0, is_call=True
        )
        assert result.iv is not None
        assert result.iv == pytest.approx(sigma, abs=1e-4)

    def test_thirty_minute_ttm_resolves(self):
        S, K, sigma, r = 710.0, 709.0, 0.18, 0.05
        T = 30.0 / (365.0 * 24.0 * 60.0)  # 30 min
        price = _bs_price(S, K, T, sigma, r, 0.0, is_call=True)
        result = implied_volatility(
            option_price=price, spot=S, strike=K, ttm=T, rate=r, dividend=0.0, is_call=True
        )
        # Old behaviour returned status="expired" with iv=None for any
        # ttm < 1/365 yr; assert we now solve and recover the input vol
        # (otherwise convergence_failure / input_error would silently slip
        # through a status-only check).
        assert result.iv is not None, f"solver returned status={result.status}"
        assert result.iv == pytest.approx(sigma, abs=1e-4)


class TestInputGuards:
    @pytest.mark.parametrize(
        "spot,strike,ttm,vol",
        [
            (0.0, 100.0, 0.25, 0.20),
            (100.0, 0.0, 0.25, 0.20),
            (100.0, 100.0, 0.0, 0.20),
            (100.0, 100.0, 0.25, 0.0),
        ],
    )
    def test_non_positive_input_raises(self, spot, strike, ttm, vol):
        with pytest.raises(ValueError):
            black_scholes_greeks(spot, strike, ttm, vol, 0.05, 0.0, True)


class TestThetaScaling:
    """Theta is documented as per-calendar-day (annual / 365)."""

    def test_theta_is_annual_divided_by_365(self):
        # Pin scaling: compute the annualized theta by hand from the same
        # closed-form recursion the function uses, then compare.
        S, K, T, vol, r, q = 100.0, 100.0, 0.25, 0.20, 0.05, 0.0
        sqrt_t = math.sqrt(T)
        d1 = (math.log(S / K) + (r - q + 0.5 * vol * vol) * T) / (vol * sqrt_t)
        d2 = d1 - vol * sqrt_t
        theta_annual = (
            -S * math.exp(-q * T) * float(norm.pdf(d1)) * vol / (2.0 * sqrt_t)
            - r * K * math.exp(-r * T) * float(norm.cdf(d2))
        )

        greeks = black_scholes_greeks(S, K, T, vol, r, q, is_call=True)
        # API returns theta per calendar day; reconstruct the annual via × 365.
        assert greeks.theta * 365.0 == pytest.approx(theta_annual, rel=1e-12)


class TestDividendYield:
    """Nonzero dividend yield exercises the disc_q discount path. With
    q > 0, call delta is multiplied by exp(-qT) so it must be strictly
    less than the q=0 case at the same spot/strike/vol."""

    def test_call_delta_decreases_with_dividend(self):
        S, K, T, vol, r = 100.0, 100.0, 1.0, 0.20, 0.05
        no_div = black_scholes_greeks(S, K, T, vol, r, 0.0, is_call=True)
        with_div = black_scholes_greeks(S, K, T, vol, r, 0.05, is_call=True)
        # exp(-0.05) ≈ 0.9512, so delta should drop by roughly that factor —
        # not exact because d1 also shifts, but the inequality is strict.
        assert with_div.delta < no_div.delta

    def test_put_call_parity_holds_with_nonzero_dividend(self):
        S, K, T, vol, r, q = 100.0, 100.0, 0.5, 0.25, 0.04, 0.03
        call = black_scholes_greeks(S, K, T, vol, r, q, is_call=True)
        put = black_scholes_greeks(S, K, T, vol, r, q, is_call=False)
        # Same delta-parity as the q=0 test — must hold for any q.
        assert call.delta - put.delta == pytest.approx(math.exp(-q * T), abs=1e-12)
        # Gamma and vega are call/put symmetric independent of q.
        assert call.gamma == pytest.approx(put.gamma, abs=1e-12)
        assert call.vega == pytest.approx(put.vega, abs=1e-12)
