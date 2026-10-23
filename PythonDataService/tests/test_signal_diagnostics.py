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


def test_compute_signal_diagnostics_regime_gate():
    z = pd.Series([-2.0, 0.0, 1.0, 2.0])
    thresh = pd.Series([-1.0, 0.0, 0.0, 1.0])  # 2 active of 4 → 50% filtered
    gated = pd.Series([0.0, 0.0, 0.0, 1.0])  # 1 active post-gate

    ungated = compute_signal_diagnostics(z_scores=z, threshold_signal=thresh, regime_gated_signal=None)
    with_gate = compute_signal_diagnostics(z_scores=z, threshold_signal=thresh, regime_gated_signal=gated)

    assert ungated.signal_mean == pytest.approx(0.25, abs=1e-12, rel=0), "without regime gate: signal mean"
    assert ungated.pct_time_active == pytest.approx(0.5, abs=1e-12, rel=0), "without regime gate: pct time active"
    assert ungated.pct_filtered_by_threshold == pytest.approx(0.5, abs=1e-12, rel=0), (
        "without regime gate: pct filtered by threshold"
    )
    assert ungated.pct_gated_by_regime == 0.0, "without regime gate: nothing gated by regime"
    assert ungated.avg_abs_signal == pytest.approx(1.25, abs=1e-12, rel=0), "without regime gate: avg abs signal"
    assert with_gate.pct_time_active == pytest.approx(0.25, abs=1e-12, rel=0), "with regime gate: pct time active"
    assert with_gate.pct_gated_by_regime == pytest.approx(0.25, abs=1e-12, rel=0), (
        "with regime gate: pct gated by regime"
    )


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


@pytest.mark.parametrize(
    "values",
    [
        pytest.param((0.01, 0.02), id="short_series_returns_raw_n"),
        pytest.param((0.0,) * 100, id="zero_variance_returns_raw_n"),
    ],
)
def test_compute_effective_sample_size_degenerate_returns_raw_n(values):
    n = len(values)

    result = compute_effective_sample_size(pd.Series(list(values)))

    assert isinstance(result, EffectiveSampleSize), "result is an EffectiveSampleSize"
    assert result.raw_n == n, "raw_n is the series length"
    assert result.effective_n == float(n), "effective_n falls back to the series length"
    assert result.independent_bets == n, "independent_bets falls back to the series length"


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


# ─── Sharpe CI (Lo 2002) ────────────────────────────────────────────────────


def test_compute_sharpe_ci_brackets_point_estimate():
    """The 95 % CI should bracket the point estimate symmetrically."""
    rng = np.random.default_rng(seed=42)
    # 500 IID returns at a known per-bar Sharpe ≈ 0.05
    returns = rng.normal(loc=0.001, scale=0.02, size=500)

    result = compute_sharpe_ci(returns)

    assert result.valid is True
    assert result.ci_lower < result.point < result.ci_upper
    # Symmetric interval: (upper − point) ≈ (point − lower) within 1 % rel.
    assert (result.ci_upper - result.point) == pytest.approx(
        result.point - result.ci_lower, rel=0.01
    )


@pytest.mark.parametrize(
    ("returns", "n_eff"),
    [
        pytest.param((0.01, 0.02), 2.0, id="invalid_when_too_few_samples"),
        pytest.param((0.0,) * 100, 100.0, id="invalid_on_zero_variance_returns"),
    ],
)
def test_compute_sharpe_ci_invalid_returns(returns, n_eff):
    result = compute_sharpe_ci(np.array(returns))

    assert result.valid is False


def test_hac_accounts_for_serial_dependence_in_both_moments():
    returns = np.repeat(np.array([-.03, .01, .04, -.01] * 30), 10)
    uncorrelated = compute_sharpe_ci(returns, hac_lag=0)
    correlated = compute_sharpe_ci(returns, hac_lag=9)
    assert correlated.valid
    assert correlated.se > uncorrelated.se
    assert correlated.point == pytest.approx(float(returns.mean()/returns.std(ddof=1)*np.sqrt(98280)), abs=1e-12)


def test_hac_does_not_bridge_segments():
    returns = np.array([-.02]*10 + [.03]*10)
    pooled = compute_sharpe_ci(returns, hac_lag=5)
    segmented = compute_sharpe_ci(returns, hac_lag=5, segment_lengths=[10, 10])
    assert pooled.valid and segmented.valid
    assert pooled.se != segmented.se
    assert segmented.segment_count == 2


@pytest.mark.parametrize("returns", [[1., 2., np.nan, 4.], [1., np.inf, 2., 3.]])
def test_inference_rejects_nonfinite_without_dropping_alignment(returns):
    assert not compute_sharpe_ci(np.array(returns)).valid
    assert not compute_deflated_sharpe(np.array([returns]), 0).valid


def test_hac_rejects_invalid_segment_partition():
    with pytest.raises(ValueError, match="partition"):
        compute_sharpe_ci(np.arange(10.), segment_lengths=[5, 4])
    result = compute_sharpe_ci(np.arange(10.), hac_lag=5, segment_lengths=[5, 5])
    assert not result.valid
    assert result.unavailable_reason


def test_dsr_single_trial_and_identical_trials_have_zero_null_maximum():
    returns = np.random.default_rng(11).normal(.001, .02, 100)
    single = compute_deflated_sharpe(returns[None, :], 0)
    duplicate = compute_deflated_sharpe(np.tile(returns, (4, 1)), 0)
    assert single.valid and duplicate.valid
    assert duplicate.effective_trials == 1
    assert duplicate.expected_max_under_null == 0
    assert duplicate.dsr_probability == pytest.approx(single.dsr_probability, abs=1e-12)


def test_dsr_reports_unavailable_for_constant_trial_or_small_effective_count():
    returns = np.random.default_rng(12).normal(0, .02, 100)
    constant = compute_deflated_sharpe(np.stack([returns, np.zeros(100)]), 0)
    assert not constant.valid
    assert "constant" in constant.unavailable_reason
    correlated = compute_deflated_sharpe(np.stack([returns, returns + np.linspace(0, .001, 100)]), 0)
    assert 1 < correlated.effective_trials < 2
    assert not correlated.valid
    assert "between one and two" in correlated.unavailable_reason


def test_dsr_uses_observed_trial_spread_and_raw_sample_length():
    trials = np.random.default_rng(23).normal(0, .02, (16, 1000))
    result = compute_deflated_sharpe(trials, 0, bars_per_year=1)
    assert result.valid
    assert result.n_trials == 16
    assert result.sample_size == 1000
    assert result.trial_sharpe_std == pytest.approx(float(np.std(trials.mean(axis=1)/trials.std(axis=1, ddof=1), ddof=1)), abs=1e-12)
    assert result.raw_sharpe == pytest.approx(float(trials[0].mean()/trials[0].std(ddof=1)), abs=1e-12)
    assert 2 < result.kurtosis < 4


@pytest.mark.parametrize("value", [.01, -.01, .1])
def test_constant_nonzero_returns_are_unavailable_and_json_safe(value):
    import json
    from dataclasses import asdict

    from app.research.signal.sharpe import compute_iid_sharpe_ci

    returns = np.full(100, value)
    results = [compute_sharpe_ci(returns), compute_iid_sharpe_ci(returns),
               compute_deflated_sharpe(returns[None, :], 0)]
    for result in results:
        assert not result.valid
        assert "constant" in result.unavailable_reason
        json.dumps(asdict(result), allow_nan=False)


def test_dsr_unavailable_moments_do_not_leak_nan_into_report():
    import json
    from dataclasses import asdict

    returns = np.ones(100)
    returns[-1] = np.nextafter(1., 2.)
    # Distinct observations, but the variance is too small relative to the
    # mean for SciPy to estimate higher moments reliably.
    result = compute_deflated_sharpe(returns[None, :], 0)
    assert not result.valid
    json.dumps(asdict(result), allow_nan=False)


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


@pytest.mark.parametrize(
    ("trend", "n_eff", "pct_active", "badge", "trades_above", "trades_below"),
    [
        # A bucket with ≥ 30 effective trades should earn a Pass badge.
        # 100 days, all in one bucket. With N_eff=100,000 and pct_active=0.10:
        # trades = 100,000 * 0.10 * (100/100) = 10,000 — well over 30.
        pytest.param("Trending Up", 100_000.0, 0.10, "Pass", 30, float("inf"), id="badge_pass_when_trades_exceed_threshold"),
        # Buckets with positive but < 30 effective trades earn Sparse.
        # 100 days, all in one bucket, very low N_eff and pct_active:
        # trades = 50 * 0.05 * 1.0 = 2.5 → sparse.
        pytest.param("Sideways", 50.0, 0.05, "Sparse", 0, 30, id="badge_sparse_for_thin_buckets"),
    ],
)
def test_compute_joint_regime_coverage_badge(trend, n_eff, pct_active, badge, trades_above, trades_below):
    df = _make_daily_regimes([("Low Vol", trend)] * 100)

    buckets = compute_joint_regime_coverage(df, n_eff=n_eff, pct_active=pct_active)

    assert len(buckets) == 1, "all days land in one bucket"
    assert buckets[0].badge == badge, "badge follows the effective trades"
    assert trades_above < buckets[0].effective_trades < trades_below, "effective trades sit in the badge's range"


def test_compute_joint_regime_coverage_handles_empty_dataframe():
    result = compute_joint_regime_coverage(
        pd.DataFrame(columns=["vol_regime", "trend_regime"]),
        n_eff=1000.0,
        pct_active=0.1,
    )

    assert result == []
