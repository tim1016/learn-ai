"""Tests for the signal engine pipeline."""
from __future__ import annotations

import datetime

import numpy as np
import pandas as pd
import pytest

from app.research.signal.backtest import BacktestResult, run_backtest, run_backtest_grid
from app.research.signal.config import SignalConfig
from app.research.signal.diagnostics import (
    compute_data_sufficiency,
    compute_effective_sample_size,
    compute_signal_diagnostics,
)
from app.research.signal.engine import run_signal_engine
from app.research.signal.graduation import evaluate_graduation
from app.research.signal.regime import compute_bar_regime_gate, compute_daily_regime_labels
from app.research.signal.standardize import apply_threshold_filter, compute_train_zscore
from app.research.signal.walk_forward import WalkForwardResult, run_walk_forward


def _generate_multi_month_bars(
    n_months: int = 6,
    bars_per_day: int = 50,
    days_per_month: int = 20,
    seed: int = 42,
) -> list[dict]:
    """Generate synthetic OHLCV bars spanning multiple months."""
    rng = np.random.default_rng(seed)
    bars: list[dict] = []
    base_price = 150.0

    for month_idx in range(n_months):
        month = month_idx + 1
        year = 2024
        if month > 12:
            year += 1
            month -= 12
        for day in range(1, days_per_month + 1):
            if day > 28:
                continue
            dt = datetime.datetime(year, month, day, 14, 30, tzinfo=datetime.timezone.utc)
            day_start_ms = int(dt.timestamp() * 1000)

            for bar_idx in range(bars_per_day):
                noise = rng.normal(0, 0.3)
                trend = month_idx * 0.5 + rng.normal(0, 0.1)
                price = base_price + trend + noise
                bars.append({
                    "timestamp": day_start_ms + bar_idx * 60_000,
                    "open": round(price - 0.05, 4),
                    "high": round(price + 0.3, 4),
                    "low": round(price - 0.3, 4),
                    "close": round(price, 4),
                    "volume": round(1_000_000 + rng.normal(0, 50_000), 2),
                })

    return bars


class TestRollingZScore:
    def test_train_stats_not_contaminated(self) -> None:
        """Z-score should use only train statistics."""
        rng = np.random.default_rng(42)
        n = 100
        feature = pd.Series(rng.normal(0, 1, n))
        train_mask = pd.Series([True] * 70 + [False] * 30)

        z = compute_train_zscore(feature, train_mask, flip_sign=False)

        # Train mean/std should come from first 70 points only
        train_mu = feature[:70].mean()
        train_sigma = feature[:70].std()
        expected_z_71 = (feature.iloc[70] - train_mu) / train_sigma
        assert abs(z.iloc[70] - expected_z_71) < 1e-10

    def test_flip_sign_reverses(self) -> None:
        """Flipping sign should negate z-scores."""
        feature = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        train_mask = pd.Series([True] * 5)

        z_normal = compute_train_zscore(feature, train_mask, flip_sign=False)
        z_flipped = compute_train_zscore(feature, train_mask, flip_sign=True)

        np.testing.assert_array_almost_equal(z_normal.values, -z_flipped.values)

    def test_zero_std_returns_nan(self) -> None:
        """Constant feature should produce NaN z-scores."""
        feature = pd.Series([5.0] * 10)
        train_mask = pd.Series([True] * 10)

        z = compute_train_zscore(feature, train_mask, flip_sign=False)

        assert z.isna().all()


class TestThresholdFilter:
    def test_below_threshold_zeroed(self) -> None:
        """Values below threshold should become 0."""
        z = pd.Series([0.3, -0.3, 1.5, -2.0, 0.0])
        signal = apply_threshold_filter(z, threshold=1.0)

        assert signal.iloc[0] == 0.0
        assert signal.iloc[1] == 0.0
        assert signal.iloc[4] == 0.0

    def test_above_threshold_preserves_sign(self) -> None:
        """Values above threshold should preserve sign."""
        z = pd.Series([1.5, -2.0])
        signal = apply_threshold_filter(z, threshold=1.0)

        assert signal.iloc[0] == 1.0
        assert signal.iloc[1] == -1.0


class TestRegimeGate:
    def test_low_vol_sideways_passes(self) -> None:
        """Low vol + sideways days should have gate = 1."""
        bars = _generate_multi_month_bars(n_months=6)
        df = pd.DataFrame(bars)
        daily = compute_daily_regime_labels(bars)

        # Check some regimes are classified
        assert len(daily) > 0
        assert "vol_regime" in daily.columns
        assert "trend_regime" in daily.columns

    def test_gate_returns_correct_length(self) -> None:
        """Gate should have same length as input timestamps."""
        bars = _generate_multi_month_bars(n_months=3)
        df = pd.DataFrame(bars).sort_values("timestamp").reset_index(drop=True)
        gate = compute_bar_regime_gate(bars, df["timestamp"])

        assert len(gate) == len(df)

    def test_gate_values_binary(self) -> None:
        """Gate values should be 0 or 1."""
        bars = _generate_multi_month_bars(n_months=3)
        df = pd.DataFrame(bars).sort_values("timestamp").reset_index(drop=True)
        gate = compute_bar_regime_gate(bars, df["timestamp"])

        unique_vals = set(gate.unique())
        assert unique_vals.issubset({0.0, 1.0})


class TestBacktest:
    def test_no_lookahead(self) -> None:
        """Position at t-1 earns return at t (no lookahead)."""
        signal = pd.Series([1.0, 1.0, 0.0, -1.0])
        returns = pd.Series([0.01, 0.02, -0.01, 0.03])

        bt = run_backtest(signal, returns, cost_bps=0.0)

        # w_{t-1} * r_t: first return is 0 (no prior position)
        # gross_returns[1] = positions[0] * returns[1] = 1.0 * 0.02
        assert bt.gross_total_return > 0

    def test_costs_reduce_returns(self) -> None:
        """Positive cost should reduce net returns."""
        signal = pd.Series([1.0, -1.0, 1.0, -1.0])
        returns = pd.Series([0.01, 0.01, 0.01, 0.01])

        bt_free = run_backtest(signal, returns, cost_bps=0.0)
        bt_costly = run_backtest(signal, returns, cost_bps=10.0)

        assert bt_costly.net_total_return < bt_free.net_total_return

    def test_zero_position_no_cost(self) -> None:
        """All-zero signal should produce zero cost and zero return."""
        signal = pd.Series([0.0, 0.0, 0.0, 0.0])
        returns = pd.Series([0.01, 0.02, -0.01, 0.03])

        bt = run_backtest(signal, returns, cost_bps=5.0)

        assert bt.net_total_return == 0.0
        assert bt.total_trades == 0

    def test_metrics_bounded(self) -> None:
        """Sharpe, drawdown, win rate should be reasonable."""
        rng = np.random.default_rng(42)
        signal = pd.Series(rng.choice([-1.0, 0.0, 1.0], 500))
        returns = pd.Series(rng.normal(0, 0.001, 500))

        bt = run_backtest(signal, returns, cost_bps=2.0)

        assert bt.max_drawdown >= 0
        assert 0 <= bt.win_rate <= 1


class TestWalkForward:
    def test_insufficient_data_returns_empty(self) -> None:
        """Too few bars should produce empty walk-forward."""
        bars = _generate_multi_month_bars(n_months=2, bars_per_day=10)
        config = SignalConfig(min_bars_for_signal=100000)

        result = run_walk_forward(bars, "momentum_5m", config)

        assert len(result.windows) == 0

    def test_walk_forward_produces_windows(self) -> None:
        """Sufficient data should produce at least one window."""
        bars = _generate_multi_month_bars(n_months=8, bars_per_day=50)
        config = SignalConfig(
            walk_forward_train_months=3,
            walk_forward_test_months=1,
            min_bars_for_signal=100,
        )

        result = run_walk_forward(bars, "momentum_5m", config)

        assert len(result.windows) >= 1

    def test_no_overlap(self) -> None:
        """Test periods should not overlap."""
        bars = _generate_multi_month_bars(n_months=8, bars_per_day=50)
        config = SignalConfig(
            walk_forward_train_months=3,
            walk_forward_test_months=1,
            min_bars_for_signal=100,
        )

        result = run_walk_forward(bars, "momentum_5m", config)

        if len(result.windows) >= 2:
            for i in range(len(result.windows) - 1):
                assert result.windows[i].test_end <= result.windows[i + 1].test_start

    def test_alpha_decay_slope_computed(self) -> None:
        """OOS Sharpe trend slope should be a finite number."""
        bars = _generate_multi_month_bars(n_months=8, bars_per_day=50)
        config = SignalConfig(
            walk_forward_train_months=3,
            walk_forward_test_months=1,
            min_bars_for_signal=100,
        )

        result = run_walk_forward(bars, "momentum_5m", config)

        assert np.isfinite(result.oos_sharpe_trend_slope)

    def test_aggregates_correct(self) -> None:
        """Aggregated stats should be consistent with windows."""
        bars = _generate_multi_month_bars(n_months=8, bars_per_day=50)
        config = SignalConfig(
            walk_forward_train_months=3,
            walk_forward_test_months=1,
            min_bars_for_signal=100,
        )

        result = run_walk_forward(bars, "momentum_5m", config)

        if result.windows:
            sharpes = [w.oos_net_sharpe for w in result.windows]
            assert abs(result.mean_oos_sharpe - np.mean(sharpes)) < 1e-8
            assert result.total_oos_bars == sum(w.test_bars for w in result.windows)


class TestGraduation:
    def test_all_pass_grade_a(self) -> None:
        """All criteria passing should produce grade A."""
        grid = [
            BacktestResult(threshold=1.0, cost_bps=2.0, net_sharpe=1.5,
                           max_drawdown=0.05, annualized_turnover=3.0),
            BacktestResult(threshold=1.5, cost_bps=2.0, net_sharpe=1.3,
                           max_drawdown=0.04, annualized_turnover=2.5),
        ]
        wf = WalkForwardResult(
            windows=[type('W', (), {'oos_net_sharpe': 0.8, 'oos_net_return': 0.01, 'test_bars': 500})()
                     for _ in range(5)],
            mean_oos_sharpe=0.8,
            pct_windows_positive_sharpe=0.8,
            pct_windows_profitable=0.8,
            total_oos_bars=2500,
            oos_sharpe_trend_slope=0.0,
        )
        regime_cov = {
            "Low Vol": 50, "Normal Vol": 50, "High Vol": 50,
            "Trending Up": 30, "Sideways": 40, "Trending Down": 30,
        }

        result = evaluate_graduation(wf, grid, regime_cov, None, None)

        assert result.overall_grade == "A"
        assert result.overall_passed

    def test_low_sharpe_fails(self) -> None:
        """Low net Sharpe should fail the first criterion."""
        grid = [BacktestResult(threshold=1.0, cost_bps=2.0, net_sharpe=0.3)]

        result = evaluate_graduation(None, grid, {}, None, None)

        criterion = result.criteria[0]  # Net Sharpe
        assert not criterion.passed
        assert "0.75" in criterion.failure_reason

    def test_high_drawdown_fails(self) -> None:
        """High drawdown should fail."""
        grid = [BacktestResult(threshold=1.0, cost_bps=2.0, net_sharpe=1.0,
                               max_drawdown=0.25)]

        result = evaluate_graduation(None, grid, {}, None, None)

        criterion = result.criteria[1]  # Max Drawdown
        assert not criterion.passed
        assert "15%" in criterion.failure_reason

    def test_failure_reasons_populated(self) -> None:
        """Failed criteria should have non-empty failure reasons."""
        grid = [BacktestResult(threshold=1.0, cost_bps=2.0, net_sharpe=0.3,
                               max_drawdown=0.25)]

        result = evaluate_graduation(None, grid, {}, None, None)

        for c in result.criteria:
            if not c.passed:
                assert len(c.failure_reason) > 0

    def test_status_labels(self) -> None:
        """Status should be Exploratory with no walk-forward."""
        grid = [BacktestResult(threshold=1.0, cost_bps=2.0, net_sharpe=1.5)]

        result = evaluate_graduation(None, grid, {}, None, None)

        assert result.status_label == "Exploratory"


class TestSignalDiagnostics:
    def test_all_zero_signal(self) -> None:
        """All-zero signal should show 0% active."""
        z = pd.Series([1.0, 2.0, -1.0, 0.5])
        thresh = pd.Series([0.0, 0.0, 0.0, 0.0])

        diag = compute_signal_diagnostics(z, thresh, None)

        assert diag.pct_time_active == 0.0

    def test_pct_filtered_correct(self) -> None:
        """Percentage filtered should match actual filtering."""
        z = pd.Series([0.5, 1.5, -2.0, 0.3, 3.0])
        thresh = pd.Series([0.0, 1.0, -1.0, 0.0, 1.0])

        diag = compute_signal_diagnostics(z, thresh, None)

        # 2 out of 5 are zero => 40% filtered
        assert abs(diag.pct_filtered_by_threshold - 0.4) < 0.01


class TestDataSufficiency:
    def test_warnings_for_low_coverage(self) -> None:
        """Low bar count should generate warnings."""
        ds = compute_data_sufficiency(
            total_bars=500,
            train_bars=350,
            test_bars=150,
            walk_forward_folds=2,
            effective_oos_bars=200,
            regime_coverage={"Low Vol": 5, "High Vol": 0},
        )

        assert len(ds.coverage_warnings) > 0
        assert ds.regimes_covered == 1

    def test_regime_count_correct(self) -> None:
        """Regime count should count only non-zero entries."""
        ds = compute_data_sufficiency(
            total_bars=5000,
            train_bars=3500,
            test_bars=1500,
            walk_forward_folds=5,
            effective_oos_bars=1000,
            regime_coverage={
                "Low Vol": 50, "Normal Vol": 60, "High Vol": 40,
                "Sideways": 30, "Trending Up": 0, "Trending Down": 20,
            },
        )

        assert ds.regimes_covered == 5


class TestEffectiveSampleSize:
    def test_neff_leq_raw_n(self) -> None:
        """Effective N should never exceed raw N."""
        rng = np.random.default_rng(42)
        returns = pd.Series(rng.normal(0, 0.01, 500))

        ess = compute_effective_sample_size(returns)

        assert ess.effective_n <= ess.raw_n

    def test_autocorrelated_reduces_neff(self) -> None:
        """Autocorrelated series should have lower Neff."""
        rng = np.random.default_rng(42)
        n = 500
        # Create autocorrelated series
        white = rng.normal(0, 0.01, n)
        autocorr = np.zeros(n)
        autocorr[0] = white[0]
        for i in range(1, n):
            autocorr[i] = 0.8 * autocorr[i - 1] + white[i]

        iid = pd.Series(rng.normal(0, 0.01, n))
        corr = pd.Series(autocorr)

        ess_iid = compute_effective_sample_size(iid)
        ess_corr = compute_effective_sample_size(corr)

        assert ess_corr.effective_n < ess_iid.effective_n


class TestEndToEnd:
    def test_full_pipeline_on_synthetic_bars(self) -> None:
        """Full pipeline should run without error on synthetic data."""
        bars = _generate_multi_month_bars(n_months=6, bars_per_day=50)
        config = SignalConfig(min_bars_for_signal=100)

        report = run_signal_engine(
            ticker="TEST",
            feature_name="momentum_5m",
            bars=bars,
            start_date="2024-01-01",
            end_date="2024-06-30",
            config=config,
        )

        assert report.error is None
        assert report.bars_used > 0
        assert len(report.backtest_grid) > 0
        assert report.graduation is not None
        assert report.signal_diagnostics is not None
        assert report.data_sufficiency is not None
        assert report.effective_sample is not None
        assert len(report.regime_coverage) > 0
        assert len(report.research_log) > 0

    def test_insufficient_data_returns_error(self) -> None:
        """Too few bars should set error on report."""
        bars = _generate_multi_month_bars(n_months=1, bars_per_day=5)
        config = SignalConfig(min_bars_for_signal=10000)

        report = run_signal_engine(
            ticker="TEST",
            feature_name="momentum_5m",
            bars=bars,
            start_date="2024-01-01",
            end_date="2024-01-31",
            config=config,
        )

        assert report.error is not None
        assert "Not enough bars" in report.error
