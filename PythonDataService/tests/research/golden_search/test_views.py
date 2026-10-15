"""Golden Search read models and operator copy: what each state permits and says, and the numbers the views derive (#2696)."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import get_args

import pytest

from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.research.golden_search.actions import NOTHING_TO_TEST, action_refusals, permitted, presented_status
from app.research.golden_search.activity import TradeFloors
from app.research.golden_search.chance_final import FinalChanceScope
from app.research.golden_search.concentration import NOT_MEASURED
from app.research.golden_search.declarations import declaration_for
from app.research.golden_search.final_test import fits_to_test
from app.research.golden_search.guidance import (
    WEAK_EVIDENCE_DETAIL,
    candidate_flags,
    candidate_guidance,
    fixed_sentence,
    params_sentence,
    research_weakness,
    study_guidance,
    weakness_items,
)
from app.research.golden_search.models import (
    COMMANDS,
    RETAIN_KINDS,
    CommandName,
    EvaluationRecord,
    RetainKind,
    StudyRow,
)
from app.research.golden_search.planning import protocol_from_request
from app.research.golden_search.protocol import KnobPlan
from app.research.golden_search.views import (
    candidate_detail,
    evidence_view,
    exam_view,
    knob_summary,
    passes_completed,
    run_detail,
    summary_outcome,
)
from app.research.golden_search.zoom import ProcedureResult, ZoomRound
from app.schemas.golden_qualifications import GoldenQualificationResearch
from app.utils.session_anchors import et_midnight_ms
from tests._helpers.golden_search import metrics
from tests._helpers.golden_search_study import plan_request

EMA = declaration_for("ema_crossover_signal")
assert EMA is not None


def _row(**overrides: object) -> StudyRow:
    base = dict(
        id="s1", parent_study_id=None, strategy_key="ema_crossover_signal", symbol="SPY", state="locked", revision=0,
        status="idle", attempt=0, job_id=None, pending_stage=None, stage_token=None, created_at_ms=0, updated_at_ms=0,
        finished_at_ms=None, protocol={"method": "zoom"}, protocol_hash="h", receipt={}, results={}, candidate_key=None,
        exam_locked=False, decision=None, budget_cap=5000, consumed_evaluations=0, cache_hits=0, invalid_points=0,
        incomplete=False, failure_reason=None, hidden=False,
    )
    return StudyRow(**{**base, **overrides})  # type: ignore[arg-type]


def _guidance(state: str, **kwargs: object) -> dict[str, str]:
    args = dict(presented_status="completed", exam_outcome=None, claim=None, exposure_state=None, exam_locked=False, failure_reason=None)
    return study_guidance(state=state, **{**args, **kwargs})  # type: ignore[arg-type]


# ── Guidance by state ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("outcome", "claim", "state", "headline"),
    [
        ("meets_rules", "confirmatory", "not_opened", "Approve the settings you want to use"),
        ("not_enough_evidence", "confirmatory", "not_opened", "Not enough evidence to recommend a change"),
        ("meets_rules", "exploratory", "previously_used", "This test was already used"),
        ("meets_rules", "exploratory", "history_unknown", "This test's history is unknown"),
        ("does_not_meet_rules", "confirmatory", "not_opened", "Keeping your current settings is recommended"),
        ("could_not_evaluate", "confirmatory", "not_opened", "Keeping your current settings is recommended"),
    ],
)
def test_the_review_headline_follows_the_exam_and_its_exposure(outcome: str, claim: str, state: str, headline: str) -> None:
    guidance = _guidance("awaiting_review", exam_outcome=outcome, claim=claim, exposure_state=state)
    assert guidance["headline"] == headline
    weak = not (outcome == "meets_rules" and claim == "confirmatory")
    assert (guidance["detail"] == WEAK_EVIDENCE_DETAIL) is weak


def test_retaining_says_whether_the_final_test_was_opened() -> None:
    assert _guidance("retained", exam_locked=True)["detail"].endswith("The opened final test remains recorded as exposed.")
    assert _guidance("retained", exam_locked=False)["detail"].endswith("The final test has not been opened.")


def test_a_stopped_or_waiting_stage_says_so_instead_of_its_running_copy() -> None:
    stopped = _guidance("search_running", presented_status="failed", failure_reason="RuntimeError: boom")
    assert stopped == {"headline": "This stage stopped before it finished", "detail": "RuntimeError: boom"}
    assert _guidance("search_running", presented_status="queued")["headline"] == "Waiting for a worker"
    assert _guidance("search_running", presented_status="running")["headline"] == "Searching the development period"
    failed = _guidance("qualification_failed", failure_reason="The default changed since you reviewed.")
    assert failed["detail"] == "The default changed since you reviewed."


def test_weakness_names_every_reason_an_approval_must_acknowledge() -> None:
    assert research_weakness("meets_rules", "confirmatory", "not_opened") == []
    assert research_weakness("not_enough_evidence", "exploratory", "previously_used") == [
        "EXAM_NOT_ENOUGH_EVIDENCE",
        "EXPOSURE_PREVIOUSLY_USED",
    ]


def test_the_exam_view_names_each_weakness_the_approval_acknowledges() -> None:
    checks = [
        {"code": "NET_POSITIVE", "label": "Positive net result after stated costs", "status": "fail", "detail": ""},
        {"code": "DRAWDOWN_WITHIN", "label": "Worst drawdown within your ceiling", "status": "pass", "detail": ""},
    ]
    exam = {"claim": "exploratory", "exposure_state": "history_unknown"}
    weak = {"outcome": "does_not_meet_rules", "checks": checks}

    assert weakness_items(exam, weak) == [
        {"code": "EXAM_DOES_NOT_MEET_RULES", "text": "failing the stated rules (positive net result after stated costs)"},
        {"code": "EXPOSURE_HISTORY_UNKNOWN", "text": "the unknown history of this test interval"},
    ]
    assert weakness_items({**exam, "claim": "confirmatory"}, {**weak, "outcome": "meets_rules"}) == []
    assert weakness_items(exam, {**weak, "outcome": None}) == []  # not scored yet


#: The frozen study identity every exam-view test validates its stored chance records against (#3024).
_FINAL_SCOPE = FinalChanceScope(objective="sharpe_ratio", development_window=(0, 1), final_window=(2, 3), capital=100_000.0, point_hash="")


def _fit(key: str, outcome: str | None, *, same_as: tuple[str, ...] = ()) -> dict:
    return {
        "key": key, "same_as": list(same_as), "point": {"gap": 0.3 if key == "all_period" else 0.5}, "point_hash": f"h-{key}",
        "outcome": outcome, "checks": [], "retention": None, "metrics": None,
    }


def _exam(*fits: dict) -> dict:
    return {"window": {"start_ms": 0, "end_ms": 1}, "claim": "confirmatory", "exposure_state": "not_opened", "fits": list(fits), "incumbent_metrics": None}


def test_the_final_test_runs_each_distinct_fit_and_never_the_current_settings() -> None:
    def stored(key: str, digest: str) -> dict:
        return {"key": key, "point": {"gap": digest}, "point_hash": digest}

    both = fits_to_test([stored("incumbent", "a"), stored("all_period", "b"), stored("recent", "c")])
    assert [(fit["key"], fit["same_as"], fit["outcome"]) for fit in both] == [("all_period", [], None), ("recent", [], None)]
    # Two fits with one set of settings are one run, named by the first.
    twins = fits_to_test([stored("incumbent", "a"), stored("all_period", "b"), stored("recent", "b")])
    assert [(fit["key"], fit["same_as"]) for fit in twins] == [("all_period", ["recent"])]
    # A fit that returned the current settings is not tested against itself.
    assert [fit["key"] for fit in fits_to_test([stored("incumbent", "a"), stored("all_period", "a"), stored("recent", "c")])] == ["recent"]
    assert fits_to_test([stored("incumbent", "a"), stored("all_period", "a")]) == []


def test_the_exam_view_shows_every_fit_and_says_when_the_pick_followed_the_test() -> None:
    stored = _exam(_fit("all_period", "meets_rules"), _fit("recent", "does_not_meet_rules"))

    undecided = exam_view(stored, chosen_key=None, chance_policy=None, positive_net_required=False, final_scope=_FINAL_SCOPE)
    assert [(fit["key"], fit["label"], fit["outcome"]) for fit in undecided["fits"]] == [
        ("all_period", "All-period fit", "meets_rules"),
        ("recent", "Recent fit", "does_not_meet_rules"),
    ]
    assert (undecided["pick"], undecided["chosen_key"]) == ("after_test", None)
    assert undecided["selection_note"] == "2 fits ran on the final test. The one you approve is recorded as chosen after the final test, from 2 fits."
    assert [item["code"] for item in undecided["fits"][1]["weakness"]] == ["EXAM_DOES_NOT_MEET_RULES"]

    chosen = exam_view(stored, chosen_key="recent", chance_policy=None, positive_net_required=False, final_scope=_FINAL_SCOPE)
    assert (chosen["chosen_key"], chosen["selection_note"]) == ("recent", "Chosen after the final test, from 2 fits.")
    # A study that ended without an approval promises none.
    assert exam_view(stored, chosen_key=None, chance_policy=None, positive_net_required=False, final_scope=_FINAL_SCOPE, approvable=False)["selection_note"] == "2 fits ran on the final test."
    # One result for two fits with the same settings is no choice, so nothing is noted; either name finds it.
    twins = exam_view(_exam(_fit("all_period", "meets_rules", same_as=("recent",))), chosen_key="recent", chance_policy=None, positive_net_required=False, final_scope=_FINAL_SCOPE)
    assert (twins["fits"][0]["label"], twins["chosen_key"], twins["selection_note"]) == ("All-period and recent fit", "all_period", None)


def test_a_final_test_recorded_with_one_locked_candidate_reads_as_that_one_fit() -> None:
    legacy = {
        "candidate_key": "recent", "candidate_point": {"gap": 0.5}, "candidate_point_hash": "h", "window": {"start_ms": 0, "end_ms": 1},
        "claim": "confirmatory", "exposure_state": "not_opened", "outcome": "meets_rules", "checks": [], "retention": 0.8,
        "candidate_metrics": metrics(1.2).as_dict(), "incumbent_metrics": metrics(1.0).as_dict(),
    }

    view = exam_view(legacy, chosen_key="recent", chance_policy=None, positive_net_required=False, final_scope=_FINAL_SCOPE)

    (fit,) = view["fits"]
    assert (fit["key"], fit["outcome"], fit["retention"], fit["metrics"]) == ("recent", "meets_rules", 0.8, legacy["candidate_metrics"])
    assert (view["pick"], view["chosen_key"], view["selection_note"]) == ("before_test", "recent", None)


def test_a_qualified_version_says_when_its_fit_was_chosen_with_two_results_in_view() -> None:
    chosen_from_two = {"exam_outcome": "meets_rules", "fits_tested": 2, "pick": "after_test"}
    assert GoldenQualificationResearch.from_research(chosen_from_two).selection_note == "Chosen after the final test, from 2 fits."
    # One fit, a pick locked before the test, or research recorded before both fits sat it: nothing to note.
    for research in ({**chosen_from_two, "fits_tested": 1}, {**chosen_from_two, "pick": "before_test"}, {"exam_outcome": "meets_rules"}):
        assert GoldenQualificationResearch.from_research(research).selection_note is None


def test_one_outcome_stands_for_the_final_test_the_named_fits_else_the_strongest() -> None:
    stored = _exam(_fit("all_period", "does_not_meet_rules"), _fit("recent", "meets_rules"))
    assert summary_outcome(stored, None) == "meets_rules"
    assert summary_outcome(stored, "all_period") == "does_not_meet_rules"
    # Until every fit is scored the test has no outcome.
    assert summary_outcome(_exam(_fit("all_period", "meets_rules"), _fit("recent", None)), None) is None


# ── Permitted actions ────────────────────────────────────────────────────


def test_every_command_and_retain_kind_the_wire_names_is_one_the_service_accepts() -> None:
    # The runtime lists are the allow-lists a request is checked against: a Literal member missing from one is a refusal for a valid name.
    assert set(COMMANDS) == set(get_args(CommandName)) and len(COMMANDS) == len(get_args(CommandName))
    assert set(RETAIN_KINDS) == set(get_args(RetainKind)) and len(RETAIN_KINDS) == len(get_args(RetainKind))


def test_an_authorized_stage_without_a_worker_reads_queued_and_can_be_cancelled_or_authorized_again() -> None:
    row = _row(state="search_running", status="queued", pending_stage="search", stage_token="t")
    presented = presented_status(row, live=False)
    refusals = action_refusals(row, presented=presented, resume_refusal=None)
    # Its dispatch may have been lost: Run research issues a fresh token (#2814 review).
    assert presented == "queued" and permitted(refusals) == ["run_research", "cancel"]
    exam = _row(state="exam_running", status="queued", pending_stage="exam", stage_token="t", exam_locked=True)
    assert permitted(action_refusals(exam, presented="queued", resume_refusal=None)) == ["cancel"]


def test_a_bound_stage_whose_worker_died_reads_interrupted_and_finish_follows_the_resume_rules() -> None:
    row = _row(state="validation_running", status="running", pending_stage="validation", job_id="j")
    presented = presented_status(row, live=False)
    assert presented == "interrupted"
    assert permitted(action_refusals(row, presented=presented, resume_refusal=None)) == ["run_research", "close", "finish", "revise"]
    blocked = action_refusals(row, presented=presented, resume_refusal="the engine or strategy code changed since launch")
    assert blocked["finish"] == blocked["run_research"] == "the engine or strategy code changed since launch"
    # Redis unreachable: never declared dead, so nothing that assumes the worker stopped is offered.
    assert permitted(action_refusals(row, presented=presented_status(row, live=None), resume_refusal=None)) == ["cancel"]


def test_a_stopped_approval_can_be_finished_revised_or_ended_by_keeping_the_current_settings() -> None:
    row = _row(state="qualification_pending", status="failed", pending_stage="qualification", exam_locked=True)
    assert permitted(action_refusals(row, presented="failed", resume_refusal=None)) == ["retain", "finish", "revise"]
    # Code moved since lock: Finish is refused, and keeping the current settings is still an exit besides Revise.
    blocked = action_refusals(row, presented="failed", resume_refusal="the engine or strategy code changed since launch")
    assert permitted(blocked) == ["retain", "revise"]


def _evidence(fit_hash: str) -> dict:
    """Stored evidence whose all-period fit has ``fit_hash``; the incumbent's is ``a``."""
    return {"evidence": {"candidates": [{"key": "incumbent", "point": {}, "point_hash": "a"}, {"key": "all_period", "point": {}, "point_hash": fit_hash}]}}


def test_the_final_test_opens_from_compare_once_when_a_fit_differs_from_the_current_settings() -> None:
    comparing = _row(state="awaiting_candidate", results=_evidence("b"))
    assert permitted(action_refusals(comparing, presented="completed", resume_refusal=None)) == ["open_exam", "retain", "close", "revise"]
    same = action_refusals(_row(state="awaiting_candidate", results=_evidence("a")), presented="completed", resume_refusal=None)
    assert same["open_exam"] == NOTHING_TO_TEST and permitted(same) == ["retain", "close", "revise"]
    reviewing = _row(state="awaiting_review", exam_locked=True, results=_evidence("b"))
    assert permitted(action_refusals(reviewing, presented="completed", resume_refusal=None)) == ["approve", "retain", "close", "revise"]


@pytest.mark.parametrize(
    ("state", "kept"),
    [
        ("locked", ["close", "revise"]),
        ("awaiting_candidate", ["retain", "close", "revise"]),
        ("awaiting_review", ["retain", "close", "revise"]),
    ],
)
def test_moved_code_refuses_every_command_that_starts_a_stage_and_nothing_else(state: str, kept: list[str]) -> None:
    row = _row(state=state, exam_locked=state == "awaiting_review", results=_evidence("b"))
    moved = "No new stage can run for this study: the engine or strategy code changed since launch"

    refusals = action_refusals(row, presented="completed", resume_refusal=None, stage_refusal=moved)

    assert permitted(refusals) == kept
    assert {refusals[command] for command in ("continue", "open_exam", "approve") if refusals[command] == moved}


# ── Procedure views ──────────────────────────────────────────────────────


def _round(knob: str, pass_index: int, *, moved: bool, quantization_limit: bool = False) -> ZoomRound:
    return ZoomRound(
        pass_index=pass_index, knob=knob, round_index=0, low=0.0, high=1.0, values=(), invalid=(), results=(),
        chosen=0.0, moved=moved, current_before=0.0, quantization_limit=quantization_limit,
    )


def _result(stop: str, rounds: tuple[ZoomRound, ...], *, method: str = "zoom", winner: dict | None = None) -> ProcedureResult:
    return ProcedureResult(
        winner=winner or {"symbol": "SPY", "gap": 0.3, "gap_bps": 0.0, "rsi_min": 50.0, "rsi_max": 70.0, "hold_bars": 7},
        winner_hash="w", winner_metrics=None, stop_reason=stop, rounds=rounds, edge_hits=(), evaluated_hashes=(), method=method,  # type: ignore[arg-type]
    )


def test_the_knob_summary_says_where_each_knob_started_ended_and_why_it_stopped() -> None:
    protocol = protocol_from_request(plan_request("SPY"))
    rounds = (_round("gap", 0, moved=True), _round("hold_bars", 0, moved=True, quantization_limit=True))

    summary = knob_summary(_result("no_improvement", rounds), protocol, EMA)

    assert summary == [
        {"knob": "gap", "label": "Crossover gap", "unit": "price ($)", "start_value": 0.2, "retained_value": 0.3, "moved": True,
         "stop_reason": "no_improvement", "stop_explanation": "No better tested move"},
        {"knob": "hold_bars", "label": "Hold time", "unit": "decision bars", "start_value": 5, "retained_value": 7, "moved": True,
         "stop_reason": "quantization_limit", "stop_explanation": "Minimum step reached"},
    ]
    budget = knob_summary(_result("budget", rounds), protocol, EMA)
    assert {item["stop_explanation"] for item in budget} == {"Budget reached"}
    grid = knob_summary(_result("no_improvement", (), method="grid"), protocol, EMA)
    assert {item["stop_explanation"] for item in grid} == {"Every listed value tested"}


def test_a_pass_the_budget_interrupted_is_not_counted_complete() -> None:
    two_passes = (_round("gap", 0, moved=True), _round("hold_bars", 0, moved=False), _round("gap", 1, moved=False))
    assert passes_completed(_result("no_improvement", two_passes)) == 2
    assert passes_completed(_result("budget", two_passes)) == 1
    assert passes_completed(_result("budget", (), method="grid")) == 0


# ── Candidate copy and evidence view ─────────────────────────────────────


def test_the_parameter_sentence_reads_the_whole_tuple_and_the_fixed_one_its_held_knobs() -> None:
    point = {"symbol": "SPY", "gap": 0.15, "rsi_min": 48.0, "rsi_max": 72.0, "fast_period": 8, "slow_period": 21, "hold_bars": 4}
    assert params_sentence(EMA, point) == "Gap $0.15 · RSI 48–72 · EMA 8/21 · hold 4 bars"
    # The bps gap is named only when a point sets it, so a searched bps gap tells two points apart.
    assert params_sentence(EMA, {**point, "gap_bps": 2.5}) == "Gap $0.15 · normalized gap 2.5 bps · RSI 48–72 · EMA 8/21 · hold 4 bars"
    plans = (KnobPlan("gap_bps", "fixed", 0.0, 0.0, 0.0), KnobPlan("hold_bars", "fixed", 0.0, 0.0, 5.0))
    assert fixed_sentence(EMA, plans) == "Normalized gap fixed at 0 bps · Hold time fixed at 5 decision bars"


@pytest.mark.parametrize(
    ("kwargs", "title"),
    [
        (dict(key="incumbent"), "No change can be the best decision"),
        (dict(key="all_period", same_as_incumbent=True), "The search returned the current settings"),
        (dict(key="recent", ineligibility="DRAWDOWN_ABOVE_CEILING", losing_neighbors=True), "The extra return comes with a warning"),
        (dict(key="all_period", losing_neighbors=True), "Nearby settings lose money"),
        (dict(key="all_period", ineligibility="TOO_FEW_TRADES"), "This candidate does not meet your rules"),
        (dict(key="all_period"), "Prefer evidence that survives small changes"),
        (dict(key="all_period", neighbors_audited=False), "Check how fragile it is"),
    ],
)
def test_candidate_guidance_names_the_candidates_situation(kwargs: dict, title: str) -> None:
    args = dict(same_as_incumbent=False, ineligibility=None, losing_neighbors=False, neighbors_audited=True)
    assert candidate_guidance(**{**args, **kwargs})["title"] == title


def test_flags_lead_with_the_failed_rule_then_the_candidates_findings() -> None:
    flags = candidate_flags(
        ineligibility="DRAWDOWN_ABOVE_CEILING", total_trades=91, min_trades=30, drawdown_ceiling=0.12,
        finding_codes=["NEIGHBORS_LOSE_MONEY", "VALIDATION_GOT_WORSE", "NEIGHBORS_LOSE_MONEY"],
    )
    assert flags == [
        {"code": "DRAWDOWN_ABOVE_CEILING", "text": "Above 12% limit"},
        {"code": "NEIGHBORS_LOSE_MONEY", "text": "Nearby settings lose money"},
    ]
    assert candidate_flags(ineligibility="TOO_FEW_TRADES", total_trades=17, min_trades=30, drawdown_ceiling=0.2, finding_codes=[]) == [
        {"code": "TOO_FEW_TRADES", "text": "17 trades, below 30"}
    ]


def _candidate(key: str, point_hash: str, *, net: float | None = 1_000.0, drawdown: float = 0.1, neighbors: list | None = None) -> dict:
    return {
        "key": key, "point": {"symbol": "SPY", "gap": 0.2}, "point_hash": point_hash,
        "development_metrics": metrics(1.0, net=net, drawdown=drawdown).as_dict(), "neighbors": neighbors or [],
        "neighbors_audited": key != "incumbent", "stress": [], "edge_hits": [],
    }


def test_the_evidence_view_marks_duplicates_the_incumbent_and_ineligible_candidates() -> None:
    protocol = replace(protocol_from_request(plan_request("SPY")), policy=replace(protocol_from_request(plan_request("SPY")).policy, max_drawdown_ceiling=0.12))
    losing = [
        {
            "knob": "gap",
            "one_sided": True,
            "rows": [
                {"value": 0.15, "status": "tested", "metrics": metrics(1.0, net=-5.0).as_dict(), "reason": None},
                {"value": 0.2, "status": "center", "metrics": metrics(1.0).as_dict(), "reason": None},
            ],
        }
    ]
    stored = {
        "window": {"start_ms": 0, "end_ms": 1},
        "candidates": [_candidate("incumbent", "i"), _candidate("all_period", "a", neighbors=losing), _candidate("recent", "r", drawdown=0.15)],
        "recommendation": {"headline": "h", "findings": [{"code": "NEIGHBORS_LOSE_MONEY", "text": "t"}]},
        "findings": [{"code": "NEIGHBORS_LOSE_MONEY", "text": "t", "candidate": "all_period"}],
        "incomplete": False,
    }

    stored["candidates"][1]["stress"] = [{"scenario": "slip", "label": "Slippage x2", "metrics": metrics(1.0, total_return=0.004).as_dict()}]
    measured = {"status": "missing", "reason": "The development run made no trades."}
    stored["candidates"][1]["concentration"] = measured

    view = evidence_view(stored, protocol, EMA, pair_maps=[])

    incumbent, all_period, recent = view["candidates"]
    # The Compare measures read the development window, and each stress against its own candidate's run.
    years = TradeFloors(protocol, {}).trading_years((protocol.development_start_ms, protocol.development_end_ms))
    assert all_period["trades_per_year"] == pytest.approx(50 / float(years), abs=1e-9, rel=0)
    assert all_period["stress"][0]["return_change"] == pytest.approx(-0.006, abs=1e-12, rel=0)
    assert all_period["stress_tally"] == {"in_profit": 1, "recorded": 1, "scenarios": 1}
    assert [row["step"] for row in all_period["neighbors"][0]["rows"]] == [-1, 0]
    # The stored measure reads back as recorded; evidence recorded before it was measured says so.
    assert all_period["concentration"] == measured
    assert incumbent["concentration"] == {"status": "missing", "reason": NOT_MEASURED}
    assert view["fits_to_test"] == [{"key": "all_period", "same_as": [], "label": "All-period fit"}, {"key": "recent", "same_as": [], "label": "Recent fit"}]
    assert all_period["guidance"]["title"] == "Nearby settings lose money"
    assert all_period["flags"] == [{"code": "NEIGHBORS_LOSE_MONEY", "text": "Nearby settings lose money"}]
    assert (recent["eligible"], recent["ineligibility"]) == (False, "DRAWDOWN_ABOVE_CEILING")
    assert recent["flags"] == [{"code": "DRAWDOWN_ABOVE_CEILING", "text": "Above 12% limit"}]
    assert incumbent["guidance"]["title"] == "No change can be the best decision"
    assert view["scope"]["capital"] == 100_000.0


# ── Candidate run detail ─────────────────────────────────────────────────


def test_a_detail_run_reads_back_as_cumulative_return_drawdown_and_months() -> None:
    days = (date(2025, 1, 30), date(2025, 1, 31), date(2025, 2, 3))
    closes = [session_close_ms_utc(day) for day in days]
    record = EvaluationRecord(
        study_id="s", evaluation_key="k", point_hash="p", point={"symbol": "SPY"}, window_start_ms=et_midnight_ms(days[0]),
        window_end_ms=et_midnight_ms(date(2025, 2, 4)), scenario="base", detail=True, stage="evidence", fold_index=None,
        status="completed", attempt=1, retries=0, total_trades=1, net_profit=2_010.0, total_return_pct=0.0201, sharpe_ratio=1.0,
        max_drawdown_pct=0.01, win_rate=1.0, error=None, created_at_ms=0, completed_at_ms=0,
        detail_json={
            "initial_cash": 100_000.0,
            "daily_equity": [[closes[0], 101_000.0], [closes[1], 99_990.0], [closes[2], 102_010.0]],
            "trades": [{"exit_ms": closes[2] - 60_000, "pnl": 10.0}],
        },
    )

    detail = run_detail(record)

    assert [point["value"] for point in detail["cumulative_return"]] == pytest.approx([0.01, -0.0001, 0.0201], abs=1e-12, rel=0)
    drawdown = [point["drawdown"] for point in detail["drawdown"]]
    assert drawdown[0] == 0.0 and drawdown[2] == 0.0
    assert math.isclose(drawdown[1], 99_990.0 / 101_000.0 - 1.0, abs_tol=1e-12, rel_tol=0)
    january, february = detail["monthly"]
    assert (january["month_start_ms"], february["month_start_ms"]) == (et_midnight_ms(date(2025, 1, 1)), et_midnight_ms(date(2025, 2, 1)))
    assert [(month["year"], month["month"]) for month in detail["monthly"]] == [(2025, 1), (2025, 2)]
    assert math.isclose(january["net_profit"], -10.0, abs_tol=1e-9, rel_tol=0)
    assert math.isclose(february["return_fraction"], 2_020.0 / 99_990.0, abs_tol=1e-12, rel_tol=0)
    assert (january["trades"], february["trades"]) == (0, 1)
    # The candidate read draws the development run's concentration curve from its trades, held back like the
    # stored measure for evidence recorded before it; this one trade nets $10 of the run's $2,010.
    stored = {"key": "all_period", "point": {"symbol": "SPY"}, "concentration": {"status": "missing", "reason": "r"}}
    floors = TradeFloors(protocol_from_request(plan_request("SPY")), {})
    window = (record.window_start_ms, record.window_end_ms)
    read = candidate_detail(stored, strategy_key="ema_crossover_signal", development=record, exam=None, commission_per_order=0.0, floors=floors, development_window=window)
    assert read["development"]["concentration_curve"]["reason"].startswith("Its trades add up to $10.00")
    # The trade charts read the same trades, so they say the same.
    assert read["development"]["trade_charts"]["reason"] == read["development"]["concentration_curve"]["reason"]
    legacy = candidate_detail(
        {"key": "all_period", "point": {"symbol": "SPY"}}, strategy_key="ema_crossover_signal", development=record, exam=None, commission_per_order=0.0,
        floors=floors, development_window=window,
    )
    assert legacy["development"]["concentration_curve"] == {"points": [], "best_count": None, "reason": NOT_MEASURED}


def test_a_failed_detail_run_has_metrics_and_no_series() -> None:
    record = EvaluationRecord(
        study_id="s", evaluation_key="k", point_hash="p", point={}, window_start_ms=0, window_end_ms=1, scenario="base", detail=True,
        stage="evidence", fold_index=None, status="failed", attempt=1, retries=0, total_trades=0, net_profit=None, total_return_pct=None,
        sharpe_ratio=None, max_drawdown_pct=None, win_rate=None, error="engine refused", detail_json=None, created_at_ms=0, completed_at_ms=0,
    )
    detail = run_detail(record)
    assert detail["metrics"]["error"] == "engine refused" and detail["daily_equity"] == [] and detail["monthly"] == []


def test_the_decimal_sentence_drops_trailing_zeros_but_keeps_the_gap_in_cents() -> None:
    point = {"symbol": "SPY", "gap": Decimal("0.1"), "rsi_min": 45, "rsi_max": 70, "fast_period": 5, "slow_period": 10, "hold_bars": 3}
    assert params_sentence(EMA, point) == "Gap $0.10 · RSI 45–70 · EMA 5/10 · hold 3 bars"


def test_exposure_reads_the_window_keys_grid_and_walk_forward_requests_store() -> None:
    """The exposure ledger reads these keys out of the other procedures' stored requests (#2696)."""
    from app.schemas.grid_search import GridSearchSpecRequest
    from app.schemas.walk_forward_study import WalkForwardStudySpecRequest

    for model in (GridSearchSpecRequest, WalkForwardStudySpecRequest):
        assert {"start_ms", "end_ms"} <= set(model.model_fields), model.__name__
