from __future__ import annotations

import numpy as np
import pytest

from app.ml.preprocessing.stationarity import StationarityResult, run_stationarity_tests


class TestRunStationarityTests:
    def test_white_noise_is_stationary(self) -> None:
        """White noise should be detected as stationary."""
        rng = np.random.default_rng(42)
        series = rng.normal(0, 1, size=500)
        result = run_stationarity_tests(series)
        assert isinstance(result, StationarityResult)
        assert result.is_stationary is True
        assert result.adf_pvalue < 0.05

    def test_random_walk_is_non_stationary(self) -> None:
        """A random walk (cumulative sum of noise) should be non-stationary."""
        rng = np.random.default_rng(42)
        series = np.cumsum(rng.normal(0, 1, size=500))
        result = run_stationarity_tests(series)
        assert result.is_stationary is False
        # ADF should fail to reject (high p-value)
        assert result.adf_pvalue > 0.05

    def test_trending_series_is_non_stationary(self) -> None:
        """A linearly trending series should be non-stationary."""
        series = np.arange(1, 501, dtype=np.float64)
        result = run_stationarity_tests(series)
        assert result.is_stationary is False

    def test_short_series_returns_default(self) -> None:
        """Series shorter than 20 should return default non-stationary result."""
        series = np.array([1.0, 2.0, 3.0])
        result = run_stationarity_tests(series)
        assert result.is_stationary is False
        assert result.adf_pvalue == 1.0

    def test_summary_property(self) -> None:
        """Summary string should contain status and p-values."""
        rng = np.random.default_rng(42)
        series = rng.normal(0, 1, size=200)
        result = run_stationarity_tests(series)
        summary = result.summary
        assert "ADF p=" in summary
        assert "KPSS p=" in summary
        assert "STATIONARY" in summary

    def test_returns_correct_types(self) -> None:
        """All fields should be correct types."""
        rng = np.random.default_rng(42)
        series = rng.normal(0, 1, size=200)
        result = run_stationarity_tests(series)
        assert isinstance(result.adf_statistic, float)
        assert isinstance(result.adf_pvalue, float)
        assert isinstance(result.kpss_statistic, float)
        assert isinstance(result.kpss_pvalue, float)
        assert isinstance(result.is_stationary, bool)
