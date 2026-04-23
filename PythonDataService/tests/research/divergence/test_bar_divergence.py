"""Tests for app.research.divergence.analysis.bar_divergence diff helpers."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.research.divergence.analysis.bar_divergence import (
    IndicatorPair,
    diff_stats,
    pairwise_diff,
)


def test_diff_stats_empty_series_returns_none_for_aggregates():
    result = diff_stats(pd.Series([], dtype=float), pd.Series([], dtype=float))

    assert result["n"] == 0
    assert result["mean"] is None
    assert result["mean_abs"] is None
    assert result["rmse"] is None
    assert result["corr"] is None


def test_diff_stats_identical_series_yields_zero_diff_and_perfect_corr():
    a = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    b = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])

    result = diff_stats(a, b)

    assert result["n"] == 5
    assert result["mean"] == pytest.approx(0.0, abs=1e-12, rel=0)
    assert result["rmse"] == pytest.approx(0.0, abs=1e-12, rel=0)
    assert result["corr"] == pytest.approx(1.0, abs=1e-12, rel=0)


def test_diff_stats_offset_series_measured_correctly():
    a = pd.Series([10.0, 11.0, 12.0])
    b = pd.Series([9.0, 10.0, 11.0])

    result = diff_stats(a, b)

    assert result["n"] == 3
    assert result["mean"] == pytest.approx(1.0, abs=1e-12, rel=0)
    assert result["mean_abs"] == pytest.approx(1.0, abs=1e-12, rel=0)
    assert result["max_abs"] == pytest.approx(1.0, abs=1e-12, rel=0)
    assert result["rmse"] == pytest.approx(1.0, abs=1e-12, rel=0)


def test_diff_stats_nan_rows_excluded():
    a = pd.Series([1.0, np.nan, 3.0])
    b = pd.Series([1.0, 2.0, 3.5])

    result = diff_stats(a, b)

    assert result["n"] == 2  # NaN rows drop
    assert result["mean"] == pytest.approx(-0.25, abs=1e-12, rel=0)


def test_pairwise_diff_raises_when_column_missing():
    merged = pd.DataFrame({"time_utc": pd.to_datetime([0], unit="ms", utc=True), "ema_20_tv": [1.0]})
    pair = IndicatorPair(
        indicator="ema_20",
        tv_col="ema_20_tv",
        other_col="ema_20_native",
        other_label="native",
        timeframe="15m",
    )

    with pytest.raises(KeyError):
        pairwise_diff(merged, pair)


def test_pairwise_diff_produces_expected_columns_and_stats():
    merged = pd.DataFrame(
        {
            "time_utc": pd.to_datetime([1_704_067_200_000, 1_704_067_260_000], unit="ms", utc=True),
            "ema_20_tv": [10.0, 11.0],
            "ema_20_native": [9.5, 11.5],
        }
    )
    pair = IndicatorPair(
        indicator="ema_20",
        tv_col="ema_20_tv",
        other_col="ema_20_native",
        other_label="native",
        timeframe="15m",
    )

    df, stats = pairwise_diff(merged, pair)

    assert set(df.columns) >= {"time_utc", "tv", "other", "diff", "abs_diff", "indicator", "impl", "timeframe"}
    np.testing.assert_allclose(df["diff"].to_numpy(), np.array([0.5, -0.5]), atol=1e-12, rtol=0)
    assert stats["indicator"] == "ema_20"
    assert stats["impl"] == "native"
    assert stats["timeframe"] == "15m"
    assert stats["n"] == 2
