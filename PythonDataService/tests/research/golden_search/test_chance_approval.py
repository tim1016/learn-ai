"""The chance-approval authority: what an enrolled study's approval must acknowledge (#3015)."""

from __future__ import annotations

from typing import Any

from app.research.golden_search.chance_approval import (
    CHANCE_UNAVAILABLE,
    CHANCE_WITHIN_NOISE,
    chance_weakness_items,
    enrollment_policy,
)


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
    assert _codes(missing) == [CHANCE_UNAVAILABLE, CHANCE_UNAVAILABLE]
    assert "profitability" in missing[0]["text"] and "Not measured for this study" in missing[0]["text"]
    assert "carry-over" in missing[1]["text"] and "No matching stored rehearsal" in missing[1]["text"]

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
    empty = _measured_share(0.0, label="no_defined_paths")
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
