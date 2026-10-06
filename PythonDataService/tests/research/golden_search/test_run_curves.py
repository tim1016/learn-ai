"""Every run of a study by what asked for it, and the curve each kept (#2930)."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.research.golden_search.declarations import declaration_for, point_hash
from app.research.golden_search.evidence import drawdown_series
from app.research.golden_search.models import EvaluationRecord, StudyRow
from app.research.golden_search.procedure_history import FoldWindow
from app.research.golden_search.run_curves import curve_view, curves_view, group_of, named_points, runs_page

EMA = declaration_for("ema_crossover_signal")
assert EMA is not None
STRATEGY = "ema_crossover_signal"
INCUMBENT = {"symbol": "SPY", "gap": 0.2, "gap_bps": 0.0, "rsi_min": 50.0, "rsi_max": 70.0}
WINNER = {**INCUMBENT, "gap": 0.3}
FOLD_WINNER = {**INCUMBENT, "gap": 0.4}
# Fold 0 trains on [0, 100) and tests on [100, 200); fold 1 trains on [100, 200) — fold 0's test window — and tests on [200, 300).
FOLDS = [FoldWindow(0, 0, 100, 100, 200), FoldWindow(1, 100, 200, 200, 300)]


def _run(key: str, stage: str, point: dict[str, Any] = WINNER, **overrides: Any) -> EvaluationRecord:
    base: dict[str, Any] = dict(
        study_id="s1", evaluation_key=key, point_hash=point_hash(STRATEGY, point), point=point, window_start_ms=0, window_end_ms=300,
        scenario="base", detail=False, stage=stage, fold_index=None, status="completed", attempt=1, retries=0, total_trades=40,
        net_profit=1_000.0, total_return_pct=0.01, sharpe_ratio=1.0, max_drawdown_pct=0.1, win_rate=0.5, error=None, detail_json=None,
        created_at_ms=0, completed_at_ms=1, has_curve=True,
    )
    return EvaluationRecord(**{**base, **overrides})


def _study(**results: Any) -> StudyRow:
    return StudyRow(
        id="s1", parent_study_id=None, strategy_key=STRATEGY, symbol="SPY", state="awaiting_candidate", revision=0, status="idle", attempt=1,
        job_id=None, pending_stage=None, stage_token=None, created_at_ms=0, updated_at_ms=0, finished_at_ms=None,
        protocol={"incumbent": {"params": INCUMBENT}, "stress": [{"key": "slippage_1c", "label": "Extra 1¢/share slippage"}]},
        protocol_hash="h", receipt={}, results=results, candidate_key=None, exam_locked=False, decision=None, budget_cap=5000,
        consumed_evaluations=0, cache_hits=0, invalid_points=0, incomplete=False, failure_reason=None, hidden=False,
    )


def _page(records: list[EvaluationRecord], **query: Any) -> dict[str, Any]:
    study = _study(search={"procedure": {"winner": WINNER}}, validation={"folds": [{"fold_index": 0, "winner": FOLD_WINNER}, {"fold_index": 1, "winner": None}]})
    return runs_page(study, records, folds=FOLDS, declaration=EMA, **query)


@pytest.mark.parametrize(
    ("overrides", "group"),
    [
        ({"stage": "search"}, "search"),
        ({"stage": "pair_audit"}, "pair_audit"),
        ({"stage": "recent"}, "recent"),
        ({"stage": "validation", "fold_index": 0, "window_start_ms": 0, "window_end_ms": 100}, "fold_training"),
        ({"stage": "validation", "fold_index": 0, "window_start_ms": 100, "window_end_ms": 200}, "fold_test"),
        # The same window is fold 1's training window: the fold decides, never the window alone.
        ({"stage": "validation", "fold_index": 1, "window_start_ms": 100, "window_end_ms": 200}, "fold_training"),
        ({"stage": "evidence", "detail": True}, "candidate"),
        ({"stage": "evidence"}, "neighbor"),
        ({"stage": "evidence", "scenario": "slippage_1c"}, "stress"),
        ({"stage": "exam", "detail": True}, "final"),
    ],
)
def test_a_run_belongs_to_the_group_of_the_step_that_asked_for_it(overrides: dict[str, Any], group: str) -> None:
    record = _run("k", **{"stage": "search", **overrides})
    assert group_of(record, {fold.fold_index: fold for fold in FOLDS}) == group


def test_a_run_from_an_unknown_step_is_refused_not_filed_somewhere() -> None:
    with pytest.raises(ValueError, match="unknown step"):
        group_of(_run("k", "mystery"), {})


def test_settings_carry_the_names_the_study_gives_them_and_a_fold_winner_only_inside_its_fold() -> None:
    names = named_points(_study(search={"procedure": {"winner": WINNER}}, recent={"procedure": {"winner": WINNER}}, validation={"folds": [{"fold_index": 0, "winner": FOLD_WINNER}]}))

    assert names.of(_run("a", "search", INCUMBENT)) == ["Current settings"]
    assert names.of(_run("b", "search", WINNER)) == ["All-period fit", "Recent fit"]
    assert names.of(_run("c", "validation", FOLD_WINNER, fold_index=0)) == ["Fold winner"]
    assert names.of(_run("d", "validation", FOLD_WINNER, fold_index=1)) == []


def test_the_page_lists_one_group_sorted_with_unmeasured_runs_last_beside_every_groups_counts() -> None:
    records = [
        _run("s1", "search", total_return_pct=0.01),
        _run("s2", "search", INCUMBENT, total_return_pct=0.03),
        _run("s3", "search", total_return_pct=None, status="failed", error="no bars", has_curve=False),
        _run("s4", "search", total_return_pct=0.02),
        _run("x1", "evidence", scenario="slippage_1c"),
        _run("f1", "validation", FOLD_WINNER, fold_index=0, window_start_ms=100, window_end_ms=200),
    ]

    best_first = _page(records, group="search", sort="total_return_pct", descending=True)
    assert [row["evaluation_key"] for row in best_first["rows"]] == ["s2", "s4", "s1", "s3"]
    # A run without the measure stays last when the sort turns round: it is not the worst, it is unmeasured.
    assert [row["evaluation_key"] for row in _page(records, group="search", sort="total_return_pct")["rows"]] == ["s1", "s4", "s2", "s3"]
    assert (best_first["total"], best_first["sort"], best_first["folds"]) == (4, "total_return_pct", 2)
    assert best_first["groups"] == [
        # The failed run never had a curve to keep; it is not one recorded before curves.
        {"key": "search", "label": "All-period search", "runs": 4, "completed": 3, "with_curve": 3},
        {"key": "fold_test", "label": "Fold test", "runs": 1, "completed": 1, "with_curve": 1},
        {"key": "stress", "label": "Cost stress", "runs": 1, "completed": 1, "with_curve": 1},
    ]
    assert best_first["rows"][0]["label"] == "All-period search · Current settings"
    assert best_first["rows"][0]["params_sentence"].startswith("Gap $0.20")

    everything = _page(records)
    assert [row["label"] for row in everything["rows"][-2:]] == ["Cost stress · Extra 1¢/share slippage · All-period fit", "Fold 1 test · Fold winner"]
    second = _page(records, page=2, page_size=4)
    assert (second["total"], [row["evaluation_key"] for row in second["rows"]]) == (6, ["x1", "f1"])
    with pytest.raises(ValueError, match="page"):
        _page(records, page=0)


def test_one_fold_of_a_group_can_be_listed_alone_and_two_runs_never_read_the_same() -> None:
    records = [
        _run("a0", "validation", fold_index=0, window_start_ms=0, window_end_ms=100),
        _run("b0", "validation", INCUMBENT, fold_index=0, window_start_ms=0, window_end_ms=100),
        _run("a1", "validation", fold_index=1, window_start_ms=100, window_end_ms=200),
    ]

    first_fold = _page(records, group="fold_training", fold_index=0)

    assert [row["evaluation_key"] for row in first_fold["rows"]] == ["a0", "b0"]
    # The same settings trained in two folds differ by label; two points of one fold differ by their settings.
    described = [(row["label"], row["params_sentence"]) for row in _page(records, group="fold_training")["rows"]]
    assert len(set(described)) == len(described) == 3


def test_a_curve_is_the_return_on_starting_capital_and_the_fall_from_its_own_peak() -> None:
    days = [session_close_ms_utc(day) for day in (date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 8))]
    daily = [(days[0], 101_000.0), (days[1], 99_990.0), (days[2], 102_010.0)]

    view = curve_view("k", daily, 100_000.0)

    assert view["close_ms"] == days
    assert view["cumulative_return"] == pytest.approx([0.01, -0.0001, 0.0201], abs=1e-12, rel=0)
    assert view["drawdown"] == pytest.approx([0.0, 99_990.0 / 101_000.0 - 1.0, 0.0], abs=1e-12, rel=0)
    assert view["drawdown"] == [value for _, value in drawdown_series(daily)]
    # The deepest fall is the series' own lowest point, worked out here and not in the browser.
    assert view["deepest_drawdown"] == pytest.approx(99_990.0 / 101_000.0 - 1.0, abs=1e-12, rel=0)


def test_a_run_whose_equity_never_rose_above_zero_has_a_return_and_no_fall_from_peak() -> None:
    view = curve_view("k", [(1, 0.0), (2, 0.0)], 100_000.0)
    assert view["cumulative_return"] == [-1.0, -1.0] and view["drawdown"] is None and view["deepest_drawdown"] is None


def test_curves_come_back_in_the_order_asked_and_a_run_that_kept_none_is_named_not_drawn() -> None:
    stored = {"b": [(1, 100.0)], "a": [(1, 110.0)]}

    view = curves_view(["a", "old", "b"], stored, 100.0)

    assert [curve["evaluation_key"] for curve in view["curves"]] == ["a", "b"]
    assert view["not_recorded"] == ["old"]
