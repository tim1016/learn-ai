"""The Compare step's decision summary (#2811): stored evidence classified row by row, missing never read as a pass."""

from __future__ import annotations

from typing import Any

import pytest

from app.research.golden_search.activity import TradeFloors, activity_plan
from app.research.golden_search.decision_summary import decision_summaries
from app.research.golden_search.exposure_rules import EXPOSURE_EXPLANATIONS
from app.research.golden_search.planning import protocol_from_request
from app.research.golden_search.protocol import recent_window_ms
from tests._helpers.golden_search_study import frequency_plan_request

PROTOCOL = protocol_from_request(frequency_plan_request("SPY"))
FLOORS = TradeFloors(PROTOCOL, {"activity": activity_plan(PROTOCOL)})
DEVELOPMENT = (PROTOCOL.development_start_ms, PROTOCOL.development_end_ms)
RECENT = recent_window_ms(PROTOCOL)
# 50 trades a year: 12 over the 60-session development period, 5 over the 21-session recent window.
DEVELOPMENT_FLOOR, RECENT_FLOOR = FLOORS.at(DEVELOPMENT), FLOORS.at(RECENT)


def _metrics(trades: int = 20, net: float | None = 500.0, status: str = "completed") -> dict[str, Any]:
    return {"status": status, "total_trades": trades, "net_profit": net, "error": None if status == "completed" else "engine stopped"}


def _candidate(key: str = "all_period", **overrides: Any) -> dict[str, Any]:
    neighbor = {"value": 0.25, "status": "tested", "metrics": _metrics(net=100.0), "reason": None}
    return {
        "key": key,
        "development_metrics": _metrics(),
        "neighbors_audited": key != "incumbent",
        "neighbors": [] if key == "incumbent" else [{"knob": "gap", "rows": [neighbor], "one_sided": False}],
        "stress": [{"scenario": "slippage_1c", "label": "Extra 1¢/share slippage", "metrics": _metrics(net=200.0)}],
        "edge_hits": [],
        **overrides,
    }


def _results(*candidates: dict[str, Any], verdict: str | None = "still worked", folds: tuple[str, ...] = ("completed", "completed"), **extra: Any) -> dict[str, Any]:
    validation = {
        "verdict": None if verdict is None else {"label": verdict, "reason": "median out-of-sample Sharpe 0.800 is positive"},
        "folds": [{"status": status} for status in folds],
    }
    return {"evidence": {"candidates": list(candidates)}, "validation": validation, **extra}


def _rows(results: dict[str, Any], key: str = "all_period", exposure: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    summaries = decision_summaries(results, floors=FLOORS, development=DEVELOPMENT, exposure=exposure)
    return {row["key"]: row for row in next(item for item in summaries if item["candidate_key"] == key)["rows"]}


def test_no_summary_before_the_candidate_evidence_exists() -> None:
    assert decision_summaries({}, floors=FLOORS, development=DEVELOPMENT, exposure=None) == []


def test_development_activity_is_judged_against_the_frozen_development_floor() -> None:
    enough = _rows(_results(_candidate(development_metrics=_metrics(trades=DEVELOPMENT_FLOOR))))["development_activity"]
    short = _rows(_results(_candidate(development_metrics=_metrics(trades=DEVELOPMENT_FLOOR - 1))))["development_activity"]
    assert (enough["status"], short["status"]) == ("meets", "concern")
    assert short["text"] == f"{DEVELOPMENT_FLOOR - 1} trades over the development period, below its minimum of {DEVELOPMENT_FLOOR}."
    assert enough["link"] == {"kind": "tab", "target": "trades"}


@pytest.mark.parametrize("metrics", [None, _metrics(status="failed")])
def test_a_development_run_that_was_not_recorded_is_missing_and_so_is_its_stress(metrics: dict[str, Any] | None) -> None:
    rows = _rows(_results(_candidate(development_metrics=metrics)))
    assert rows["development_activity"]["status"] == rows["stress"]["status"] == "missing"


def test_recent_activity_uses_the_recent_windows_own_floor_and_says_when_nothing_met_it() -> None:
    def recent(stop_reason: str, metrics: dict[str, Any] | None) -> dict[str, Any]:
        window = {"start_ms": RECENT[0], "end_ms": RECENT[1]}
        return {"window": window, "procedure": {"stop_reason": stop_reason, "winner_metrics": metrics}, "counts": {}, "incomplete": False}

    def row(**recent_fit: Any) -> dict[str, Any]:
        return _rows(_results(_candidate("recent"), recent=recent(**recent_fit)), "recent")["recent_activity"]

    assert row(stop_reason="no_improvement", metrics=_metrics(trades=RECENT_FLOOR))["status"] == "meets"
    assert row(stop_reason="no_improvement", metrics=_metrics(trades=RECENT_FLOOR - 1))["status"] == "concern"
    # Nothing passing every rule (say, none made money) is judged on the starting point's own trade count.
    busy_seed = row(stop_reason="no_eligible", metrics=_metrics(trades=RECENT_FLOOR + 5, net=-1.0))
    assert busy_seed["status"] == "meets"
    assert busy_seed["text"] == f"{RECENT_FLOOR + 5} trades over the recent window; its minimum is {RECENT_FLOOR}. No setting met every rule there, so this candidate is its starting point."
    assert row(stop_reason="no_eligible", metrics=_metrics(trades=1))["status"] == "concern"
    assert row(stop_reason="budget", metrics=None)["status"] == "missing"
    # Only the recent candidate carries the row.
    assert "recent_activity" not in _rows(_results(_candidate()))


def test_test_over_time_reports_the_verdict_with_coverage_of_every_scheduled_fold() -> None:
    worked = _rows(_results(_candidate()))["test_over_time"]
    assert (worked["status"], worked["link"]) == ("meets", {"kind": "step", "target": "test"})
    assert worked["text"].endswith("2 of 2 scheduled folds completed.")
    holes = _rows(_results(_candidate(), verdict="could not be judged", folds=("completed", "failed")))["test_over_time"]
    assert holes["status"] == "missing" and holes["text"].endswith("1 of 2 scheduled folds completed.")
    assert _rows(_results(_candidate(), verdict="too few trades"))["test_over_time"]["status"] == "concern"
    assert _rows(_results(_candidate(), verdict=None))["test_over_time"]["status"] == "missing"
    # The verdict judges the search procedure, so the current settings carry no such row.
    assert "test_over_time" not in _rows(_results(_candidate("incumbent")), "incumbent")


def test_neighbors_name_the_knob_that_loses_and_never_pass_an_unrecorded_run() -> None:
    def hood(*rows: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"knob": "hold_bars", "rows": list(rows), "one_sided": False}]

    tested = {"value": 6.0, "status": "tested", "metrics": _metrics(net=50.0), "reason": None}
    losing = {**tested, "metrics": _metrics(net=-10.0)}
    untested = {**tested, "status": "untested", "metrics": None}
    null_net = {**tested, "metrics": _metrics(net=None)}
    assert _rows(_results(_candidate(neighbors=hood(tested))))["neighbors"]["status"] == "meets"
    assert _rows(_results(_candidate(neighbors=hood(tested, losing))))["neighbors"]["text"] == "A one-step change in hold_bars loses money."
    assert _rows(_results(_candidate(neighbors=hood(tested, untested))))["neighbors"]["status"] == "missing"
    assert _rows(_results(_candidate(neighbors=hood(null_net))))["neighbors"]["status"] == "missing"
    assert _rows(_results(_candidate("incumbent")), "incumbent")["neighbors"]["status"] == "missing"


def test_stress_names_the_scenario_that_turns_a_loss_and_never_passes_an_unrecorded_run() -> None:
    def stressed(*nets: float | None) -> list[dict[str, Any]]:
        return [{"scenario": f"s{i}", "label": f"Stress {i}", "metrics": None if net is None else _metrics(net=net)} for i, net in enumerate(nets)]

    assert _rows(_results(_candidate(stress=stressed(10.0, 20.0))))["stress"]["status"] == "meets"
    assert _rows(_results(_candidate(stress=stressed(10.0, -1.0))))["stress"]["text"] == "Stress 1 turns the result into a loss."
    assert _rows(_results(_candidate(stress=stressed(10.0, None))))["stress"]["status"] == "missing"
    assert _rows(_results(_candidate(stress=[])))["stress"]["status"] == "missing"
    assert _rows(_results(_candidate(development_metrics=_metrics(net=-5.0))))["stress"]["status"] == "concern"


def test_stress_calls_a_zero_result_break_even_not_a_loss() -> None:
    def stressed(*nets: float) -> list[dict[str, Any]]:
        return [{"scenario": f"s{i}", "label": f"Stress {i}", "metrics": _metrics(net=net)} for i, net in enumerate(nets)]

    zero = _rows(_results(_candidate(stress=stressed(0.0))))["stress"]
    assert (zero["status"], zero["text"]) == ("concern", "Stress 0 leaves it at break-even.")
    assert _rows(_results(_candidate(stress=stressed(-1.0, 0.0))))["stress"]["text"] == "Stress 0 turns the result into a loss. Stress 1 leaves it at break-even."
    flat = _rows(_results(_candidate(development_metrics=_metrics(net=0.0), stress=stressed(5.0))))["stress"]
    assert (flat["status"], flat["text"]) == ("concern", "It only breaks even before any cost stress.")


def test_concentration_is_not_measured_so_it_is_always_missing() -> None:
    row = _rows(_results(_candidate()))["concentration"]
    assert row["status"] == "missing" and row["text"].startswith("Not measured")


def test_final_exposure_follows_the_ledger_and_an_opened_test_records_its_own_state() -> None:
    fresh = _rows(_results(_candidate()), exposure={"state": "not_opened", "explanation": EXPOSURE_EXPLANATIONS["not_opened"]})["final_exposure"]
    used = _rows(_results(_candidate()), exposure={"state": "previously_used", "explanation": EXPOSURE_EXPLANATIONS["previously_used"]})["final_exposure"]
    assert (fresh["status"], used["status"], used["text"]) == ("meets", "concern", EXPOSURE_EXPLANATIONS["previously_used"])
    assert _rows(_results(_candidate()))["final_exposure"]["status"] == "missing"
    opened = _results(_candidate(), exam={"exposure_state": "history_unknown"})
    assert _rows(opened)["final_exposure"]["status"] == "concern"


def test_every_row_links_to_its_evidence() -> None:
    rows = _rows(_results(_candidate()), exposure={"state": "not_opened", "explanation": ""})
    assert {key: (row["link"]["kind"], row["link"]["target"]) for key, row in rows.items()} == {
        "development_activity": ("tab", "trades"),
        "test_over_time": ("step", "test"),
        "neighbors": ("tab", "neighbors"),
        "stress": ("tab", "stress"),
        "concentration": ("tab", "months"),
        "final_exposure": ("step", "decision"),
    }
