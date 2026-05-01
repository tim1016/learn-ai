"""Signal diagnostics, data sufficiency, and effective sample size.

Also hosts the inferential helpers used by the graduation ladder:

* `compute_sharpe_ci` — Lo (2002) standard error of the annualized Sharpe,
  using the autocorrelation-adjusted effective sample size as the
  denominator. Drives the headline "OOS Sharpe = X ± Y, 95% CI [..]" on
  the verdict block.
* `compute_deflated_sharpe` — Bailey & López de Prado (2014) Deflated
  Sharpe Ratio, used to deflate the in-sample grid's best Sharpe for
  selection bias across N grid trials.
* `compute_joint_regime_coverage` — replaces the marginal day counts with
  a true joint (vol × trend) bucket count plus an estimate of effective
  independent trades per bucket, so the "regime coverage" panel reflects
  decision-relevant sample size, not bars-of-data.

See `docs/signal-engine-authority.md` § 4 for the authority on every
formula in this module.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats


@dataclass
class SignalDiagnostics:
    """Diagnostic metrics for the generated signal."""

    signal_mean: float = 0.0
    signal_std: float = 0.0
    pct_time_active: float = 0.0
    avg_abs_signal: float = 0.0
    pct_filtered_by_threshold: float = 0.0
    pct_gated_by_regime: float = 0.0


@dataclass
class DataSufficiency:
    """Data sufficiency assessment."""

    total_bars: int = 0
    train_bars: int = 0
    test_bars: int = 0
    walk_forward_folds: int = 0
    effective_oos_bars: int = 0
    regimes_covered: int = 0
    regime_coverage: dict[str, int] = field(default_factory=dict)
    coverage_warnings: list[str] = field(default_factory=list)


@dataclass
class EffectiveSampleSize:
    """Autocorrelation-adjusted sample size."""

    raw_n: int = 0
    effective_n: float = 0.0
    autocorrelation_lag1: float = 0.0
    independent_bets: int = 0
    max_lag_used: int = 0
    rho_sum: float = 0.0


@dataclass
class SharpeCi:
    """Lo (2002) confidence interval for the annualized Sharpe ratio.

    Reported on the combined out-of-sample equity curve. A CI that
    straddles zero means the Sharpe is statistically indistinguishable
    from noise at the requested confidence level.
    """

    point: float = 0.0
    se: float = 0.0
    ci_lower: float = 0.0
    ci_upper: float = 0.0
    confidence_level: float = 0.95
    n_eff_used: float = 0.0
    valid: bool = False
    """False when N_eff is too small to compute a meaningful CI."""


@dataclass
class DeflatedSharpe:
    """Bailey & López de Prado (2014) Deflated Sharpe Ratio.

    Reports the probability that the observed maximum Sharpe across
    `n_trials` grid configurations would have been achieved under a
    null hypothesis of zero true Sharpe. Values below 0.5 mean the
    headline Sharpe is plausibly the result of selection bias rather
    than true edge.
    """

    raw_sharpe: float = 0.0
    expected_max_under_null: float = 0.0
    """SR_0 in annualized units; the expected maximum under the null."""
    dsr_probability: float = 0.0
    """P(true Sharpe > 0 | observed max Sharpe, n_trials). Range [0, 1]."""
    n_trials: int = 0
    skewness: float = 0.0
    kurtosis: float = 0.0
    valid: bool = False


@dataclass
class RegimeBucket:
    """A single (vol × trend) cell of the joint regime coverage grid."""

    vol_label: str = ""
    trend_label: str = ""
    days: int = 0
    """Calendar days falling into this joint bucket."""
    effective_trades: float = 0.0
    """Estimated number of independent trades in this bucket. Computed as
    `N_eff * pct_active * (days / total_days)` — see authority doc § 4.11."""
    badge: str = "Empty"
    """One of: ``Pass`` (≥ 30 trades), ``Sparse`` (1–29), ``Empty`` (0)."""


def compute_signal_diagnostics(
    z_scores: pd.Series,
    threshold_signal: pd.Series,
    regime_gated_signal: pd.Series | None,
) -> SignalDiagnostics:
    """Compute signal-level diagnostics."""
    clean_z = z_scores.dropna()
    clean_thresh = threshold_signal.dropna()

    total = len(clean_z)
    if total == 0:
        return SignalDiagnostics()

    signal_mean = float(clean_z.mean())
    signal_std = float(clean_z.std()) if len(clean_z) > 1 else 0.0

    # % time active after threshold
    active_after_threshold = float((clean_thresh != 0).sum())
    pct_filtered = 1.0 - active_after_threshold / total

    # Final activity after regime gate
    if regime_gated_signal is not None:
        clean_gated = regime_gated_signal.dropna()
        active_final = float((clean_gated != 0).sum())
        pct_gated = (active_after_threshold - active_final) / total if total > 0 else 0.0
        pct_active = active_final / total
    else:
        pct_gated = 0.0
        pct_active = active_after_threshold / total

    avg_abs = float(clean_z.abs().mean())

    return SignalDiagnostics(
        signal_mean=signal_mean,
        signal_std=signal_std,
        pct_time_active=pct_active,
        avg_abs_signal=avg_abs,
        pct_filtered_by_threshold=pct_filtered,
        pct_gated_by_regime=pct_gated,
    )


def compute_data_sufficiency(
    total_bars: int,
    train_bars: int,
    test_bars: int,
    walk_forward_folds: int,
    effective_oos_bars: int,
    regime_coverage: dict[str, int],
) -> DataSufficiency:
    """Assess data sufficiency and generate warnings."""
    regimes_covered = sum(1 for v in regime_coverage.values() if v > 0)

    warnings: list[str] = []
    if total_bars < 1000:
        warnings.append(f"Total bars ({total_bars}) below 1000 — limited statistical power.")
    if walk_forward_folds < 3:
        warnings.append(f"Only {walk_forward_folds} walk-forward folds — results may not generalize.")
    if effective_oos_bars < 500:
        warnings.append(f"Effective OOS bars ({effective_oos_bars}) below 500 — OOS metrics unreliable.")
    if regimes_covered < 4:
        warnings.append(f"Only {regimes_covered}/6 regimes covered — regime-conditional results incomplete.")

    for regime, count in regime_coverage.items():
        if 0 < count < 20:
            warnings.append(f"Regime '{regime}' has only {count} observations — insufficient for analysis.")

    return DataSufficiency(
        total_bars=total_bars,
        train_bars=train_bars,
        test_bars=test_bars,
        walk_forward_folds=walk_forward_folds,
        effective_oos_bars=effective_oos_bars,
        regimes_covered=regimes_covered,
        regime_coverage=regime_coverage,
        coverage_warnings=warnings,
    )


def compute_effective_sample_size(returns: pd.Series) -> EffectiveSampleSize:
    """Compute effective sample size accounting for autocorrelation.

    N_eff = N / (1 + 2 * sum(rho_k)) where rho_k is the autocorrelation
    at lag k. Summation truncated when autocorrelation drops below 0.05.
    """
    clean = returns.dropna().values
    n = len(clean)

    if n < 3:
        return EffectiveSampleSize(raw_n=n, effective_n=float(n), independent_bets=n)

    mean_r = float(np.mean(clean))
    demeaned = clean - mean_r
    var = float(np.sum(demeaned**2) / n)

    if var < 1e-20:
        return EffectiveSampleSize(raw_n=n, effective_n=float(n), independent_bets=n)

    # Lag-1 autocorrelation
    rho1 = float(np.sum(demeaned[1:] * demeaned[:-1]) / n) / var

    max_lag = min(int(math.sqrt(n)), n // 3)
    rho_sum = 0.0
    last_lag_used = 0

    for k in range(1, max_lag + 1):
        rho_k = float(np.sum(demeaned[k:] * demeaned[:-k]) / n) / var
        if rho_k < 0.05:
            break
        rho_sum += rho_k
        last_lag_used = k

    denominator = 1 + 2 * rho_sum
    if denominator < 1.0:
        denominator = 1.0

    n_eff = n / denominator

    return EffectiveSampleSize(
        raw_n=n,
        effective_n=n_eff,
        autocorrelation_lag1=rho1,
        independent_bets=math.floor(n_eff),
        max_lag_used=last_lag_used,
        rho_sum=rho_sum,
    )


# Annualization factor: 1-minute bars × 390 minutes/session × 252 sessions/year.
_BARS_PER_YEAR_DEFAULT = 252 * 390


def compute_sharpe_ci(
    bar_returns: np.ndarray | pd.Series,
    n_eff: float | None = None,
    bars_per_year: int = _BARS_PER_YEAR_DEFAULT,
    confidence_level: float = 0.95,
) -> SharpeCi:
    """Lo (2002) confidence interval for the annualized Sharpe ratio.

    Per Lo (2002, eq. 14), under IID returns the per-period Sharpe estimator
    has variance ``(1 + 0.5·SR_p²)/T``. Annualization multiplies by the
    bars-per-year factor under the square root. Replacing T with the
    autocorrelation-adjusted N_eff folds in the autocorrelation correction
    (Newey-West-style truncation done in `compute_effective_sample_size`).

    Returns a `SharpeCi` with ``valid=False`` when N_eff is too small for a
    meaningful interval (the UI should show "insufficient sample size"
    instead of a misleading band).
    """
    arr = np.asarray(bar_returns, dtype=float)
    arr = arr[~np.isnan(arr)]
    n = len(arr)

    if n < 3:
        return SharpeCi(valid=False, n_eff_used=float(n))

    mean_r = float(np.mean(arr))
    std_r = float(np.std(arr, ddof=1))
    if std_r < 1e-12:
        return SharpeCi(valid=False, n_eff_used=float(n))

    sqrt_bpy = math.sqrt(bars_per_year)
    sr_period = mean_r / std_r
    sr_annual = sr_period * sqrt_bpy

    t_eff = float(n_eff) if (n_eff is not None and n_eff > 1) else float(n)
    if t_eff <= 1:
        return SharpeCi(point=sr_annual, valid=False, n_eff_used=t_eff)

    var_sr_period = (1.0 + 0.5 * sr_period**2) / t_eff
    if var_sr_period <= 0:
        return SharpeCi(point=sr_annual, valid=False, n_eff_used=t_eff)

    se_period = math.sqrt(var_sr_period)
    se_annual = se_period * sqrt_bpy

    # Two-sided z critical value
    tail = (1.0 - confidence_level) / 2.0
    z_critical = float(scipy_stats.norm.ppf(1.0 - tail))

    return SharpeCi(
        point=sr_annual,
        se=se_annual,
        ci_lower=sr_annual - z_critical * se_annual,
        ci_upper=sr_annual + z_critical * se_annual,
        confidence_level=confidence_level,
        n_eff_used=t_eff,
        valid=True,
    )


def compute_deflated_sharpe(
    selected_sharpe_annual: float,
    bar_returns: np.ndarray | pd.Series,
    n_trials: int,
    n_eff: float | None = None,
    bars_per_year: int = _BARS_PER_YEAR_DEFAULT,
) -> DeflatedSharpe:
    """Bailey & López de Prado (2014) Deflated Sharpe Ratio.

    Inflates the standard error of the observed maximum Sharpe by an
    extreme-value-theory correction for `n_trials` independent grid
    configurations, then computes the probability that the true Sharpe
    exceeds zero.

    All math is performed in per-period units (consistent with Lo's
    denominator); the expected-max benchmark `SR_0` is reported back in
    annualized units for display.
    """
    arr = np.asarray(bar_returns, dtype=float)
    arr = arr[~np.isnan(arr)]
    n_bars = len(arr)

    if n_bars < 3 or n_trials < 1:
        return DeflatedSharpe(
            raw_sharpe=selected_sharpe_annual, n_trials=n_trials, valid=False
        )

    t_eff = float(n_eff) if (n_eff is not None and n_eff > 2) else float(n_bars)
    if t_eff <= 2:
        return DeflatedSharpe(
            raw_sharpe=selected_sharpe_annual, n_trials=n_trials, valid=False
        )

    sqrt_bpy = math.sqrt(bars_per_year)
    sr_period = selected_sharpe_annual / sqrt_bpy

    # Higher moments of the per-bar return series.
    skew = float(scipy_stats.skew(arr, bias=False))
    # Pearson kurtosis (not Fisher / excess) — matches Bailey & López de Prado.
    kurt = float(scipy_stats.kurtosis(arr, fisher=False, bias=False))

    # Expected max under H_0 of zero true Sharpe across n_trials independent
    # trials, in standardised (unit-SE) units. Bailey & López de Prado, eq 6.
    if n_trials >= 2:
        gamma_e = 0.5772156649015329  # Euler-Mascheroni
        z_a = float(scipy_stats.norm.ppf(1.0 - 1.0 / n_trials))
        z_b = float(scipy_stats.norm.ppf(1.0 - 1.0 / (n_trials * math.e)))
        sr_0_standardised = (1.0 - gamma_e) * z_a + gamma_e * z_b
    else:
        sr_0_standardised = 0.0

    # SE under the null is approximately 1 / sqrt(T_eff), so SR_0 in per-period
    # units is the standardised value divided by sqrt(T_eff).
    sr_0_period = sr_0_standardised / math.sqrt(t_eff)

    # Lo's non-normality-corrected denominator (Bailey & López de Prado, eq 9).
    denom_inner = 1.0 - skew * sr_period + (kurt - 1.0) / 4.0 * sr_period**2
    if denom_inner <= 0:
        return DeflatedSharpe(
            raw_sharpe=selected_sharpe_annual,
            expected_max_under_null=sr_0_period * sqrt_bpy,
            n_trials=n_trials,
            skewness=skew,
            kurtosis=kurt,
            valid=False,
        )

    z_stat = (sr_period - sr_0_period) * math.sqrt(t_eff - 1.0) / math.sqrt(denom_inner)
    dsr = float(scipy_stats.norm.cdf(z_stat))

    return DeflatedSharpe(
        raw_sharpe=selected_sharpe_annual,
        expected_max_under_null=sr_0_period * sqrt_bpy,
        dsr_probability=dsr,
        n_trials=n_trials,
        skewness=skew,
        kurtosis=kurt,
        valid=True,
    )


def compute_joint_regime_coverage(
    daily_regimes: pd.DataFrame,
    n_eff: float,
    pct_active: float,
    min_trades_for_pass: int = 30,
) -> list[RegimeBucket]:
    """Joint (vol × trend) regime grid with effective-trades estimate.

    Replaces the marginal day-count grid (which displayed equal counts in
    every column of a row, because it was projecting a 1-D distribution
    onto a 2-D layout) with a true joint count and an estimate of how
    many decision-relevant trades fell into each cell.

    The per-bucket effective-trades estimate is

        ``trades_bucket ≈ N_eff · pct_active · (days_bucket / total_days)``

    which assumes uniform signal activity across regimes. It is an
    approximation, but materially more honest than presenting raw bar
    counts as if they were independent observations.
    """
    if daily_regimes is None or len(daily_regimes) == 0:
        return []

    if "vol_regime" not in daily_regimes.columns or "trend_regime" not in daily_regimes.columns:
        return []

    total_days = len(daily_regimes)
    if total_days == 0:
        return []

    # Build joint count using groupby.
    joint = (
        daily_regimes.groupby(["vol_regime", "trend_regime"]).size().reset_index(name="days")
    )

    buckets: list[RegimeBucket] = []
    for _, row in joint.iterrows():
        days = int(row["days"])
        if total_days > 0 and pct_active > 0 and n_eff > 0:
            trades = float(n_eff) * float(pct_active) * (days / float(total_days))
        else:
            trades = 0.0

        if days == 0:
            badge = "Empty"
        elif trades >= float(min_trades_for_pass):
            badge = "Pass"
        else:
            badge = "Sparse"

        buckets.append(
            RegimeBucket(
                vol_label=str(row["vol_regime"]),
                trend_label=str(row["trend_regime"]),
                days=days,
                effective_trades=trades,
                badge=badge,
            )
        )

    return buckets
