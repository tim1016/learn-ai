"""Tests for IV series diagnostics."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.research.options.diagnostics import run_iv_diagnostics, MAX_MISSING_PCT


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

    return pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "iv_30d_atm": iv,
        "dte_low": np.full(n_days, 25),
        "dte_high": np.full(n_days, 35),
    })


class TestIvDiagnostics:
    """Test IV diagnostics validation."""

    def test_valid_series_passes(self):
        # Use low iv_std to avoid random discontinuities
        df = _make_iv_df(n_days=100, missing_pct=0.0, iv_std=0.01)
        report = run_iv_diagnostics(df)
        assert report.valid is True
        assert report.missing_pct == 0.0
        assert report.total_trading_days == 100
        assert report.valid_iv_days == 100

    def test_high_missing_pct_fails(self):
        df = _make_iv_df(n_days=100, missing_pct=0.20)
        report = run_iv_diagnostics(df)
        assert report.valid is False
        assert report.missing_pct > MAX_MISSING_PCT
        assert any("Missing data" in w for w in report.warnings)

    def test_too_few_days_fails(self):
        df = _make_iv_df(n_days=10)
        report = run_iv_diagnostics(df)
        assert report.valid is False  # < 30 valid days

    def test_discontinuities_flagged(self):
        df = _make_iv_df(n_days=100, discontinuities=3)
        report = run_iv_diagnostics(df)
        assert report.discontinuities > 0
        assert any("IV change" in w for w in report.warnings)

    def test_empty_dataframe(self):
        df = pd.DataFrame()
        report = run_iv_diagnostics(df)
        assert report.valid is False
        assert any("Empty" in w for w in report.warnings)

    def test_none_input(self):
        report = run_iv_diagnostics(None)
        assert report.valid is False

    def test_distribution_stats_computed(self):
        df = _make_iv_df(n_days=100, iv_mean=0.25, iv_std=0.05)
        report = run_iv_diagnostics(df)
        assert report.iv_mean is not None
        assert report.iv_std is not None
        assert report.iv_min is not None
        assert report.iv_max is not None
        assert abs(report.iv_mean - 0.25) < 0.05  # Rough check

    def test_date_coverage(self):
        df = _make_iv_df(n_days=100)
        report = run_iv_diagnostics(df)
        assert report.first_date is not None
        assert report.last_date is not None
        assert report.first_date < report.last_date

    def test_moderate_missing_passes(self):
        """10% missing is below the 15% threshold."""
        df = _make_iv_df(n_days=200, missing_pct=0.10)
        report = run_iv_diagnostics(df)
        assert report.missing_pct <= MAX_MISSING_PCT
        # Should still be valid with enough data points
        assert report.valid_iv_days >= 30

    def test_missing_column_warns(self):
        df = pd.DataFrame({"date": ["2024-01-01"], "wrong_col": [0.25]})
        report = run_iv_diagnostics(df)
        assert report.valid is False
        assert any("not found" in w for w in report.warnings)
