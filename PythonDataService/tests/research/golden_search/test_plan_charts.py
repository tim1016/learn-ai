"""The Plan step's charts (#2821): hand-counted sessions and positions, ``atol=1e-9``."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Any

import pytest

from app.research.golden_search.activity import TradeFloors, activity_plan
from app.research.golden_search.budget import review_protocol
from app.research.golden_search.declarations import declaration_for
from app.research.golden_search.models import StudyRow
from app.research.golden_search.plan_charts import coverage, minimums, runs_used, search_space, windows, workload
from app.research.golden_search.planning import fold_windows, protocol_from_request
from app.research.golden_search.protocol import GoldenSearchProtocol
from app.utils.session_anchors import et_midnight_ms
from tests._helpers.golden_search_study import frequency_plan_request, plan_request

EMA = declaration_for("ema_crossover_signal")
assert EMA is not None


def _row(protocol: GoldenSearchProtocol, **receipt: Any) -> StudyRow:
    review = review_protocol(protocol, EMA)
    assert review.estimate is not None
    base = {
        "intervals": {"data_start_ms": protocol.development_start_ms - 20 * 86_400_000},
        "folds": [fold.as_dict() for fold in fold_windows(protocol)],
        "activity": activity_plan(protocol),
        "estimate": review.estimate.as_dict(),
    }
    return StudyRow(
        id="s1", parent_study_id=None, strategy_key="ema_crossover_signal", symbol="SPY", state="locked", revision=0,
        status="idle", attempt=0, job_id=None, pending_stage=None, stage_token=None, created_at_ms=0, updated_at_ms=0,
        finished_at_ms=None, protocol=protocol.as_dict(), protocol_hash="h", receipt={**base, **receipt}, results={}, candidate_key=None,
        exam_locked=False, decision=None, budget_cap=5000, consumed_evaluations=120, cache_hits=0, invalid_points=0,
        incomplete=False, failure_reason=None, hidden=False,
    )  # fmt: skip


def _protocol(rate: int | None = 100) -> GoldenSearchProtocol:
    """The test plan; with an expected trade frequency its floors come from the receipt."""
    return protocol_from_request(plan_request("SPY") if rate is None else frequency_plan_request("SPY", rate))


def test_every_window_counts_its_sessions_and_takes_the_floor_the_stages_use() -> None:
    protocol = _protocol()
    row = _row(protocol)
    found = {window["key"]: window for window in windows(row, protocol, TradeFloors(protocol, row.receipt))}

    frozen = {window["key"]: window for window in row.receipt["activity"]["windows"]}
    # The calendar's count agrees with the sessions the receipt froze for every window it froze.
    for key in ("development", "final", "forward"):
        assert (found[key]["sessions"], found[key]["minimum_trades"]) == (frozen[key]["trading_sessions"], frozen[key]["minimum_trades"])
    assert found["training_0"]["minimum_trades"] == frozen["training_0"]["minimum_trades"]
    # A single fold's test and the run-up have no minimum of their own.
    assert found["test_0"]["minimum_trades"] is None and found["run_up"]["minimum_trades"] is None
    assert [window["kind"] for window in found.values()][:2] == ["run_up", "development"]


def test_a_window_counts_the_calendars_sessions_by_hand() -> None:
    protocol = _protocol()
    # The run-up from Mon 2 Dec 2024 to the development start (1 Jan 2025): 21 sessions in December (Christmas closed).
    row = _row(protocol, intervals={"data_start_ms": et_midnight_ms(date(2024, 12, 2))})
    run_up = windows(row, protocol, TradeFloors(protocol, row.receipt))[0]
    assert (run_up["key"], run_up["sessions"], run_up["minimum_trades"]) == ("run_up", 21, None)


def test_the_search_space_places_each_range_and_the_incumbent_in_its_legal_domain() -> None:
    protocol = _protocol()
    knobs = {knob["name"]: knob for knob in search_space(_row(protocol), protocol)}

    # Gap searched 0.00–0.60 by 0.05 in a 0–2 domain, the incumbent at 0.2; the hold 2–12 by 1 in a 1–… domain.
    gap = knobs["gap"]
    assert (gap["searched"], gap["values"], gap["step"]) == (True, 13, pytest.approx(0.05, abs=1e-12))
    assert (gap["low_position"], gap["high_position"], gap["current_position"]) == pytest.approx((0.0, 0.3, 0.1), abs=1e-9, rel=0)
    # A held knob is one value: RSI lower gate 50 in a 0–100 domain.
    rsi = knobs["rsi_min"]
    assert (rsi["searched"], rsi["values"], rsi["low"], rsi["high"], rsi["low_position"]) == (False, 1, 50.0, 50.0, pytest.approx(0.5, abs=1e-9))
    assert list(knobs) == [plan.name for plan in protocol.knobs]
    # Zoom starts at the seed, the current settings here; Grid has no start.
    assert (gap["start"], gap["start_position"]) == (pytest.approx(0.2, abs=1e-12), pytest.approx(0.1, abs=1e-9, rel=0))
    grid = replace(protocol, method="grid")
    assert {knob["start"] for knob in search_space(_row(grid), grid)} == {None}


def test_workload_counts_each_steps_runs_under_the_stage_that_planned_them() -> None:
    protocol = _protocol()
    row = _row(protocol)
    # The search plans its pair audits too; the proof draws its units outside the evaluator.
    used = runs_used({"search": 10, "pair_audit": 25, "validation": 4, "evidence": 6, "exam": 2}, 3)
    load = workload(row, used)

    stages = {stage["stage"]: stage for stage in load["stages"]}
    assert {key: stage["used"] for key, stage in stages.items()} == {"search": 35, "recent": 0, "validation": 4, "evidence": 6, "exam": 2, "proof": 3}
    assert sum(stage["used"] for stage in load["stages"]) == 10 + 25 + 4 + 6 + 2 + 3
    assert stages["search"]["planned"] == next(item["max_evaluations"] for item in row.receipt["estimate"]["stages"] if item["stage"] == "search")
    assert (load["cap"], load["consumed"]) == (5000, 120)


def test_an_evaluation_step_the_estimate_does_not_plan_fails_loudly() -> None:
    with pytest.raises(ValueError, match="no row in the study's estimate"):
        runs_used({"rehearsal": 1}, 0)


def test_a_frequency_plan_shows_its_yearly_terms_and_a_fixed_plan_its_two_floors() -> None:
    protocol = _protocol()
    frequency = minimums(_row(protocol), protocol)
    development = next(window for window in frequency["windows"] if window["key"] == "development")
    expected = sum(year["selected_sessions"] / year["year_sessions"] for year in development["years"])
    assert development["trading_years"] == pytest.approx(expected, abs=1e-9, rel=0)

    fixed = _protocol(rate=None)
    flat = minimums(_row(fixed, activity=None), fixed)
    assert flat["expected_trades_per_year"] is None
    assert [(window["key"], window["minimum_trades"], window["trading_years"]) for window in flat["windows"]] == [("selection", 1, None), ("final", 1, None)]


def test_coverage_counts_each_months_sessions_by_lake_status_and_missing_rows_as_missing() -> None:
    row = _row(_protocol(), intervals={"data_start_ms": et_midnight_ms(date(2024, 6, 3))})
    protocol = replace(_protocol(), final_end_ms=et_midnight_ms(date(2024, 8, 1)))
    statuses = {date(2024, 6, 3): "complete", date(2024, 6, 4): "failed", date(2024, 6, 5): "stale", date(2024, 7, 1): "fetching"}

    months = coverage(row, protocol, statuses)["months"]
    # June 2024 from the 3rd: 19 sessions (Juneteenth closed); July: 22 (the 4th closed).
    assert [(m["year"], m["month"], m["sessions"], m["complete"], m["fetching"], m["stale"], m["failed"], m["missing"]) for m in months] == [
        (2024, 6, 19, 1, 0, 1, 1, 16),
        (2024, 7, 22, 0, 1, 0, 0, 21),
    ]
    assert coverage(row, protocol, "catalog down") == {"status": "missing", "reason": "catalog down"}
    with pytest.raises(ValueError, match="does not know"):
        coverage(row, protocol, {date(2024, 6, 3): "archived"})
