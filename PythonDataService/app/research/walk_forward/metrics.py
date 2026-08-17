"""Shared walk-forward retention metrics.

Formula: fold_retention = test_sharpe / train_sharpe; aggregate retention
  is the equal-weight arithmetic mean of defined fold retentions.
Reference: repository-internal research contract documented in
  docs/references/walk-forward.md.
Canonical implementation: this file.
Validated against: tests/research/walk_forward/test_metrics.py.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable


def sharpe_retention(
    test_sharpe: float | None,
    train_sharpe: float | None,
) -> float | None:
    """Return a fold-local test/train Sharpe ratio when it is defined."""
    if test_sharpe is None or train_sharpe is None or train_sharpe == 0:
        return None
    return test_sharpe / train_sharpe


def mean_fold_retention(values: Iterable[float | None]) -> float | None:
    """Return the equal-weight mean of defined fold-local retentions."""
    defined = [value for value in values if value is not None]
    return statistics.fmean(defined) if defined else None
