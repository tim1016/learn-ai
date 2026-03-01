"""Tests for options research runner — integration with IC pipeline using synthetic IV data."""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.research.options_runner import run_options_feature_research, _compute_daily_forward_return


def _make_synthetic_data(
    n_days: int = 200,
    iv_mean: float = 0.25,
    seed: int = 42,
) -> tuple[list[dict], list[dict]]:
    """Create aligned synthetic IV data and stock daily bars."""
    np.random.seed(seed)
    dates = pd.bdate_range(start="2024-01-01", periods=n_days)

    # Stock bars
    close = 100.0 + np.cumsum(np.random.normal(0.01, 1.0, n_days))
    close = np.maximum(close, 10.0)  # Ensure positive

    stock_bars = []
    for i, dt in enumerate(dates):
        ts = int(dt.timestamp() * 1000)
        c = float(close[i])
        stock_bars.append({
            "timestamp": ts,
            "open": c * 0.999,
            "high": c * 1.005,
            "low": c * 0.995,
            "close": c,
            "volume": float(np.random.randint(100000, 1000000)),
        })

    # IV data — slight negative correlation with returns for realism
    iv = np.full(n_days, iv_mean, dtype=float)
    for i in range(1, n_days):
        ret = (close[i] - close[i - 1]) / close[i - 1]
        iv[i] = iv[i - 1] - ret * 0.5 + np.random.normal(0, 0.01)
    iv = np.clip(iv, 0.10, 0.80)

    iv_data = []
    for i, dt in enumerate(dates):
        iv_data.append({
            "date": dt.strftime("%Y-%m-%d"),
            "atm_iv": float(iv[i]),
            "iv_otm_put": float(iv[i] * 1.15),
            "iv_otm_call": float(iv[i] * 0.90),
            "stock_close": float(close[i]),
        })

    return iv_data, stock_bars


class TestComputeDailyForwardReturn:
    """Test daily forward return computation."""

    def test_directional_return(self):
        bars = [
            {"timestamp": i * 86400000, "open": 100, "high": 105, "low": 95, "close": 100 + i, "volume": 1000}
            for i in range(10)
        ]
        target = _compute_daily_forward_return(bars, "directional")
        # Should be ln(close_{t+1}/close_t)
        assert len(target) == 10
        assert pd.notna(target.iloc[0])
        assert pd.isna(target.iloc[-1])  # No forward return for last bar

    def test_volatility_target(self):
        bars = [
            {"timestamp": i * 86400000, "open": 100, "high": 105, "low": 95, "close": 100 + np.sin(i), "volume": 1000}
            for i in range(20)
        ]
        target = _compute_daily_forward_return(bars, "volatility")
        assert len(target) == 20
        # Last 5 should be NaN (forward-looking)
        assert target.iloc[-5:].isna().all()

    def test_abs_return(self):
        bars = [
            {"timestamp": i * 86400000, "open": 100, "high": 105, "low": 95, "close": 100 + i, "volume": 1000}
            for i in range(10)
        ]
        target = _compute_daily_forward_return(bars, "abs_return")
        # All non-NaN values should be >= 0
        valid = target.dropna()
        assert (valid >= 0).all()

    def test_unknown_target_raises(self):
        bars = [{"timestamp": 0, "open": 100, "high": 105, "low": 95, "close": 100, "volume": 1000}]
        with pytest.raises(ValueError, match="Unknown target_type"):
            _compute_daily_forward_return(bars, "invalid")


class TestOptionsRunner:
    """Integration tests for options feature research runner."""

    def test_iv_rank_60_directional(self):
        """Run iv_rank_60 with directional target — should produce valid report."""
        iv_data, stock_bars = _make_synthetic_data(n_days=200)

        report = run_options_feature_research(
            ticker="SPY",
            feature_name="iv_rank_60",
            iv_data=iv_data,
            stock_daily_bars=stock_bars,
            start_date="2024-01-01",
            end_date="2024-10-01",
        )

        assert report.error is None
        assert report.ticker == "SPY"
        assert report.feature_name == "iv_rank_60"
        assert report.bars_used > 0
        # IC values should exist
        assert len(report.ic_values) > 0
        # Stats should be populated
        assert report.mean_ic != 0.0 or report.ic_p_value <= 1.0

    def test_iv_30d_raw(self):
        """Run iv_30d (raw IV) — baseline data validation."""
        iv_data, stock_bars = _make_synthetic_data(n_days=150)

        report = run_options_feature_research(
            ticker="AAPL",
            feature_name="iv_30d",
            iv_data=iv_data,
            stock_daily_bars=stock_bars,
            start_date="2024-01-01",
            end_date="2024-07-01",
        )

        assert report.error is None
        assert report.bars_used > 0

    def test_log_skew(self):
        """Run log_skew feature."""
        iv_data, stock_bars = _make_synthetic_data(n_days=150)

        report = run_options_feature_research(
            ticker="QQQ",
            feature_name="log_skew",
            iv_data=iv_data,
            stock_daily_bars=stock_bars,
            start_date="2024-01-01",
            end_date="2024-07-01",
        )

        assert report.error is None

    def test_volatility_target(self):
        """Run with volatility target type."""
        iv_data, stock_bars = _make_synthetic_data(n_days=200)

        report = run_options_feature_research(
            ticker="SPY",
            feature_name="iv_30d",
            iv_data=iv_data,
            stock_daily_bars=stock_bars,
            start_date="2024-01-01",
            end_date="2024-10-01",
            target_type="volatility",
        )

        assert report.error is None

    def test_insufficient_data_fails(self):
        """Too few data points should fail with error."""
        iv_data, stock_bars = _make_synthetic_data(n_days=10)

        report = run_options_feature_research(
            ticker="SPY",
            feature_name="iv_rank_60",
            iv_data=iv_data,
            stock_daily_bars=stock_bars,
            start_date="2024-01-01",
            end_date="2024-01-15",
        )

        assert report.error is not None
        assert report.passed_validation is False

    def test_bad_iv_data_fails_diagnostics(self):
        """IV data with too many missing values should fail diagnostics."""
        dates = pd.bdate_range(start="2024-01-01", periods=50)
        iv_data = [
            {"date": dt.strftime("%Y-%m-%d"), "atm_iv": None, "iv_otm_put": None,
             "iv_otm_call": None, "stock_close": 100.0}
            for dt in dates
        ]
        stock_bars = [
            {"timestamp": int(dt.timestamp() * 1000), "open": 100, "high": 105,
             "low": 95, "close": 100, "volume": 1000}
            for dt in dates
        ]

        report = run_options_feature_research(
            ticker="SPY",
            feature_name="iv_30d",
            iv_data=iv_data,
            stock_daily_bars=stock_bars,
            start_date="2024-01-01",
            end_date="2024-03-01",
        )

        assert report.error is not None
        assert "diagnostics" in report.error.lower() or "missing" in report.error.lower() or "valid" in report.error.lower()

    def test_report_has_stationarity(self):
        """Report should include stationarity test results."""
        iv_data, stock_bars = _make_synthetic_data(n_days=200)

        report = run_options_feature_research(
            ticker="SPY",
            feature_name="iv_rank_60",
            iv_data=iv_data,
            stock_daily_bars=stock_bars,
            start_date="2024-01-01",
            end_date="2024-10-01",
        )

        assert report.adf_pvalue is not None
        assert report.kpss_pvalue is not None

    def test_report_has_quantile_bins(self):
        """Report should include quantile analysis."""
        iv_data, stock_bars = _make_synthetic_data(n_days=200)

        report = run_options_feature_research(
            ticker="SPY",
            feature_name="iv_rank_60",
            iv_data=iv_data,
            stock_daily_bars=stock_bars,
            start_date="2024-01-01",
            end_date="2024-10-01",
        )

        assert len(report.quantile_bins) > 0

    def test_report_has_robustness(self):
        """Report should include robustness analysis when enough data."""
        iv_data, stock_bars = _make_synthetic_data(n_days=200)

        report = run_options_feature_research(
            ticker="SPY",
            feature_name="iv_rank_60",
            iv_data=iv_data,
            stock_daily_bars=stock_bars,
            start_date="2024-01-01",
            end_date="2024-10-01",
        )

        if len(report.ic_values) >= 2:
            assert report.robustness is not None
