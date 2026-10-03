"""Each fold's numbers on the Test over time charts (#2821): hand-worked, ``atol=1e-9``."""

from __future__ import annotations

from typing import Any

import pytest

from app.research.golden_search.fold_charts import fold_view
from tests._helpers.golden_search import metrics

WINDOWS = {"fold_index": 0, "train_start_ms": 0, "train_end_ms": 10, "test_start_ms": 10, "test_end_ms": 20}


def _fold(status: str, *, train: Any = None, test: Any = None, incumbent: Any = None) -> dict[str, Any]:
    return {
        **WINDOWS,
        "status": status,
        "failure_reason": None,
        "train_metrics": None if train is None else train.as_dict(),
        "test_metrics": None if test is None else test.as_dict(),
        "incumbent_test_metrics": None if incumbent is None else incumbent.as_dict(),
    }


def _numbers(view: dict[str, Any]) -> tuple[Any, ...]:
    keys = ("train_sharpe", "test_sharpe", "retention", "test_return", "incumbent_return", "return_difference", "test_trades", "incumbent_trades")
    return tuple(view[key] for key in keys)


def test_a_completed_fold_keeps_half_its_training_sharpe_and_beats_the_incumbent_by_two_points() -> None:
    view = fold_view(_fold("completed", train=metrics(2.0), test=metrics(1.0, trades=12, total_return=0.03), incumbent=metrics(0.4, trades=9, total_return=0.01)))
    assert _numbers(view) == pytest.approx((2.0, 1.0, 0.5, 0.03, 0.01, 0.02, 12, 9), abs=1e-9, rel=0)


def test_a_non_positive_training_sharpe_leaves_retention_undefined() -> None:
    view = fold_view(_fold("completed", train=metrics(-0.5), test=metrics(0.3), incumbent=metrics(0.1)))
    assert view["retention"] is None and view["test_sharpe"] == pytest.approx(0.3, abs=1e-9, rel=0)


def test_a_failed_test_run_keeps_the_winners_training_sharpe_and_nothing_else() -> None:
    failed = metrics(None, net=None, total_return=None, status="failed")
    view = fold_view(_fold("failed", train=metrics(1.5), test=failed, incumbent=metrics(0.2, total_return=0.004)))
    assert _numbers(view) == pytest.approx((1.5, None, None, None, 0.004, None, None, 50), abs=1e-9, rel=0)


def test_a_planned_fold_has_its_windows_and_no_numbers() -> None:
    view = fold_view({**WINDOWS, "status": "planned"})
    assert view["status"] == "planned" and view["failure_reason"] is None
    assert _numbers(view) == (None,) * 8
