"""Tests for app.ml.preprocessing.stationarity.

The stationarity helper wraps statsmodels' ADF + KPSS. We test the decision
logic (AND rule) and the short-series guard with deterministic inputs.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.ml.preprocessing.stationarity import (
    StationarityResult,
    run_stationarity_tests,
)


def test_short_series_returns_non_stationary_sentinel():
    rng = np.random.default_rng(seed=42)
    series = rng.normal(size=10)

    result = run_stationarity_tests(series)

    assert isinstance(result, StationarityResult)
    assert result.is_stationary is False
    assert result.adf_pvalue == pytest.approx(1.0, abs=1e-12, rel=0)
    assert result.adf_statistic == 0.0
    assert result.kpss_statistic == 0.0


def test_white_noise_is_classified_stationary():
    rng = np.random.default_rng(seed=7)
    series = rng.normal(size=500)

    result = run_stationarity_tests(series)

    assert result.is_stationary is True
    # ADF should strongly reject unit root for iid noise.
    assert result.adf_pvalue < 0.05
    # KPSS should fail to reject stationarity (p large).
    assert result.kpss_pvalue > 0.05


def test_random_walk_is_classified_non_stationary():
    rng = np.random.default_rng(seed=11)
    steps = rng.normal(size=500)
    series = np.cumsum(steps)

    result = run_stationarity_tests(series)

    assert result.is_stationary is False
