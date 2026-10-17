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


def _command(kind: str = "approve", comment: str = "") -> Command:
    return Command(command=kind, expected_revision=1, idempotency_key="approve", candidate_hash=POINT, comment=comment)


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
    validate_approval(row, _command("approve_exception", "I accept the listed failure."))
    assert approval_failures(row, POINT) == failed


@pytest.mark.parametrize("classification", ["history_unknown", "previously_used"])
def test_reused_or_unknown_final_history_requires_explicit_owner_exception(classification: str) -> None:
    row = _row()
    row["exposure"]["classification"] = classification
    with pytest.raises(ValueError, match="Normal approval"):
        validate_approval(row, _command())
    with pytest.raises(ValueError, match="nonblank"):
        validate_approval(row, _command("approve_exception", " \n "))
    validate_approval(row, _command("approve_exception", "Prior exposure acknowledged."))
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
