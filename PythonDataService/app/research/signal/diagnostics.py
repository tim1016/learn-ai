"""Signal diagnostics, data sufficiency, and effective sample size."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


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
        independent_bets=int(math.floor(n_eff)),
        max_lag_used=last_lag_used,
        rho_sum=rho_sum,
    )
