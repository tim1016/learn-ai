"""The Test over time step's charts: each fold's windows and results, beside the frozen incumbent on the same test (#2821).

Formula, per fold i over its stored record (or, before validation runs, the
fold windows the study's receipt froze at lock):
  * train and test Sharpe — the fold winner's training and test runs;
  * retention_i — ``procedure_history.fold_evidence``'s retention, the
    verdict's own per-fold value: test Sharpe / training Sharpe, undefined
    unless the fold completed with a positive training Sharpe;
  * test return and the incumbent's — ``procedure_history.fold_return`` on
    the same test window; their difference test − incumbent when both exist;
  * test trades — the winner's test trades on a completed fold; the forward
    total is the verdict's out-of-sample trade count against the forward
    minimum ``TradeFloors.at`` it used. While the stage runs (folds stored, no
    verdict yet) the total sums the folds completed so far and is marked in
    progress, so it is never judged against the minimum;
  * drift — each searched knob's value in each fold's winner, the all-period
    winner and the incumbent, an omitted default standing at its declared
    value.
Reference: PRD https://github.com/tim1016/learn-ai/issues/2821 "Server work by
  chart" (V12–V17); the verdict is app/research/walk_forward_study/verdict.py.
Canonical implementation: this file.
Validated against: tests/research/golden_search/test_fold_charts.py.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.research.golden_search.activity import TradeFloors
from app.research.golden_search.declarations import declaration_for, knob_values, scalar
from app.research.golden_search.models import StudyRow
from app.research.golden_search.procedure_history import fold_evidence, fold_return
from app.research.golden_search.protocol import GoldenSearchProtocol
from app.research.golden_search.selection import Metrics
from app.research.walk_forward_study.verdict import RETENTION_THRESHOLD


def _metrics(stored: Mapping[str, Any] | None) -> Metrics | None:
    return None if stored is None else Metrics.from_dict(stored)


def _completed(metrics: Metrics | None) -> Metrics | None:
    return metrics if metrics is not None and metrics.status == "completed" else None


def fold_view(fold: Mapping[str, Any]) -> dict[str, Any]:
    """One fold's windows, status and the numbers its charts draw; a planned or pending fold has none."""
    completed = fold["status"] == "completed"
    train = _metrics(fold.get("train_metrics"))
    test = _metrics(fold.get("test_metrics")) if completed else None
    incumbent = _completed(_metrics(fold.get("incumbent_test_metrics")))
    test_return = fold_return(test)
    incumbent_return = fold_return(incumbent)
    return {
        "fold_index": fold["fold_index"],
        "winner": fold.get("winner"),
        "train_start_ms": fold["train_start_ms"],
        "train_end_ms": fold["train_end_ms"],
        "test_start_ms": fold["test_start_ms"],
        "test_end_ms": fold["test_end_ms"],
        "status": fold["status"],
        "failure_reason": fold.get("failure_reason"),
        "train_sharpe": None if train is None else train.sharpe_ratio,
        "test_sharpe": None if test is None else test.sharpe_ratio,
        "retention": fold_evidence(fold["fold_index"], train if completed else None, test).retention,
        "test_return": test_return,
        "incumbent_return": incumbent_return,
        "return_difference": None if test_return is None or incumbent_return is None else test_return - incumbent_return,
        "test_trades": None if test is None else test.total_trades,
        "incumbent_trades": None if incumbent is None else incumbent.total_trades,
    }


def _drift(row: StudyRow, protocol: GoldenSearchProtocol, folds: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    declaration = declaration_for(row.strategy_key)
    if declaration is None:
        return []
    search = row.results.get("search")
    all_period = None if search is None else knob_values(declaration, search["procedure"]["winner"])
    current = knob_values(declaration, protocol.incumbent.params)
    winners = [None if fold.get("winner") is None else knob_values(declaration, fold["winner"]) for fold in folds]
    drift = []
    for plan in protocol.search_knobs:
        knob = declaration.knob(plan.name)
        drift.append(
            {
                "name": knob.name,
                "label": knob.label,
                "unit": knob.unit,
                "low": plan.low,
                "high": plan.high,
                "current": scalar(knob, current[knob.name]),
                "all_period": None if all_period is None else scalar(knob, all_period[knob.name]),
                "folds": [None if values is None else scalar(knob, values[knob.name]) for values in winners],
            }
        )
    return drift


def _forward_minimum(protocol: GoldenSearchProtocol, receipt: Mapping[str, Any], forward: tuple[int, int]) -> int | None:
    try:
        return TradeFloors(protocol, receipt).at(forward)
    except ValueError:
        # A receipt that froze no floor for these folds' forward window (a stage that failed on the mismatch): none to show.
        return None


def fold_charts(row: StudyRow) -> dict[str, Any]:
    """The Test over time charts' read: every fold, planned or run, and what each chart draws from it."""
    protocol = GoldenSearchProtocol.from_dict(row.protocol)
    validation = row.results.get("validation")
    stored: list[Mapping[str, Any]] = (
        list(validation["folds"]) if validation is not None else [{**fold, "status": "planned"} for fold in row.receipt.get("folds", [])]
    )
    folds = [fold_view(fold) for fold in stored]
    verdict = None if validation is None else validation.get("verdict")
    forward = (stored[0]["test_start_ms"], stored[-1]["test_end_ms"]) if stored else None
    completed = [fold for fold in folds if fold["status"] == "completed"]
    in_progress = validation is not None and verdict is None
    return {
        "planned": validation is None,
        "in_progress": in_progress,
        "folds": folds,
        "linked": [] if validation is None else list(validation.get("linked", [])),
        "incumbent_linked": [] if validation is None else list(validation.get("incumbent_linked", [])),
        "retention_threshold": RETENTION_THRESHOLD if verdict is None else verdict["retention_threshold"],
        "median_retention": None if verdict is None else verdict["study_retention"],
        "test_trades_total": verdict["oos_trade_count"] if verdict is not None else sum(fold["test_trades"] for fold in completed) if in_progress else None,
        "forward_minimum": None if forward is None else _forward_minimum(protocol, row.receipt, forward),
        "drift": _drift(row, protocol, list(stored)),
    }
