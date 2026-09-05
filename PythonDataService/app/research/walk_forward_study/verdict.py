"""The frozen walk-forward verdict (PRD #1925, "The verdict — frozen formula").

Formula, per fold ``i`` over that fold's chosen winner only:
  * ``fold_retention_i = test_sharpe_i / train_sharpe_i`` via the canonical
    ``sharpe_retention`` — undefined when either side is null/non-finite or
    ``train_sharpe_i <= 0``.
  * ``S`` = successful folds; ``D`` = folds with a defined retention.
  * ``study_retention`` = **median** of defined fold retentions.
  * ``oos_trade_count`` = trades across fold-winner test cells only.

Verdict, first matching rule wins:
  1. any fold failed, or ``S == 0``                     → could not be judged
  2. ``D == 0``                                         → could not be judged
  3. ``D < ceil(S / 2)``                                → could not be judged (coverage)
  4. ``oos_trade_count < minimum_trades``               → too few trades
  5. no successful fold has a defined ``test_sharpe_i`` → could not be judged
  6. median ``test_sharpe_i`` over defined values ``<= 0`` → stopped working
  7. ``study_retention >= 0.5``                         → still worked
  8. otherwise                                          → got worse

The ``0.5`` threshold, the ``ceil(S / 2)`` coverage floor and the median are
documented judgment calls; both counterexamples that forced the median and
the coverage rule are reproduced in the tests. Coverage is always disclosed
as "based on D of S folds", whatever the label.
Reference: PRD https://github.com/tim1016/learn-ai/issues/1925 revision 7;
  docs/references/walk-forward-study.md.
Canonical implementation: this file.
Validated against: tests/research/walk_forward_study/test_verdict.py.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.research.walk_forward.metrics import median_fold_retention, sharpe_retention

VerdictLabel = Literal["still worked", "got worse", "stopped working", "too few trades", "could not be judged"]
RETENTION_THRESHOLD = 0.5


@dataclass(frozen=True)
class FoldEvidence:
    """What the verdict reads from one fold: the chosen winner's two Sharpes and its test trades."""

    fold_index: int
    status: Literal["completed", "failed"]
    train_sharpe: float | None
    test_sharpe: float | None
    test_trades: int = 0

    @property
    def retention(self) -> float | None:
        if self.status != "completed":
            return None
        return sharpe_retention(self.test_sharpe, self.train_sharpe)


@dataclass(frozen=True)
class Verdict:
    label: VerdictLabel
    reason: str
    successful_folds: int
    defined_folds: int
    study_retention: float | None
    median_test_sharpe: float | None
    oos_trade_count: int

    @property
    def based_on(self) -> str:
        return f"based on {self.defined_folds} of {self.successful_folds} folds"

    def as_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "reason": self.reason,
            "successful_folds": self.successful_folds,
            "defined_folds": self.defined_folds,
            "study_retention": self.study_retention,
            "median_test_sharpe": self.median_test_sharpe,
            "oos_trade_count": self.oos_trade_count,
            "based_on": self.based_on,
            "retention_threshold": RETENTION_THRESHOLD,
        }


def _finite(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) else None


def compute_verdict(folds: Sequence[FoldEvidence], *, min_trades: int) -> Verdict:
    """Apply the frozen rule table to the fold-winner evidence."""
    successful = [fold for fold in folds if fold.status == "completed"]
    s = len(successful)
    retentions = [fold.retention for fold in successful]
    defined = [value for value in retentions if value is not None]
    d = len(defined)
    study_retention = median_fold_retention(retentions)
    test_sharpes = [value for value in (_finite(fold.test_sharpe) for fold in successful) if value is not None]
    median_test_sharpe = statistics.median(test_sharpes) if test_sharpes else None
    oos_trades = sum(fold.test_trades for fold in successful)

    def _verdict(label: VerdictLabel, reason: str) -> Verdict:
        return Verdict(
            label=label,
            reason=reason,
            successful_folds=s,
            defined_folds=d,
            study_retention=study_retention,
            median_test_sharpe=median_test_sharpe,
            oos_trade_count=oos_trades,
        )

    failed = len(folds) - s
    if failed > 0 or s == 0:
        return _verdict("could not be judged", f"{failed} of {len(folds)} folds failed; the out-of-sample record has holes" if folds else "no folds")
    if d == 0:
        return _verdict("could not be judged", "no fold has a defined retention (every training Sharpe was non-positive or null)")
    if d < math.ceil(s / 2):
        return _verdict("could not be judged", f"only {d} of {s} folds have a defined retention; fewer than half can be judged")
    if oos_trades < min_trades:
        return _verdict("too few trades", f"{oos_trades} out-of-sample trades across fold winners, below the minimum of {min_trades}")
    # A defined retention needs a finite test Sharpe, so d >= 1 guarantees one exists.
    assert median_test_sharpe is not None
    if median_test_sharpe <= 0:
        return _verdict("stopped working", f"median out-of-sample Sharpe {median_test_sharpe:.3f} is not positive")
    assert study_retention is not None
    if study_retention >= RETENTION_THRESHOLD:
        return _verdict("still worked", f"median fold retention {study_retention:.3f} is at least {RETENTION_THRESHOLD}")
    return _verdict("got worse", f"median fold retention {study_retention:.3f} is below {RETENTION_THRESHOLD}")
