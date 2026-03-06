"""Tests for IV Pipeline Empirical Validation Study."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from app.research.validation.iv_pipeline_study import (
    StudyReport,
    TickerResult,
    _compute_realized_vol,
    print_report,
    run_iv_pipeline_study,
)


class TestRealizedVol:
    def test_trailing_rv_length(self) -> None:
        """Trailing RV series should have same length as input."""
        close = pd.Series(100 + np.cumsum(np.random.default_rng(42).normal(0, 1, 100)))
        rv = _compute_realized_vol(close, window=21, forward=False)
        assert len(rv) == 100

    def test_trailing_rv_warmup_nan(self) -> None:
        """First `window` values should be NaN."""
        close = pd.Series(100 + np.cumsum(np.random.default_rng(42).normal(0, 1, 100)))
        rv = _compute_realized_vol(close, window=21, forward=False)
        assert rv.iloc[:21].isna().all()
        assert rv.iloc[25:].notna().all()

    def test_forward_rv_tail_nan(self) -> None:
        """Forward RV should have NaN at the end."""
        close = pd.Series(100 + np.cumsum(np.random.default_rng(42).normal(0, 1, 100)))
        rv = _compute_realized_vol(close, window=21, forward=True)
        # Last 21 values should be NaN (no forward data)
        assert rv.iloc[-21:].isna().all()

    def test_rv_is_positive(self) -> None:
        """Realized volatility should be non-negative."""
        close = pd.Series(100 + np.cumsum(np.random.default_rng(42).normal(0, 1, 200)))
        rv = _compute_realized_vol(close, window=21, forward=False)
        valid = rv.dropna()
        assert (valid >= 0).all()

    def test_rv_annualized_scale(self) -> None:
        """Annualized RV should be in a reasonable range for typical stock data."""
        rng = np.random.default_rng(42)
        # Simulate ~20% annual vol: daily vol = 0.20/sqrt(252) ~= 0.0126
        daily_ret = rng.normal(0.0004, 0.0126, 500)
        close = pd.Series(100 * np.exp(np.cumsum(daily_ret)))
        rv = _compute_realized_vol(close, window=21, forward=False)
        valid = rv.dropna()
        mean_rv = valid.mean()
        # Should be roughly 0.10–0.40 (annualized)
        assert 0.05 < mean_rv < 0.50


class TestTickerResult:
    def test_default_values(self) -> None:
        r = TickerResult(ticker="TEST")
        assert r.ticker == "TEST"
        assert r.n_obs == 0
        assert r.vrp_p_value == 1.0

    def test_quintile_list(self) -> None:
        r = TickerResult(ticker="TEST", quintile_abs_ret=[0.01, 0.02, 0.03, 0.04, 0.05])
        assert len(r.quintile_abs_ret) == 5


class TestStudyReport:
    def test_default_all_fail(self) -> None:
        report = StudyReport()
        assert not report.fact1_pass
        assert not report.fact2_pass
        assert not report.fact3_pass

    def test_print_report_runs(self, capsys) -> None:
        """print_report should not crash even with minimal data."""
        report = StudyReport(
            tickers=["TEST"],
            start_date="2024-01-01",
            end_date="2024-12-31",
            results=[TickerResult(ticker="TEST", n_obs=5)],
        )
        print_report(report)
        captured = capsys.readouterr()
        assert "TEST" in captured.out
        assert "SKIPPED" in captured.out

    def test_print_report_with_data(self, capsys) -> None:
        """print_report should display all facts for tickers with data."""
        report = StudyReport(
            tickers=["SPY"],
            start_date="2024-01-01",
            end_date="2024-12-31",
            results=[TickerResult(
                ticker="SPY",
                n_obs=200,
                mean_iv=0.18,
                mean_rv=0.14,
                vrp_mean=0.04,
                vrp_t_stat=3.5,
                vrp_p_value=0.001,
                pct_iv_gt_rv=0.75,
                iv_fwd_rv_corr=0.65,
                iv_fwd_rv_p=0.0001,
                iv_fwd_rv_r2=0.42,
                quintile_abs_ret=[0.01, 0.015, 0.02, 0.025, 0.035],
                quintile_monotonic=True,
                spearman_iv_absret=0.35,
                spearman_iv_absret_p=0.001,
            )],
            fact1_pass=True,
            fact2_pass=True,
            fact3_pass=True,
        )
        print_report(report)
        captured = capsys.readouterr()
        assert "PASS" in captured.out
        assert "ALL FACTS CONFIRMED" in captured.out
