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
from app.research.golden_search.run_curves import (
    MAX_OTHER_LINES,
    curve_set_lines,
    curve_view,
    curves_view,
    group_of,
    named_points,
    runs_page,
)

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


def test_two_unnamed_points_that_differ_only_in_the_bps_gap_read_differently() -> None:
    plain = {**INCUMBENT, "gap": 0.5}
    records = [_run("p0", "search", plain), _run("p1", "search", {**plain, "gap_bps": 2.5})]

    rows = _page(records, group="search")["rows"]

    assert [row["label"] for row in rows] == ["All-period search", "All-period search"]
    assert rows[0]["params_sentence"] != rows[1]["params_sentence"]
    assert "normalized gap 2.5 bps" in rows[1]["params_sentence"] and "bps" not in rows[0]["params_sentence"]


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


# ── A step's set of lines ────────────────────────────────────────────────

DEVELOPMENT = (0, 300)
RECENT = (200, 300)
SEARCHED = {"procedure": {"winner": WINNER}, "window": {"start_ms": 0, "end_ms": 300}}


def _set_study(**results: Any) -> StudyRow:
    study = _study(**results)
    protocol = {
        **study.protocol,
        "development_start_ms": DEVELOPMENT[0],
        "development_end_ms": DEVELOPMENT[1],
        "knobs": [{"name": "gap", "mode": "search"}, {"name": "rsi_min", "mode": "fixed"}],
        "stress": [{"key": "slippage_1c", "label": "Extra 1¢/share slippage"}],
    }
    return StudyRow(**{**{field: getattr(study, field) for field in study.__dataclass_fields__}, "protocol": protocol})


def _chosen(records: list[EvaluationRecord], curve_set: str, candidate: str | None = None, **results: Any) -> Any:
    return curve_set_lines(_set_study(**results), records, folds=FOLDS, declaration=EMA, curve_set=curve_set, candidate_key=candidate)  # type: ignore[arg-type]


def _drawn(records: list[EvaluationRecord], curve_set: str, candidate: str | None = None, **results: Any) -> list[tuple[str, str, str]]:
    return [(line.record.evaluation_key, line.role, line.label) for line in _chosen(records, curve_set, candidate, **results).lines]


def test_a_search_set_is_every_point_the_procedure_scored_with_its_winner_and_the_incumbent_named() -> None:
    records = [
        _run("s1", "search", INCUMBENT),
        _run("s2", "search", {**INCUMBENT, "gap": 0.25}),
        _run("s3", "search", WINNER),
        # Another procedure's point, and a neighbor the evidence asked for, are not this search's.
        _run("r1", "recent", WINNER, window_start_ms=200),
        _run("n1", "evidence", {**INCUMBENT, "gap": 0.35}),
    ]

    assert _drawn(records, "search", search=SEARCHED) == [("s1", "incumbent", "Current settings"), ("s2", "other", "Scored point"), ("s3", "winner", "Winner")]
    assert _drawn(records, "recent", recent={"procedure": {"winner": WINNER}, "window": {"start_ms": 200, "end_ms": 300}})[0] == ("r1", "winner", "Winner")


def test_a_search_that_did_not_score_the_incumbent_draws_it_from_the_step_that_did_and_says_so_when_none_did() -> None:
    # The search started from another seed, as it must when the incumbent is a qualified version.
    searched = [_run("s3", "search", WINNER), _run("c1", "evidence", INCUMBENT, detail=True)]

    assert _drawn(searched, "search", search=SEARCHED) == [("s3", "winner", "Winner"), ("c1", "incumbent", "Current settings")]
    # Nothing ran the incumbent on the recent window: the set says so and draws no line for it.
    recent = _chosen([_run("r1", "recent", WINNER, window_start_ms=200)], "recent", recent={"procedure": {"winner": WINNER}, "window": {"start_ms": 200, "end_ms": 300}})
    assert [line.role for line in recent.lines] == ["winner"]
    assert recent.notes == ("The current settings were not run on this window, so they have no line here.",)


def test_an_incumbent_run_that_kept_no_curve_gives_way_to_one_that_did_and_the_set_says_so_when_none_did() -> None:
    # The search's own run of the incumbent kept no curve; the evidence's detail run of it did.
    twinned = [_run("s1", "search", INCUMBENT, has_curve=False), _run("s3", "search", WINNER), _run("c1", "evidence", INCUMBENT, detail=True)]
    chosen = _chosen(twinned, "search", search=SEARCHED)

    assert [(line.record.evaluation_key, line.role) for line in chosen.lines] == [("s3", "winner"), ("c1", "incumbent")]
    assert (chosen.notes, chosen.not_recorded) == ((), 0)
    # A failed run, and no other on the window: no line, and the set says why.
    failed = _chosen([_run("s1", "search", INCUMBENT, status="failed", has_curve=False), _run("s3", "search", WINNER)], "search", search=SEARCHED)
    assert [line.role for line in failed.lines] == ["winner"]
    assert failed.notes == ("The current settings’ run on this window kept no curve, so they have no line here.",)


def test_a_named_run_is_a_base_cost_run_and_the_first_that_kept_a_curve() -> None:
    records = [
        # A stressed run of the incumbent recorded first is never its line; nor is an earlier run that kept no curve.
        _run("x-inc", "evidence", INCUMBENT, scenario="slippage_1c"),
        _run("old", "evidence", INCUMBENT, has_curve=False),
        _run("c1", "evidence", INCUMBENT, detail=True),
        _run("s3", "search", WINNER),
    ]

    assert _drawn(records, "search", search=SEARCHED) == [("s3", "winner", "Winner"), ("c1", "incumbent", "Current settings")]


def test_a_search_that_returned_the_incumbent_names_one_line_for_both() -> None:
    kept = {"procedure": {"winner": INCUMBENT}, "window": {"start_ms": 0, "end_ms": 300}}
    chosen = _chosen([_run("s1", "search", INCUMBENT), _run("s2", "search", WINNER)], "search", search=kept)

    assert [(line.role, line.label) for line in chosen.lines] == [("winner", "Winner · current settings"), ("other", "Scored point")]
    assert chosen.notes == ()


def test_a_fold_test_set_finds_each_run_by_what_it_is_even_when_another_step_scored_it_first() -> None:
    validation = {"folds": [{"fold_index": 0, "winner": FOLD_WINNER}, {"fold_index": 1, "winner": WINNER}]}
    records = [
        # Fold 1's test window is the recent window here: the recent search scored both runs first.
        _run("r-win", "recent", WINNER, window_start_ms=200, window_end_ms=300),
        _run("r-inc", "recent", INCUMBENT, window_start_ms=200, window_end_ms=300),
        _run("t0", "validation", INCUMBENT, fold_index=0, window_start_ms=100, window_end_ms=200),
        _run("w0", "validation", FOLD_WINNER, fold_index=0, window_start_ms=100, window_end_ms=200),
        # A training run of the winner is not a test.
        _run("x0", "validation", FOLD_WINNER, fold_index=0, window_start_ms=0, window_end_ms=100),
    ]

    assert _drawn(records, "fold_tests", validation=validation) == [
        ("w0", "winner", "Fold 1 winner"),
        ("t0", "incumbent", "Fold 1 current settings"),
        ("r-win", "winner", "Fold 2 winner"),
        ("r-inc", "incumbent", "Fold 2 current settings"),
    ]
    # A fold with no winner still shows the incumbent; a fold that chose the incumbent is one line.
    assert _drawn(records, "fold_tests", validation={"folds": [{"fold_index": 0, "winner": None}, {"fold_index": 1, "winner": INCUMBENT}]}) == [
        ("t0", "incumbent", "Fold 1 current settings"),
        ("r-inc", "winner", "Fold 2 winner · current settings"),
    ]


def _evidence(key: str, point: dict[str, Any], *, below: tuple[float, str] | None = None, above: tuple[float, str] | None = None) -> dict[str, Any]:
    """A stored candidate; ``below`` / ``above`` are its gap neighbors as the audit recorded them (value, status), none when it was not audited."""
    neighbors = []
    if below is not None and above is not None:
        rows = [{"value": value, "status": status, "metrics": None, "reason": None} for value, status in (below, (point["gap"], "center"), above)]
        neighbors = [{"knob": "gap", "rows": rows, "one_sided": False}]
    return {"key": key, "point": point, "point_hash": point_hash(STRATEGY, point), "neighbors_audited": bool(neighbors), "neighbors": neighbors}


def test_a_candidate_set_is_its_own_run_its_stresses_and_its_audited_neighbors_wherever_they_were_scored() -> None:
    below, above = {**INCUMBENT, "gap": 0.25}, {**INCUMBENT, "gap": 0.35}
    records = [
        # The search scored the candidate and one neighbor before the evidence asked; the other is the evidence's own.
        _run("s-win", "search", WINNER),
        _run("s-below", "search", below),
        _run("d-win", "evidence", WINNER, detail=True),
        _run("n-above", "evidence", above),
        _run("x-win", "evidence", WINNER, scenario="slippage_1c"),
        # The same settings on the final test, and another point's stress, are not this candidate's development evidence.
        _run("f-win", "exam", WINNER, detail=True, window_start_ms=300, window_end_ms=400),
        _run("x-inc", "evidence", INCUMBENT, scenario="slippage_1c"),
    ]
    evidence = {"candidates": [_evidence("all_period", WINNER, below=(0.25, "tested"), above=(0.35, "tested")), _evidence("incumbent", INCUMBENT)]}

    assert _drawn(records, "candidate", "all_period", evidence=evidence) == [
        ("s-win", "candidate", "All-period fit"),
        ("x-win", "stress", "Extra 1¢/share slippage"),
        ("s-below", "neighbor", "Crossover gap one step below (0.25)"),
        ("n-above", "neighbor", "Crossover gap one step above (0.35)"),
    ]
    # The incumbent's neighbors were not audited: the search's nearby points are not passed off as an audit.
    incumbent = _drawn([*records, _run("s-inc", "search", INCUMBENT)], "candidate", "incumbent", evidence=evidence)
    assert incumbent == [("s-inc", "candidate", "Current settings"), ("x-inc", "stress", "Extra 1¢/share slippage")]
    assert _drawn(records, "candidate", "recent", evidence=evidence) == []
    # The audit ran out of budget before the neighbor below: the search's run of that point is not drawn as its audit.
    partly = {"candidates": [_evidence("all_period", WINNER, below=(0.25, "untested"), above=(0.35, "tested"))]}
    assert [key for key, role, _ in _drawn(records, "candidate", "all_period", evidence=partly) if role == "neighbor"] == ["n-above"]


def test_a_neighbor_is_found_by_the_value_its_audit_recorded_not_by_the_knobs_step_today() -> None:
    # The audit stepped the gap by 0.2 when it ran; the knob's neighbor step is another size now.
    records = [
        _run("d-win", "evidence", WINNER, detail=True),
        _run("n-far", "evidence", {**INCUMBENT, "gap": 0.1}),
        # One step below by today's declaration, scored by the search: never the audit's neighbor.
        _run("s-near", "search", {**INCUMBENT, "gap": 0.3 - float(EMA.knob("gap").neighbor_step)}),
    ]
    evidence = {"candidates": [_evidence("all_period", WINNER, below=(0.1, "tested"), above=(0.5, "tested"))]}

    neighbors = [(key, label) for key, role, label in _drawn(records, "candidate", "all_period", evidence=evidence) if role == "neighbor"]

    assert neighbors == [("n-far", "Crossover gap one step below (0.1)")]


def test_a_set_marks_the_current_settings_whatever_their_role_and_counts_its_failed_runs() -> None:
    kept = {"procedure": {"winner": INCUMBENT}, "window": {"start_ms": 0, "end_ms": 300}}
    returned = _chosen([_run("s1", "search", INCUMBENT), _run("s2", "search", WINNER)], "search", search=kept)
    assert [(line.role, line.incumbent) for line in returned.lines] == [("winner", True), ("other", False)]
    searched = _chosen([_run("s1", "search", INCUMBENT), _run("s3", "search", WINNER)], "search", search=SEARCHED)
    assert [(line.role, line.incumbent) for line in searched.lines] == [("incumbent", True), ("winner", False)]
    evidence = {"candidates": [_evidence("incumbent", INCUMBENT)]}
    assert [line.incumbent for line in _chosen([_run("c1", "evidence", INCUMBENT, detail=True)], "candidate", "incumbent", evidence=evidence).lines] == [True]

    # Every run of the set failed: nothing to draw, and the set says they failed rather than that nothing ran.
    failed = _chosen([_run("s1", "search", INCUMBENT, status="failed", has_curve=False), _run("s3", "search", WINNER, status="failed", has_curve=False)], "search", search=SEARCHED)
    assert (failed.lines, failed.failed, failed.not_recorded) == ((), 2, 0)


def test_a_set_keeps_every_named_line_spreads_the_others_over_the_search_and_counts_only_completed_runs_without_a_curve() -> None:
    crowd = [_run(f"o{index:04d}", "search", {**INCUMBENT, "gap": 1 + index / 1000}) for index in range(MAX_OTHER_LINES * 2)]
    records = [
        *crowd,
        _run("old", "search", {**INCUMBENT, "gap": 9.0}, has_curve=False),
        _run("failed", "search", {**INCUMBENT, "gap": 9.1}, status="failed", has_curve=False),
        _run("pending", "search", {**INCUMBENT, "gap": 9.2}, status="pending", has_curve=False),
        _run("inc", "search", INCUMBENT),
        _run("win", "search", WINNER),
    ]

    chosen = _chosen(records, "search", search=SEARCHED)

    drawn = [line.record.evaluation_key for line in chosen.lines]
    others = [key for key in drawn if key.startswith("o")]
    # Both named lines arrived last and are still drawn; the others are every second point, first to last.
    assert drawn[-2:] == ["inc", "win"] and len(others) == MAX_OTHER_LINES
    assert others == [f"o{index:04d}" for index in range(0, MAX_OTHER_LINES * 2, 2)]
    # A run still pending or one that failed never had a curve to keep.
    assert (chosen.left_out, chosen.not_recorded) == (MAX_OTHER_LINES, 1)
