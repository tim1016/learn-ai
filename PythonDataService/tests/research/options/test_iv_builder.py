"""Tests for IV builder — constant-maturity interpolation and quality filters."""
from __future__ import annotations

import pytest

from app.research.options.iv_builder import (
    _interpolate_iv,
    _normalize_iv_fallback,
    _get_option_price,
    MIN_OPTION_PRICE,
    MIN_IV,
    MAX_IV,
)


class TestInterpolateIv:
    """Test 30-day constant-maturity interpolation formula."""

    def test_equal_weight_at_midpoint(self):
        """When DTE brackets are symmetric around 30, weight equally."""
        iv = _interpolate_iv(iv_low=0.20, dte_low=25, iv_high=0.30, dte_high=35)
        assert abs(iv - 0.25) < 1e-10

    def test_weight_favors_closer_bracket(self):
        """When trade date is closer to low-DTE, weight should favor low."""
        iv = _interpolate_iv(iv_low=0.20, dte_low=28, iv_high=0.30, dte_high=35)
        # weight_low = (35-30)/(35-28) = 5/7 ≈ 0.714
        # weight_high = (30-28)/(35-28) = 2/7 ≈ 0.286
        expected = (5/7) * 0.20 + (2/7) * 0.30
        assert abs(iv - expected) < 1e-10

    def test_exact_30_dte_returns_that_iv(self):
        """When one bracket is exactly 30 DTE, return its IV."""
        iv = _interpolate_iv(iv_low=0.20, dte_low=30, iv_high=0.30, dte_high=45)
        assert abs(iv - 0.20) < 1e-10

    def test_same_dte_averages(self):
        """When both brackets have same DTE, average IVs."""
        iv = _interpolate_iv(iv_low=0.20, dte_low=30, iv_high=0.30, dte_high=30)
        assert abs(iv - 0.25) < 1e-10

    def test_result_bounded_by_inputs(self):
        """Interpolated IV should be between the two bracket IVs."""
        iv = _interpolate_iv(iv_low=0.15, dte_low=20, iv_high=0.35, dte_high=45)
        assert 0.15 <= iv <= 0.35


class TestNormalizeIvFallback:
    """Test DTE normalization fallback when only one bracket available."""

    def test_30_dte_unchanged(self):
        """At DTE=30, normalization should return the same IV."""
        iv = _normalize_iv_fallback(0.25, dte=30)
        assert abs(iv - 0.25) < 1e-10

    def test_lower_dte_increases_iv(self):
        """Lower DTE should result in higher normalized IV (shorter period)."""
        iv = _normalize_iv_fallback(0.20, dte=15)
        # sqrt(30/15) = sqrt(2) ≈ 1.414
        import math
        expected = 0.20 * math.sqrt(30 / 15)
        assert abs(iv - expected) < 1e-10
        assert iv > 0.20

    def test_higher_dte_decreases_iv(self):
        """Higher DTE should result in lower normalized IV."""
        iv = _normalize_iv_fallback(0.30, dte=60)
        assert iv < 0.30

    def test_zero_dte_returns_original(self):
        """DTE=0 should return original IV (guard clause)."""
        iv = _normalize_iv_fallback(0.25, dte=0)
        assert iv == 0.25


class TestGetOptionPrice:
    """Test option price extraction with priority logic."""

    def test_prefers_mid_when_available(self):
        bar = {"bid": 1.50, "ask": 1.60, "close": 1.55, "volume": 100}
        price, source = _get_option_price(bar)
        assert abs(price - 1.55) < 1e-10
        assert source == "mid"

    def test_falls_back_to_close(self):
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
        """If bid is 0, should fall back to close."""
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
