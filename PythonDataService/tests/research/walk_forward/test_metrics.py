"""Pinned tests for canonical walk-forward retention arithmetic."""

from __future__ import annotations

import pytest

from app.research.walk_forward.metrics import mean_fold_retention, sharpe_retention


def test_sharpe_retention_is_fold_local_test_over_train() -> None:
    assert sharpe_retention(0.5, 2.0) == pytest.approx(0.25, abs=1e-12, rel=0)


@pytest.mark.parametrize(
    ("test_sharpe", "train_sharpe"),
    [(None, 1.0), (1.0, None), (1.0, 0.0)],
)
def test_sharpe_retention_is_none_when_ratio_is_undefined(
    test_sharpe: float | None,
    train_sharpe: float | None,
) -> None:
    assert sharpe_retention(test_sharpe, train_sharpe) is None


def test_mean_fold_retention_ignores_undefined_folds_and_weights_defined_equally() -> None:
    assert mean_fold_retention([0.5, None, 1.5]) == pytest.approx(1.0, abs=1e-12, rel=0)
    assert mean_fold_retention([None, None]) is None
