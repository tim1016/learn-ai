"""Tests for FRED Treasury rate service."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from app.services.fred_service import (
    _interpolate_rate,
    _parse_latest_rate,
    get_risk_free_rate,
    clear_cache,
    FALLBACK_RATE,
    TENOR_MAP,
)


class TestInterpolateRate:
    """Test linear interpolation between Treasury tenors."""

    def test_exact_match(self):
        rates = {28: 0.04, 91: 0.045, 182: 0.047, 365: 0.05}
        assert _interpolate_rate(rates, 91) == 0.045

    def test_interpolation_between_tenors(self):
        rates = {28: 0.04, 91: 0.045}
        # Midpoint: (28+91)/2 ≈ 59.5
        result = _interpolate_rate(rates, 60)
        # weight = (60-28)/(91-28) = 32/63 ≈ 0.508
        expected = 0.04 + (32 / 63) * (0.045 - 0.04)
        assert abs(result - expected) < 1e-10

    def test_below_shortest_tenor(self):
        rates = {28: 0.04, 91: 0.045}
        assert _interpolate_rate(rates, 7) == 0.04

    def test_above_longest_tenor(self):
        rates = {28: 0.04, 91: 0.045, 365: 0.05}
        assert _interpolate_rate(rates, 500) == 0.05

    def test_empty_rates_returns_fallback(self):
        assert _interpolate_rate({}, 30) == FALLBACK_RATE

    def test_30_day_interpolation(self):
        """Typical use case: interpolate to ~30 DTE."""
        rates = {28: 0.042, 91: 0.045}
        result = _interpolate_rate(rates, 30)
        # 30 is just above 28, should be very close to 0.042
        assert 0.042 <= result <= 0.045


class TestParseLatestRate:
    """Test parsing FRED observation values."""

    def test_valid_rate(self):
        obs = [{"value": "4.30"}]
        assert _parse_latest_rate(obs) == pytest.approx(0.043)

    def test_skips_missing_values(self):
        obs = [{"value": "."}, {"value": "4.50"}]
        assert _parse_latest_rate(obs) == pytest.approx(0.045)

    def test_all_missing(self):
        obs = [{"value": "."}, {"value": "."}]
        assert _parse_latest_rate(obs) is None

    def test_empty_list(self):
        assert _parse_latest_rate([]) is None


class TestGetRiskFreeRate:
    """Test the main get_risk_free_rate function with mocked FRED calls."""

    def setup_method(self):
        clear_cache()

    @patch("app.services.fred_service._fetch_series")
    def test_fetches_and_caches(self, mock_fetch):
        mock_fetch.return_value = [{"value": "5.10"}]

        rate = get_risk_free_rate(dte_days=30, observation_date="2025-01-15")
        assert rate > 0
        assert rate == pytest.approx(0.051)  # 5.10% → 0.051

        # Verify all 4 tenor series were fetched
        assert mock_fetch.call_count == len(TENOR_MAP)

    @patch("app.services.fred_service._fetch_series")
    def test_fallback_on_failure(self, mock_fetch):
        mock_fetch.return_value = []

        rate = get_risk_free_rate(dte_days=30, observation_date="2025-01-15")
        assert rate == FALLBACK_RATE

    @patch("app.services.fred_service._fetch_series")
    def test_cache_prevents_refetch(self, mock_fetch):
        mock_fetch.return_value = [{"value": "4.30"}]

        get_risk_free_rate(dte_days=30, observation_date="2025-01-15")
        initial_count = mock_fetch.call_count

        get_risk_free_rate(dte_days=30, observation_date="2025-01-15")
        assert mock_fetch.call_count == initial_count  # No additional calls
