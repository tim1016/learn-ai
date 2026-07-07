from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Tier
# ---------------------------------------------------------------------------

OperatorNoticeTier = Literal["info", "warning", "critical"]

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
        "wait",
        "open_runbook",
        "focus_cockpit_action",
        "renew_control_plane_lease",
        "external_manual_check",
        "redeploy",
    ]
    label: str | None = None
    target: str | None = None


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
    action: OperatorNoticeAction
    runbook_slug: str | None = None
    occurred_at_ms: int | None = None


class OperatorIncident(BaseModel):
    """Persisted operator incident (PR 2 writes; PR 1 declares the shape)."""

    schema_version: int = 1
    incident_id: str
    category: Literal["watchdog", "activity", "reconciliation", "order", "submit", "safety-halt"]
    notice: OperatorNotice
    started_at_ms: int
    resolved_at_ms: int | None = None
    evidence: dict[str, object] = Field(default_factory=dict)
