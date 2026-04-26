"""Sanity tests for DSR + PBO + robustness_score."""
from __future__ import annotations

import numpy as np

from app.engine.edge.robustness_stats import (
    deflated_sharpe_ratio,
    probability_of_backtest_overfitting,
    robustness_score,
)


def test_dsr_high_for_strong_solo_sharpe():
    dsr = deflated_sharpe_ratio(
        observed_sharpe=2.0, n_trials=1, skew=0.0, kurtosis=3.0, n_observations=252,
    )
    assert dsr > 0.95


def test_dsr_drops_with_more_trials():
    args = dict(observed_sharpe=2.0, skew=0.0, kurtosis=3.0, n_observations=252)
    high = deflated_sharpe_ratio(n_trials=1, **args)
    low = deflated_sharpe_ratio(n_trials=1000, **args)
    assert low < high


def test_dsr_zero_or_low_for_weak_strategy():
    dsr = deflated_sharpe_ratio(
        observed_sharpe=0.0, n_trials=10, skew=0.0, kurtosis=3.0, n_observations=252,
    )
    assert dsr < 0.5


def test_pbo_zero_when_clear_winner_is_consistent():
    rng = np.random.default_rng(0)
    n_trials, n_periods = 10, 8
    base = rng.normal(0.0, 0.1, size=(n_trials, n_periods))
    base[0] += 1.0  # strategy 0 strictly dominates everywhere
    pbo = probability_of_backtest_overfitting(base)
    assert pbo < 0.1


def test_pbo_high_for_pure_noise_strategies():
    rng = np.random.default_rng(0)
    matrix = rng.normal(0.0, 1.0, size=(20, 8))
    pbo = probability_of_backtest_overfitting(matrix)
    assert 0.3 < pbo < 0.7  # noise should be ~50/50 ranks


def test_robustness_score_counts_positive_cells():
    matrix = np.array([[1.0, -0.5, 0.5], [-0.1, 0.8, -0.2]])
    assert robustness_score(matrix) == 0.5


def test_robustness_score_zero_for_empty():
    assert robustness_score(np.array([])) == 0.0
