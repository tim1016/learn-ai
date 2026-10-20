"""Tests for IV series diagnostics."""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.research.options.diagnostics import MAX_MISSING_PCT, run_iv_diagnostics


def _make_iv_df(
    n_days: int = 100,
    missing_pct: float = 0.0,
    iv_mean: float = 0.25,
    iv_std: float = 0.05,
    discontinuities: int = 0,
) -> pd.DataFrame:
    """Create a synthetic IV DataFrame for testing."""
    np.random.seed(42)
    dates = pd.bdate_range(start="2024-01-01", periods=n_days)
    iv = np.random.normal(iv_mean, iv_std, n_days)
    iv = np.clip(iv, 0.05, 3.0)

    # Insert missing values
    n_missing = int(n_days * missing_pct)
    if n_missing > 0:
        missing_idx = np.random.choice(n_days, n_missing, replace=False)
        iv[missing_idx] = np.nan

    # Insert discontinuities
    if discontinuities > 0:
        for i in range(min(discontinuities, n_days - 1)):
            iv[i + 1] = iv[i] * 2.0  # 100% change

    return pd.DataFrame(
        {
            "date": dates.strftime("%Y-%m-%d"),
            "iv_30d_atm": iv,
            "dte_low": np.full(n_days, 25),
            "dte_high": np.full(n_days, 35),
        }
    )


class TestIvDiagnostics:
    """Test IV diagnostics validation."""

    def test_valid_series_passes_and_too_few_days_fails(self):
        # Use low iv_std to avoid random discontinuities
        df = _make_iv_df(n_days=100, missing_pct=0.0, iv_std=0.01)
        report = run_iv_diagnostics(df)
        assert report.valid is True, "valid series passes"
        assert report.missing_pct == 0.0, "valid series has no missing data"
        assert report.total_trading_days == 100, "valid series counts 100 trading days"
        assert report.valid_iv_days == 100, "valid series counts 100 valid IV days"

        df = _make_iv_df(n_days=10)
        report = run_iv_diagnostics(df)
        assert report.valid is False, "too few days fails (< 30 valid days)"

    def test_missing_pct_threshold(self):
        df = _make_iv_df(n_days=100, missing_pct=0.20)
        report = run_iv_diagnostics(df)
        assert report.valid is False, "high missing pct fails"
        assert report.missing_pct > MAX_MISSING_PCT, "high missing pct exceeds the threshold"
        assert any("Missing data" in w for w in report.warnings), "high missing pct warns about missing data"

        # 10% missing is below the 15% threshold.
        df = _make_iv_df(n_days=200, missing_pct=0.10)
        report = run_iv_diagnostics(df)
        assert report.missing_pct <= MAX_MISSING_PCT, "moderate missing pct is within the threshold"
        # Should still be valid with enough data points
        assert report.valid_iv_days >= 30, "moderate missing pct leaves enough valid days"

    def test_discontinuities_flagged(self):
        df = _make_iv_df(n_days=100, discontinuities=3)
        report = run_iv_diagnostics(df)
        assert report.discontinuities > 0
        assert any("IV change" in w for w in report.warnings)

    def test_unusable_input_is_invalid(self):
        df = pd.DataFrame()
        report = run_iv_diagnostics(df)
        assert report.valid is False, "empty dataframe is invalid"
        assert any("Empty" in w for w in report.warnings), "empty dataframe warns it is empty"

        report = run_iv_diagnostics(None)
        assert report.valid is False, "none input is invalid"

        df = pd.DataFrame({"date": ["2024-01-01"], "wrong_col": [0.25]})
        report = run_iv_diagnostics(df)
        assert report.valid is False, "missing column is invalid"
        assert any("not found" in w for w in report.warnings), "missing column warns the column was not found"

    def test_distribution_stats_and_date_coverage(self):
        df = _make_iv_df(n_days=100, iv_mean=0.25, iv_std=0.05)
        report = run_iv_diagnostics(df)
        assert report.iv_mean is not None, "distribution stats compute iv_mean"
        assert report.iv_std is not None, "distribution stats compute iv_std"
        assert report.iv_min is not None, "distribution stats compute iv_min"
        assert report.iv_max is not None, "distribution stats compute iv_max"
        assert abs(report.iv_mean - 0.25) < 0.05, "iv_mean is roughly the series mean"  # Rough check
        assert report.first_date is not None, "date coverage has a first date"
        assert report.last_date is not None, "date coverage has a last date"
        assert report.first_date < report.last_date, "date coverage first date precedes last date"
