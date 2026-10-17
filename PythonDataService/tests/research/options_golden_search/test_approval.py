"""Normal and explicit-exception approval guard every required evidence scope."""

from __future__ import annotations

from typing import Any

import pytest

from app.research.options_golden_search.approval import approval_failures, validate_approval
from app.schemas.options_golden_search import Command

POINT = "a" * 64
OTHER = "b" * 64


def _gate(scope: str, *, status: str = "meets_rules") -> dict[str, Any]:
    return {"gate_id": scope + ":profit", "status": status, "actual": "10", "required": ">0", "reason": "test"}


def _row() -> dict[str, Any]:
    return {
        "finalists": [POINT, OTHER], "exposure": {"classification": "not_opened"},
        "gate_outcomes": [_gate("pooled_forward")],
        "candidates": [{"candidate_hash": POINT, "gate_outcomes": [_gate("development")]}],
        "final_results": [
            {"candidate_hash": POINT, "verdict": "meets_rules", "gate_outcomes": [_gate("final")]},
            {"candidate_hash": OTHER, "verdict": "does_not_meet_rules", "gate_outcomes": [_gate("final", status="does_not_meet_rules")]},
        ],
    }


def _command(kind: str = "approve", comment: str = "", waived_gate_ids: tuple[str, ...] = ()) -> Command:
    return Command(command=kind, expected_revision=1, idempotency_key="approve", candidate_hash=POINT, comment=comment, waived_gate_ids=waived_gate_ids)


def test_a_complete_final_comparison_can_approve_the_passing_candidate_despite_other_performance_failures() -> None:
    validate_approval(_row(), _command())


@pytest.mark.parametrize("scope", ["procedure", "development", "final"])
def test_normal_approval_requires_every_scientific_scope_and_exception_preserves_failed_rule(scope: str) -> None:
    row = _row()
    if scope == "procedure":
        row["gate_outcomes"] = [_gate("pooled_forward", status="does_not_meet_rules")]
    elif scope == "development":
        row["candidates"][0]["gate_outcomes"] = [_gate("development", status="does_not_meet_rules")]
    else:
        row["final_results"][0]["gate_outcomes"] = [_gate("final", status="does_not_meet_rules")]
    with pytest.raises(ValueError, match="Normal approval"):
        validate_approval(row, _command())
    failed = approval_failures(row, POINT)
    assert len(failed) == 1
    validate_approval(row, _command("approve_exception", "I accept the listed failure.", (failed[0]["gate_id"],)))
    assert approval_failures(row, POINT) == failed


@pytest.mark.parametrize("classification", ["history_unknown", "previously_used"])
def test_reused_or_unknown_final_history_requires_explicit_owner_exception(classification: str) -> None:
    row = _row()
    row["exposure"]["classification"] = classification
    with pytest.raises(ValueError, match="Normal approval"):
        validate_approval(row, _command())
    with pytest.raises(ValueError, match="nonblank"):
        validate_approval(row, _command("approve_exception", " \n "))
    validate_approval(row, _command("approve_exception", "Prior exposure acknowledged.", ("final:exposure",)))
    assert any(gate["gate_id"] == "final:exposure" for gate in approval_failures(row, POINT))


def test_an_unfinished_or_duplicated_frozen_finalist_blocks_both_approval_paths() -> None:
    row = _row()
    row["final_results"] = row["final_results"][:1]
    for kind in ("approve", "approve_exception"):
        with pytest.raises(ValueError, match="Every frozen finalist"):
            validate_approval(row, _command(kind, "Accept risk"))
    row["final_results"] *= 2
    with pytest.raises(ValueError, match="Every frozen finalist"):
        validate_approval(row, _command("approve_exception", "Accept risk"))


def test_technical_failure_in_another_mandatory_finalist_cannot_receive_an_exception() -> None:
    row = _row()
    row["final_results"][1]["verdict"] = "could_not_evaluate"
    with pytest.raises(ValueError, match="technically"):
        validate_approval(row, _command("approve_exception", "Accept risk"))


@pytest.mark.parametrize("scope", ["procedure", "development", "final"])
def test_missing_input_or_proof_is_never_a_performance_exception(scope: str) -> None:
    row = _row()
    if scope == "procedure":
        row["gate_outcomes"] = [_gate("forward", status="could_not_evaluate")]
    elif scope == "development":
        row["candidates"][0]["gate_outcomes"] = [_gate("development", status="could_not_evaluate")]
    else:
        row["final_results"][0]["gate_outcomes"] = [_gate("final", status="could_not_evaluate")]
    with pytest.raises(ValueError, match="Technical invalidity"):
        validate_approval(row, _command("approve_exception", "Accept risk"))


@pytest.mark.parametrize("waived", [(), ("final:profit",), ("forward_0:sharpe", "development:profit", "final:profit", "final:exposure", "extra:rule"), ("forward_0:sharpe", "development:profit", "final:profit", "final:exposure", "final:exposure")])
def test_exception_rejects_missing_unrelated_or_duplicate_gate_acknowledgements(waived: tuple[str, ...]) -> None:
    row = _row()
    row["gate_outcomes"] = [{**_gate("forward_0", status="not_enough_evidence"), "gate_id": "forward_0:sharpe"}]
    row["candidates"][0]["gate_outcomes"] = [_gate("development", status="does_not_meet_rules")]
    row["final_results"][0]["gate_outcomes"] = [_gate("final", status="does_not_meet_rules")]
    row["exposure"]["classification"] = "previously_used"
    with pytest.raises(ValueError, match="exactly every"):
        validate_approval(row, _command("approve_exception", "I accept all relevant risks.", waived))
    exact = ("forward_0:sharpe", "development:profit", "final:profit", "final:exposure")
    validate_approval(row, _command("approve_exception", "I reviewed every scope.", exact))


def test_full_stress_scope_is_preserved_and_explicit_nonoverridable_rule_cannot_be_waived() -> None:
    row = _row()
    gate = {**_gate("final:fees", status="not_enough_evidence"), "overridable": True}
    row["final_results"][0]["gate_outcomes"] = [gate]
    validate_approval(row, _command("approve_exception", "Acknowledge fee stress.", ("final:fees:profit",)))
    gate["overridable"] = False
    with pytest.raises(ValueError, match="Technical invalidity"):
        validate_approval(row, _command("approve_exception", "Acknowledge fee stress.", ("final:fees:profit",)))


def test_read_review_and_command_share_the_exact_complete_failure_set_and_batch_blocker() -> None:
    from app.research.options_golden_search.projections import approval_reviews

    row = _row()
    row["gate_outcomes"] = [_gate("forward_0", status="not_enough_evidence")]
    row["candidates"][0]["gate_outcomes"] = [_gate("development", status="does_not_meet_rules")]
    row["final_results"][0]["gate_outcomes"] = [_gate("final", status="does_not_meet_rules")]
    row["exposure"]["classification"] = "history_unknown"
    review = approval_reviews(row)[POINT]
    assert review["failures"] == approval_failures(row, POINT)
    assert review["blocked_reason"] is None
    assert {gate["gate_id"] for gate in review["failures"]} == {"forward_0:profit", "development:profit", "final:profit", "final:exposure"}
    row["final_results"][1]["verdict"] = "could_not_evaluate"
    assert "technically" in approval_reviews(row)[POINT]["blocked_reason"]


def test_normal_approval_cannot_smuggle_a_waiver() -> None:
    with pytest.raises(ValueError, match="cannot waive"):
        validate_approval(_row(), _command("approve", "", ("final:profit",)))
