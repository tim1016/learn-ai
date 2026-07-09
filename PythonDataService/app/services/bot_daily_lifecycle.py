"""Pure daily lifecycle projection for bot control surfaces."""

from __future__ import annotations

from dataclasses import dataclass

from app.engine.live.bot_lifecycle_state import (
    BotDisplayStatus,
    BotLifecyclePhase,
    BotLifecycleStateRecord,
    BotRollCallOfferRecord,
)
from app.schemas.live_runs import (
    BotDailyLifecycleProjection,
    BotLifecycleAction,
    BotLifecycleCondition,
    HostProcessStartCapability,
    InstanceProcessView,
)


@dataclass(frozen=True)
class BotDailyLifecycleEvidence:
    strategy_instance_id: str
    process: InstanceProcessView
    start_capability: HostProcessStartCapability
    latest_run_id: str | None
    active_run_id: str | None
    persisted_state: BotLifecycleStateRecord | None = None
    roll_call_offer: BotRollCallOfferRecord | None = None
    conditions: tuple[BotLifecycleCondition, ...] = ()
    now_ms: int = 0


def project_bot_daily_lifecycle(evidence: BotDailyLifecycleEvidence) -> BotDailyLifecycleProjection:
    """Return the operator-facing three-state lifecycle projection.

    Reads are pure: this function never writes drift back to disk. Lifecycle
    commands and scheduled ticks are the only persist points.
    """

    phase = _observed_phase(evidence)
    persisted_phase = evidence.persisted_state.phase if evidence.persisted_state is not None else None
    on_roster = evidence.persisted_state.on_roster if evidence.persisted_state is not None else True
    display_status = _display_status(
        phase=phase,
        process_state=evidence.process.state,
        on_roster=on_roster,
        condition_count=len(evidence.conditions),
        start_enabled=evidence.start_capability.enabled,
        offer_available=evidence.roll_call_offer is not None,
    )
    primary_action = _primary_action(phase, display_status, evidence)
    ambient_actions = _ambient_actions(phase, on_roster)
    attention_badge = _attention_badge(display_status)
    return BotDailyLifecycleProjection(
        phase=phase.value,
        presence_label=_presence_label(phase),
        display_status=display_status.value,
        attention_badge=attention_badge.value if attention_badge is not None else None,
        reason=_reason(display_status, evidence),
        on_roster=on_roster,
        active_run_id=evidence.active_run_id,
        latest_run_id=evidence.latest_run_id,
        drift_detected=persisted_phase is not None and persisted_phase != phase,
        conditions=list(evidence.conditions),
        primary_action=primary_action,
        ambient_actions=ambient_actions,
    )


def _observed_phase(evidence: BotDailyLifecycleEvidence) -> BotLifecyclePhase:
    if evidence.persisted_state is not None and evidence.persisted_state.phase == BotLifecyclePhase.RETIRED:
        return BotLifecyclePhase.RETIRED
    if evidence.process.state in {"running", "stopping"}:
        return BotLifecyclePhase.ON_DUTY
    return BotLifecyclePhase.OFF_DUTY


def _presence_label(phase: BotLifecyclePhase) -> str:
    match phase:
        case BotLifecyclePhase.OFF_DUTY:
            return "Off duty"
        case BotLifecyclePhase.ON_DUTY:
            return "On duty"
        case BotLifecyclePhase.RETIRED:
            return "Retired"


def _display_status(
    *,
    phase: BotLifecyclePhase,
    process_state: str,
    on_roster: bool,
    condition_count: int,
    start_enabled: bool,
    offer_available: bool,
) -> BotDisplayStatus:
    if phase == BotLifecyclePhase.RETIRED:
        return BotDisplayStatus.RETIRED
    if phase == BotLifecyclePhase.ON_DUTY:
        return BotDisplayStatus.CLOCKING_OUT if process_state == "stopping" else BotDisplayStatus.ON_DUTY
    if condition_count > 0:
        return BotDisplayStatus.SICK_BAY
    if not on_roster:
        return BotDisplayStatus.OFF_ROSTER
    if start_enabled and offer_available:
        return BotDisplayStatus.READY
    return BotDisplayStatus.OFF_DUTY


def _attention_badge(display_status: BotDisplayStatus) -> BotDisplayStatus | None:
    if display_status in {
        BotDisplayStatus.SICK_BAY,
        BotDisplayStatus.READY,
        BotDisplayStatus.OFF_ROSTER,
    }:
        return display_status
    return None


def _reason(display_status: BotDisplayStatus, evidence: BotDailyLifecycleEvidence) -> str | None:
    if display_status == BotDisplayStatus.READY:
        return "Roll call offered a fresh start before stop-time."
    if display_status == BotDisplayStatus.OFF_ROSTER:
        return "This bot is intentionally left off tomorrow's duty roster."
    if display_status == BotDisplayStatus.SICK_BAY:
        count = len(evidence.conditions)
        noun = "condition" if count == 1 else "conditions"
        verb = "needs" if count == 1 else "need"
        return f"{count} {noun} {verb} a cure before start."
    if display_status == BotDisplayStatus.OFF_DUTY and not evidence.start_capability.enabled:
        return evidence.start_capability.disabled_reason_code or "Start is not yet proven safe."
    if (
        display_status == BotDisplayStatus.OFF_DUTY
        and evidence.start_capability.enabled
        and evidence.roll_call_offer is None
    ):
        return "Run roll call to issue a start offer."
    if display_status == BotDisplayStatus.CLOCKING_OUT:
        return "Clean-exit procedure is in progress."
    return None


def _primary_action(
    phase: BotLifecyclePhase,
    display_status: BotDisplayStatus,
    evidence: BotDailyLifecycleEvidence,
) -> BotLifecycleAction | None:
    if phase == BotLifecyclePhase.ON_DUTY:
        return BotLifecycleAction(id="end_day_now", label="End day now")
    if (
        display_status == BotDisplayStatus.READY
        and evidence.start_capability.enabled
        and evidence.roll_call_offer is not None
    ):
        return BotLifecycleAction(
            id="confirm_start",
            label="Start",
            offer_id=evidence.roll_call_offer.offer_id,
            expires_at_ms=evidence.roll_call_offer.expires_at_ms,
        )
    return None


def _ambient_actions(phase: BotLifecyclePhase, on_roster: bool) -> list[BotLifecycleAction]:
    if phase == BotLifecyclePhase.RETIRED:
        return []
    actions = [
        BotLifecycleAction(
            id="take_off_roster" if on_roster else "add_to_roster",
            label="Take off roster" if on_roster else "Add to roster",
        )
    ]
    if phase == BotLifecyclePhase.OFF_DUTY:
        actions.append(BotLifecycleAction(id="retire_replace", label="Retire & Replace"))
    return actions


__all__ = [
    "BotDailyLifecycleEvidence",
    "project_bot_daily_lifecycle",
]
