"""Tests for indicator reliability analysis."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.research.indicator_reliability import (
    compute_forward_return,
    compute_indicator_reliability,
    find_best_horizon,
    format_indicator_display_name,
    get_indicator_category,
    HorizonICAnalysis,
)


def _create_test_df(n_bars: int = 500, seed: int = 42) -> pd.DataFrame:
    """Create a test DataFrame with OHLCV + RSI-like indicator."""
    np.random.seed(seed)

    # Generate random walk price
    returns = np.random.randn(n_bars) * 0.001
    close = 100 * np.exp(np.cumsum(returns))
    high = close * (1 + np.abs(np.random.randn(n_bars)) * 0.002)
    low = close * (1 - np.abs(np.random.randn(n_bars)) * 0.002)
    open_ = low + (high - low) * np.random.rand(n_bars)

    # Generate timestamps (1-minute bars during RTH)
    base_ts = pd.Timestamp("2024-01-02 09:30:00", tz="US/Eastern")
    timestamps = []
    current = base_ts
    for _ in range(n_bars):
        timestamps.append(int(current.timestamp() * 1000))
        current += pd.Timedelta(minutes=1)
        # Skip to next day if past 16:00
        if current.hour >= 16:
            current = (current + pd.Timedelta(days=1)).replace(hour=9, minute=30)

    # Create mock RSI (slightly correlated with future returns for testing)
    rsi = 50 + 30 * np.tanh(returns * 50) + np.random.randn(n_bars) * 5

    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.random.randint(1000, 10000, n_bars),
            "rsi_14": rsi,
        }
    )


class TestComputeForwardReturn:
    """Tests for compute_forward_return function."""

    def test_basic_forward_return(self):
        """Test basic forward return calculation."""
        df = _create_test_df(100)
        fwd = compute_forward_return(df, horizon=5)

        assert len(fwd) == len(df)
        # Last 5 bars should be NaN
        assert fwd.iloc[-5:].isna().all()
        # Most others should have values
        assert fwd.iloc[:-10].notna().sum() > 0

    def test_variable_horizons(self):
        """Test different horizon values."""
        df = _create_test_df(100)

        for horizon in [1, 5, 10, 15, 30]:
            fwd = compute_forward_return(df, horizon=horizon)
            assert len(fwd) == len(df)
            # Last `horizon` bars should be NaN
            assert fwd.iloc[-horizon:].isna().all()

    def test_cross_day_masking(self):
        """Test that returns spanning day boundaries are masked."""
        df = _create_test_df(500)  # Multiple days
        fwd = compute_forward_return(df, horizon=15, mask_overnight=True)

        # Should have NaN values where horizon crosses day boundary
        # Not all values should be valid
        valid_count = fwd.notna().sum()
        assert valid_count < len(df) - 15


class TestComputeIndicatorReliability:
    """Tests for compute_indicator_reliability function."""

    def test_single_horizon(self):
        """Test IC computation for a single horizon."""
        df = _create_test_df(500)
        results, slope_results = compute_indicator_reliability(
            df=df,
            indicator_column="rsi_14",
            horizons=[10],
            include_slope=False,
        )

        assert len(results) == 1
        assert slope_results is None

        r = results[0]
        assert r.horizon == 10
        assert -1 <= r.mean_ic <= 1
        assert r.effective_n > 0
        assert isinstance(r.interpretation, str)

    def test_multiple_horizons(self):
        """Test IC computation across multiple horizons."""
        df = _create_test_df(500)
        results, _ = compute_indicator_reliability(
            df=df,
            indicator_column="rsi_14",
            horizons=[1, 5, 10, 15, 30],
            include_slope=False,
        )

        assert len(results) == 5
        horizons = [r.horizon for r in results]
        assert horizons == [1, 5, 10, 15, 30]

    def test_with_slope(self):
        """Test IC computation including slope analysis."""
        df = _create_test_df(500)
        results, slope_results = compute_indicator_reliability(
            df=df,
            indicator_column="rsi_14",
            horizons=[5, 10],
            include_slope=True,
        )

        assert len(results) == 2
        assert slope_results is not None
        assert len(slope_results) == 2

        # Slope IC should be different from raw IC
        # (at least in general - could be similar by chance)
        assert isinstance(slope_results[0].mean_ic, float)

    def test_daily_ic_series(self):
        """Test that daily IC values are returned."""
        df = _create_test_df(500)
        results, _ = compute_indicator_reliability(
            df=df,
            indicator_column="rsi_14",
            horizons=[10],
            include_slope=False,
        )

        r = results[0]
        assert len(r.daily_ic_values) > 0
        assert len(r.daily_ic_dates) == len(r.daily_ic_values)

    def test_missing_column_raises(self):
        """Test that missing indicator column raises ValueError."""
        df = _create_test_df(100)

        with pytest.raises(ValueError, match="not found"):
            compute_indicator_reliability(
                df=df,
                indicator_column="nonexistent",
                horizons=[10],
            )


class TestFindBestHorizon:
    """Tests for find_best_horizon function."""

    def test_finds_best(self):
        """Test that best horizon is identified."""
        results = [
            HorizonICAnalysis(
                horizon=1,
                mean_ic=0.01,
                t_stat=1.0,
                p_value=0.32,
                nw_t_stat=0.9,
                nw_p_value=0.37,
                effective_n=100,
                interpretation="Noise",
                daily_ic_values=[],
                daily_ic_dates=[],
            ),
            HorizonICAnalysis(
                horizon=10,
                mean_ic=0.04,
                t_stat=3.5,
                p_value=0.001,
                nw_t_stat=3.2,
                nw_p_value=0.002,
                effective_n=100,
                interpretation="Strong",
                daily_ic_values=[],
                daily_ic_dates=[],
            ),
            HorizonICAnalysis(
                horizon=30,
                mean_ic=0.02,
                t_stat=1.5,
                p_value=0.14,
                nw_t_stat=1.3,
                nw_p_value=0.20,
                effective_n=100,
                interpretation="Weak",
                daily_ic_values=[],
                daily_ic_dates=[],
            ),
        ]

        best = find_best_horizon(results)
        assert best == 10  # Horizon with highest |IC| and p < 0.10

    def test_returns_none_if_no_significant(self):
        """Test that None is returned if no horizon is significant."""
        results = [
            HorizonICAnalysis(
                horizon=5,
                mean_ic=0.005,
                t_stat=0.5,
                p_value=0.62,
                nw_t_stat=0.4,
                nw_p_value=0.69,
                effective_n=100,
                interpretation="Noise",
                daily_ic_values=[],
                daily_ic_dates=[],
            ),
        ]

        best = find_best_horizon(results)
        assert best is None


class TestFormatDisplayName:
    """Tests for format_indicator_display_name function."""

    def test_rsi(self):
        """Test RSI formatting."""
        name = format_indicator_display_name("rsi", {"length": 14})
        assert name == "RSI (14)"

    def test_macd(self):
        """Test MACD formatting."""
        name = format_indicator_display_name("macd", {"fast": 12, "slow": 26, "signal": 9})
        assert name == "MACD (12, 26, 9)"

    def test_ema(self):
        """Test EMA formatting."""
        name = format_indicator_display_name("ema", {"length": 20})
        assert name == "EMA (20)"

    def test_no_params(self):
        """Test indicator with no params."""
        name = format_indicator_display_name("obv", {})
        assert name == "OBV"


class TestGetIndicatorCategory:
    """Tests for get_indicator_category function."""

    def test_known_indicator(self):
        """Test category lookup for known indicator."""
        cat = get_indicator_category("rsi")
        assert cat == "momentum"

    def test_trend_indicator(self):
        """Test category lookup for trend indicator."""
        cat = get_indicator_category("ema")
        assert cat == "overlap"

    def test_unknown_indicator(self):
        """Test category lookup for unknown indicator."""
        cat = get_indicator_category("not_an_indicator")
        assert cat is None
