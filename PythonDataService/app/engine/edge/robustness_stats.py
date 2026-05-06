"""Robustness diagnostics: Deflated Sharpe Ratio + Probability of Backtest Overfitting.

Formula: DSR = (SR_observed - E[SR_max]) / sqrt(Var[SR_max]); PBO from combinatorially symmetric cross-validation on strategy returns
Reference: López de Prado, M. (2014) "The Deflated Sharpe Ratio" J. Portfolio Mgmt 40(5); Bailey, D. & López de Prado, M. (2014) "The Probability of Backtest Overfitting" J. Computational Finance
Canonical implementation: app/engine/edge/robustness_stats.py
Validated against: NONE — pending

References:
- López de Prado, M. (2014), "The Deflated Sharpe Ratio", J. Portfolio Mgmt 40(5).
- Bailey, D., López de Prado, M. (2014), "The Probability of Backtest Overfitting",
  J. Computational Finance.

These implementations target reasonable accuracy for in-app diagnostics; full
fidelity to the published numerical examples requires the supplemental materials
of the original papers.
"""

from __future__ import annotations

import numpy as np
from scipy.special import comb
from scipy.stats import norm

EULER_MASCHERONI = 0.5772156649015329


def deflated_sharpe_ratio(
    *,
    observed_sharpe: float,
    n_trials: int,
    skew: float,
    kurtosis: float,
    n_observations: int,
    benchmark_sharpe: float = 0.0,
) -> float:
    """DSR per López de Prado (2014).

    DSR = Z[ (SR - SR_0) * sqrt(n - 1) / sqrt(1 - skew * SR + (kurt - 1)/4 * SR^2) ]
    where SR_0 is the expected maximum across n_trials random strategies.
    """
    if n_trials < 1:
        raise ValueError("n_trials must be >= 1")
    sr0 = _expected_max_sr(n_trials, benchmark_sharpe=benchmark_sharpe)
    denom = 1.0 - skew * observed_sharpe + (kurtosis - 1.0) / 4.0 * observed_sharpe**2
    if denom <= 0:
        return 0.0
    test_stat = (observed_sharpe - sr0) * np.sqrt(n_observations - 1) / np.sqrt(denom)
    return float(norm.cdf(test_stat))


def _expected_max_sr(n_trials: int, *, benchmark_sharpe: float = 0.0) -> float:
    """E[max SR] for n_trials i.i.d. standard-normal random strategies (Bailey-LdP)."""
    if n_trials == 1:
        return benchmark_sharpe
    z_n = norm.ppf(1.0 - 1.0 / n_trials)
    z_inv = norm.ppf(1.0 - 1.0 / (n_trials * np.e))
    return benchmark_sharpe + (1.0 - EULER_MASCHERONI) * z_n + EULER_MASCHERONI * z_inv


def probability_of_backtest_overfitting(matrix: np.ndarray) -> float:
    """CSCV PBO per Bailey-LdP (2014).

    Input: matrix of trial-period performance (rows = trials, cols = periods).
    Each row is a candidate strategy; each column is one out-of-sample period.
    Procedure:
        1. Split columns into S equal partitions.
        2. For every C(S, S/2) train/test split, identify the best strategy
           in-sample and rank its out-of-sample performance.
        3. PBO = P(rank > median) — the chance the in-sample winner is below
           the median out-of-sample.
    """
    if matrix.ndim != 2:
        raise ValueError("matrix must be 2-D (trials x periods)")
    n_trials, n_periods = matrix.shape
    if n_periods % 2 != 0:
        n_periods -= 1
        matrix = matrix[:, :n_periods]
    s = n_periods
    half = s // 2
    if half == 0 or n_trials < 2:
        return 0.0

    # For computational sanity cap the number of combinations — exact CSCV is
    # C(s, s/2) which explodes; we sample if too large.
    max_combos = 200
    total_combos = int(comb(s, half))
    rng = np.random.default_rng(123)

    overfitting_count = 0
    iterations = 0
    if total_combos <= max_combos:
        from itertools import combinations

        combos = combinations(range(s), half)
    else:
        all_idx = np.arange(s)
        combos = (rng.choice(all_idx, size=half, replace=False) for _ in range(max_combos))

    for train_cols in combos:
        train_cols = np.asarray(list(train_cols))
        test_cols = np.setdiff1d(np.arange(s), train_cols)
        train_perf = matrix[:, train_cols].mean(axis=1)
        test_perf = matrix[:, test_cols].mean(axis=1)
        winner = int(np.argmax(train_perf))
        rank = (test_perf < test_perf[winner]).sum() + 1
        median_rank = (n_trials + 1) / 2
        if rank < median_rank:
            overfitting_count += 1
        iterations += 1

    return float(overfitting_count / iterations) if iterations else 0.0


def robustness_score(matrix: np.ndarray) -> float:
    """Share of (asset, period) cells with positive Sharpe."""
    if matrix.size == 0:
        return 0.0
    return float((matrix > 0).mean())
