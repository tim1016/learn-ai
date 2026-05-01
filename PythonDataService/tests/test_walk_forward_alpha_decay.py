"""Tests for the alpha-decay power guard in walk_forward.

The alpha-decay regression is `S_i = β₀ + β₁ · i + ε_i` over fold index `i`.
With fewer than ``ALPHA_DECAY_MIN_FOLDS`` folds the t-test has too few
residual degrees of freedom to be informative; the result must mark the
test as invalid so the UI suppresses the misleading p-value.
"""

from __future__ import annotations

import pytest

from app.research.signal.walk_forward import (
    ALPHA_DECAY_MIN_FOLDS,
    ALPHA_DECAY_SIGNIFICANCE_LEVEL,
    _compute_sharpe_trend_slope,
)


def test_alpha_decay_below_min_folds_marks_test_invalid():
    """Three folds is the canonical example from the AAPL momentum run.

    The slope is still reported (it is descriptive), but ``is_test_valid``
    must be False so consumers render an "insufficient folds" placeholder
    rather than a misleading p-value.
    """
    sharpes = [0.8, 0.0, 0.0]  # the example from the audit PDF

    result = _compute_sharpe_trend_slope(sharpes)

    assert result.n_folds_used == 3
    assert result.is_test_valid is False
    assert result.is_significant is False
    # The slope is reported regardless — it is descriptive, not inferential.
    assert result.slope < 0.0


def test_alpha_decay_at_or_above_min_folds_marks_test_valid():
    sharpes = [0.5, 0.45, 0.40, 0.35, 0.30]  # 5 folds, mild downward trend

    result = _compute_sharpe_trend_slope(sharpes)

    assert result.n_folds_used == ALPHA_DECAY_MIN_FOLDS
    assert result.is_test_valid is True


def test_alpha_decay_significant_requires_both_valid_and_p_below_threshold():
    """A clearly-significant downward trend over enough folds is significant.

    The trend must have *some* residual noise — a perfectly collinear
    sequence has zero residual variance and the t-statistic falls back
    to zero by the divide-by-zero guard, which would mask significance.
    Real walk-forward Sharpes always have noise.
    """
    # 6 folds with a clear downward trend plus small jitter.
    sharpes = [1.05, 0.78, 0.62, 0.41, 0.18, -0.05]

    result = _compute_sharpe_trend_slope(sharpes)

    assert result.is_test_valid is True
    assert result.p_value < ALPHA_DECAY_SIGNIFICANCE_LEVEL
    assert result.is_significant is True


def test_alpha_decay_not_significant_when_trend_is_noise():
    """Five folds of jittery but trendless Sharpes should not be flagged."""
    sharpes = [0.4, 0.5, 0.4, 0.5, 0.4]

    result = _compute_sharpe_trend_slope(sharpes)

    assert result.is_test_valid is True
    assert result.is_significant is False


def test_alpha_decay_two_folds_reports_slope_but_test_remains_invalid():
    """With exactly 2 folds the slope is descriptively well-defined but the
    t-test has zero residual degrees of freedom; ``is_test_valid`` stays
    False and the p-value is uninformative (defaults to 1.0)."""
    sharpes = [0.5, 0.3]

    result = _compute_sharpe_trend_slope(sharpes)

    assert result.n_folds_used == 2
    assert result.is_test_valid is False
    assert result.is_significant is False
    # Slope is descriptive — the difference divided by Δi.
    assert result.slope == pytest.approx(-0.2, abs=1e-12)
    # p-value is the n ≤ 2 default, not a real test result.
    assert result.p_value == 1.0


def test_alpha_decay_constant_x_returns_default():
    """Edge-case: zero variance in fold index can't happen with > 1 fold,
    but the helper should return safe defaults if it ever did."""
    result = _compute_sharpe_trend_slope([0.5])

    # n < 2 path
    assert result.is_test_valid is False
    assert result.slope == pytest.approx(0.0, abs=1e-12)
