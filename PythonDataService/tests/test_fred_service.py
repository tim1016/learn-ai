"""Tests for FRED Treasury rate service."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services.fred_service import (
    RATE_SOURCE_DEFAULT,
    RATE_SOURCE_FRED,
    TENOR_MAP,
    _interpolate_rate,
    _parse_latest_rate,
    clear_cache,
    get_risk_free_rate,
    get_risk_free_rate_and_source,
)
from app.services.risk_free_rate import DEFAULT_RISK_FREE_RATE


class TestInterpolateRate:
    """Test linear interpolation between Treasury tenors."""

    @pytest.mark.parametrize(
        ("rate_items", "tenor", "expected"),
        [
            pytest.param(((28, 0.04), (91, 0.045), (182, 0.047), (365, 0.05)), 91, 0.045, id="exact_match"),
            pytest.param(((28, 0.04), (91, 0.045)), 7, 0.04, id="below_shortest_tenor"),
            pytest.param(((28, 0.04), (91, 0.045), (365, 0.05)), 500, 0.05, id="above_longest_tenor"),
            pytest.param((), 30, DEFAULT_RISK_FREE_RATE, id="empty_rates_returns_fallback"),
        ],
    )
    def test_returns_exact_rate(self, rate_items, tenor, expected):
        assert _interpolate_rate(dict(rate_items), tenor) == expected

    def test_interpolation_between_tenors(self):
        rates = {28: 0.04, 91: 0.045}
        # Midpoint: (28+91)/2 ≈ 59.5
        result = _interpolate_rate(rates, 60)
        # weight = (60-28)/(91-28) = 32/63 ≈ 0.508
        expected = 0.04 + (32 / 63) * (0.045 - 0.04)
        assert abs(result - expected) < 1e-10


class TestParseLatestRate:
    """Test parsing FRED observation values."""

    @pytest.mark.parametrize(
        ("values", "expected"),
        [
            pytest.param(("4.30",), 0.043, id="valid_rate"),
            pytest.param((".", "4.50"), 0.045, id="skips_missing_values"),
        ],
    )
    def test_parses_latest_rate(self, values, expected):
        obs = [{"value": value} for value in values]
        assert _parse_latest_rate(obs) == pytest.approx(expected)

    @pytest.mark.parametrize(
        "values",
        [
            pytest.param((".", "."), id="all_missing"),
            pytest.param((), id="empty_list"),
        ],
    )
    def test_returns_none_when_no_rate_is_present(self, values):
        obs = [{"value": value} for value in values]
        assert _parse_latest_rate(obs) is None


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
        assert get_risk_free_rate_and_source(dte_days=30, observation_date="2025-01-15")[1] == RATE_SOURCE_FRED

    @patch("app.services.fred_service._fetch_series")
    def test_fallback_on_failure(self, mock_fetch):
        mock_fetch.return_value = []

        rate = get_risk_free_rate(dte_days=30, observation_date="2025-01-15")
        assert rate == DEFAULT_RISK_FREE_RATE
        # A fallback is never reported as FRED's answer (#2764).
        assert get_risk_free_rate_and_source(dte_days=30, observation_date="2025-01-15") == (
            DEFAULT_RISK_FREE_RATE,
            RATE_SOURCE_DEFAULT,
        )

    @patch("app.services.fred_service._fetch_series")
    def test_cache_prevents_refetch(self, mock_fetch):
        mock_fetch.return_value = [{"value": "4.30"}]

        get_risk_free_rate(dte_days=30, observation_date="2025-01-15")
        initial_count = mock_fetch.call_count

        get_risk_free_rate(dte_days=30, observation_date="2025-01-15")
        assert mock_fetch.call_count == initial_count  # No additional calls
