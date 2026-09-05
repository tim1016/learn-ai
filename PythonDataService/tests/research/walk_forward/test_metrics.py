"""Pinned tests for canonical walk-forward retention arithmetic."""

from __future__ import annotations

import math

import pytest

from app.research.walk_forward.metrics import mean_fold_retention, median_fold_retention, sharpe_retention


def test_sharpe_retention_is_fold_local_test_over_train() -> None:
    assert sharpe_retention(0.5, 2.0) == pytest.approx(0.25, abs=1e-12, rel=0)


@pytest.mark.parametrize(
    ("test_sharpe", "train_sharpe"),
    [(None, 1.0), (1.0, None), (1.0, 0.0), (1.0, math.nan), (math.inf, 1.0), (1.0, -math.inf)],
)
def test_sharpe_retention_is_none_when_ratio_is_undefined(
    test_sharpe: float | None,
    train_sharpe: float | None,
) -> None:
    assert sharpe_retention(test_sharpe, train_sharpe) is None


def test_a_negative_training_sharpe_yields_an_undefined_retention_not_a_sign_flipped_one() -> None:
    """Regression: ``-0.5 / -1.0`` used to come back as ``0.5`` — "retained half" of a loss."""
    assert sharpe_retention(-0.5, -1.0) is None
    assert sharpe_retention(0.5, -1.0) is None


def test_mean_fold_retention_ignores_undefined_folds_and_weights_defined_equally() -> None:
    assert mean_fold_retention([0.5, None, 1.5]) == pytest.approx(1.0, abs=1e-12, rel=0)
    assert mean_fold_retention([None, None]) is None


def test_median_fold_retention_is_unmoved_by_a_near_zero_denominator() -> None:
    train = [0.0001, 2, 2, 2, 2, 2]
    test = [0.001, 0.1, 0.1, 0.1, 0.1, 0.1]
    retentions = [sharpe_retention(t, tr) for t, tr in zip(test, train, strict=True)]

    assert mean_fold_retention(retentions) == pytest.approx(1.7083333333, abs=1e-9, rel=0)
    assert median_fold_retention(retentions) == pytest.approx(0.05, abs=1e-12, rel=0)
    assert median_fold_retention([None, None]) is None


def test_median_and_mean_agree_on_a_healthy_study() -> None:
    retentions = [sharpe_retention(t, 1.0) for t in (0.8, 0.7, 0.9, 0.6)]
    assert median_fold_retention(retentions) == pytest.approx(0.75, abs=1e-12, rel=0)
    assert mean_fold_retention(retentions) == pytest.approx(0.75, abs=1e-12, rel=0)
