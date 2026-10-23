"""Signal diagnostics, data sufficiency, and effective sample size.

Formula: N_eff = N / (1 + 2·Σ rho_k); joint (vol×trend) regime coverage.
Reference: Internal data-sufficiency heuristics; not independent numerical validation.
Canonical implementation: app/research/signal/diagnostics.py
Validated against: NONE — signal/data sufficiency heuristics have no external numerical fixture.

Sharpe inference is canonical in app/research/signal/sharpe.py and re-exported
here for existing report consumers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.research.signal.sharpe import (
    DeflatedSharpe as DeflatedSharpe,
)
from app.research.signal.sharpe import (
    SharpeCi as SharpeCi,
)
from app.research.signal.sharpe import (
    compute_deflated_sharpe as compute_deflated_sharpe,
)
from app.research.signal.sharpe import (
    compute_sharpe_ci as compute_sharpe_ci,
)


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
