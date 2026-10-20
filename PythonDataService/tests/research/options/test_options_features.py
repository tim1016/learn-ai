"""Tests for options feature computation."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.research.features.options_features import OptionsFeatures


def _make_iv_df(n: int = 100, seed: int = 42) -> pd.DataFrame:
    """Create synthetic IV data for testing."""
    np.random.seed(seed)
    return pd.DataFrame(
        {
            "iv_30d_atm": np.random.uniform(0.15, 0.35, n),
            "iv_30d_put": np.random.uniform(0.18, 0.40, n),
            "iv_30d_call": np.random.uniform(0.12, 0.30, n),
        }
    )


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


class TestIvRank:
    """Test IV Rank feature."""

    @pytest.mark.parametrize(
        ("iv_range", "expected_rank"),
        [
            # When IV is at its rolling max, rank should be 1.0 (monotonically increasing).
            pytest.param((1, 101), 1.0, id="rank_one_at_max"),
            # When IV is at its rolling min, rank should be 0.0 (monotonically decreasing).
            pytest.param((100, 0, -1), 0.0, id="rank_zero_at_min"),
        ],
    )
    def test_rank_at_rolling_extreme(self, iv_range, expected_rank):
        iv = list(range(*iv_range))
        df = pd.DataFrame({"iv_30d_atm": iv})
        result = OptionsFeatures.compute_iv_rank(df, window=60, min_periods=30)
        # Last values should be close to the expected rank
        assert result.iloc[-1] == pytest.approx(expected_rank, abs=1e-10)

    def test_rank_bounds_constant_warmup_and_long_window(self):
        # IV Rank should be bounded [0, 1].
        df = _make_iv_df(n=200)
        result = OptionsFeatures.compute_iv_rank(df, window=60, min_periods=30)
        valid = result.dropna()
        assert valid.min() >= 0.0 - 1e-10, "bounded zero-one: minimum is at least 0"
        assert valid.max() <= 1.0 + 1e-10, "bounded zero-one: maximum is at most 1"

        # Constant IV should return 0.5 (or near it).
        df = pd.DataFrame({"iv_30d_atm": [0.25] * 100})
        result = OptionsFeatures.compute_iv_rank(df, window=60, min_periods=30)
        valid = result.dropna()
        assert all(abs(v - 0.5) < 1e-10 for v in valid), "constant IV returns half"

        # Before min_periods, rolling min/max are NaN → rank is 0.5 (fallback).
        df = _make_iv_df(n=100)
        result = OptionsFeatures.compute_iv_rank(df, window=60, min_periods=30)
        # Rolling min/max produce NaN before min_periods, but np.where
        # catches zero-denominator and returns 0.5. Verify warmup values are 0.5.
        warmup = result.iloc[:29]
        assert all(v == 0.5 or pd.isna(v) for v in warmup), "warmup values are 0.5 or NaN"

        # 252-day window produces valid results.
        df = _make_iv_df(n=300)
        result = OptionsFeatures.compute_iv_rank(df, window=252, min_periods=60)
        valid = result.dropna()
        assert len(valid) > 0, "252-day window produces valid results"
        assert valid.min() >= 0.0 - 1e-10, "252-day window: minimum is at least 0"
        assert valid.max() <= 1.0 + 1e-10, "252-day window: maximum is at most 1"


class TestLogSkew:
    """Test log put-call skew."""

    def test_skew_sign_and_scale_invariance(self):
        # Positive skew when put IV > call IV.
        df = pd.DataFrame(
            {
                "iv_30d_atm": [0.25],
                "iv_30d_put": [0.30],
                "iv_30d_call": [0.20],
            }
        )
        result = OptionsFeatures.compute_log_skew(df)
        assert result.iloc[0] > 0, "positive when put higher"  # ln(0.30/0.20) > 0

        # Negative skew when call IV > put IV.
        df = pd.DataFrame(
            {
                "iv_30d_atm": [0.25],
                "iv_30d_put": [0.20],
                "iv_30d_call": [0.30],
            }
        )
        result = OptionsFeatures.compute_log_skew(df)
        assert result.iloc[0] < 0, "negative when call higher"  # ln(0.20/0.30) < 0

        # Zero skew when put IV == call IV.
        df = pd.DataFrame(
            {
                "iv_30d_atm": [0.25],
                "iv_30d_put": [0.25],
                "iv_30d_call": [0.25],
            }
        )
        result = OptionsFeatures.compute_log_skew(df)
        assert abs(result.iloc[0]) < 1e-10, "zero when put equals call"

        # Log skew should be the same regardless of absolute vol level.
        #
        # If put=0.30, call=0.20 gives skew S1, and put=0.60, call=0.40
        # gives skew S2, then S1 == S2 (scale invariance of log ratio).
        df1 = pd.DataFrame(
            {
                "iv_30d_atm": [0.25],
                "iv_30d_put": [0.30],
                "iv_30d_call": [0.20],
            }
        )
        df2 = pd.DataFrame(
            {
                "iv_30d_atm": [0.50],
                "iv_30d_put": [0.60],
                "iv_30d_call": [0.40],
            }
        )
        skew1 = OptionsFeatures.compute_log_skew(df1).iloc[0]
        skew2 = OptionsFeatures.compute_log_skew(df2).iloc[0]
        assert abs(skew1 - skew2) < 1e-10, "scale invariance of the log ratio"

    @pytest.mark.parametrize(
        "put_iv",
        [
            pytest.param(0.0, id="nan_when_zero_iv"),
            pytest.param(np.nan, id="nan_when_missing"),
        ],
    )
    def test_nan_when_put_iv_unusable(self, put_iv):
        df = pd.DataFrame(
            {
                "iv_30d_atm": [0.25],
                "iv_30d_put": [put_iv],
                "iv_30d_call": [0.25],
            }
        )
        result = OptionsFeatures.compute_log_skew(df)
        assert pd.isna(result.iloc[0])


class TestVrp:
    """Test Volatility Risk Premium with namespace isolation."""

    def test_research_mode_uses_forward_and_accepts_vrp_5_forward(self):
        # Research mode uses forward RV — NaN at the end.
        iv_df = _make_iv_df(n=50)
        stock_df = _make_stock_df(n=50)
        result = OptionsFeatures.compute_vrp(iv_df, stock_df, mode="research")
        assert result.name == "vrp_5_forward", "research mode uses the forward vrp name"
        # Forward-looking: last 5 values should be NaN
        assert result.iloc[-5:].isna().all(), "research mode forward RV is NaN for the last 5 values"

        # vrp_5_forward should work in research mode.
        result = OptionsFeatures.compute_feature("vrp_5_forward", iv_df, stock_df, mode="research")
        assert result.name == "vrp_5_forward", "vrp_5_forward is accepted in research mode"
        assert len(result) == 50, "vrp_5_forward in research mode keeps all 50 rows"

    def test_vrp_requires_stock_data(self):
        """VRP should raise if stock_data is None."""
        iv_df = _make_iv_df()
        with pytest.raises(ValueError, match="VRP requires stock_data"):
            OptionsFeatures.compute_feature("vrp_5", iv_df, stock_data=None)

    @pytest.mark.parametrize(
        ("feature", "mode", "match"),
        [
            # vrp_5 must NOT be used in research mode — use vrp_5_forward instead.
            pytest.param("vrp_5", "research", "vrp_5_forward", id="vrp_5_rejects_research_mode"),
            # vrp_5_forward must NOT be used in signal mode.
            pytest.param(
                "vrp_5_forward", "signal", "must NOT be used in signal mode", id="vrp_5_forward_rejects_signal_mode"
            ),
        ],
    )
    def test_vrp_feature_rejects_wrong_mode(self, feature, mode, match):
        iv_df = _make_iv_df(n=50)
        stock_df = _make_stock_df(n=50)
        with pytest.raises(ValueError, match=match):
            OptionsFeatures.compute_feature(feature, iv_df, stock_df, mode=mode)


class TestComputeFeatureDispatch:
    """Test feature dispatch function."""

    def test_unknown_feature_raises(self):
        df = _make_iv_df()
        with pytest.raises(ValueError, match="Unknown options feature"):
            OptionsFeatures.compute_feature("nonexistent", df)
