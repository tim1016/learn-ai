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
    compute_effective_sample_size,
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
