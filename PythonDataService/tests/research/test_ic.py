"""Tests for Information Coefficient calculation."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.research.validation.ic import (
    _andrews_lag,
    _compute_effective_sample_size,
    _compute_newey_west_t_stat,
    compute_information_coefficient,
)


class TestInformationCoefficient:
    def test_correlated_data_has_positive_ic(self) -> None:
        """Feature positively correlated with returns should have positive mean IC."""
        rng = np.random.default_rng(42)
        n = 500
        # 5 days of 100 bars, same date for grouping
        timestamps = []
        base_ts = 1704117000000
        for day in range(5):
            for bar in range(100):
                timestamps.append(base_ts + day * 86_400_000 + bar * 60_000)

        feature = pd.Series(rng.normal(0, 1, n))
        # Target correlated with feature
        target = pd.Series(0.5 * feature + rng.normal(0, 0.5, n))
        ts = pd.Series(timestamps)

        result = compute_information_coefficient(feature, target, ts)

        assert result.mean_ic > 0
        assert len(result.daily_ic_values) == 5
        assert len(result.daily_ic_dates) == 5

    def test_uncorrelated_data_has_near_zero_ic(self) -> None:
        rng = np.random.default_rng(99)
        n = 300
        timestamps = [1704117000000 + i * 60_000 for i in range(n)]

        feature = pd.Series(rng.normal(0, 1, n))
        target = pd.Series(rng.normal(0, 1, n))  # independent
        ts = pd.Series(timestamps)

        result = compute_information_coefficient(feature, target, ts)

        assert abs(result.mean_ic) < 0.3  # should be near zero

    def test_insufficient_data_returns_neutral(self) -> None:
        feature = pd.Series([np.nan] * 10)
        target = pd.Series([np.nan] * 10)
        ts = pd.Series([1704117000000 + i * 60_000 for i in range(10)])

        result = compute_information_coefficient(feature, target, ts)

        assert result.mean_ic == 0.0
        assert result.ic_p_value == 1.0
        assert len(result.daily_ic_values) == 0

    def test_t_stat_direction_matches_ic(self) -> None:
        rng = np.random.default_rng(7)
        n = 300
        timestamps = []
        base_ts = 1704117000000
        for day in range(3):
            for bar in range(100):
                timestamps.append(base_ts + day * 86_400_000 + bar * 60_000)

        feature = pd.Series(rng.normal(0, 1, n))
        target = pd.Series(0.3 * feature + rng.normal(0, 0.7, n))
        ts = pd.Series(timestamps)

        result = compute_information_coefficient(feature, target, ts)

        # t-stat should have same sign as mean IC
        if result.mean_ic > 0:
            assert result.ic_t_stat > 0
        elif result.mean_ic < 0:
            assert result.ic_t_stat < 0


class TestNeweyWest:
    def test_nw_t_stat_computed(self) -> None:
        """Newey-West t-stat should be computed for correlated data."""
        rng = np.random.default_rng(42)
        n = 500
        timestamps = []
        base_ts = 1704117000000
        for day in range(5):
            for bar in range(100):
                timestamps.append(base_ts + day * 86_400_000 + bar * 60_000)

        feature = pd.Series(rng.normal(0, 1, n))
        target = pd.Series(0.5 * feature + rng.normal(0, 0.5, n))
        ts = pd.Series(timestamps)

        result = compute_information_coefficient(feature, target, ts)

        assert result.nw_t_stat != 0.0
        assert result.nw_p_value < 1.0

    def test_effective_n_less_than_actual_n(self) -> None:
        """Effective N should be <= actual N when ICs are autocorrelated."""
        rng = np.random.default_rng(42)
        n = 500
        timestamps = []
        base_ts = 1704117000000
        for day in range(5):
            for bar in range(100):
                timestamps.append(base_ts + day * 86_400_000 + bar * 60_000)

        feature = pd.Series(rng.normal(0, 1, n))
        target = pd.Series(0.5 * feature + rng.normal(0, 0.5, n))
        ts = pd.Series(timestamps)

        result = compute_information_coefficient(feature, target, ts)

        assert result.effective_n > 0
        assert result.effective_n <= len(result.daily_ic_values)

    def test_nw_t_stat_direction_matches_ic(self) -> None:
        """Newey-West t-stat should have the same sign as mean IC."""
        rng = np.random.default_rng(7)
        n = 300
        timestamps = []
        base_ts = 1704117000000
        for day in range(3):
            for bar in range(100):
                timestamps.append(base_ts + day * 86_400_000 + bar * 60_000)

        feature = pd.Series(rng.normal(0, 1, n))
        target = pd.Series(0.3 * feature + rng.normal(0, 0.7, n))
        ts = pd.Series(timestamps)

        result = compute_information_coefficient(feature, target, ts)

        if result.mean_ic > 0:
            assert result.nw_t_stat > 0
        elif result.mean_ic < 0:
            assert result.nw_t_stat < 0

    def test_effective_n_helpers_with_iid_data(self) -> None:
        """For IID data, effective N should be close to actual N."""
        rng = np.random.default_rng(123)
        ic_array = rng.normal(0.02, 0.05, 50)

        effective = _compute_effective_sample_size(ic_array)

        # For IID data, effective N should be near actual N
        assert effective >= len(ic_array) * 0.7

    def test_nw_helpers_with_small_array(self) -> None:
        """Helpers should handle small arrays gracefully."""
        small = np.array([0.01, 0.02])

        nw_t, nw_p = _compute_newey_west_t_stat(small)
        effective = _compute_effective_sample_size(small)

        assert effective == 2.0  # Too small for correction


class TestAndrewsLag:
    """Test Andrews (1991) automatic bandwidth selection."""

    def test_small_sample(self) -> None:
        """Small n=10 → floor(4 * (10/100)^(2/9)) = floor(2.15) = 2."""
        assert _andrews_lag(10) == 2

    def test_100_sample(self) -> None:
        """n=100 → floor(4 * 1.0) = 4."""
        assert _andrews_lag(100) == 4

    def test_250_sample(self) -> None:
        """n=250 → floor(4 * (250/100)^(2/9)) ≈ floor(4 * 1.22) = 4."""
        lag = _andrews_lag(250)
        assert lag >= 4

    def test_500_sample(self) -> None:
        """n=500 → floor(4 * (5)^(2/9)) ≈ floor(5.4) = 5."""
        lag = _andrews_lag(500)
        assert lag >= 5

    def test_monotonically_increasing(self) -> None:
        """Larger n should give >= lag than smaller n."""
        lags = [_andrews_lag(n) for n in [50, 100, 200, 500, 1000]]
        for i in range(len(lags) - 1):
            assert lags[i] <= lags[i + 1]

    def test_n_eff_uses_andrews_lag(self) -> None:
        """Effective N should use Andrews lag, consistent with Newey-West."""
        rng = np.random.default_rng(42)
        # Autocorrelated series
        ic = np.zeros(200)
        ic[0] = rng.normal(0.02, 0.05)
        for i in range(1, 200):
            ic[i] = 0.5 * ic[i-1] + rng.normal(0.01, 0.03)

        # With min_lag=5, effective N should be less than actual N
        eff = _compute_effective_sample_size(ic, min_lag=5)
        assert eff < 200
        assert eff > 0
