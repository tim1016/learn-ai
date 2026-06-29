"""Read-side lifecycle projection over existing bot/account evidence.

This module intentionally does not write a new event log. It normalizes the
existing durable sources (Intent WAL, Account events, Activity projection rows)
into one typed row shape so chart details and timelines can share ordering,
labels, and evidence references without inventing a parallel truth source.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from app.engine.live.intent_events import IntentEvent, IntentEventType
from app.schemas.live_runs import (
    AccountEventProjection,
    BotLifecycleEvent,
    LifecycleChartStatus,
    LifecycleEventCategory,
    LifecycleEventSeverity,
    LifecycleEvidenceRef,
)

INT64_MAX = 9_223_372_036_854_775_807

SOURCE_RANKS: Mapping[str, int] = {
    "decision": 10,
    "risk_gate": 20,
    "intent_pending": 30,
    "submit": 40,
    "broker_ack": 50,
    "fill": 60,
    "position_change": 70,
    "account_balance": 80,
    "freeze_halt_poison": 85,
    "lifecycle_transition": 90,
    "account_event": 100,
    "evidence": 110,
}

ACCOUNT_EVENT_TS_FIELDS: tuple[str, ...] = (
    "ts_ms",
    "recorded_at_ms",
    "created_at_ms",
    "approved_at_ms",
    "cleared_at_ms",
    "updated_at_ms",
    "decided_at_ms",
    "completed_at_ms",
    "started_at_ms",
)

STATUS_LABELS: Mapping[LifecycleChartStatus, str] = {
    "passed": "Clear",
    "active": "Here now",
    "blocked": "Blocked",
    "poison": "Poisoned",
    "freeze": "Frozen",
    "inactive": "Waiting",
    "unknown": "Unknown",
}


def lifecycle_status_label(status: LifecycleChartStatus) -> str:
    """Return the server-authored label for a lifecycle chart status."""

    return STATUS_LABELS[status]


def sort_lifecycle_events(events: Iterable[BotLifecycleEvent]) -> list[BotLifecycleEvent]:
    """Sort by the v3 deterministic timeline tuple.

    Missing legacy timestamps sort after timestamped rows; within that
    unresolved tail, source rank and source-local seq keep ordering stable.
    """

    return sorted(
        events,
        key=lambda event: (
            event.ts_ms if event.ts_ms is not None else INT64_MAX,
            event.source_rank,
            event.source_local_seq,
        ),
    )


def project_intent_events(
    events: Sequence[IntentEvent],
    *,
    bot_id: str | None = None,
    account_id: str | None = None,
    run_id: str | None = None,
    wal_path: Path | None = None,
) -> list[BotLifecycleEvent]:
    """Project Intent WAL rows into lifecycle timeline events."""

    projected: list[BotLifecycleEvent] = []
    for event in events:
        mapped = _intent_event_mapping(event)
        if mapped is None:
            continue
        event_type, node_id, status, source, summary, operator_next_step = mapped
        ts_ms = event.appended_at_ms if event.appended_at_ms is not None else event.ts_ms
        ts_ms_source = (
            "appended_at_ms"
            if event.appended_at_ms is not None
            else ("ts_ms" if event.ts_ms is not None else None)
        )
        why = _intent_why(event, fallback=summary)
        projected.append(
            BotLifecycleEvent(
                event_id=f"intent_wal:{run_id or 'unknown'}:{event.seq}:{event.event_type.value}",
                bot_id=bot_id,
                account_id=account_id,
                event_type=event_type,
                category="order",
                node_id=node_id,
                status=status,
                status_label=lifecycle_status_label(status),
                severity=_severity_for_status(status),
                ts_ms=ts_ms,
                ts_ms_resolved=ts_ms is not None,
                source=source,
                source_rank=SOURCE_RANKS[source],
                source_local_seq=event.seq,
                summary=summary,
                why=why,
                operator_next_step=operator_next_step,
                evidence_refs=[
                    LifecycleEvidenceRef(
                        source="intent_wal",
                        source_label="Intent WAL",
                        source_local_seq=event.seq,
                        path=str(wal_path) if wal_path is not None else None,
                        row_id=event.intent_id,
                        summary=event.event_type.value,
                    )
                ],
                payload={
                    "intent_id": event.intent_id,
                    "order_ref": event.order_ref,
                    "intent_kind": event.intent_kind.value,
                    "reason": event.reason,
                    "drop_reason": event.drop_reason,
                    "ts_ms_source": ts_ms_source,
                },
            )
        )
    return sort_lifecycle_events(projected)


def project_account_events(
    rows: Sequence[Mapping[str, Any]],
    *,
    account_id: str,
) -> list[AccountEventProjection]:
    """Return tolerant typed projections for raw account-event rows."""

    return [
        normalize_account_event(row, account_id=account_id, file_position=index)
        for index, row in enumerate(rows, start=1)
    ]


def normalize_account_event(
    row: Mapping[str, Any],
    *,
    account_id: str,
    file_position: int,
) -> AccountEventProjection:
    """Normalize one raw account-event dict without backfilling history."""

    ts_ms, ts_field = _resolve_account_event_ts_ms(row)
    event_type = str(row.get("event_type") or "account_event")
    row_account_id = row.get("account_id")
    resolved_account_id = row_account_id if isinstance(row_account_id, str) and row_account_id else account_id
    reason = _string_or_none(row.get("reason")) or _string_or_none(row.get("operator_reason"))
    summary = _account_event_summary(event_type, row)
    return AccountEventProjection(
        account_id=resolved_account_id,
        event_type=event_type,
        seq=_positive_int_or_none(row.get("seq")),
        file_position=file_position,
        ts_ms=ts_ms,
        ts_ms_resolved=ts_ms is not None,
        ts_ms_source=ts_field,
        summary=summary,
        why=reason,
        payload=dict(row),
    )


def account_event_to_lifecycle_event(event: AccountEventProjection) -> BotLifecycleEvent:
    """Lift a typed account-event projection into the common lifecycle row."""

    status = _status_for_account_event(event)
    source = _source_for_account_event(event)
    return BotLifecycleEvent(
        event_id=f"account_event:{event.account_id}:{event.seq or event.file_position}:{event.event_type}",
        account_id=event.account_id,
        event_type=event.event_type,
        category=_category_for_account_event(event),
        node_id=_node_for_account_event(event),
        status=status,
        status_label=lifecycle_status_label(status),
        severity=_severity_for_status(status),
        ts_ms=event.ts_ms,
        ts_ms_resolved=event.ts_ms_resolved,
        source=source,
        source_rank=SOURCE_RANKS[source],
        source_local_seq=event.seq or event.file_position,
        summary=event.summary,
        why=event.why,
        operator_next_step=_string_or_none(event.payload.get("operator_next_step")),
        evidence_refs=[
            LifecycleEvidenceRef(
                source="account_events",
                source_label="Account events",
                source_local_seq=event.seq or event.file_position,
                row_id=event.event_type,
                summary=event.summary,
            )
        ],
        payload={
            "ts_ms_source": event.ts_ms_source,
            "ts_ms_resolved": event.ts_ms_resolved,
            **event.payload,
        },
    )


def latest_event_for_node(
    events: Iterable[BotLifecycleEvent],
    node_id: str,
) -> BotLifecycleEvent | None:
    """Return the latest projected event for a chart node."""

    matching = [event for event in events if event.node_id == node_id]
    if not matching:
        return None
    return sort_lifecycle_events(matching)[-1]


def _intent_event_mapping(
    event: IntentEvent,
) -> tuple[str, str, LifecycleChartStatus, str, str, str | None] | None:
    if event.event_type is IntentEventType.PENDING_INTENT:
        return (
            "BrokerOrderRequested",
            "intent_wal",
            "active",
            "intent_pending",
            "Order intent persisted before broker submission.",
            "WAIT_FOR_BROKER_ACK",
        )
    if event.event_type is IntentEventType.SIZING_RESOLVED:
        return (
            "RiskCheckPassed",
            "submit_order",
            "passed",
            "risk_gate",
            "Order sizing resolved before submit.",
            "GATE_PASSING",
        )
    if event.event_type in {
        IntentEventType.SUBMITTED,
        IntentEventType.SUBMITTED_RECOVERED,
        IntentEventType.ADOPTED_BROKER_ORDER,
    }:
        return (
            "BrokerOrderPlaced",
            "place_order",
            "passed",
            "submit",
            "Order reached the broker submit boundary.",
            "WATCH_BROKER_ACK",
        )
    if event.event_type is IntentEventType.INTENT_NOT_ACCEPTED:
        return (
            "BrokerOrderRejected",
            "ack_or_reconcile",
            "blocked",
            "broker_ack",
            "Broker probe proved the intent was not accepted.",
            "RETRY_OR_RECONCILE",
        )
    if event.event_type is IntentEventType.ACK_FAILED_UNCERTAIN:
        return (
            "BrokerOrderUncertain",
            "ack_or_reconcile",
            "blocked",
            "broker_ack",
            "Broker acknowledgement failed; submit outcome is uncertain.",
            "PROBE_BROKER_BEFORE_RETRY",
        )
    if event.event_type is IntentEventType.SUBMIT_UNCERTAIN_HALTED:
        return (
            "BrokerOrderUncertain",
            "ack_or_reconcile",
            "blocked",
            "broker_ack",
            "Submit outcome could not be proven; the run halted.",
            "RECOVER_ACCOUNT_BEFORE_RESTART",
        )
    if event.event_type is IntentEventType.INTENT_DROPPED_BEFORE_SUBMIT:
        return (
            "BotBlocked",
            "submit_order",
            "blocked",
            "submit",
            "Order intent was dropped before broker submission.",
            "CLEAR_SUBMISSION_GATE",
        )
    return None


def _intent_why(event: IntentEvent, *, fallback: str) -> str:
    if event.drop_reason is not None:
        return f"Submission gate dropped the intent: {event.drop_reason}."
    if event.reason:
        return event.reason
    return fallback


def _resolve_account_event_ts_ms(row: Mapping[str, Any]) -> tuple[int | None, str | None]:
    for field in ACCOUNT_EVENT_TS_FIELDS:
        ts_ms = _non_negative_int_or_none(row.get(field))
        if ts_ms is not None:
            return ts_ms, field
    return None, None


def _positive_int_or_none(value: object) -> int | None:
    parsed = _non_negative_int_or_none(value)
    if parsed is None or parsed < 1:
        return None
    return parsed


def _non_negative_int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _account_event_summary(event_type: str, row: Mapping[str, Any]) -> str:
    if event_type == "account_freeze_recorded":
        return "Account freeze recorded."
    if event_type == "account_freeze_cleared":
        return "Account freeze cleared."
    if event_type.startswith("account_owner_submit_"):
        return event_type.replace("_", " ").capitalize() + "."
    if event_type == "account_instance_binding_recorded":
        state = row.get("lifecycle_state")
        return f"Account instance binding recorded ({state})." if state else "Account instance binding recorded."
    return event_type.replace("_", " ").capitalize() + "."


def _source_for_account_event(event: AccountEventProjection) -> str:
    event_type = event.event_type
    if event_type in {"account_freeze_recorded", "account_freeze_cleared"}:
        return "freeze_halt_poison"
    if event_type.startswith("account_owner_submit_"):
        return "submit" if event_type.endswith("prepared") else "broker_ack"
    if event_type == "account_owner_generation_recorded":
        return "lifecycle_transition"
    return "account_event"


def _category_for_account_event(event: AccountEventProjection) -> LifecycleEventCategory:
    if event.event_type in {"account_freeze_recorded", "account_freeze_cleared"}:
        return "freeze"
    if event.event_type.startswith("account_owner_submit_"):
        return "order"
    if "poison" in event.event_type:
        return "poison"
    if "halt" in event.event_type:
        return "halt"
    return "account_event"


def _node_for_account_event(event: AccountEventProjection) -> str:
    if event.event_type in {"account_freeze_recorded", "account_freeze_cleared"}:
        return "account_safety"
    if event.event_type.startswith("account_owner_submit_"):
        return "broker_writer"
    return "recovery"


def _status_for_account_event(event: AccountEventProjection) -> LifecycleChartStatus:
    if event.event_type == "account_freeze_recorded":
        return "freeze"
    if event.event_type == "account_freeze_cleared":
        return "passed"
    if event.event_type.endswith("_rejected") or event.event_type.endswith("_uncertain"):
        return "blocked"
    return "active"


def _severity_for_status(status: LifecycleChartStatus) -> LifecycleEventSeverity:
    if status in {"poison", "freeze"}:
        return "critical"
    if status in {"blocked", "unknown"}:
        return "warning"
    return "info"
