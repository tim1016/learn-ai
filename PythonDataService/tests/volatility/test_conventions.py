"""
Tests for app.volatility.conventions module.

Tests SurfaceConventions dataclass and conversion functions.
"""

from __future__ import annotations

import math
from collections.abc import Callable

import pytest

from app.volatility.conventions import SurfaceConventions, dte_to_ttm, ttm_to_dte


class TestForwardPricing:
    """BSM forward pricing tests."""

    @pytest.mark.parametrize(
        ("dividend", "ttm"),
        [
            pytest.param(0.0, 1.0, id="forward_bsm"),
            pytest.param(0.0, 0.5, id="forward_zero_dividend"),
            pytest.param(0.02, 0.25, id="forward_with_dividend"),
            pytest.param(0.0, 0.0, id="forward_zero_ttm"),
        ],
    )
    def test_forward(self, spot: float, rate: float, dividend: float, ttm: float) -> None:
        """Forward = S * exp((r - q) * T) with known values: q=0 reduces to S * exp(r*T), and T=0 gives spot."""
        conventions = SurfaceConventions(rate=rate, dividend_yield=dividend)

        forward = conventions.forward(spot, ttm)
        expected = spot * math.exp((rate - dividend) * ttm)

        assert abs(forward - expected) < 1e-10


class TestDiscountFactor:
    """Discount factor tests."""

    def test_discount_factor(self, rate: float) -> None:
        """df = exp(-r*T), equals 1.0 at T=0, and is decreasing in T."""
        conventions = SurfaceConventions(rate=rate)
        ttm = 1.0

        df = conventions.discount_factor(ttm)
        expected = math.exp(-rate * ttm)
        df_zero_ttm = conventions.discount_factor(0.0)
        df_1 = conventions.discount_factor(0.5)
        df_2 = conventions.discount_factor(1.0)

        assert abs(df - expected) < 1e-10, "df = exp(-r*T)"
        assert abs(df_zero_ttm - 1.0) < 1e-10, "df at T=0 equals 1.0"
        assert df_1 > df_2, "df is decreasing in T"


class TestDayCountConversions:
    """Day count convention conversion tests."""

    @pytest.mark.parametrize(
        ("dte", "day_count", "expected", "tolerance"),
        [
            # 365 days = 1.0 year under Actual365Fixed
            pytest.param(365, "Actual365Fixed", 1.0, 1e-10, id="dte_to_ttm_actual365"),
            # 360 days = 1.0 year under Actual360
            pytest.param(360, "Actual360", 1.0, 1e-10, id="dte_to_ttm_actual360"),
            # 365.25 days = 1.0 year under ActualActual
            pytest.param(int(365.25), "ActualActual", 1.0, 0.001, id="dte_to_ttm_actual_actual"),
            # 30 days ≈ 0.082 years under Actual365Fixed
            pytest.param(30, "Actual365Fixed", 30 / 365.0, 1e-10, id="dte_to_ttm_fractional"),
        ],
    )
    def test_dte_to_ttm(self, dte: int, day_count: str, expected: float, tolerance: float) -> None:
        ttm = dte_to_ttm(dte, day_count=day_count)
        assert abs(ttm - expected) < tolerance

    @pytest.mark.parametrize(
        ("original_ttm", "day_count", "tolerance"),
        [
            # dte_to_ttm(ttm_to_dte(x)) ≈ x under Actual365Fixed, within the rounding tolerance
            pytest.param(0.25, "Actual365Fixed", 0.001, id="ttm_to_dte_roundtrip_actual365"),
            # dte_to_ttm(ttm_to_dte(x)) ≈ x under Actual360
            pytest.param(0.5, "Actual360", 1e-10, id="ttm_to_dte_roundtrip_actual360"),
        ],
    )
    def test_ttm_to_dte_roundtrip(self, original_ttm: float, day_count: str, tolerance: float) -> None:
        dte = ttm_to_dte(original_ttm, day_count=day_count)
        recovered_ttm = dte_to_ttm(dte, day_count=day_count)

        assert abs(original_ttm - recovered_ttm) < tolerance

    @pytest.mark.parametrize(
        ("ttm", "day_count", "expected_dte"),
        [
            # 1.0 year = 365 days under Actual365Fixed
            pytest.param(1.0, "Actual365Fixed", 365, id="ttm_to_dte_actual365"),
            # 1.0 year = 360 days under Actual360
            pytest.param(1.0, "Actual360", 360, id="ttm_to_dte_actual360"),
            # ttm_to_dte rounds to nearest integer: round(36.5365) = 37
            pytest.param(0.1001, "Actual365Fixed", 37, id="ttm_to_dte_rounding"),
        ],
    )
    def test_ttm_to_dte(self, ttm: float, day_count: str, expected_dte: int) -> None:
        dte = ttm_to_dte(ttm, day_count=day_count)
        assert dte == expected_dte

    def test_dte_to_ttm_zero_days(self) -> None:
        """0 days = 0.0 years."""
        ttm = dte_to_ttm(0, day_count="Actual365Fixed")
        assert ttm == 0.0


class TestUnsupportedDayCount:
    """Error handling for unsupported day count conventions."""

    @pytest.mark.parametrize(
        ("convert", "value"),
        [
            pytest.param(dte_to_ttm, 30, id="dte_to_ttm_unsupported_raises"),
            pytest.param(ttm_to_dte, 0.1, id="ttm_to_dte_unsupported_raises"),
        ],
    )
    def test_unsupported_day_count_raises(self, convert: Callable, value: float) -> None:
        """Unsupported day_count raises ValueError."""
        with pytest.raises(ValueError, match="Unsupported day_count"):
            convert(value, day_count="InvalidConvention")

    def test_forward_unsupported_model_raises(self, rate: float) -> None:
        """Unsupported forward_model raises NotImplementedError."""
        conventions = SurfaceConventions(rate=rate, forward_model="unsupported_model")

        with pytest.raises(NotImplementedError):
            conventions.forward(100.0, 1.0)

    def test_discount_unsupported_model_raises(self, rate: float) -> None:
        """Unsupported discount_model raises NotImplementedError."""
        conventions = SurfaceConventions(rate=rate, discount_model="unsupported_model")

        with pytest.raises(NotImplementedError):
            conventions.discount_factor(1.0)
