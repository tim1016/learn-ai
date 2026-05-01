"""Tests for app.research.signal.diagnostics."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.research.signal.diagnostics import (
    DataSufficiency,
    EffectiveSampleSize,
    SignalDiagnostics,
    compute_data_sufficiency,
    compute_deflated_sharpe,
    compute_effective_sample_size,
    compute_joint_regime_coverage,
    compute_sharpe_ci,
    compute_signal_diagnostics,
)


def test_compute_signal_diagnostics_empty_returns_default():
    result = compute_signal_diagnostics(
        z_scores=pd.Series([np.nan, np.nan]),
        threshold_signal=pd.Series([np.nan, np.nan]),
        regime_gated_signal=None,
    )

    assert result == SignalDiagnostics()


def test_compute_signal_diagnostics_without_regime_gate():
    z = pd.Series([-2.0, 0.0, 1.0, 2.0])
    thresh = pd.Series([-1.0, 0.0, 0.0, 1.0])  # 2 active of 4 → 50% filtered

    result = compute_signal_diagnostics(z_scores=z, threshold_signal=thresh, regime_gated_signal=None)

    assert result.signal_mean == pytest.approx(0.25, abs=1e-12, rel=0)
    assert result.pct_time_active == pytest.approx(0.5, abs=1e-12, rel=0)
    assert result.pct_filtered_by_threshold == pytest.approx(0.5, abs=1e-12, rel=0)
    assert result.pct_gated_by_regime == 0.0
    assert result.avg_abs_signal == pytest.approx(1.25, abs=1e-12, rel=0)


def test_compute_signal_diagnostics_with_regime_gate():
    z = pd.Series([-2.0, 0.0, 1.0, 2.0])
    thresh = pd.Series([-1.0, 0.0, 0.0, 1.0])  # 2 active pre-gate
    gated = pd.Series([0.0, 0.0, 0.0, 1.0])  # 1 active post-gate

    result = compute_signal_diagnostics(z_scores=z, threshold_signal=thresh, regime_gated_signal=gated)

    assert result.pct_time_active == pytest.approx(0.25, abs=1e-12, rel=0)
    assert result.pct_gated_by_regime == pytest.approx(0.25, abs=1e-12, rel=0)


def test_compute_data_sufficiency_warns_on_short_history():
    result = compute_data_sufficiency(
        total_bars=500,
        train_bars=300,
        test_bars=200,
        walk_forward_folds=2,
        effective_oos_bars=100,
        regime_coverage={"Low Vol-Trending Up": 50, "Normal Vol-Sideways": 100},
    )

    assert isinstance(result, DataSufficiency)
    assert result.regimes_covered == 2
    assert any("Total bars" in w for w in result.coverage_warnings)
    assert any("walk-forward" in w for w in result.coverage_warnings)
    assert any("OOS" in w for w in result.coverage_warnings)
    assert any("regimes covered" in w for w in result.coverage_warnings)


def test_compute_data_sufficiency_warns_on_thin_regime():
    result = compute_data_sufficiency(
        total_bars=5000,
        train_bars=3000,
        test_bars=2000,
        walk_forward_folds=5,
        effective_oos_bars=1500,
        regime_coverage={
            "A": 500,
            "B": 500,
            "C": 500,
            "D": 500,
            "E": 500,
            "F": 10,  # thin
        },
    )

    assert any("only 10 observations" in w for w in result.coverage_warnings)


def test_compute_data_sufficiency_all_green_has_no_warnings():
    result = compute_data_sufficiency(
        total_bars=5000,
        train_bars=3500,
        test_bars=1500,
        walk_forward_folds=5,
        effective_oos_bars=1500,
        regime_coverage={str(i): 100 for i in range(6)},
    )

    assert result.coverage_warnings == []


def test_compute_effective_sample_size_short_series_returns_raw_n():
    result = compute_effective_sample_size(pd.Series([0.01, 0.02]))

    assert isinstance(result, EffectiveSampleSize)
    assert result.raw_n == 2
    assert result.effective_n == 2.0
    assert result.independent_bets == 2


def test_compute_effective_sample_size_iid_returns_near_n():
    rng = np.random.default_rng(seed=5)
    returns = pd.Series(rng.normal(size=1000))

    result = compute_effective_sample_size(returns)

    # iid should give N_eff close to N — some shrinkage is expected because
    # the implementation truncates the autocorrelation sum at rho_k < 0.05,
    # which still picks up finite-sample noise at short lags.
    assert result.raw_n == 1000
    assert result.effective_n == pytest.approx(1000.0, abs=200.0, rel=0)
    # Lag-1 autocorrelation must be near zero for iid noise.
    assert abs(result.autocorrelation_lag1) < 0.1


def test_compute_effective_sample_size_autocorrelated_reduces_n():
    rng = np.random.default_rng(seed=5)
    noise = rng.normal(size=1000)
    # Build a highly persistent AR(1): x_t = 0.9 * x_{t-1} + eps_t.
    x = np.zeros(1000)
    for i in range(1, 1000):
        x[i] = 0.9 * x[i - 1] + noise[i]

    result = compute_effective_sample_size(pd.Series(x))

    # Strong positive autocorrelation must shrink effective N well below raw.
    assert result.effective_n < result.raw_n / 2
    assert result.autocorrelation_lag1 > 0.5


def test_compute_effective_sample_size_zero_variance_returns_raw_n():
    result = compute_effective_sample_size(pd.Series([0.0] * 100))

    assert result.raw_n == 100
    assert result.effective_n == 100.0
    assert result.independent_bets == 100


# ─── Sharpe CI (Lo 2002) ────────────────────────────────────────────────────


def test_compute_sharpe_ci_brackets_point_estimate():
    """The 95 % CI should bracket the point estimate symmetrically."""
    rng = np.random.default_rng(seed=42)
    # 500 IID returns at a known per-bar Sharpe ≈ 0.05
    returns = rng.normal(loc=0.001, scale=0.02, size=500)

    result = compute_sharpe_ci(returns, n_eff=500.0)

    assert result.valid is True
    assert result.ci_lower < result.point < result.ci_upper
    # Symmetric interval: (upper − point) ≈ (point − lower) within 1 % rel.
    assert (result.ci_upper - result.point) == pytest.approx(
        result.point - result.ci_lower, rel=0.01
    )


def test_compute_sharpe_ci_invalid_when_too_few_samples():
    result = compute_sharpe_ci(np.array([0.01, 0.02]), n_eff=2.0)

    assert result.valid is False


def test_compute_sharpe_ci_invalid_on_zero_variance_returns():
    result = compute_sharpe_ci(np.zeros(100), n_eff=100.0)

    assert result.valid is False


def test_compute_sharpe_ci_widens_when_n_eff_shrinks():
    """Autocorrelation correction should widen the CI."""
    rng = np.random.default_rng(seed=7)
    returns = rng.normal(loc=0.001, scale=0.02, size=500)

    raw = compute_sharpe_ci(returns, n_eff=500.0)
    autocorrelated = compute_sharpe_ci(returns, n_eff=100.0)

    assert autocorrelated.se > raw.se
    assert (autocorrelated.ci_upper - autocorrelated.ci_lower) > (
        raw.ci_upper - raw.ci_lower
    )


# ─── Deflated Sharpe (Bailey & López de Prado 2014) ────────────────────────


def test_compute_deflated_sharpe_more_trials_lowers_dsr():
    """Searching across more configurations should deflate the Sharpe more."""
    rng = np.random.default_rng(seed=11)
    returns = rng.normal(loc=0.0008, scale=0.02, size=2000)
    sr_annual = 1.0  # mid-range annualised Sharpe

    one_trial = compute_deflated_sharpe(
        selected_sharpe_annual=sr_annual,
        bar_returns=returns,
        n_trials=1,
        n_eff=2000.0,
    )
    many_trials = compute_deflated_sharpe(
        selected_sharpe_annual=sr_annual,
        bar_returns=returns,
        n_trials=200,
        n_eff=2000.0,
    )

    assert one_trial.valid is True
    assert many_trials.valid is True
    assert one_trial.dsr_probability >= many_trials.dsr_probability
    assert many_trials.expected_max_under_null > one_trial.expected_max_under_null


def test_compute_deflated_sharpe_invalid_with_insufficient_data():
    result = compute_deflated_sharpe(
        selected_sharpe_annual=0.5,
        bar_returns=np.array([0.01, 0.02]),
        n_trials=10,
        n_eff=2.0,
    )

    assert result.valid is False


def test_compute_deflated_sharpe_reports_n_trials_and_moments():
    rng = np.random.default_rng(seed=23)
    returns = rng.normal(loc=0.0, scale=0.02, size=1000)

    result = compute_deflated_sharpe(
        selected_sharpe_annual=0.6,
        bar_returns=returns,
        n_trials=16,
        n_eff=1000.0,
    )

    assert result.n_trials == 16
    assert result.raw_sharpe == pytest.approx(0.6, abs=1e-12)
    # Skew of standard-normal-ish returns is close to zero.
    assert abs(result.skewness) < 0.5
    # Pearson kurtosis (not excess) of normal returns ≈ 3.
    assert 2.0 < result.kurtosis < 4.0


# ─── Joint regime coverage ──────────────────────────────────────────────────


def _make_daily_regimes(rows: list[tuple[str, str]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["vol_regime", "trend_regime"])


def test_compute_joint_regime_coverage_counts_joint_buckets_correctly():
    """The joint coverage must be a true (vol × trend) cross-tab, not a marginal projection."""
    df = _make_daily_regimes(
        [
            ("Low Vol", "Trending Up"),
            ("Low Vol", "Trending Up"),
            ("Low Vol", "Sideways"),
            ("Normal Vol", "Trending Down"),
        ]
    )

    buckets = compute_joint_regime_coverage(df, n_eff=1000.0, pct_active=0.10)

    by_key = {(b.vol_label, b.trend_label): b for b in buckets}
    assert by_key[("Low Vol", "Trending Up")].days == 2
    assert by_key[("Low Vol", "Sideways")].days == 1
    assert by_key[("Normal Vol", "Trending Down")].days == 1


def test_compute_joint_regime_coverage_badge_pass_when_trades_exceed_threshold():
    """A bucket with ≥ 30 effective trades should earn a Pass badge."""
    # 100 days, all in one bucket. With N_eff=100,000 and pct_active=0.10:
    # trades = 100,000 * 0.10 * (100/100) = 10,000 — well over 30.
    df = _make_daily_regimes([("Low Vol", "Trending Up")] * 100)

    buckets = compute_joint_regime_coverage(df, n_eff=100_000.0, pct_active=0.10)

    assert len(buckets) == 1
    assert buckets[0].badge == "Pass"
    assert buckets[0].effective_trades > 30


def test_compute_joint_regime_coverage_badge_sparse_for_thin_buckets():
    """Buckets with positive but < 30 effective trades earn Sparse."""
    # 100 days, all in one bucket, very low N_eff and pct_active:
    # trades = 50 * 0.05 * 1.0 = 2.5 → sparse.
    df = _make_daily_regimes([("Low Vol", "Sideways")] * 100)

    buckets = compute_joint_regime_coverage(df, n_eff=50.0, pct_active=0.05)

    assert buckets[0].badge == "Sparse"
    assert 0 < buckets[0].effective_trades < 30


def test_compute_joint_regime_coverage_handles_empty_dataframe():
    result = compute_joint_regime_coverage(
        pd.DataFrame(columns=["vol_regime", "trend_regime"]),
        n_eff=1000.0,
        pct_active=0.1,
    )

    assert result == []
