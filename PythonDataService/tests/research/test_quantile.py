"""Tests for quantile analysis."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.research.validation.quantile import compute_quantile_analysis


class TestQuantileAnalysis:
    def test_correct_bin_count(self) -> None:
        rng = np.random.default_rng(42)
        feature = pd.Series(rng.normal(0, 1, 500))
        target = pd.Series(rng.normal(0, 0.01, 500))

        result = compute_quantile_analysis(feature, target, n_bins=5)

        assert len(result.bins) == 5
        assert all(b.count > 0 for b in result.bins)

    def test_monotonic_feature_detected(self) -> None:
        """A feature perfectly correlated with returns should be monotonic."""
        rng = np.random.default_rng(42)
        n = 1000
        feature = pd.Series(rng.uniform(-1, 1, n))
        # Target perfectly correlated
        target = pd.Series(feature * 0.01 + rng.normal(0, 0.001, n))

        result = compute_quantile_analysis(feature, target, n_bins=5)

        assert result.is_monotonic is True
        assert result.monotonicity_ratio >= 0.75

    def test_random_feature_not_monotonic(self) -> None:
        rng = np.random.default_rng(42)
        n = 1000
        feature = pd.Series(rng.normal(0, 1, n))
        target = pd.Series(rng.normal(0, 1, n))  # independent

        result = compute_quantile_analysis(feature, target, n_bins=5)

        # Random data unlikely to be monotonic
        assert result.monotonicity_ratio < 1.0

    def test_insufficient_data_returns_empty(self) -> None:
        feature = pd.Series([1.0, 2.0])
        target = pd.Series([0.01, 0.02])

        result = compute_quantile_analysis(feature, target, n_bins=5)

        assert len(result.bins) == 0
        assert result.is_monotonic is False

    def test_all_nan_returns_empty(self) -> None:
        feature = pd.Series([np.nan] * 50)
        target = pd.Series([np.nan] * 50)

        result = compute_quantile_analysis(feature, target, n_bins=5)

        assert len(result.bins) == 0

    def test_bins_have_correct_fields(self) -> None:
        rng = np.random.default_rng(42)
        feature = pd.Series(rng.normal(0, 1, 200))
        target = pd.Series(rng.normal(0, 0.01, 200))

        result = compute_quantile_analysis(feature, target, n_bins=5)

        for b in result.bins:
            assert isinstance(b.bin_number, int)
            assert isinstance(b.lower_bound, float)
            assert isinstance(b.upper_bound, float)
            assert isinstance(b.mean_return, float)
            assert isinstance(b.count, int)
            assert b.count > 0
