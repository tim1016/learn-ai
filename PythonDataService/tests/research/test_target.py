"""Tests for 15-minute forward return computation."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.research.target import compute_15min_forward_return, validate_return_series


class TestCompute15MinForwardReturn:
    def test_single_day_produces_valid_returns(self, sample_bars_single_day: list[dict]) -> None:
        returns = compute_15min_forward_return(sample_bars_single_day, horizon=15)
        assert len(returns) == len(sample_bars_single_day)
        assert returns.notna().sum() > 0

    def test_last_horizon_bars_are_nan(self, sample_bars_single_day: list[dict]) -> None:
        horizon = 15
        returns = compute_15min_forward_return(sample_bars_single_day, horizon=horizon)
        # Last `horizon` bars of the day must be NaN
        assert returns.iloc[-horizon:].isna().all()

    def test_no_cross_day_contamination(self, sample_bars_multi_day: list[dict]) -> None:
        returns = compute_15min_forward_return(sample_bars_multi_day, horizon=15)
        df = pd.DataFrame(sample_bars_multi_day).sort_values("timestamp").reset_index(drop=True)
        df["date"] = pd.to_datetime(df["timestamp"], unit="ms").dt.date

        for date, day_df in df.groupby("date"):
            day_indices = day_df.index
            if len(day_indices) <= 15:
                continue
            # Last 15 bars of each day must be NaN
            tail_indices = day_indices[-15:]
            for idx in tail_indices:
                assert pd.isna(returns.iloc[idx]), (
                    f"Expected NaN at index {idx} (day {date}), got {returns.iloc[idx]}"
                )

    def test_return_values_are_log_returns(self, sample_bars_single_day: list[dict]) -> None:
        returns = compute_15min_forward_return(sample_bars_single_day, horizon=15)
        df = pd.DataFrame(sample_bars_single_day).sort_values("timestamp").reset_index(drop=True)

        # Check a known valid return
        first_valid_idx = returns.first_valid_index()
        if first_valid_idx is not None:
            expected = np.log(df.loc[first_valid_idx + 15, "close"] / df.loc[first_valid_idx, "close"])
            np.testing.assert_allclose(returns.iloc[first_valid_idx], expected, atol=1e-10)

    def test_empty_bars_returns_empty(self) -> None:
        returns = compute_15min_forward_return([], horizon=15)
        assert len(returns) == 0


class TestValidateReturnSeries:
    def test_valid_series_passes(self) -> None:
        series = pd.Series(np.random.default_rng(0).normal(0, 0.01, 100))
        assert validate_return_series(series) is True

    def test_mostly_nan_fails(self) -> None:
        series = pd.Series([np.nan] * 80 + [0.01] * 20)
        assert validate_return_series(series) is False

    def test_zero_variance_fails(self) -> None:
        series = pd.Series([0.005] * 100)
        assert validate_return_series(series) is False

    def test_empty_series_fails(self) -> None:
        series = pd.Series([], dtype=float)
        assert validate_return_series(series) is False
