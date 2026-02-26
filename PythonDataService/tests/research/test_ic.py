"""Tests for Information Coefficient calculation."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.research.validation.ic import compute_information_coefficient


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
