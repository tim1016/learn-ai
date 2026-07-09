from __future__ import annotations

from app.engine.live.bot_lifecycle_state import (
    BotLifecyclePhase,
    BotLifecycleStateRecord,
    BotRollCallOfferRecord,
)
from app.schemas.live_runs import (
    BotDailyLifecycleProjection,
    BotLifecycleCondition,
    HostProcessStartCapability,
    InstanceProcessView,
)
from app.services.bot_daily_lifecycle import (
    BotDailyLifecycleEvidence,
    project_bot_daily_lifecycle,
)


def _project(
    *,
    process_state: str = "idle",
    start_enabled: bool = False,
    persisted_state: BotLifecycleStateRecord | None = None,
    roll_call_offer: BotRollCallOfferRecord | None = None,
    condition_count: int = 0,
    conditions: tuple[BotLifecycleCondition, ...] = (),
) -> BotDailyLifecycleProjection:
    return project_bot_daily_lifecycle(
        BotDailyLifecycleEvidence(
            strategy_instance_id="paper-ema",
            process=InstanceProcessView(state=process_state),
            start_capability=HostProcessStartCapability(
                enabled=start_enabled,
                run_id="run-1" if start_enabled else None,
                disabled_reason_code=None if start_enabled else "HOST_SERVICE_OFFLINE",
            ),
            latest_run_id="run-1",
            active_run_id="run-1" if process_state in {"running", "stopping"} else None,
            persisted_state=persisted_state,
            roll_call_offer=roll_call_offer,
            condition_count=condition_count,
            conditions=conditions,
            now_ms=1_700_000_000_000,
        )
    )


def _state(
    phase: BotLifecyclePhase = BotLifecyclePhase.OFF_DUTY,
    *,
    on_roster: bool = True,
    active_run_id: str | None = None,
) -> BotLifecycleStateRecord:
    return BotLifecycleStateRecord(
        phase=phase,
        on_roster=on_roster,
        active_run_id=active_run_id,
        last_transition_at_ms=1_700_000_000_000,
    )


def _offer() -> BotRollCallOfferRecord:
    return BotRollCallOfferRecord(
        offer_id="offer-1",
        strategy_instance_id="paper-ema",
        run_id="run-1",
        session_date="2026-07-08",
        issued_at_ms=1_700_000_000_000,
        expires_at_ms=1_700_010_000_000,
        evidence_snapshot={"readiness_verdict": "READY"},
    )


def _condition() -> BotLifecycleCondition:
    return BotLifecycleCondition(
        scope="account",
        severity="warning",
        title="Account evidence stale",
        detail="Receipt acct-recon-1 expired before this triage snapshot.",
        owner_label="Account DU1234567",
        cure_action="reconcile_now",
        cure_label="Run account reconcile",
    )


def test_project_bot_daily_lifecycle_off_duty_ready_has_single_start_action() -> None:
    projection = _project(start_enabled=True, persisted_state=_state(), roll_call_offer=_offer())

    assert projection.phase == "OFF_DUTY"
    assert projection.presence_label == "Off duty"
    assert projection.display_status == "Ready"
    assert projection.primary_action is not None
    assert projection.primary_action.id == "confirm_start"
    assert projection.primary_action.offer_id == "offer-1"
    assert [action.id for action in projection.ambient_actions] == [
        "take_off_roster",
        "retire_replace",
    ]


def test_project_bot_daily_lifecycle_running_has_end_day_action_without_pause_or_resume() -> None:
    projection = _project(
        process_state="running",
        persisted_state=_state(BotLifecyclePhase.ON_DUTY, active_run_id="run-1"),
    )

    action_ids = [action.id for action in projection.ambient_actions]
    if projection.primary_action is not None:
        action_ids.append(projection.primary_action.id)
    assert projection.phase == "ON_DUTY"
    assert projection.display_status == "On duty"
    assert projection.primary_action is not None
    assert projection.primary_action.id == "end_day_now"
    assert "pause" not in action_ids
    assert "resume" not in action_ids


def test_project_bot_daily_lifecycle_stopping_renders_clocking_out() -> None:
    projection = _project(
        process_state="stopping",
        persisted_state=_state(BotLifecyclePhase.ON_DUTY, active_run_id="run-1"),
    )

    assert projection.phase == "ON_DUTY"
    assert projection.display_status == "Clocking out"
    assert projection.primary_action is not None
    assert projection.primary_action.id == "end_day_now"


def test_project_bot_daily_lifecycle_off_roster_has_closed_roster_action() -> None:
    projection = _project(
        start_enabled=True,
        persisted_state=_state(on_roster=False),
        roll_call_offer=_offer(),
    )

    assert projection.display_status == "Off roster"
    assert projection.primary_action is None
    assert [action.id for action in projection.ambient_actions] == [
        "add_to_roster",
        "retire_replace",
    ]


def test_project_bot_daily_lifecycle_retired_is_terminal() -> None:
    projection = _project(
        start_enabled=True,
        persisted_state=_state(BotLifecyclePhase.RETIRED, on_roster=False),
        roll_call_offer=_offer(),
    )

    assert projection.phase == "RETIRED"
    assert projection.display_status == "Retired"
    assert projection.primary_action is None
    assert projection.ambient_actions == []


def test_project_bot_daily_lifecycle_flags_drift_without_persisting_it() -> None:
    projection = _project(process_state="running", persisted_state=_state())

    assert projection.phase == "ON_DUTY"
    assert projection.drift_detected is True


def test_project_bot_daily_lifecycle_conditions_put_off_duty_bot_in_sick_bay() -> None:
    projection = _project(
        start_enabled=True,
        persisted_state=_state(),
        roll_call_offer=_offer(),
        condition_count=2,
    )

    assert projection.phase == "OFF_DUTY"
    assert projection.display_status == "Sick bay"
    assert projection.attention_badge == "Sick bay"
    assert projection.reason == "2 conditions need a cure before start."
    assert projection.primary_action is None


def test_project_bot_daily_lifecycle_returns_condition_cure_copy() -> None:
    projection = _project(
        start_enabled=True,
        persisted_state=_state(),
        roll_call_offer=_offer(),
        conditions=(_condition(),),
    )

    assert projection.display_status == "Sick bay"
    assert projection.reason == "1 condition needs a cure before start."
    assert projection.conditions == [_condition()]
    assert projection.primary_action is None


def test_project_bot_daily_lifecycle_start_gate_without_offer_stays_off_duty() -> None:
    projection = _project(start_enabled=True, persisted_state=_state())

    assert projection.display_status == "Off duty"
    assert projection.reason == "Run roll call to issue a start offer."
    assert projection.primary_action is None
