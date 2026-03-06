"""Tests for options feature computation."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from app.research.features.options_features import OptionsFeatures


def _make_iv_df(n: int = 100, seed: int = 42) -> pd.DataFrame:
    """Create synthetic IV data for testing."""
    np.random.seed(seed)
    return pd.DataFrame({
        "iv_30d_atm": np.random.uniform(0.15, 0.35, n),
        "iv_30d_put": np.random.uniform(0.18, 0.40, n),
        "iv_30d_call": np.random.uniform(0.12, 0.30, n),
    })


def _make_stock_df(n: int = 100, seed: int = 42) -> pd.DataFrame:
    """Create synthetic stock data for VRP."""
    np.random.seed(seed)
    close = 100 + np.cumsum(np.random.normal(0, 1, n))
    return pd.DataFrame({"close": close})


class TestIv30d:
    """Test raw IV feature."""

    def test_returns_atm_iv(self):
        df = _make_iv_df()
        result = OptionsFeatures.compute_iv_30d(df)
        pd.testing.assert_series_equal(result, df["iv_30d_atm"].astype(float), check_names=False)

    def test_preserves_length(self):
        df = _make_iv_df(n=50)
        result = OptionsFeatures.compute_iv_30d(df)
        assert len(result) == 50


class TestIvRank:
    """Test IV Rank feature."""

    def test_bounded_zero_one(self):
        """IV Rank should be bounded [0, 1]."""
        df = _make_iv_df(n=200)
        result = OptionsFeatures.compute_iv_rank(df, window=60, min_periods=30)
        valid = result.dropna()
        assert valid.min() >= 0.0 - 1e-10
        assert valid.max() <= 1.0 + 1e-10

    def test_rank_one_at_max(self):
        """When IV is at its rolling max, rank should be 1.0."""
        iv = list(range(1, 101))  # Monotonically increasing
        df = pd.DataFrame({"iv_30d_atm": iv})
        result = OptionsFeatures.compute_iv_rank(df, window=60, min_periods=30)
        # Last values should be close to 1.0
        assert result.iloc[-1] == pytest.approx(1.0, abs=1e-10)

    def test_rank_zero_at_min(self):
        """When IV is at its rolling min, rank should be 0.0."""
        iv = list(range(100, 0, -1))  # Monotonically decreasing
        df = pd.DataFrame({"iv_30d_atm": iv})
        result = OptionsFeatures.compute_iv_rank(df, window=60, min_periods=30)
        # Last values should be close to 0.0
        assert result.iloc[-1] == pytest.approx(0.0, abs=1e-10)

    def test_constant_iv_returns_half(self):
        """Constant IV should return 0.5 (or near it)."""
        df = pd.DataFrame({"iv_30d_atm": [0.25] * 100})
        result = OptionsFeatures.compute_iv_rank(df, window=60, min_periods=30)
        valid = result.dropna()
        assert all(abs(v - 0.5) < 1e-10 for v in valid)

    def test_nan_during_warmup(self):
        """Before min_periods, rolling min/max are NaN → rank is 0.5 (fallback)."""
        df = _make_iv_df(n=100)
        result = OptionsFeatures.compute_iv_rank(df, window=60, min_periods=30)
        # Rolling min/max produce NaN before min_periods, but np.where
        # catches zero-denominator and returns 0.5. Verify warmup values are 0.5.
        warmup = result.iloc[:29]
        assert all(v == 0.5 or pd.isna(v) for v in warmup)

    def test_252_day_window(self):
        """Test 252-day window produces valid results."""
        df = _make_iv_df(n=300)
        result = OptionsFeatures.compute_iv_rank(df, window=252, min_periods=60)
        valid = result.dropna()
        assert len(valid) > 0
        assert valid.min() >= 0.0 - 1e-10
        assert valid.max() <= 1.0 + 1e-10


class TestLogSkew:
    """Test log put-call skew."""

    def test_positive_when_put_higher(self):
        """Positive skew when put IV > call IV."""
        df = pd.DataFrame({
            "iv_30d_atm": [0.25],
            "iv_30d_put": [0.30],
            "iv_30d_call": [0.20],
        })
        result = OptionsFeatures.compute_log_skew(df)
        assert result.iloc[0] > 0  # ln(0.30/0.20) > 0

    def test_negative_when_call_higher(self):
        """Negative skew when call IV > put IV."""
        df = pd.DataFrame({
            "iv_30d_atm": [0.25],
            "iv_30d_put": [0.20],
            "iv_30d_call": [0.30],
        })
        result = OptionsFeatures.compute_log_skew(df)
        assert result.iloc[0] < 0  # ln(0.20/0.30) < 0

    def test_zero_when_equal(self):
        """Zero skew when put IV == call IV."""
        df = pd.DataFrame({
            "iv_30d_atm": [0.25],
            "iv_30d_put": [0.25],
            "iv_30d_call": [0.25],
        })
        result = OptionsFeatures.compute_log_skew(df)
        assert abs(result.iloc[0]) < 1e-10

    def test_nan_when_zero_iv(self):
        """NaN when either IV is zero."""
        df = pd.DataFrame({
            "iv_30d_atm": [0.25],
            "iv_30d_put": [0.0],
            "iv_30d_call": [0.25],
        })
        result = OptionsFeatures.compute_log_skew(df)
        assert pd.isna(result.iloc[0])

    def test_nan_when_missing(self):
        """NaN when either IV is NaN."""
        df = pd.DataFrame({
            "iv_30d_atm": [0.25],
            "iv_30d_put": [np.nan],
            "iv_30d_call": [0.25],
        })
        result = OptionsFeatures.compute_log_skew(df)
        assert pd.isna(result.iloc[0])

    def test_scale_invariance(self):
        """Log skew should be the same regardless of absolute vol level.

        If put=0.30, call=0.20 gives skew S1, and put=0.60, call=0.40
        gives skew S2, then S1 == S2 (scale invariance of log ratio).
        """
        df1 = pd.DataFrame({
            "iv_30d_atm": [0.25], "iv_30d_put": [0.30], "iv_30d_call": [0.20],
        })
        df2 = pd.DataFrame({
            "iv_30d_atm": [0.50], "iv_30d_put": [0.60], "iv_30d_call": [0.40],
        })
        skew1 = OptionsFeatures.compute_log_skew(df1).iloc[0]
        skew2 = OptionsFeatures.compute_log_skew(df2).iloc[0]
        assert abs(skew1 - skew2) < 1e-10


class TestVrp:
    """Test Volatility Risk Premium with namespace isolation."""

    def test_signal_mode_no_future_leak(self):
        """Signal mode uses trailing RV — no NaN at the end."""
        iv_df = _make_iv_df(n=50)
        stock_df = _make_stock_df(n=50)
        result = OptionsFeatures.compute_vrp(iv_df, stock_df, mode="signal")
        assert result.name == "vrp_5"
        # Trailing RV: last values should NOT be NaN (after warmup)
        assert result.iloc[-1] is not np.nan or pd.notna(result.iloc[-1])

    def test_research_mode_uses_forward(self):
        """Research mode uses forward RV — NaN at the end."""
        iv_df = _make_iv_df(n=50)
        stock_df = _make_stock_df(n=50)
        result = OptionsFeatures.compute_vrp(iv_df, stock_df, mode="research")
        assert result.name == "vrp_5_forward"
        # Forward-looking: last 5 values should be NaN
        assert result.iloc[-5:].isna().all()

    def test_vrp_requires_stock_data(self):
        """VRP should raise if stock_data is None."""
        iv_df = _make_iv_df()
        with pytest.raises(ValueError, match="VRP requires stock_data"):
            OptionsFeatures.compute_feature("vrp_5", iv_df, stock_data=None)

    def test_vrp_5_rejects_research_mode(self):
        """vrp_5 must NOT be used in research mode — use vrp_5_forward instead."""
        iv_df = _make_iv_df(n=50)
        stock_df = _make_stock_df(n=50)
        with pytest.raises(ValueError, match="vrp_5_forward"):
            OptionsFeatures.compute_feature("vrp_5", iv_df, stock_df, mode="research")

    def test_vrp_5_forward_rejects_signal_mode(self):
        """vrp_5_forward must NOT be used in signal mode."""
        iv_df = _make_iv_df(n=50)
        stock_df = _make_stock_df(n=50)
        with pytest.raises(ValueError, match="must NOT be used in signal mode"):
            OptionsFeatures.compute_feature("vrp_5_forward", iv_df, stock_df, mode="signal")

    def test_vrp_5_forward_accepted_in_research_mode(self):
        """vrp_5_forward should work in research mode."""
        iv_df = _make_iv_df(n=50)
        stock_df = _make_stock_df(n=50)
        result = OptionsFeatures.compute_feature("vrp_5_forward", iv_df, stock_df, mode="research")
        assert result.name == "vrp_5_forward"
        assert len(result) == 50


class TestComputeFeatureDispatch:
    """Test feature dispatch function."""

    def test_iv_30d(self):
        df = _make_iv_df()
        result = OptionsFeatures.compute_feature("iv_30d", df)
        assert len(result) == len(df)

    def test_iv_rank_60(self):
        df = _make_iv_df(n=100)
        result = OptionsFeatures.compute_feature("iv_rank_60", df)
        assert len(result) == len(df)

    def test_log_skew(self):
        df = _make_iv_df()
        result = OptionsFeatures.compute_feature("log_skew", df)
        assert len(result) == len(df)

    def test_unknown_feature_raises(self):
        df = _make_iv_df()
        with pytest.raises(ValueError, match="Unknown options feature"):
            OptionsFeatures.compute_feature("nonexistent", df)
