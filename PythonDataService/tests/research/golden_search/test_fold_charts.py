"""Each fold's numbers on the Test over time charts (#2821): hand-worked, ``atol=1e-9``."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from app.research.golden_search.activity import activity_plan
from app.research.golden_search.fold_charts import fold_charts, fold_view
from app.research.golden_search.models import StudyRow
from app.research.golden_search.planning import fold_windows, protocol_from_request
from tests._helpers.golden_search import metrics
from tests._helpers.golden_search_study import plan_request

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


@pytest.mark.parametrize(
    ("status", "train", "test", "incumbent", "numbers"),
    [
        pytest.param(
            "completed", metrics(2.0), metrics(1.0, trades=12, total_return=0.03), metrics(0.4, trades=9, total_return=0.01),
            (2.0, 1.0, 0.5, 0.03, 0.01, 0.02, 12, 9),
            id="a-completed-fold-keeps-half-its-training-sharpe-and-beats-the-incumbent-by-two-points",
        ),
        pytest.param(
            "completed", metrics(-0.5), metrics(0.3), metrics(0.1),
            (-0.5, 0.3, None, 0.01, 0.01, 0.0, 50, 50),
            id="a-non-positive-training-sharpe-leaves-retention-undefined",
        ),
        pytest.param(
            "failed", metrics(1.5), metrics(None, net=None, total_return=None, status="failed"), metrics(0.2, total_return=0.004),
            (1.5, None, None, None, 0.004, None, None, 50),
            id="a-failed-test-run-keeps-the-winners-training-sharpe-and-nothing-else",
        ),
    ],
)
def test_a_fold_view_carries_the_numbers_its_runs_gave(status: str, train: Any, test: Any, incumbent: Any, numbers: tuple[Any, ...]) -> None:
    view = fold_view(_fold(status, train=train, test=test, incumbent=incumbent))
    assert _numbers(view) == pytest.approx(numbers, abs=1e-9, rel=0), "the fold's training and test sharpe, retention, returns, difference and trade counts"


def test_an_incumbent_run_that_failed_says_why_and_one_never_run_says_nothing() -> None:
    failed = replace(metrics(None, net=None, total_return=None, status="failed"), error="engine refused the window")
    assert fold_view(_fold("completed", train=metrics(1.0), test=metrics(0.5), incumbent=failed))["incumbent_failure"] == "engine refused the window"
    assert fold_view(_fold("completed", train=metrics(1.0), test=metrics(0.5)))["incumbent_failure"] is None


def test_a_planned_fold_has_its_windows_and_no_numbers() -> None:
    view = fold_view({**WINDOWS, "status": "planned"})
    assert view["status"] == "planned" and view["failure_reason"] is None
    assert _numbers(view) == (None,) * 8


def _row(results: dict[str, Any]) -> StudyRow:
    protocol = protocol_from_request(plan_request("SPY"))
    receipt = {"folds": [fold.as_dict() for fold in fold_windows(protocol)], "activity": activity_plan(protocol)}
    return StudyRow(
        id="s1", parent_study_id=None, strategy_key="ema_crossover_signal", symbol="SPY", state="validation_running", revision=0,
        status="running", attempt=1, job_id=None, pending_stage=None, stage_token=None, created_at_ms=0, updated_at_ms=0,
        finished_at_ms=None, protocol=protocol.as_dict(), protocol_hash="h", receipt=receipt, results=results, candidate_key=None,
        exam_locked=False, decision=None, budget_cap=5000, consumed_evaluations=0, cache_hits=0, invalid_points=0,
        incomplete=False, failure_reason=None, hidden=False,
    )  # fmt: skip


def test_a_running_stage_counts_its_completed_folds_so_far_and_never_judges_them() -> None:
    first, second = (fold.as_dict() for fold in fold_windows(protocol_from_request(plan_request("SPY"))))
    # The first fold's winner leaves the hold at its declared default (5 bars) out of its canonical point.
    winner = {"symbol": "SPY", "gap": 0.25, "rsi_min": 50.0, "rsi_max": 70.0}
    folds = [
        {**first, "status": "completed", "winner": winner, "train_metrics": metrics(1.2).as_dict(), "test_metrics": metrics(0.6, trades=11).as_dict(), "incumbent_test_metrics": None, "failure_reason": None},
        {**second, "status": "pending", "winner": None, "train_metrics": None, "test_metrics": None, "incumbent_test_metrics": None, "failure_reason": None},
    ]
    charts = fold_charts(_row({"validation": {"folds": folds, "verdict": None, "linked": [], "incumbent_linked": []}}))

    assert (charts["planned"], charts["in_progress"], charts["test_trades_total"], charts["median_retention"], charts["below_minimum"]) == (False, True, 11, None, None)
    assert charts["forward_minimum"] is not None and charts["linked"] == []
    drift = {knob["name"]: knob["folds"] for knob in charts["drift"]}
    assert drift == {"gap": [pytest.approx(0.25, abs=1e-12), None], "hold_bars": [5, None]}
    assert charts["folds"][0]["winner"] == winner and charts["folds"][1]["winner"] is None


def test_before_testing_over_time_the_folds_are_the_receipts_plan() -> None:
    charts = fold_charts(_row({}))
    assert (charts["planned"], charts["in_progress"], charts["test_trades_total"]) == (True, False, None)
    assert {fold["status"] for fold in charts["folds"]} == {"planned"}
