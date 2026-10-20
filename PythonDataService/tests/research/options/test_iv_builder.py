"""Tests for IV builder — constant-maturity interpolation and quality filters."""

from __future__ import annotations

import math

from app.research.options.iv_builder import (
    TARGET_DTE,
    _get_option_price,
    _interpolate_iv,
    _normalize_iv_fallback,
)


class TestInterpolateIv:
    """Test 30-day constant-maturity variance-time interpolation."""

    def test_interpolate_iv(self):
        # When both brackets have same DTE, average IVs.
        iv = _interpolate_iv(iv_low=0.20, dte_low=30, iv_high=0.30, dte_high=30)
        assert abs(iv - 0.25) < 1e-10, "same DTE averages the IVs"

        # Verify the variance-time interpolation formula directly.
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
        assert abs(iv - expected) < 1e-10, "variance-time interpolation formula"

        # When low bracket is exactly 30 DTE, its variance dominates.
        iv = _interpolate_iv(iv_low=0.20, dte_low=30, iv_high=0.30, dte_high=45)
        # w_low = (45-30)/(45-30) = 1.0, w_high = 0.0
        # total_var = 1.0 * 0.04 * (30/365)
        # result = sqrt(total_var / (30/365)) = 0.20
        assert abs(iv - 0.20) < 1e-10, "exact 30 DTE low bracket returns the low bracket IV"

        # When DTE is closer to low bracket, result leans toward iv_low.
        iv = _interpolate_iv(iv_low=0.20, dte_low=28, iv_high=0.30, dte_high=35)
        # Closer to 28 than 35, so result should be closer to 0.20
        assert iv < 0.25, "weight favors the closer bracket"

        # Interpolated IV should be between the two bracket IVs.
        iv = _interpolate_iv(iv_low=0.15, dte_low=20, iv_high=0.35, dte_high=45)
        assert 0.15 <= iv <= 0.35, "result is bounded by the bracket IVs"


class TestNormalizeIvFallback:
    """Fallback removed — should always return None."""

    def test_always_returns_none(self):
        assert _normalize_iv_fallback(0.25, dte=30) is None


class TestGetOptionPrice:
    """Test strict price hierarchy: mid (tight spread) → VWAP → close (in range) → reject."""

    def test_price_hierarchy_with_bid_ask(self):
        bar = {"bid": 1.50, "ask": 1.60, "close": 1.55, "volume": 100}
        price, source = _get_option_price(bar)
        assert abs(price - 1.55) < 1e-10, "prefers mid price on a tight spread"
        assert source == "mid", "prefers mid source on a tight spread"

        # Wide spread (>15%) should skip mid, fall to VWAP or close.
        bar = {"bid": 0.50, "ask": 1.50, "vw": 0.90, "volume": 100}
        price, source = _get_option_price(bar)
        assert price == 0.90, "wide spread mid is rejected and VWAP price is used"
        assert source == "vwap", "wide spread mid is rejected and VWAP source is used"

        # Close accepted if within bid-ask range and wide spread skipped mid.
        bar = {"bid": 0.50, "ask": 1.50, "close": 1.00, "volume": 100}
        price, source = _get_option_price(bar)
        assert price == 1.00, "close within the bid-ask range is accepted"
        assert source == "close_filtered", "close within the bid-ask range reports close_filtered"

        # Close outside bid-ask range is rejected.
        bar = {"bid": 1.00, "ask": 1.20, "close": 2.00, "volume": 100}
        # Spread is tight so mid is used (spread/mid = 0.2/1.1 ≈ 18% > 15%)
        # Actually: spread = 0.20, mid = 1.10, ratio = 0.182 > 0.15 → skip mid
        # No VWAP → close = 2.00 outside [1.00, 1.20] → rejected
        price, source = _get_option_price(bar)
        assert price is None, "close outside the bid-ask range has no price"
        assert source == "rejected", "close outside the bid-ask range is rejected"

        # If bid is 0, should fall to VWAP or close.
        bar = {"bid": 0, "ask": 1.0, "close": 0.50, "volume": 100}
        price, source = _get_option_price(bar)
        assert price == 0.50, "zero bid falls to the close price"
        assert source == "close_filtered", "zero bid falls to close_filtered"

    def test_price_hierarchy_without_bid_ask(self):
        # VWAP used when no valid bid/ask.
        bar = {"vw": 2.50, "close": 2.00, "volume": 100}
        price, source = _get_option_price(bar)
        assert price == 2.50, "VWAP tier price is used when no valid bid/ask"
        assert source == "vwap", "VWAP tier source is used when no valid bid/ask"

        bar = {"close": 2.00, "volume": 100}
        price, source = _get_option_price(bar)
        assert price == 2.00, "falls back to close price with no bid-ask"
        assert source == "close_filtered", "falls back to close_filtered with no bid-ask"

        bar = {"close": 2.00, "volume": 10}
        price, source = _get_option_price(bar)
        assert price is None, "low volume close has no price"
        assert source == "rejected", "low volume close is rejected"

        bar = {"close": 0.01, "volume": 200}
        price, _source = _get_option_price(bar)
        assert price is None, "price below the minimum is rejected"

        # Support 'c' as alias for 'close'.
        bar = {"c": 3.00, "v": 200}
        price, _source = _get_option_price(bar)
        assert price == 3.00, "'c' field is an alias for close"
