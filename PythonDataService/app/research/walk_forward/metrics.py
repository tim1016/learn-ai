"""Shared walk-forward retention metrics.

Formula:
  * ``fold_retention = test_sharpe / train_sharpe`` — **undefined** (``None``)
    when either side is null or non-finite, or when ``train_sharpe <= 0``. A
    negative training Sharpe used to pass the guard and return a
    sign-flipped ratio that is meaningless as retention; zero was the only
    value refused. Retention is a fraction of something worth retaining,
    so a non-positive denominator has no retention to report (PRD #1925,
    "The verdict — frozen formula").
  * ``mean_fold_retention`` — equal-weight arithmetic mean of defined fold
    retentions; the spec-path walk-forward's aggregate.
  * ``median_fold_retention`` — median of defined fold retentions; the
    registry-strategy study's ``study_retention``. The median is unmoved by
    one outlier from a near-zero denominator (six folds with training
    Sharpes ``[0.0001, 2, 2, 2, 2, 2]`` and test Sharpes ``[0.001, 0.1, …]``
    lose 95% of their Sharpe in five folds; the mean reports 1.71, the
    median 0.05).
Reference: repository-internal research contract documented in
  docs/references/walk-forward.md and docs/references/walk-forward-study.md.
Canonical implementation: this file.
Validated against: tests/research/walk_forward/test_metrics.py.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable


def sharpe_retention(
    test_sharpe: float | None,
    train_sharpe: float | None,
) -> float | None:
    """Return a fold-local test/train Sharpe ratio when it is defined."""
    if test_sharpe is None or train_sharpe is None:
        return None
    if not (math.isfinite(test_sharpe) and math.isfinite(train_sharpe)):
        return None
    if train_sharpe <= 0:
        return None
    return test_sharpe / train_sharpe


def mean_fold_retention(values: Iterable[float | None]) -> float | None:
    """Return the equal-weight mean of defined fold-local retentions."""
    defined = [value for value in values if value is not None]
    return statistics.fmean(defined) if defined else None


def median_fold_retention(values: Iterable[float | None]) -> float | None:
    """Return the median of defined fold-local retentions."""
    defined = [value for value in values if value is not None]
    return statistics.median(defined) if defined else None
