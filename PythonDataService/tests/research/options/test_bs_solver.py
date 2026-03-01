"""Tests for Black-Scholes IV solver."""
from __future__ import annotations

import math

import pytest

from app.research.options.bs_solver import bs_price, bs_vega, implied_volatility


class TestBsPrice:
    """Test BS pricing against known analytical values."""

    def test_atm_call_positive(self):
        """ATM call should have positive price."""
        price = bs_price(S=100, K=100, T=0.25, r=0.05, sigma=0.20, option_type="call")
        assert price > 0

    def test_atm_put_positive(self):
        """ATM put should have positive price."""
        price = bs_price(S=100, K=100, T=0.25, r=0.05, sigma=0.20, option_type="put")
        assert price > 0

    def test_put_call_parity(self):
        """C - P = S - K*exp(-rT) (put-call parity)."""
        S, K, T, r, sigma = 100, 100, 0.5, 0.05, 0.25
        call = bs_price(S, K, T, r, sigma, "call")
        put = bs_price(S, K, T, r, sigma, "put")
        parity_rhs = S - K * math.exp(-r * T)
        assert abs((call - put) - parity_rhs) < 1e-10

    def test_deep_itm_call_near_intrinsic(self):
        """Deep ITM call ~ intrinsic value."""
        price = bs_price(S=200, K=100, T=0.25, r=0.05, sigma=0.20, option_type="call")
        intrinsic = 200 - 100 * math.exp(-0.05 * 0.25)
        assert abs(price - intrinsic) < 1.0  # Within $1

    def test_deep_otm_call_near_zero(self):
        """Deep OTM call ~ 0."""
        price = bs_price(S=50, K=200, T=0.25, r=0.05, sigma=0.20, option_type="call")
        assert price < 0.01

    def test_zero_time_returns_zero(self):
        """T=0 should return 0."""
        assert bs_price(S=100, K=100, T=0, r=0.05, sigma=0.20, option_type="call") == 0.0

    def test_zero_vol_returns_zero(self):
        """sigma=0 should return 0."""
        assert bs_price(S=100, K=100, T=0.25, r=0.05, sigma=0, option_type="call") == 0.0

    def test_known_value(self):
        """Test against a known BS price (S=100, K=100, T=1, r=0.05, sigma=0.2).

        Expected call price ~ 10.4506 (standard textbook value).
        """
        price = bs_price(S=100, K=100, T=1.0, r=0.05, sigma=0.20, option_type="call")
        assert abs(price - 10.4506) < 0.01


class TestBsVega:
    """Test BS vega."""

    def test_atm_vega_positive(self):
        """ATM vega should be positive and significant."""
        v = bs_vega(S=100, K=100, T=0.25, r=0.05, sigma=0.20)
        assert v > 0

    def test_vega_peaks_at_atm(self):
        """Vega should be highest at ATM."""
        v_atm = bs_vega(S=100, K=100, T=0.25, r=0.05, sigma=0.20)
        v_itm = bs_vega(S=100, K=80, T=0.25, r=0.05, sigma=0.20)
        v_otm = bs_vega(S=100, K=120, T=0.25, r=0.05, sigma=0.20)
        assert v_atm > v_itm
        assert v_atm > v_otm

    def test_zero_time_returns_zero(self):
        assert bs_vega(S=100, K=100, T=0, r=0.05, sigma=0.20) == 0.0


class TestImpliedVolatility:
    """Test Newton-Raphson IV solver."""

    def test_atm_call_roundtrip(self):
        """Price with known sigma, then recover it via IV solver."""
        sigma = 0.25
        price = bs_price(S=100, K=100, T=0.5, r=0.05, sigma=sigma, option_type="call")
        iv = implied_volatility(price, S=100, K=100, T=0.5, r=0.05, option_type="call")
        assert iv is not None
        assert abs(iv - sigma) < 1e-6

    def test_atm_put_roundtrip(self):
        """Roundtrip for ATM put."""
        sigma = 0.30
        price = bs_price(S=100, K=100, T=0.5, r=0.05, sigma=sigma, option_type="put")
        iv = implied_volatility(price, S=100, K=100, T=0.5, r=0.05, option_type="put")
        assert iv is not None
        assert abs(iv - sigma) < 1e-6

    def test_itm_call_roundtrip(self):
        """Roundtrip for ITM call."""
        sigma = 0.35
        price = bs_price(S=110, K=100, T=0.5, r=0.05, sigma=sigma, option_type="call")
        iv = implied_volatility(price, S=110, K=100, T=0.5, r=0.05, option_type="call")
        assert iv is not None
        assert abs(iv - sigma) < 1e-4

    def test_otm_call_roundtrip(self):
        """Roundtrip for OTM call."""
        sigma = 0.40
        price = bs_price(S=90, K=100, T=0.5, r=0.05, sigma=sigma, option_type="call")
        iv = implied_volatility(price, S=90, K=100, T=0.5, r=0.05, option_type="call")
        assert iv is not None
        assert abs(iv - sigma) < 1e-4

    def test_reject_negative_price(self):
        """Negative market price should return None."""
        assert implied_volatility(-1.0, S=100, K=100, T=0.5, r=0.05, option_type="call") is None

    def test_reject_zero_price(self):
        """Zero market price should return None."""
        assert implied_volatility(0.0, S=100, K=100, T=0.5, r=0.05, option_type="call") is None

    def test_reject_low_dte(self):
        """T < 7/365 should return None (gamma distortion)."""
        price = bs_price(S=100, K=100, T=5/365, r=0.05, sigma=0.20, option_type="call")
        assert implied_volatility(price, S=100, K=100, T=5/365, r=0.05, option_type="call") is None

    def test_reject_extreme_iv(self):
        """IV outside [0.05, 3.0] should be rejected."""
        # Very low IV - price very close to intrinsic
        # Very high IV options - can't really get above 300% with reasonable params
        # Test with a price that would require IV > 3.0
        iv = implied_volatility(50.0, S=100, K=100, T=30/365, r=0.05, option_type="call")
        # This should be None because the IV would be unreasonably high
        # or it might solve but be > 3.0
        assert iv is None or iv <= 3.0

    def test_negative_spot_returns_none(self):
        assert implied_volatility(5.0, S=-100, K=100, T=0.5, r=0.05, option_type="call") is None

    def test_negative_strike_returns_none(self):
        assert implied_volatility(5.0, S=100, K=-100, T=0.5, r=0.05, option_type="call") is None

    def test_high_vol_stock(self):
        """Test solver with high-vol stock (e.g. TSLA-like: sigma=0.80)."""
        sigma = 0.80
        price = bs_price(S=250, K=250, T=30/365, r=0.043, sigma=sigma, option_type="call")
        iv = implied_volatility(price, S=250, K=250, T=30/365, r=0.043, option_type="call")
        assert iv is not None
        assert abs(iv - sigma) < 1e-4

    def test_low_vol_stock(self):
        """Test solver with low-vol stock (e.g. utility: sigma=0.12)."""
        sigma = 0.12
        price = bs_price(S=50, K=50, T=60/365, r=0.043, sigma=sigma, option_type="call")
        iv = implied_volatility(price, S=50, K=50, T=60/365, r=0.043, option_type="call")
        assert iv is not None
        assert abs(iv - sigma) < 1e-4
