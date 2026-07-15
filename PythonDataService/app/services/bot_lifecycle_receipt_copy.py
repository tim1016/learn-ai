"""Closed operator-copy maps for lifecycle chart receipts."""

from __future__ import annotations

from collections.abc import Mapping

ReceiptCopy = tuple[str, str | None]

_HEADLINES: Mapping[str, Mapping[str, str]] = {
    "account_identity": {
        "CONSISTENT": "Broker account observations agree.",
        "CONFLICTING": "Broker account observations conflict.",
        "UNKNOWN": "Broker account consistency is unknown.",
        "NOT_COMPARABLE": "Broker account observations are not comparable.",
    },
    "action_plan_consumption": {
        "ACTIVE": "The committed action plan is active for this run.",
        "DECLARATIVE_ONLY": "The committed action plan is declared but not consumed by the current runtime.",
        "UNKNOWN": "Action-plan consumption is unknown.",
    },
    "broker_activity_state": {
        "ready": "Broker-activity publisher is ready.",
        "starting": "Broker-activity publisher is starting.",
        "degraded": "Broker-activity publisher is degraded.",
        "unavailable": "Broker-activity publisher is unavailable.",
    },
    "broker_connection": {
        "CONNECTED": "Broker connection is up.",
        "DISCONNECTED": "Broker connection is down.",
        "UNKNOWN": "Broker connection is unknown.",
    },
    "broker_safety": {
        "PAPER_ONLY": "Broker safety is paper-only.",
        "UNSAFE": "Broker safety is not safe for paper trading.",
        "UNKNOWN": "Broker safety is unknown.",
    },
    "capability_disabled": {
        "default": "{label} has a backend-authored disabled reason.",
    },
    "capability_enabled": {
        "true": "{label} is currently available.",
        "false": "{label} is currently blocked.",
    },
    "configuration_reason": {
        "STRATEGY_KEY_MISSING": "The run is missing its strategy key.",
        "MAX_ORDERS_CAP_UNSET": "The run is missing a usable daily order cap.",
        "SIZING_PRESET_MISSING": "The run is missing a committed sizing policy.",
        "SIZING_PROVENANCE_MISSING": "The run is missing sizing provenance.",
        "INSTANCE_BROKER_SELF_INCONSISTENT": "The bot's broker ownership view is not self-consistent.",
    },
    "desired_path_status": {
        "ok": "Desired-state sidecar resolved cleanly.",
        "absent": "Desired-state sidecar is absent; effective state is RUNNING.",
        "corrupt": "Desired-state sidecar is corrupt.",
        "unknown_no_ledger_binding": "Desired-state sidecar cannot be resolved without a ledger binding.",
    },
    "execution_posture": {
        "PAPER_EXECUTION": "Execution posture is paper trading.",
        "READ_ONLY": "Execution posture is read-only observation.",
        "UNSAFE": "Execution posture is unsafe.",
        "UNKNOWN": "Execution posture is unknown.",
    },
    "instrument_surface": {
        "explicit": "Instrument surface is explicit.",
        "policy": "Instrument surface is policy-driven.",
    },
    "readiness_gate_status": {
        "pass": "{name} is passing.",
        "block": "{name} is blocking this lifecycle step.",
        "freeze": "{name} froze account activity.",
        "poison": "{name} poisoned this run.",
    },
    "reconciliation_next_step": {
        "FAILED": "Run reconciliation before resuming.",
        "STALE": "Refresh reconciliation before treating the gate as clear.",
        "NOT_AVAILABLE": "Wait for a reconciliation receipt before resuming.",
        "IN_PROGRESS": "Wait for reconciliation to finish.",
    },
    "reconciliation_state": {
        "CLEAN": "Broker and engine state agree.",
        "ADOPTED": "Reconciliation adopted broker intent evidence.",
        "STALE": "Reconciliation evidence is stale.",
        "FAILED": "Reconciliation failed.",
        "IN_PROGRESS": "Reconciliation is still in progress.",
        "NOT_AVAILABLE": "No reconciliation receipt is available yet.",
    },
    "risk_posture": {
        "FLAT": "The bot is flat.",
        "LONG": "The bot is long.",
        "SHORT": "The bot is short.",
        "MIXED": "The bot has mixed exposure.",
        "UNKNOWN": "The bot's current exposure is unknown.",
    },
}

_HEADLINE_DEFAULTS: Mapping[str, str] = {
    "account_identity": "Broker account consistency is not available.",
    "action_plan_consumption": "Action-plan consumption is recorded.",
    "broker_activity_state": "Broker-activity publisher state is unknown.",
    "broker_connection": "Broker connection is unavailable.",
    "broker_safety": "Broker safety verdict is unavailable.",
    "capability_disabled": "{label} has a backend-authored disabled reason.",
    "capability_enabled": "{label} eligibility is recorded.",
    "configuration_reason": "A backend configuration check needs attention.",
    "desired_path_status": "Desired-state sidecar status is unknown.",
    "execution_posture": "Execution posture is unavailable.",
    "instrument_surface": "Instrument surface is recorded.",
    "readiness_gate_status": "{name} is not proven yet.",
    "reconciliation_next_step": "Review reconciliation before acting.",
    "reconciliation_state": "Reconciliation state is unknown.",
    "risk_posture": "Current exposure is unavailable.",
}

_DEFAULT_RECEIPT_COPY: Mapping[str, ReceiptCopy] = {
    "account_clerk.generation": ("Account Clerk generation is {value}.", None),
    "account_clerk.phase": ("Account Clerk phase is {value}.", None),
    "account_clerk.lease_active": ("Account Clerk lease is {value}.", None),
    "account_owner.generation": ("AccountOwner generation is {value}.", None),
    "account_owner.phase": ("AccountOwner phase is {value}.", None),
    "adopted_intent_count": ("Reconciliation adopted {value} intent(s).", None),
    "broker_activity.latest_row_seq": ("Latest broker-activity row sequence is {value}.", None),
    "broker_observed_at_ms": (
        "Broker-observed time is recorded.",
        "The timestamp is stored as int64 ms UTC.",
    ),
    "current_risk.pending_order_count": (
        "{value} pending order(s) are currently attributed to this bot.",
        "Zero means known empty; unavailable would be shown separately.",
    ),
    "daily_order_cap.limit": (
        "The daily order cap is {value} order(s).",
        "The cap is read from engine readiness.",
    ),
    "daily_order_cap.used": (
        "{value} order(s) have been used today.",
        "Daily cap usage is counted by the backend.",
    ),
    "drop_reason": (
        "The intent was dropped before broker submission.",
        "The raw drop reason is kept in the audit payload.",
    ),
    "event_type": (
        "A lifecycle event reached this node.",
        "The event type is preserved in the audit payload.",
    ),
    "intent_id": (
        "Order intent {value} was recorded.",
        "Intent ids are opaque audit tokens and are preserved exactly.",
    ),
    "last_reconcile_ms": (
        "Last reconcile time is recorded.",
        "The timestamp is stored as int64 ms UTC.",
    ),
    "monitor.orders_today": (
        "{value} order(s) are counted against today's cap.",
        "This is the backend daily-order-cap usage, not a full activity history.",
    ),
    "order_id": ("Broker order id {value} was recorded.", None),
    "order_ref": (
        "Order reference {value} was recorded.",
        "Order refs are opaque audit tokens and are preserved exactly.",
    ),
    "perm_id": ("Broker permanent id {value} was recorded.", None),
    "reconciliation.state": (
        "{reconciliation_state_headline}",
        "This is the backend cold-start reconciliation projection.",
    ),
    "sidecar_wal_seq": ("Reconciliation read sidecar WAL sequence {value}.", None),
    "source_seq": (
        "Source sequence {value} authored this event.",
        "Sequence numbers preserve event ordering.",
    ),
    "ts_ms_source": (
        "The event timestamp source is recorded.",
        "Timestamp provenance is preserved in the audit payload.",
    ),
}

_VALUE_RECEIPT_COPY: Mapping[tuple[str, str], ReceiptCopy] = {
    ("broker_activity.latest_row_seq", "not_available"): (
        "No broker-activity row sequence is available yet.",
        None,
    ),
}

_RECONCILIATION_NEXT_STEP_VALUES: Mapping[str, str] = {
    "FAILED": "run_reconciliation_before_resume",
    "STALE": "refresh_reconciliation",
    "NOT_AVAILABLE": "wait_for_reconciliation_receipt",
    "IN_PROGRESS": "wait_for_reconciliation",
}


def headline(domain: str, code: object, **values: object) -> str:
    """Return backend-authored operator copy for a closed domain/code pair."""

    key = str(code)
    template = _HEADLINES.get(domain, {}).get(key, _HEADLINE_DEFAULTS.get(domain, "{code} is recorded."))
    return template.format(code=key, **values)


def default_receipt_copy(label: str, value: str, unit: str | None) -> ReceiptCopy:
    """Return default receipt copy for labels that do not pass explicit prose."""

    template = _VALUE_RECEIPT_COPY.get((label, value), _DEFAULT_RECEIPT_COPY.get(label))
    if template is not None:
        return _format_receipt_copy(template, label=label, value=value, unit=unit)
    if label.startswith("watchdog."):
        return "Watchdog incident evidence is recorded.", "The raw watchdog value is preserved in the audit payload."
    if label.startswith("live_config."):
        return "Committed live configuration evidence is recorded.", None
    return f"{_human_label(label)} is recorded by the backend.", "The raw audit value is preserved below."


def desired_state_detail(path_status: str, updated_at_ms: int | None) -> str:
    if path_status == "absent":
        return "No sidecar exists, so the documented effective default is RUNNING."
    if updated_at_ms is None:
        return "No update timestamp is available for this desired-state sidecar."
    return "Desired-state timestamp is stored as int64 ms UTC."


def reconciliation_next_step_value(state: str) -> str:
    return _RECONCILIATION_NEXT_STEP_VALUES.get(state, "review_reconciliation")


def _format_receipt_copy(template: ReceiptCopy, *, label: str, value: str, unit: str | None) -> ReceiptCopy:
    headline_template, detail_template = template
    format_values = {
        "label": label,
        "reconciliation_state_headline": headline("reconciliation_state", value),
        "unit": unit or "",
        "value": value,
    }
    headline_text = headline_template.format(**format_values)
    detail_text = detail_template.format(**format_values) if detail_template is not None else None
    return headline_text, detail_text


def _human_label(label: str) -> str:
    return label.replace("_", " ").replace(".", " ").strip().capitalize()


__all__ = [
    "default_receipt_copy",
    "desired_state_detail",
    "headline",
    "reconciliation_next_step_value",
]
