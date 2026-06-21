"""PRD #619-B runtime-freshness action gating."""

from __future__ import annotations

import pytest

from app.schemas.live_runs import (
    DesiredStateView,
    InstanceProcessView,
    LiveBinding,
)
from app.services.operator_capability import evaluate_action
from app.services.runtime_freshness import unavailable_runtime_freshness


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
