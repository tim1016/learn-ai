from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# Tier
# ---------------------------------------------------------------------------

OperatorNoticeTier = Literal["info", "warning", "critical"]
OperatorNoticeActionability = Literal["actuatable", "routed", "self_resolving", "no_remedy"]
OperatorNoticeRemedyStatus = Literal["inherent", "unbuilt"]

# ---------------------------------------------------------------------------
# Codes — PR-1-through-PR-6 slots plus later ADR-backed notice families.
# Frontend type generation is stable across the initiative.
# ---------------------------------------------------------------------------

OperatorNoticeCode = Literal[
    # PR 1 — runtime freshness (implemented in this PR).
    "runtime.market_closed",
    "runtime.market_session_halted",
    "runtime.market_data_stale",
    "runtime.market_data_first_bar_timeout",
    "runtime.market_data_feed_stalled",
    "runtime.broker_probe_stale",
    "runtime.broker_probe_missing",
    "runtime.command_loop_unresponsive",
    "runtime.engine_runtime_incompatible",
    "runtime.control_plane_lease_stale",
    "runtime.control_plane_boot_id_mismatch",
    # PR 2 — watchdog two-phase halt (reserved).
    "watchdog.flatten_completed",
    "watchdog.flatten_not_needed",
    "watchdog.flatten_timed_out",
    "watchdog.flatten_failed",
    "watchdog.broker_disconnected_before_flatten",
    # PR 5 — activity health (reserved).
    "activity.publisher_starting",
    "activity.publisher_not_running",
    "activity.publisher_degraded",
    "activity.source_blind_to_bot_orders",
    "activity.dropped_paused_intent",
    # PR 6 — reconciliation (reserved).
    "reconciliation.required_after_uncertain_flatten",
    "reconciliation.discovered_execution_not_in_engine_state",
    "reconciliation.divergence_while_submitting",
    # Fleet liveness — reserved silent-state code.
    "fleet.sibling_liveness_unproven",
    # Broker session mirror — ADR 0018 orphaned-socket observability.
    "broker_session.orphaned_socket",
    # PRD #928 / ADR 0024 — order and submit terminal outcomes (reserved).
    "order.rejected",
    "submit.uncertain",
    "submit.halted",
    "submit.launch_failed",
    "submit.unmapped_diagnostic",
    # Stream-primary PRD — safety-halt incident bridge.
    "safety_halt.poisoned",
]


class NoticeCodeContract(BaseModel):
    """Per-code truthfulness metadata pinned by the exhaustiveness gate."""

    tier: OperatorNoticeTier
    actionability: OperatorNoticeActionability
    remedy_status: OperatorNoticeRemedyStatus | None = None


NOTICE_CODE_CONTRACTS: dict[str, NoticeCodeContract] = {
    "runtime.market_closed": NoticeCodeContract(tier="info", actionability="self_resolving"),
    "runtime.market_session_halted": NoticeCodeContract(tier="info", actionability="self_resolving"),
    "runtime.market_data_stale": NoticeCodeContract(tier="warning", actionability="routed"),
    "runtime.market_data_first_bar_timeout": NoticeCodeContract(tier="critical", actionability="routed"),
    "runtime.market_data_feed_stalled": NoticeCodeContract(tier="warning", actionability="routed"),
    "runtime.broker_probe_stale": NoticeCodeContract(tier="warning", actionability="self_resolving"),
    "runtime.broker_probe_missing": NoticeCodeContract(tier="warning", actionability="routed"),
    "runtime.command_loop_unresponsive": NoticeCodeContract(tier="critical", actionability="routed"),
    "runtime.engine_runtime_incompatible": NoticeCodeContract(tier="critical", actionability="actuatable"),
    "runtime.control_plane_lease_stale": NoticeCodeContract(tier="critical", actionability="actuatable"),
    "runtime.control_plane_boot_id_mismatch": NoticeCodeContract(tier="critical", actionability="routed"),
    "watchdog.flatten_completed": NoticeCodeContract(tier="info", actionability="self_resolving"),
    "watchdog.flatten_not_needed": NoticeCodeContract(tier="info", actionability="self_resolving"),
    "watchdog.flatten_timed_out": NoticeCodeContract(tier="critical", actionability="routed"),
    "watchdog.flatten_failed": NoticeCodeContract(tier="critical", actionability="routed"),
    "watchdog.broker_disconnected_before_flatten": NoticeCodeContract(tier="critical", actionability="routed"),
    "activity.publisher_starting": NoticeCodeContract(tier="info", actionability="self_resolving"),
    "activity.publisher_not_running": NoticeCodeContract(tier="critical", actionability="routed"),
    "activity.publisher_degraded": NoticeCodeContract(tier="warning", actionability="self_resolving"),
    "activity.source_blind_to_bot_orders": NoticeCodeContract(tier="warning", actionability="routed"),
    "activity.dropped_paused_intent": NoticeCodeContract(tier="warning", actionability="no_remedy", remedy_status="inherent"),
    "reconciliation.required_after_uncertain_flatten": NoticeCodeContract(tier="critical", actionability="actuatable"),
    "reconciliation.discovered_execution_not_in_engine_state": NoticeCodeContract(tier="critical", actionability="routed"),
    "reconciliation.divergence_while_submitting": NoticeCodeContract(
        tier="critical",
        actionability="no_remedy",
        remedy_status="unbuilt",
    ),
    "fleet.sibling_liveness_unproven": NoticeCodeContract(
        tier="critical",
        actionability="no_remedy",
        remedy_status="unbuilt",
    ),
    "broker_session.orphaned_socket": NoticeCodeContract(tier="critical", actionability="routed"),
    "order.rejected": NoticeCodeContract(tier="critical", actionability="routed"),
    "submit.uncertain": NoticeCodeContract(tier="critical", actionability="routed"),
    "submit.halted": NoticeCodeContract(tier="critical", actionability="self_resolving"),
    "submit.launch_failed": NoticeCodeContract(tier="critical", actionability="actuatable"),
    "submit.unmapped_diagnostic": NoticeCodeContract(tier="critical", actionability="routed"),
    "safety_halt.poisoned": NoticeCodeContract(tier="critical", actionability="actuatable"),
}

# ---------------------------------------------------------------------------
# Runtime freshness reason codes — moved from app/services/runtime_freshness.py.
# Single source of truth for the closed enum reaching the cockpit through
# the runtime.* notice family.
# ---------------------------------------------------------------------------

RuntimeFreshnessReasonCode = Literal[
    "ENGINE_RUNTIME_MISSING",
    "ENGINE_RUNTIME_INVALID_OR_INCOMPATIBLE",
    "COMMAND_LOOP_STALE",
    "BROKER_PROBE_STALE",
    "BROKER_PROBE_MISSING",
    "BAR_LOOP_HEARTBEAT_STALE",
    "BAR_LOOP_FIRST_BAR_TIMEOUT",
    "BAR_LOOP_SOURCE_MISSING",
    "BAR_LOOP_LATEST_BAR_STALE",
    "BAR_LOOP_SESSION_CLOSED",
    "BAR_LOOP_SESSION_HALTED",
    "CONTROL_PLANE_LEASE_STALE",
    "CONTROL_PLANE_BOOT_ID_MISMATCH",
]


class OperatorNoticeAction(BaseModel):
    """Cockpit affordance attached to a notice."""

    kind: Literal[
        "none",
        "open_runbook",
        "focus_cockpit_action",
        "renew_control_plane_lease",
        "external_manual_check",
        "redeploy",
    ]
    label: str | None = None
    target: str | None = None


def validate_actionability_action_pairing(
    *,
    actionability: OperatorNoticeActionability,
    action: OperatorNoticeAction,
    remedy_status: OperatorNoticeRemedyStatus | None,
    noun: str,
) -> None:
    """Enforce the actionability ↔ action pairing shared by notices and receipts.

    ``noun`` names the validated shape ("notices" / "receipts") so error
    messages stay specific to the model that failed.
    """
    action_kind = action.kind
    if actionability == "actuatable":
        if action_kind not in {"renew_control_plane_lease", "focus_cockpit_action", "redeploy"}:
            raise ValueError(f"actuatable {noun} require an inline cockpit action")
        if not action.label:
            raise ValueError(f"actuatable {noun} require an action label")
    elif actionability == "routed":
        if action_kind not in {"open_runbook", "external_manual_check"}:
            raise ValueError(f"routed {noun} require an external route action")
        if not action.target:
            raise ValueError(f"routed {noun} require a named target")
        if not action.label:
            raise ValueError(f"routed {noun} require an action label")
    elif actionability in {"self_resolving", "no_remedy"}:
        if action_kind != "none":
            raise ValueError(f"{actionability} {noun} cannot carry a clickable action")
        if action.label is not None or action.target is not None:
            raise ValueError(f"{actionability} {noun} cannot carry action label or target")
        if actionability == "no_remedy" and remedy_status is None:
            raise ValueError(f"no_remedy {noun} require remedy_status")
    else:
        raise AssertionError(f"unknown {noun} actionability: {actionability}")
    if actionability != "no_remedy" and remedy_status is not None:
        raise ValueError(f"remedy_status is only legal for no_remedy {noun}")


class OperatorNotice(BaseModel):
    """Backend-authored, trader-readable failure surface.

    The cockpit renders ``title``, ``message``, and ``action`` verbatim.
    ``source_codes`` is for forensics only.
    ``forensic_facts`` carries raw numeric diagnostics (e.g. ``age_ms``)
    for the engineer-targeted "Forensic detail" panel; never pre-formatted copy.
    """

    code: OperatorNoticeCode
    tier: OperatorNoticeTier
    title: str
    message: str
    source_codes: list[str] = Field(default_factory=list)
    forensic_facts: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    actionability: OperatorNoticeActionability
    resolution: str = Field(min_length=1)
    remedy_status: OperatorNoticeRemedyStatus | None = None
    action: OperatorNoticeAction
    runbook_slug: str | None = None
    occurred_at_ms: int | None = None

    @model_validator(mode="after")
    def _truthfulness_contract(self) -> OperatorNotice:
        contract = NOTICE_CODE_CONTRACTS.get(str(self.code))
        if contract is None:
            raise ValueError(f"{self.code} is missing a NOTICE_CODE_CONTRACTS entry")
        if self.tier != contract.tier:
            raise ValueError(f"{self.code} must use tier={contract.tier}")
        if self.actionability != contract.actionability:
            raise ValueError(f"{self.code} must use actionability={contract.actionability}")
        if self.remedy_status != contract.remedy_status:
            raise ValueError(f"{self.code} must use remedy_status={contract.remedy_status!r}")

        validate_actionability_action_pairing(
            actionability=self.actionability,
            action=self.action,
            remedy_status=self.remedy_status,
            noun="notices",
        )
        return self


class OperatorIncident(BaseModel):
    """Persisted operator incident (PR 2 writes; PR 1 declares the shape)."""

    schema_version: int = 1
    incident_id: str
    category: Literal["watchdog", "activity", "reconciliation", "order", "submit", "safety-halt"]
    notice: OperatorNotice
    started_at_ms: int
    resolved_at_ms: int | None = None
    evidence: dict[str, object] = Field(default_factory=dict)
