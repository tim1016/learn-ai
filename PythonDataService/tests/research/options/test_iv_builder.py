"""Tests for IV builder — constant-maturity interpolation and quality filters."""
from __future__ import annotations

import math

import pytest

from app.research.options.iv_builder import (
    _interpolate_iv,
    _normalize_iv_fallback,
    _get_option_price,
    MIN_OPTION_PRICE,
    MIN_IV,
    MAX_IV,
    TARGET_DTE,
)


class TestInterpolateIv:
    """Test 30-day constant-maturity variance-time interpolation."""

    def test_same_dte_averages(self):
        """When both brackets have same DTE, average IVs."""
        iv = _interpolate_iv(iv_low=0.20, dte_low=30, iv_high=0.30, dte_high=30)
        assert abs(iv - 0.25) < 1e-10

    def test_variance_time_formula(self):
        """Verify the variance-time interpolation formula directly."""
        iv_low, dte_low = 0.20, 25
        iv_high, dte_high = 0.30, 35

        t_low = dte_low / 365
        t_high = dte_high / 365
        t_target = TARGET_DTE / 365

        w_low = (dte_high - TARGET_DTE) / (dte_high - dte_low)
        w_high = (TARGET_DTE - dte_low) / (dte_high - dte_low)

        total_var = w_low * iv_low**2 * t_low + w_high * iv_high**2 * t_high
        expected = math.sqrt(total_var / t_target)

        iv = _interpolate_iv(iv_low=iv_low, dte_low=dte_low, iv_high=iv_high, dte_high=dte_high)
        assert abs(iv - expected) < 1e-10

    def test_not_simple_linear(self):
        """Variance-time result differs from simple linear interpolation."""
        iv_low, dte_low = 0.20, 25
        iv_high, dte_high = 0.30, 35

        iv = _interpolate_iv(iv_low=iv_low, dte_low=dte_low, iv_high=iv_high, dte_high=dte_high)
        linear = 0.5 * iv_low + 0.5 * iv_high  # simple linear = 0.25
        assert iv != pytest.approx(linear, abs=1e-6)

    def test_exact_30_dte_low_bracket(self):
        """When low bracket is exactly 30 DTE, its variance dominates."""
        iv = _interpolate_iv(iv_low=0.20, dte_low=30, iv_high=0.30, dte_high=45)
        # w_low = (45-30)/(45-30) = 1.0, w_high = 0.0
        # total_var = 1.0 * 0.04 * (30/365)
        # result = sqrt(total_var / (30/365)) = 0.20
        assert abs(iv - 0.20) < 1e-10

    def test_weight_favors_closer_bracket(self):
        """When DTE is closer to low bracket, result leans toward iv_low."""
        iv = _interpolate_iv(iv_low=0.20, dte_low=28, iv_high=0.30, dte_high=35)
        # Closer to 28 than 35, so result should be closer to 0.20
        assert iv < 0.25

    def test_result_positive(self):
        """Interpolated IV should always be positive."""
        iv = _interpolate_iv(iv_low=0.10, dte_low=20, iv_high=0.50, dte_high=45)
        assert iv > 0

    def test_result_bounded_by_inputs(self):
        """Interpolated IV should be between the two bracket IVs."""
        iv = _interpolate_iv(iv_low=0.15, dte_low=20, iv_high=0.35, dte_high=45)
        assert 0.15 <= iv <= 0.35


class TestNormalizeIvFallback:
    """Fallback removed — should always return None."""

    def test_always_returns_none(self):
        assert _normalize_iv_fallback(0.25, dte=30) is None

    def test_returns_none_for_any_dte(self):
        assert _normalize_iv_fallback(0.20, dte=15) is None
        assert _normalize_iv_fallback(0.30, dte=60) is None

    def test_returns_none_for_zero_dte(self):
        assert _normalize_iv_fallback(0.25, dte=0) is None


class TestGetOptionPrice:
    """Test strict price hierarchy: mid (tight spread) → VWAP → close (in range) → reject."""

    def test_prefers_mid_tight_spread(self):
        bar = {"bid": 1.50, "ask": 1.60, "close": 1.55, "volume": 100}
        price, source = _get_option_price(bar)
        assert abs(price - 1.55) < 1e-10
        assert source == "mid"

    def test_rejects_wide_spread_mid(self):
        """Wide spread (>15%) should skip mid, fall to VWAP or close."""
        bar = {"bid": 0.50, "ask": 1.50, "vw": 0.90, "volume": 100}
        price, source = _get_option_price(bar)
        assert price == 0.90
        assert source == "vwap"

    def test_vwap_tier(self):
        """VWAP used when no valid bid/ask."""
        bar = {"vw": 2.50, "close": 2.00, "volume": 100}
        price, source = _get_option_price(bar)
        assert price == 2.50
        assert source == "vwap"

    def test_close_within_bidask_range(self):
        """Close accepted if within bid-ask range and wide spread skipped mid."""
        bar = {"bid": 0.50, "ask": 1.50, "close": 1.00, "volume": 100}
        price, source = _get_option_price(bar)
        assert price == 1.00
        assert source == "close_filtered"

    def test_close_outside_bidask_rejected(self):
        """Close outside bid-ask range is rejected."""
        bar = {"bid": 1.00, "ask": 1.20, "close": 2.00, "volume": 100}
        # Spread is tight so mid is used (spread/mid = 0.2/1.1 ≈ 18% > 15%)
        # Actually: spread = 0.20, mid = 1.10, ratio = 0.182 > 0.15 → skip mid
        # No VWAP → close = 2.00 outside [1.00, 1.20] → rejected
        price, source = _get_option_price(bar)
        assert price is None
        assert source == "rejected"

    def test_falls_back_to_close_no_bidask(self):
        bar = {"close": 2.00, "volume": 100}
        price, source = _get_option_price(bar)
        assert price == 2.00
        assert source == "close_filtered"

    def test_rejects_low_volume_close(self):
        bar = {"close": 2.00, "volume": 10}
        price, source = _get_option_price(bar)
        assert price is None
        assert source == "rejected"

    def test_rejects_below_min_price(self):
        bar = {"close": 0.01, "volume": 200}
        price, source = _get_option_price(bar)
        assert price is None

    def test_rejects_zero_bid(self):
        """If bid is 0, should fall to VWAP or close."""
        bar = {"bid": 0, "ask": 1.0, "close": 0.50, "volume": 100}
        price, source = _get_option_price(bar)
        assert price == 0.50
        assert source == "close_filtered"

    def test_uses_c_field_alias(self):
        """Support 'c' as alias for 'close'."""
        bar = {"c": 3.00, "v": 200}
        price, source = _get_option_price(bar)
        assert price == 3.00


class TestQualityFilters:
    """Test IV quality filter boundaries."""

    def test_min_iv_boundary(self):
        assert MIN_IV == 0.05

    def test_max_iv_boundary(self):
        assert MAX_IV == 3.0

    def test_min_option_price(self):
        assert MIN_OPTION_PRICE == 0.05
