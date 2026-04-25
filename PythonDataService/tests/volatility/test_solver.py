"""
Tests for the QuantLib IV solver.
"""

from __future__ import annotations

import math

import pytest
from scipy.stats import norm

from app.volatility.solver import (
    SolveStatus,
    implied_volatility,
    solve_iv_chain,
)


def bs_price(
    spot: float,
    strike: float,
    ttm: float,
    rate: float,
    vol: float,
    is_call: bool,
) -> float:
    """Reference Black-Scholes price for test data generation."""
    import math

    d1 = (math.log(spot / strike) + (rate + 0.5 * vol**2) * ttm) / (vol * math.sqrt(ttm))
    d2 = d1 - vol * math.sqrt(ttm)
    df = math.exp(-rate * ttm)
    if is_call:
        return spot * norm.cdf(d1) - strike * df * norm.cdf(d2)
    return strike * df * norm.cdf(-d2) - spot * norm.cdf(-d1)


class TestImpliedVolatility:
    """Tests for the single-contract IV solver."""

    def test_atm_call_recovers_vol(self, spot: float, rate: float) -> None:
        """ATM call priced at 25% vol should recover ~25% IV."""
        vol = 0.25
        ttm = 0.5
        strike = spot
        price = bs_price(spot, strike, ttm, rate, vol, is_call=True)

        result = implied_volatility(price, spot, strike, ttm, rate)

        assert result.status in (SolveStatus.NEWTON_OK, SolveStatus.QUANTLIB_OK, SolveStatus.BRENT_FALLBACK)
        assert result.iv is not None
        # QuantLib accuracy is on price, not vol; ~0.1% vol tolerance is realistic
        assert abs(result.iv - vol) < 0.005

    def test_atm_put_recovers_vol(self, spot: float, rate: float) -> None:
        """ATM put priced at 25% vol should recover ~25% IV."""
        vol = 0.25
        ttm = 0.5
        strike = spot
        price = bs_price(spot, strike, ttm, rate, vol, is_call=False)

        result = implied_volatility(price, spot, strike, ttm, rate, is_call=False)

        assert result.iv is not None
        assert abs(result.iv - vol) < 0.005

    @pytest.mark.parametrize("vol", [0.05, 0.15, 0.30, 0.60, 1.0, 2.0])
    def test_range_of_vols(self, spot: float, rate: float, vol: float) -> None:
        """IV solver should handle a wide range of input vols."""
        ttm = 0.25
        strike = spot * 1.05
        price = bs_price(spot, strike, ttm, rate, vol, is_call=True)

        result = implied_volatility(price, spot, strike, ttm, rate)

        assert result.iv is not None
        # Tolerance scales with vol level: 0.5% relative or 0.005 absolute
        tol = max(vol * 0.005, 0.005)
        assert abs(result.iv - vol) < tol, f"Expected {vol}, got {result.iv}"

    @pytest.mark.parametrize("moneyness", [0.70, 0.80, 0.90, 1.00, 1.10, 1.20, 1.30])
    def test_range_of_strikes(self, spot: float, rate: float, moneyness: float) -> None:
        """IV solver should handle ITM through OTM options."""
        vol = 0.30
        ttm = 0.5
        strike = spot * moneyness
        is_call = moneyness >= 1.0
        price = bs_price(spot, strike, ttm, rate, vol, is_call=is_call)

        if price < 0.001:
            pytest.skip("Price too low for reliable recovery")

        result = implied_volatility(price, spot, strike, ttm, rate, is_call=is_call)

        assert result.iv is not None
        assert abs(result.iv - vol) < 1e-3

    def test_expired_option_returns_expired(self, spot: float) -> None:
        """TTM below MIN_TIME_TO_EXPIRY (1 minute) should return EXPIRED.

        The solver's floor was lowered from 1 calendar day to 1 minute so
        the data-lab options companion can solve 0DTE intraday IVs (see
        ``MIN_TIME_TO_EXPIRY`` rationale). This test pins the new floor.
        """
        sub_minute_ttm = 1e-7  # ~3 seconds, well below 1/(365*24*60)
        result = implied_volatility(5.0, spot, spot, ttm=sub_minute_ttm, rate=0.05)
        assert result.status == SolveStatus.EXPIRED
        assert result.iv is None

    def test_zero_price_returns_price_too_low(self, spot: float) -> None:
        """Zero price should return PRICE_TOO_LOW."""
        result = implied_volatility(0.0, spot, spot, ttm=0.5, rate=0.05)
        assert result.status == SolveStatus.PRICE_TOO_LOW

    def test_negative_spot_returns_input_error(self) -> None:
        """Negative spot should return INPUT_ERROR."""
        result = implied_volatility(5.0, -100, 100, ttm=0.5, rate=0.05)
        assert result.status == SolveStatus.INPUT_ERROR

    def test_intrinsic_violation(self, spot: float, rate: float) -> None:
        """Price below intrinsic should return INTRINSIC_VIOLATION."""
        strike = 80.0  # deep ITM call
        ttm = 0.5
        intrinsic = max(spot - strike * math.exp(-rate * ttm), 0)
        price = intrinsic * 0.5  # below intrinsic

        result = implied_volatility(price, spot, strike, ttm, rate, is_call=True)
        assert result.status == SolveStatus.INTRINSIC_VIOLATION

    def test_deterministic(self, spot: float, rate: float) -> None:
        """Same inputs must produce identical outputs."""
        vol = 0.25
        ttm = 0.5
        price = bs_price(spot, spot, ttm, rate, vol, is_call=True)

        results = [implied_volatility(price, spot, spot, ttm, rate) for _ in range(10)]
        ivs = [r.iv for r in results]
        assert len(set(ivs)) == 1, f"Non-deterministic: {ivs}"


class TestSolveIvChain:
    """Tests for the batch IV solver."""

    def test_batch_solve(self, spot: float, rate: float) -> None:
        """Batch solver should process multiple records."""
        vol = 0.25
        ttm = 0.5
        records = [
            {
                "strike": spot * m,
                "ttm": ttm,
                "option_price": bs_price(spot, spot * m, ttm, rate, vol, m >= 1.0),
                "is_call": m >= 1.0,
            }
            for m in [0.90, 0.95, 1.00, 1.05, 1.10]
        ]

        results = solve_iv_chain(records, spot, rate)

        assert len(results) == 5
        for r in results:
            assert r["iv"] is not None
            assert r["iv_status"] in ("newton_ok", "quantlib_ok", "brent_fallback")
            assert abs(r["iv"] - vol) < 1e-3
