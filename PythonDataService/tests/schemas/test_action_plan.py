"""Slice 1A — ActionPlan baseline schema (PRD #593, issue #594).

Slice 1A ships only the empty-plan acceptance gate. Stock and option entry
leg shapes land in Slices 1B (#595) and 1C (#596); negative-case schema
errors (orphan close_leg, duplicate leg_id, missing underlying) are covered
in the same later slices alongside the shapes they reject.

Prior art: tests/schemas/test_host_runner_deploy_request_sizing.py.
"""

from __future__ import annotations

from app.schemas.action_plan import ActionPlan


def test_empty_action_plan_round_trips() -> None:
    plan = ActionPlan(on_enter=[], on_exit=[])

    assert plan.on_enter == []
    assert plan.on_exit == []
    assert plan.model_dump() == {"on_enter": [], "on_exit": []}


def test_empty_action_plan_constructs_from_defaults() -> None:
    plan = ActionPlan()

    assert plan.on_enter == []
    assert plan.on_exit == []
