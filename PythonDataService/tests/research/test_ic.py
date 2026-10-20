"""Tests for Information Coefficient calculation."""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.research.validation.ic import (
    _andrews_lag,
    _compute_effective_sample_size,
    _compute_newey_west_t_stat,
    compute_information_coefficient,
)


class TestInformationCoefficient:
    def test_correlated_data_has_positive_ic_and_newey_west_stats(self) -> None:
        """Feature positively correlated with returns should have positive mean IC and computed Newey-West stats."""
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

        assert result.mean_ic > 0, "correlated feature has a positive mean IC"
        assert len(result.daily_ic_values) == 5, "one daily IC value per day"
        assert len(result.daily_ic_dates) == 5, "one daily IC date per day"
        assert result.nw_t_stat != 0.0, "Newey-West t-stat is computed for correlated data"
        assert result.nw_p_value < 1.0, "Newey-West p-value is below one for correlated data"
        assert result.effective_n > 0, "effective N is positive"
        assert result.effective_n <= len(result.daily_ic_values), "effective N is at most the actual N for autocorrelated ICs"

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

    def test_t_stats_direction_matches_ic(self) -> None:
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

        # t-stat and Newey-West t-stat should have same sign as mean IC
        if result.mean_ic > 0:
            assert result.ic_t_stat > 0, "t-stat is positive when mean IC is positive"
            assert result.nw_t_stat > 0, "Newey-West t-stat is positive when mean IC is positive"
        elif result.mean_ic < 0:
            assert result.ic_t_stat < 0, "t-stat is negative when mean IC is negative"
            assert result.nw_t_stat < 0, "Newey-West t-stat is negative when mean IC is negative"


class TestNeweyWest:
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

        _nw_t, _nw_p = _compute_newey_west_t_stat(small)
        effective = _compute_effective_sample_size(small)

        assert effective == 2.0  # Too small for correction


class TestAndrewsLag:
    """Test Andrews (1991) automatic bandwidth selection."""

    def test_reference_sample_sizes(self) -> None:
        """Andrews lag at the reference sample sizes."""
        assert _andrews_lag(10) == 2, "n=10 → floor(4 * (10/100)^(2/9)) = floor(2.15) = 2"
        assert _andrews_lag(100) == 4, "n=100 → floor(4 * 1.0) = 4"
        assert _andrews_lag(250) >= 4, "n=250 → floor(4 * (250/100)^(2/9)) ≈ floor(4 * 1.22) = 4"
        assert _andrews_lag(500) >= 5, "n=500 → floor(4 * (5)^(2/9)) ≈ floor(5.4) = 5"

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
            ic[i] = 0.5 * ic[i - 1] + rng.normal(0.01, 0.03)

        # With min_lag=5, effective N should be less than actual N
        eff = _compute_effective_sample_size(ic, min_lag=5)
        assert eff < 200
        assert eff > 0
