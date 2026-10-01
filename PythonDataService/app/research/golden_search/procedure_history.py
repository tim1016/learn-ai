"""Chronological validation of a search procedure: fold windows, fold evidence and linked fold returns.

Formula: folds are the canonical walk-forward plan over the development
interval (``app.research.walk_forward_study.folds.plan_folds``), as
``int64 ms UTC`` ET-midnight windows; a planner boundary snapped forward to
the next session past ``development_end`` is clipped back to it, which drops
no session (none lies between them). Each test fold starts flat with fresh
capital, so its return ``r_i`` is that fold's net profit over the starting
capital. The linked line is the growth of 1 compounded across the
non-overlapping test folds: ``L_k = Π_{i<=k} (1 + r_i) − 1`` at fold ``k``'s
end. A missing fold (``None``) breaks the line: it and every later point are
``None`` — never interpolated, never skipped. Raw dollar equity curves of
independently reset portfolios are never spliced. Fold verdicts reuse the
legacy five-label ``compute_verdict`` unchanged; a fold is ``completed`` only
when it has both an eligible training winner and a completed test run.
Reference: PRD https://github.com/tim1016/learn-ai/issues/2696 "Validate the
  procedure without leaking its future"; PRD #1925 for the verdict.
Canonical implementation: this file.
Validated against: tests/research/golden_search/test_procedure_history.py.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.research.golden_search.protocol import GoldenSearchProtocol, development_folds
from app.research.golden_search.selection import Metrics
from app.research.walk_forward_study.verdict import FoldEvidence
from app.utils.session_anchors import et_midnight_ms


@dataclass(frozen=True)
class FoldWindow:
    """One fold's training and test windows, each half-open ``[start_ms, end_ms)``."""

    fold_index: int
    train_start_ms: int
    train_end_ms: int
    test_start_ms: int
    test_end_ms: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "fold_index": self.fold_index,
            "train_start_ms": self.train_start_ms,
            "train_end_ms": self.train_end_ms,
            "test_start_ms": self.test_start_ms,
            "test_end_ms": self.test_end_ms,
        }


def fold_windows(protocol: GoldenSearchProtocol) -> list[FoldWindow]:
    """The plan's folds as ms windows inside the development interval; raises ``FoldPlanError``."""
    end = protocol.development_end_ms

    def clip(ms: int) -> int:
        return min(ms, end)

    return [
        FoldWindow(
            fold_index=fold.fold_index,
            train_start_ms=et_midnight_ms(fold.train_start),
            train_end_ms=clip(et_midnight_ms(fold.train_end)),
            test_start_ms=clip(et_midnight_ms(fold.test_start)),
            test_end_ms=clip(et_midnight_ms(fold.test_end)),
        )
        for fold in development_folds(protocol)
    ]


def fold_return(test: Metrics | None) -> float | None:
    """A test fold's return on its fresh starting capital (the engine's ``net_profit_pct``), or ``None`` when absent."""
    if test is None or test.status != "completed" or test.total_return_pct is None:
        return None
    return test.total_return_pct if math.isfinite(test.total_return_pct) else None


def link_fold_returns(fold_returns: Sequence[float | None]) -> list[float | None]:
    """Growth of 1 linked across consecutive test folds; a missing or non-finite fold breaks every later point."""
    linked: list[float | None] = []
    growth = 1.0
    broken = False
    for value in fold_returns:
        if broken or value is None or not math.isfinite(value):
            broken = True
            linked.append(None)
            continue
        growth *= 1.0 + value
        linked.append(growth - 1.0)
    return linked


def fold_evidence(fold_index: int, train: Metrics | None, test: Metrics | None) -> FoldEvidence:
    """What the legacy verdict reads from one fold: ``train`` is the winner's training run, ``None`` when no winner."""
    if train is None or test is None or train.status != "completed" or test.status != "completed":
        return FoldEvidence(fold_index=fold_index, status="failed", train_sharpe=None, test_sharpe=None, test_trades=0)
    return FoldEvidence(
        fold_index=fold_index,
        status="completed",
        train_sharpe=train.sharpe_ratio,
        test_sharpe=test.sharpe_ratio,
        test_trades=test.total_trades,
    )
