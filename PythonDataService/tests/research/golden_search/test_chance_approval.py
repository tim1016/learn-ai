"""The chance-approval authority: what an enrolled study's approval must acknowledge (#3015)."""

from __future__ import annotations

from typing import Any

import pytest

from app.research.golden_search import service
from app.research.golden_search.chance_approval import (
    CHANCE_UNAVAILABLE,
    CHANCE_WITHIN_NOISE,
    approval_weakness,
    chance_weakness_items,
    enrollment_policy,
)
from app.research.golden_search.guidance import weakness_words
from app.research.golden_search.models import GoldenSearchRefusal, StudyRow
from app.research.golden_search.planning import protocol_from_request
from tests._helpers.golden_search_study import plan_request


def _measured_share(status_share: float, *, label: str = "clearly_as_good", replications: int = 10_000, thresholds: dict[str, float] | None = None) -> dict[str, Any]:
    numerator = round(status_share * replications)
    return {
        "status": "measured",
        "label": label,
        "counts": {"better": numerator, "tied": 0, "worse": replications - numerator, "undefined": 0},
        "at_least_as_good": {"count": numerator, "share": status_share},
        "basis": {"replications": replications, "thresholds": thresholds if thresholds is not None else {"clear": 0.95}},
    }


def _measured_profitability(positive_share: float, *, label: str = "clearly_positive") -> dict[str, Any]:
    numerator = round(positive_share * 10_000)
    return {
        "status": "measured",
        "label": label,
        "counts": {"positive": numerator, "zero": 0, "negative": 10_000 - numerator, "undefined": 0},
        "basis": {"replications": 10_000, "thresholds": {"clear": 0.95}},
    }


def _carry(status: str = "measured") -> dict[str, Any]:
    if status == "measured":
        return {"status": "measured", "label": "middle", "rank": 0.5, "below": 5_000, "finite": 10_000, "undefined": 0}
    if status == "failed":
        return {"status": "failed", "reason": "ValueError: boom", "sentence": "The carry over reading could not be computed and nothing was stored: ValueError: boom."}
    return {"status": "missing", "code": "carry_over_unavailable", "reason": "No matching stored rehearsal could carry over, so no rank exists."}


def _codes(items: list[dict[str, str]]) -> list[str]:
    return [item["code"] for item in items]


POLICY = enrollment_policy()


def test_the_policy_names_the_required_components_and_the_two_codes() -> None:
    assert POLICY["schema_version"] == "golden-search-chance-approval/v1"
    assert POLICY["required_final_components"] == ["comparison", "profitability", "carry_over"]
    assert POLICY["informational"] == ["fit_to_fit", "compare_drawdown"]
    assert POLICY["weakness"]["code"] == CHANCE_WITHIN_NOISE
    assert POLICY["unavailable"]["code"] == CHANCE_UNAVAILABLE


def test_a_clear_complete_measured_fit_carries_no_item() -> None:
    chance = {"comparison": _measured_share(0.9999), "profitability": _measured_profitability(0.9999), "carry_over": _carry()}
    assert chance_weakness_items(POLICY, chance, positive_net_required=True) == []
    # A legacy study reads the same measured evidence the same way.
    assert chance_weakness_items(None, chance, positive_net_required=True) == []


def test_a_share_below_the_stored_threshold_is_weak_and_threshold_equality_is_clear() -> None:
    weak = chance_weakness_items(POLICY, {"comparison": _measured_share(0.9499), "profitability": _measured_profitability(1.0), "carry_over": _carry()}, positive_net_required=True)
    assert _codes(weak) == [CHANCE_WITHIN_NOISE]
    assert "9,499 of 10,000" in weak[0]["text"]
    # 9,500 of 10,000 sits exactly at the 0.95 spelling: clear, not weak.
    at_threshold = chance_weakness_items(POLICY, {"comparison": _measured_share(0.95), "profitability": _measured_profitability(1.0), "carry_over": _carry()}, positive_net_required=True)
    assert at_threshold == []
    # The threshold is the reading's own: a study whose reading stored 0.80 judges by 0.80, never today's 0.95.
    own = chance_weakness_items(POLICY, {"comparison": _measured_share(0.85, thresholds={"clear": 0.8}), "profitability": _measured_profitability(1.0), "carry_over": _carry()}, positive_net_required=True)
    assert own == []


def test_a_weak_profitability_is_named_only_when_positive_net_is_required() -> None:
    chance = {"comparison": _measured_share(1.0), "profitability": _measured_profitability(0.5), "carry_over": _carry()}
    assert _codes(chance_weakness_items(POLICY, chance, positive_net_required=True)) == [CHANCE_WITHIN_NOISE]
    assert chance_weakness_items(POLICY, chance, positive_net_required=False) == []


def test_an_enrolled_study_must_acknowledge_missing_or_failed_required_components_by_name() -> None:
    missing = chance_weakness_items(POLICY, {"comparison": _measured_share(1.0), "profitability": {"status": "missing", "code": "not_measured", "reason": "Not measured for this study."}, "carry_over": _carry("missing")}, positive_net_required=True)
    # Two unavailable components are one acknowledgement that names both, never one code listed twice.
    assert _codes(missing) == [CHANCE_UNAVAILABLE]
    assert "profitability" in missing[0]["text"] and "Not measured for this study" in missing[0]["text"]
    assert "carry-over" in missing[0]["text"] and "No matching stored rehearsal" in missing[0]["text"]

    failed = chance_weakness_items(POLICY, {"comparison": _measured_share(1.0), "profitability": _measured_profitability(1.0), "carry_over": _carry("failed")}, positive_net_required=True)
    assert _codes(failed) == [CHANCE_UNAVAILABLE]
    assert "could not be computed" in failed[0]["text"]

    # A legacy study keeps the original absent-reading behavior: nothing missing ever gates.
    assert chance_weakness_items(None, {"comparison": _measured_share(1.0), "profitability": {"status": "missing", "code": "not_measured", "reason": "Not measured."}, "carry_over": _carry("missing")}, positive_net_required=True) == []


def test_a_missing_stored_threshold_is_unavailable_and_today_constants_are_never_borrowed() -> None:
    no_threshold = _measured_share(0.99)
    no_threshold["basis"]["thresholds"] = {}
    items = chance_weakness_items(POLICY, {"comparison": no_threshold, "profitability": _measured_profitability(1.0), "carry_over": _carry()}, positive_net_required=True)
    assert _codes(items) == [CHANCE_UNAVAILABLE]
    assert "no clear threshold" in items[0]["text"]
    # Legacy absence remains legacy: a reading without its threshold never borrows today's.
    assert chance_weakness_items(None, {"comparison": no_threshold, "profitability": _measured_profitability(1.0), "carry_over": _carry()}, positive_net_required=True) == []


def test_a_measured_reading_with_no_defined_path_is_unavailable_for_enrolled_studies() -> None:
    # Coherent with its own counts: every path undefined, so the label and the counts agree (#3024).
    empty = _measured_share(0.0, label="no_defined_paths")
    empty["counts"] = {"better": 0, "tied": 0, "worse": 0, "undefined": 10_000}
    enrolled = chance_weakness_items(POLICY, {"comparison": empty, "profitability": _measured_profitability(1.0), "carry_over": _carry()}, positive_net_required=True)
    assert _codes(enrolled) == [CHANCE_UNAVAILABLE]
    assert "no resampled path produced a defined result" in enrolled[0]["text"]
    # A legacy study reads the same empty reading as measured-weak support, per owner decision 3.
    legacy = chance_weakness_items(None, {"comparison": empty, "profitability": _measured_profitability(1.0), "carry_over": _carry()}, positive_net_required=True)
    assert _codes(legacy) == [CHANCE_WITHIN_NOISE]


def test_the_carry_over_rank_never_decides_a_weakness() -> None:
    # Every rank, however extreme, is a measured carry-over: required to exist, never a threshold test.
    for rank in (0.0, 0.9999):
        carry = {**_carry(), "rank": rank, "label": "below_most" if rank == 0.0 else "above_most"}
        assert chance_weakness_items(POLICY, {"comparison": _measured_share(1.0), "profitability": _measured_profitability(1.0), "carry_over": carry}, positive_net_required=True) == []


def test_a_measured_carry_over_with_no_defined_path_is_unavailable() -> None:
    empty = {**_carry(), "label": "no_defined_paths", "finite": 0, "undefined": 10_000}
    items = chance_weakness_items(POLICY, {"comparison": _measured_share(1.0), "profitability": _measured_profitability(1.0), "carry_over": empty}, positive_net_required=True)
    assert _codes(items) == [CHANCE_UNAVAILABLE]
    assert "no defined rehearsal path" in items[0]["text"]


def test_one_weak_component_and_one_unavailable_both_show_without_duplicating_the_control() -> None:
    items = chance_weakness_items(
        POLICY,
        {"comparison": _measured_share(0.5), "profitability": {"status": "missing", "code": "not_measured", "reason": "Not measured for this study."}, "carry_over": _carry()},
        positive_net_required=True,
    )
    assert _codes(items) == [CHANCE_WITHIN_NOISE, CHANCE_UNAVAILABLE]
    assert "support against the current settings" in items[0]["text"]
    assert "profitability" in items[1]["text"]


def test_two_weak_shares_are_one_acknowledgement_that_names_both_and_gates_exactly_as_before() -> None:
    chance = {"comparison": _measured_share(0.5), "profitability": _measured_profitability(0.5), "carry_over": _carry()}

    items = chance_weakness_items(POLICY, chance, positive_net_required=True)

    assert _codes(items) == [CHANCE_WITHIN_NOISE]
    assert "support against the current settings" in items[0]["text"] and "resampled profit" in items[0]["text"]
    # Whether an approval needs the acknowledgement is unchanged: one weak share asks, and a legacy study asks the same.
    assert _codes(chance_weakness_items(None, chance, positive_net_required=True)) == [CHANCE_WITHIN_NOISE]
    assert _codes(chance_weakness_items(POLICY, {**chance, "profitability": _measured_profitability(1.0)}, positive_net_required=True)) == [CHANCE_WITHIN_NOISE]
    assert _codes(chance_weakness_items(POLICY, {**chance, "profitability": _measured_profitability(1.0), "comparison": _measured_share(1.0)}, positive_net_required=True)) == []


def test_a_share_that_ran_clearly_the_other_way_says_so_and_a_mixed_one_stays_inside_the_noise() -> None:
    # 100 of 10,000 paths as good, so 9,900 strictly worse: past the 95% threshold the other way.
    against = chance_weakness_items(POLICY, {"comparison": _measured_share(0.01), "profitability": _measured_profitability(0.005), "carry_over": _carry()}, positive_net_required=True)

    assert _codes(against) == [CHANCE_WITHIN_NOISE]
    text = against[0]["text"]
    assert "strictly worse than the current settings in 9,900 of 10,000 paths (99.0%)" in text
    assert "negative compounded net return in 9,950 of 10,000 paths (99.5%)" in text
    assert "inside the resampling model's noise" not in text

    # 4,000 better and 6,000 worse: neither way reaches 95%, so it is genuinely inside the model's noise.
    mixed = chance_weakness_items(POLICY, {"comparison": _measured_share(0.4), "profitability": _measured_profitability(0.4), "carry_over": _carry()}, positive_net_required=True)
    assert _codes(mixed) == [CHANCE_WITHIN_NOISE]
    assert mixed[0]["text"].count("inside the resampling model's noise") == 2

    # Exactly 9,500 worse is at the threshold the other way: clear, by the same raw-count comparison the reading used.
    boundary = chance_weakness_items(POLICY, {"comparison": _measured_share(0.05), "profitability": _measured_profitability(1.0), "carry_over": _carry()}, positive_net_required=True)
    assert "strictly worse than the current settings in 9,500 of 10,000" in boundary[0]["text"]


def test_the_words_an_approval_refusal_quotes_are_the_acknowledgements_own_and_name_each_weakness_once() -> None:
    exam = {"claim": "exploratory", "exposure_state": "previously_used"}
    fit = {"outcome": "not_enough_evidence", "checks": []}
    items = chance_weakness_items(POLICY, {"comparison": _measured_share(0.5), "profitability": _measured_profitability(0.5), "carry_over": _carry()}, positive_net_required=True)

    words = weakness_words(exam, fit, items)

    assert words.startswith("not enough final-test evidence and the previously used test interval and support against the current settings")
    assert "EXAM_" not in words and "EXPOSURE_" not in words and "CHANCE_" not in words
    assert words.count("inside the resampling model's noise") == 2


def test_the_approval_weakness_lists_the_research_codes_then_each_chance_code_once() -> None:
    fit = {"key": "all_period", "point_hash": "h-all", "outcome": "does_not_meet_rules", "checks": []}
    exam = {"claim": "exploratory", "exposure_state": "previously_used", "fits": [fit]}

    weakness = approval_weakness(protocol_from_request(plan_request("SPY")), exam, fit, POLICY)

    # An enrolled fit that stored no chance record leaves all three required readings unavailable: one code, three reasons.
    assert weakness.codes == ["EXAM_DOES_NOT_MEET_RULES", "EXPOSURE_PREVIOUSLY_USED", CHANCE_UNAVAILABLE]
    assert _codes(weakness.items) == [CHANCE_UNAVAILABLE]
    assert {"comparison", "profitability", "carry_over"} <= set(weakness.chance)


# ── The approval intent's refusal, without a database ───────────────────────────────────────────────────────


def _intent_row(*, outcome: str, claim: str, exposure: str, enrolled: bool) -> StudyRow:
    fit = {"key": "all_period", "same_as": [], "point": {"gap": 0.3}, "point_hash": "h-all", "outcome": outcome, "checks": [], "retention": None, "metrics": None}
    exam = {"window": {"start_ms": 0, "end_ms": 1}, "claim": claim, "exposure_state": exposure, "fits": [fit], "incumbent_metrics": None}
    return StudyRow(
        id="s1", parent_study_id=None, strategy_key="ema_crossover_signal", symbol="SPY", state="awaiting_review", revision=1, status="idle",
        attempt=1, job_id=None, pending_stage=None, stage_token=None, created_at_ms=0, updated_at_ms=0, finished_at_ms=None,
        protocol=protocol_from_request(plan_request("SPY")).as_dict(), protocol_hash="h", receipt={},
        results={"exam": exam, **({"chance_approval": enrollment_policy()} if enrolled else {})},
        candidate_key=None, exam_locked=True, decision=None, budget_cap=5000, consumed_evaluations=0, cache_hits=0, invalid_points=0,
        incomplete=False, failure_reason=None, hidden=False,
    )


_INTENT = {
    "candidate_key": "all_period",
    "note": "Reviewed.",
    "acknowledge_missing_parity": True,
    "acknowledge_research_weakness": False,
    "expected_default_qualification_id": None,
}


async def test_an_unacknowledged_weak_approval_is_refused_in_words_with_no_code_listed_twice() -> None:
    # An enrolled study whose fit stored no chance record: all three required readings are unavailable.
    row = _intent_row(outcome="does_not_meet_rules", claim="exploratory", exposure="previously_used", enrolled=True)

    with pytest.raises(GoldenSearchRefusal) as refused:
        await service._approve(None, row, _INTENT)

    assert (refused.value.code, refused.value.field) == ("WEAKNESS_ACKNOWLEDGEMENT_REQUIRED", "acknowledge_research_weakness")
    message = str(refused.value)
    assert "EXAM_" not in message and "EXPOSURE_" not in message and "CHANCE_" not in message
    assert message.startswith("This evidence is weak: failing the stated rules and the previously used test interval and an unavailable required comparison reading")
    assert message.count("an unavailable required profitability reading") == 1 and message.count("an unavailable required carry-over reading") == 1
    assert message.endswith("; approving needs your explicit acceptance of it.")


async def test_the_acknowledgement_gate_asks_exactly_when_the_evidence_is_weak(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[str] = []

    async def insert_trial(conn: Any, study_id: str, *, stage: str, kind: str, payload: Any) -> None:
        recorded.append(f"{stage}:{kind}")

    async def update_study(conn: Any, study_id: str, *, changes: Any, decision: Any) -> StudyRow:
        recorded.append("update")
        return _intent_row(outcome="meets_rules", claim="confirmatory", exposure="not_opened", enrolled=False)

    monkeypatch.setattr(service.repo, "insert_trial", insert_trial)
    monkeypatch.setattr(service.repo, "update_study", update_study)

    # Meets the rules on an unopened interval, and a study without the enrollment marker never gates on missing readings.
    clear = _intent_row(outcome="meets_rules", claim="confirmatory", exposure="not_opened", enrolled=False)
    outcome = await service._approve(None, clear, _INTENT)
    assert outcome.dispatch["payload"]["study_id"] == "s1"
    assert recorded == ["candidate:pick", "qualification:approval", "update"]

    # The same evidence with one research weakness is refused until it is acknowledged, then goes through.
    weak = _intent_row(outcome="meets_rules", claim="exploratory", exposure="previously_used", enrolled=False)
    with pytest.raises(GoldenSearchRefusal) as refused:
        await service._approve(None, weak, _INTENT)
    assert refused.value.code == "WEAKNESS_ACKNOWLEDGEMENT_REQUIRED"
    recorded.clear()
    await service._approve(None, weak, {**_INTENT, "acknowledge_research_weakness": True})
    assert recorded == ["candidate:pick", "qualification:approval", "update"]
