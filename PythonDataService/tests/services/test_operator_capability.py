"""PRD #619-B runtime-freshness action gating + #619-D action-conflict matrix."""

from __future__ import annotations

import pytest

from app.schemas.live_runs import (
    DesiredStateView,
    InstanceProcessView,
    LiveBinding,
)
from app.services.mutation_attempt import DispatchState, MutationAttempt
from app.services.operator_capability import evaluate_action
from app.services.runtime_freshness import unavailable_runtime_freshness


def _mutation(
    *,
    action: str = "stop",
    state: DispatchState = "OUTCOME_UNKNOWN",
    instance_id: str = "inst-A",
    requested_at_ms: int = 1_700_000_000_000,
) -> MutationAttempt:
    return MutationAttempt(
        mutation_attempt_id=f"att-{action}-{state}",
        instance_id=instance_id,
        run_id=None,
        action=action,  # type: ignore[arg-type]
        requested_at_ms=requested_at_ms,
        last_transition_at_ms=requested_at_ms,
        dispatch_state=state,
    )


@pytest.mark.parametrize(
    "action",
    ["resume", "flatten_and_pause"],
)
def test_posture_demotion_disables_actions_that_require_current_runtime(
    action: str,
) -> None:
    capability = evaluate_action(
        action,  # type: ignore[arg-type]
        process=InstanceProcessView(state="running"),
        live_binding=LiveBinding(run_id="run-1"),
        desired_state=DesiredStateView(state="PAUSED", path_status="ok"),
        owned_positions_empty=False,
        runtime_freshness=unavailable_runtime_freshness(
            "ENGINE_RUNTIME_MISSING"
        ),
    )

    assert capability.enabled is False
    assert capability.disabled_reason_code == "POSTURE_DEMOTED"
    assert capability.disabled_reasons == ["POSTURE_DEMOTED"]


@pytest.mark.parametrize("action", ["pause", "stop", "mark_poisoned"])
def test_posture_demotion_keeps_fail_safe_actions_available(
    action: str,
) -> None:
    capability = evaluate_action(
        action,  # type: ignore[arg-type]
        process=InstanceProcessView(state="running"),
        live_binding=LiveBinding(run_id="run-1"),
        desired_state=DesiredStateView(state="RUNNING", path_status="ok"),
        runtime_freshness=unavailable_runtime_freshness(
            "ENGINE_RUNTIME_MISSING"
        ),
    )

    assert capability.enabled is True


# ---------------------------------------------------------------------------
# PRD #619-D action-conflict matrix.
#
# The matrix engages whenever ``latest_mutation.dispatch_state`` is anything
# other than ``EFFECT_CONFIRMED``.  These tests cover every cell that the PRD
# names as blocked or allowed, plus a few negative cases to prove the matrix
# does not over-block.
# ---------------------------------------------------------------------------


_RUNNING_BINDING = LiveBinding(run_id="run-1")
_RUNNING_PROCESS = InstanceProcessView(state="running")
_RUNNING_INTENT = DesiredStateView(state="RUNNING", path_status="ok")
_PAUSED_INTENT = DesiredStateView(state="PAUSED", path_status="ok")


@pytest.mark.parametrize(
    ("prior_action", "evaluated_action", "expected_code"),
    [
        ("stop", "resume", "MUTATION_UNRESOLVED_STOP"),
        ("stop", "stop", "MUTATION_UNRESOLVED_STOP"),
        ("resume", "resume", "MUTATION_UNRESOLVED_RESUME"),
        ("flatten", "flatten_and_pause", "MUTATION_UNRESOLVED_FLATTEN"),
    ],
)
def test_unresolved_prior_blocks_matrix_cell(
    prior_action: str, evaluated_action: str, expected_code: str
) -> None:
    capability = evaluate_action(
        evaluated_action,  # type: ignore[arg-type]
        process=_RUNNING_PROCESS,
        live_binding=_RUNNING_BINDING,
        desired_state=_PAUSED_INTENT if evaluated_action == "resume" else _RUNNING_INTENT,
        owned_positions_empty=False,
        latest_mutation=_mutation(action=prior_action),
    )

    assert capability.enabled is False
    assert expected_code in capability.disabled_reasons


@pytest.mark.parametrize(
    ("prior_action", "allowed_action"),
    [
        ("start", "stop"),
        ("start", "pause"),
        ("stop", "pause"),
        ("flatten", "pause"),
        ("flatten", "stop"),
        ("resume", "pause"),
        ("resume", "stop"),
    ],
)
def test_unresolved_prior_does_not_block_matrix_safe_cell(
    prior_action: str, allowed_action: str
) -> None:
    capability = evaluate_action(
        allowed_action,  # type: ignore[arg-type]
        process=_RUNNING_PROCESS,
        live_binding=_RUNNING_BINDING,
        desired_state=_RUNNING_INTENT,
        owned_positions_empty=False,
        latest_mutation=_mutation(action=prior_action),
    )

    assert capability.enabled is True


def test_effect_confirmed_disengages_matrix() -> None:
    capability = evaluate_action(
        "resume",
        process=_RUNNING_PROCESS,
        live_binding=_RUNNING_BINDING,
        desired_state=_PAUSED_INTENT,
        latest_mutation=_mutation(action="stop", state="EFFECT_CONFIRMED"),
    )

    assert capability.enabled is True
    assert "MUTATION_UNRESOLVED_STOP" not in capability.disabled_reasons


@pytest.mark.parametrize(
    "non_confirmed",
    [
        "PREPARED",
        "DISPATCHING",
        "RESPONSE_CONFIRMED",
        "OUTCOME_UNKNOWN",
        "EFFECT_NOT_OBSERVED",
        "NOT_PROVABLE",
        "EVIDENCE_CONFLICT",
    ],
)
def test_every_non_confirmed_state_keeps_matrix_engaged(non_confirmed: str) -> None:
    capability = evaluate_action(
        "resume",
        process=_RUNNING_PROCESS,
        live_binding=_RUNNING_BINDING,
        desired_state=_PAUSED_INTENT,
        latest_mutation=_mutation(action="stop", state=non_confirmed),  # type: ignore[arg-type]
    )

    assert capability.enabled is False
    assert "MUTATION_UNRESOLVED_STOP" in capability.disabled_reasons


def test_matrix_conflict_rides_alongside_posture_demotion() -> None:
    capability = evaluate_action(
        "resume",
        process=_RUNNING_PROCESS,
        live_binding=_RUNNING_BINDING,
        desired_state=_PAUSED_INTENT,
        runtime_freshness=unavailable_runtime_freshness("ENGINE_RUNTIME_MISSING"),
        latest_mutation=_mutation(action="stop"),
    )

    assert capability.enabled is False
    # Both the posture demotion and the matrix block are surfaced;
    # neither masks the other.
    assert "POSTURE_DEMOTED" in capability.disabled_reasons
    assert "MUTATION_UNRESOLVED_STOP" in capability.disabled_reasons


def test_matrix_conflict_rides_alongside_no_live_binding() -> None:
    capability = evaluate_action(
        "flatten_and_pause",
        process=_RUNNING_PROCESS,
        live_binding=None,
        owned_positions_empty=False,
        latest_mutation=_mutation(action="flatten"),
    )

    assert capability.enabled is False
    assert "NO_LIVE_BINDING" in capability.disabled_reasons
    assert "MUTATION_UNRESOLVED_FLATTEN" in capability.disabled_reasons


def test_no_latest_mutation_leaves_evaluation_unchanged() -> None:
    capability = evaluate_action(
        "resume",
        process=_RUNNING_PROCESS,
        live_binding=_RUNNING_BINDING,
        desired_state=_PAUSED_INTENT,
        latest_mutation=None,
    )

    assert capability.enabled is True
