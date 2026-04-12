"""
Tests for app.volatility.conventions module.

Tests SurfaceConventions dataclass and conversion functions.
"""

from __future__ import annotations

import math

import pytest

from app.volatility.conventions import SurfaceConventions, dte_to_ttm, ttm_to_dte


class TestForwardPricing:
    """BSM forward pricing tests."""

    def test_forward_bsm(self, spot: float, rate: float) -> None:
        """Forward = S * exp((r - q) * T) with known values."""
        conventions = SurfaceConventions(rate=rate, dividend_yield=0.0)
        ttm = 1.0
        forward = conventions.forward(spot, ttm)

        expected = spot * math.exp(rate * ttm)
        assert abs(forward - expected) < 1e-10

    def test_forward_zero_dividend(self, spot: float, rate: float) -> None:
        """Forward = S * exp(r*T) when q=0."""
        conventions = SurfaceConventions(rate=rate, dividend_yield=0.0)
        ttm = 0.5

        forward = conventions.forward(spot, ttm)
        expected = spot * math.exp(rate * ttm)

        assert abs(forward - expected) < 1e-10

    def test_forward_with_dividend(self, spot: float, rate: float) -> None:
        """Forward = S * exp((r - q) * T) with dividend yield."""
        dividend = 0.02
        conventions = SurfaceConventions(rate=rate, dividend_yield=dividend)
        ttm = 0.25

        forward = conventions.forward(spot, ttm)
        expected = spot * math.exp((rate - dividend) * ttm)

        assert abs(forward - expected) < 1e-10

    def test_forward_zero_ttm(self, spot: float, rate: float) -> None:
        """Forward at T=0 equals spot."""
        conventions = SurfaceConventions(rate=rate, dividend_yield=0.0)
        forward = conventions.forward(spot, 0.0)

        assert abs(forward - spot) < 1e-10


class TestDiscountFactor:
    """Discount factor tests."""

    def test_discount_factor(self, rate: float) -> None:
        """df = exp(-r*T)."""
        conventions = SurfaceConventions(rate=rate)
        ttm = 1.0

        df = conventions.discount_factor(ttm)
        expected = math.exp(-rate * ttm)

        assert abs(df - expected) < 1e-10

    def test_discount_factor_zero_ttm(self, rate: float) -> None:
        """df at T=0 equals 1.0."""
        conventions = SurfaceConventions(rate=rate)
        df = conventions.discount_factor(0.0)

        assert abs(df - 1.0) < 1e-10

    def test_discount_factor_decreasing(self, rate: float) -> None:
        """df is decreasing in T."""
        conventions = SurfaceConventions(rate=rate)

        df_1 = conventions.discount_factor(0.5)
        df_2 = conventions.discount_factor(1.0)

        assert df_1 > df_2


class TestSurfaceConventionsDataclass:
    """Frozen dataclass tests."""

    def test_to_hash_dict_complete(self) -> None:
        """to_hash_dict returns all fields."""
        conventions = SurfaceConventions(
            day_count="Actual365Fixed",
            forward_model="bsm",
            discount_model="continuous",
            rate=0.05,
            dividend_yield=0.02,
            calendar="NullCalendar",
        )

        hash_dict = conventions.to_hash_dict()

        assert hash_dict["day_count"] == "Actual365Fixed"
        assert hash_dict["forward_model"] == "bsm"
        assert hash_dict["discount_model"] == "continuous"
        assert hash_dict["rate"] == 0.05
        assert hash_dict["dividend_yield"] == 0.02
        assert hash_dict["calendar"] == "NullCalendar"

    def test_to_hash_dict_is_dict(self) -> None:
        """to_hash_dict returns a dict (not frozen)."""
        conventions = SurfaceConventions()
        hash_dict = conventions.to_hash_dict()

        assert isinstance(hash_dict, dict)

    def test_frozen_dataclass(self) -> None:
        """Frozen dataclass cannot mutate attributes."""
        conventions = SurfaceConventions(rate=0.05)

        with pytest.raises(AttributeError):
            conventions.rate = 0.06  # type: ignore


class TestDayCountConversions:
    """Day count convention conversion tests."""

    def test_dte_to_ttm_actual365(self) -> None:
        """365 days = 1.0 year under Actual365Fixed."""
        ttm = dte_to_ttm(365, day_count="Actual365Fixed")
        assert abs(ttm - 1.0) < 1e-10

    def test_dte_to_ttm_actual360(self) -> None:
        """360 days = 1.0 year under Actual360."""
        ttm = dte_to_ttm(360, day_count="Actual360")
        assert abs(ttm - 1.0) < 1e-10

    def test_dte_to_ttm_actual_actual(self) -> None:
        """365.25 days = 1.0 year under ActualActual."""
        ttm = dte_to_ttm(int(365.25), day_count="ActualActual")
        assert abs(ttm - 1.0) < 0.001

    def test_dte_to_ttm_fractional(self) -> None:
        """30 days ≈ 0.082 years under Actual365Fixed."""
        ttm = dte_to_ttm(30, day_count="Actual365Fixed")
        expected = 30 / 365.0
        assert abs(ttm - expected) < 1e-10

    def test_ttm_to_dte_roundtrip_actual365(self) -> None:
        """dte_to_ttm(ttm_to_dte(x)) ≈ x under Actual365Fixed."""
        original_ttm = 0.25
        dte = ttm_to_dte(original_ttm, day_count="Actual365Fixed")
        recovered_ttm = dte_to_ttm(dte, day_count="Actual365Fixed")

        assert abs(original_ttm - recovered_ttm) < 0.001  # Rounding tolerance

    def test_ttm_to_dte_roundtrip_actual360(self) -> None:
        """dte_to_ttm(ttm_to_dte(x)) ≈ x under Actual360."""
        original_ttm = 0.5
        dte = ttm_to_dte(original_ttm, day_count="Actual360")
        recovered_ttm = dte_to_ttm(dte, day_count="Actual360")

        assert abs(original_ttm - recovered_ttm) < 1e-10

    def test_ttm_to_dte_actual365(self) -> None:
        """1.0 year = 365 days under Actual365Fixed."""
        dte = ttm_to_dte(1.0, day_count="Actual365Fixed")
        assert dte == 365

    def test_ttm_to_dte_actual360(self) -> None:
        """1.0 year = 360 days under Actual360."""
        dte = ttm_to_dte(1.0, day_count="Actual360")
        assert dte == 360

    def test_ttm_to_dte_rounding(self) -> None:
        """ttm_to_dte rounds to nearest integer."""
        dte = ttm_to_dte(0.1001, day_count="Actual365Fixed")
        assert dte == 37  # round(36.5365) = 37

    def test_dte_to_ttm_zero_days(self) -> None:
        """0 days = 0.0 years."""
        ttm = dte_to_ttm(0, day_count="Actual365Fixed")
        assert ttm == 0.0


class TestUnsupportedDayCount:
    """Error handling for unsupported day count conventions."""

    def test_dte_to_ttm_unsupported_raises(self) -> None:
        """Unsupported day_count raises ValueError."""
        with pytest.raises(ValueError, match="Unsupported day_count"):
            dte_to_ttm(30, day_count="InvalidConvention")

    def test_ttm_to_dte_unsupported_raises(self) -> None:
        """Unsupported day_count raises ValueError."""
        with pytest.raises(ValueError, match="Unsupported day_count"):
            ttm_to_dte(0.1, day_count="InvalidConvention")

    def test_forward_unsupported_model_raises(self) -> None:
        """Unsupported forward_model raises NotImplementedError."""
        conventions = SurfaceConventions(forward_model="unsupported_model")

        with pytest.raises(NotImplementedError):
            conventions.forward(100.0, 1.0)

    def test_discount_unsupported_model_raises(self) -> None:
        """Unsupported discount_model raises NotImplementedError."""
        conventions = SurfaceConventions(discount_model="unsupported_model")

        with pytest.raises(NotImplementedError):
            conventions.discount_factor(1.0)
