"""Read-side lifecycle projection over existing bot/account evidence.

This module intentionally does not write a new event log. It normalizes the
existing durable sources (Intent WAL, Account events, Activity projection rows)
into one typed row shape so chart details and timelines can share ordering,
labels, and evidence references without inventing a parallel truth source.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

from app.engine.live.account_artifacts import resolve_account_event_ts_ms
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

STATUS_LABELS: Mapping[LifecycleChartStatus, str] = {
    "passed": "Clear",
    "active": "Here now",
    "blocked": "Blocked",
    "poison": "Poisoned",
    "freeze": "Frozen",
    "inactive": "Waiting",
    "unknown": "Unknown",
}


class IntentEventMapping(NamedTuple):
    event_type: str
    node_id: str
    status: LifecycleChartStatus
    source: str
    summary: str
    operator_next_step: str | None
    template_id: str


class AccountEventLifecycleMapping(NamedTuple):
    category: LifecycleEventCategory
    node_id: str
    source: str
    status: LifecycleChartStatus
    template_id: str


UNKNOWN_ACCOUNT_EVENT_MAPPING = AccountEventLifecycleMapping(
    category="account_event",
    node_id="recovery",
    source="account_event",
    status="unknown",
    template_id="lifecycle_projection.account_event.unknown.v1",
)

ACCOUNT_EVENT_MAPPINGS: Mapping[str, AccountEventLifecycleMapping] = {
    "account_freeze_recorded": AccountEventLifecycleMapping(
        "freeze", "account_safety", "freeze_halt_poison", "freeze", "lifecycle_projection.account_event.account_freeze_recorded.v1"
    ),
    "account_freeze_cleared": AccountEventLifecycleMapping(
        "freeze", "account_safety", "freeze_halt_poison", "passed", "lifecycle_projection.account_event.account_freeze_cleared.v1"
    ),
    "account_recovery_proof_recorded": AccountEventLifecycleMapping(
        "evidence", "recovery", "evidence", "passed", "lifecycle_projection.account_event.account_recovery_proof_recorded.v1"
    ),
    "account_audited_override_recorded": AccountEventLifecycleMapping(
        "decision", "recovery", "decision", "passed", "lifecycle_projection.account_event.account_audited_override_recorded.v1"
    ),
    "account_owner_generation_recorded": AccountEventLifecycleMapping(
        "lifecycle_transition",
        "writer_guard",
        "lifecycle_transition",
        "active",
        "lifecycle_projection.account_event.account_owner_generation_recorded.v1",
    ),
    "account_instance_binding_recorded": AccountEventLifecycleMapping(
        "lifecycle_transition",
        "account_safety",
        "lifecycle_transition",
        "active",
        "lifecycle_projection.account_event.account_instance_binding_recorded.v1",
    ),
    "account_restart_intensity_breached": AccountEventLifecycleMapping(
        "freeze",
        "account_safety",
        "freeze_halt_poison",
        "freeze",
        "lifecycle_projection.account_event.account_restart_intensity_breached.v1",
    ),
    "account_owner_submit_prepared": AccountEventLifecycleMapping(
        "order", "intent_wal", "intent_pending", "active", "lifecycle_projection.account_event.account_owner_submit_prepared.v1"
    ),
    "account_owner_submit_accepted": AccountEventLifecycleMapping(
        "order", "place_order", "submit", "passed", "lifecycle_projection.account_event.account_owner_submit_accepted.v1"
    ),
    "account_owner_submit_uncertain": AccountEventLifecycleMapping(
        "order",
        "ack_or_reconcile",
        "broker_ack",
        "blocked",
        "lifecycle_projection.account_event.account_owner_submit_uncertain.v1",
    ),
    "account_owner_submit_rejected": AccountEventLifecycleMapping(
        "order",
        "ack_or_reconcile",
        "broker_ack",
        "blocked",
        "lifecycle_projection.account_event.account_owner_submit_rejected.v1",
    ),
    "account_owner_client_id_in_use": AccountEventLifecycleMapping(
        "halt", "broker_writer", "broker_ack", "blocked", "lifecycle_projection.account_event.account_owner_client_id_in_use.v1"
    ),
    "account_owner_reconnect_frozen": AccountEventLifecycleMapping(
        "freeze",
        "recovery",
        "freeze_halt_poison",
        "freeze",
        "lifecycle_projection.account_event.account_owner_reconnect_frozen.v1",
    ),
    "account_owner_reconnect_resumed": AccountEventLifecycleMapping(
        "lifecycle_transition",
        "writer_guard",
        "lifecycle_transition",
        "passed",
        "lifecycle_projection.account_event.account_owner_reconnect_resumed.v1",
    ),
    "account_owner_reconnect_blocked": AccountEventLifecycleMapping(
        "freeze",
        "recovery",
        "freeze_halt_poison",
        "freeze",
        "lifecycle_projection.account_event.account_owner_reconnect_blocked.v1",
    ),
    "account_owner_reconnect_drain_accepted": AccountEventLifecycleMapping(
        "lifecycle_transition",
        "recovery",
        "lifecycle_transition",
        "passed",
        "lifecycle_projection.account_event.account_owner_reconnect_drain_accepted.v1",
    ),
    "account_owner_reconnect_drain_uncertain": AccountEventLifecycleMapping(
        "order",
        "recovery",
        "broker_ack",
        "blocked",
        "lifecycle_projection.account_event.account_owner_reconnect_drain_uncertain.v1",
    ),
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
    since_ms: int | None = None,
    live_state_last_intent_wal_seq: int | None = None,
) -> list[BotLifecycleEvent]:
    """Project Intent WAL rows into lifecycle timeline events."""

    projected: list[BotLifecycleEvent] = []
    stale_by_node: dict[str, list[tuple[IntentEvent, IntentEventMapping, int | None, str | None]]] = {}
    for event in events:
        mapped = _intent_event_mapping(event)
        if mapped is None:
            continue
        ts_ms = event.appended_at_ms if event.appended_at_ms is not None else event.ts_ms
        ts_ms_source = (
            "appended_at_ms" if event.appended_at_ms is not None else ("ts_ms" if event.ts_ms is not None else None)
        )
        if _is_before_projection_window(ts_ms, since_ms):
            stale_by_node.setdefault(mapped.node_id, []).append((event, mapped, ts_ms, ts_ms_source))
            continue
        why = _intent_why(event, fallback=mapped.summary)
        projected.append(
            BotLifecycleEvent(
                event_id=f"intent_wal:{run_id or 'unknown'}:{event.seq}:{event.event_type.value}",
                bot_id=bot_id,
                run_id=run_id,
                account_id=account_id,
                event_type=mapped.event_type,
                category="order",
                node_id=mapped.node_id,
                status=mapped.status,
                status_label=lifecycle_status_label(mapped.status),
                severity=_severity_for_status(mapped.status),
                ts_ms=ts_ms,
                ts_ms_resolved=ts_ms is not None,
                source=mapped.source,
                source_rank=SOURCE_RANKS[mapped.source],
                source_local_seq=event.seq,
                summary=mapped.summary,
                why=why,
                operator_next_step=mapped.operator_next_step,
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
                rendered_template_id=mapped.template_id,
            )
        )
    current_nodes = {event.node_id for event in projected}
    for node_id, stale_events in stale_by_node.items():
        if node_id in current_nodes:
            continue
        projected.append(
            _stale_intent_wal_event(
                stale_events,
                node_id=node_id,
                bot_id=bot_id,
                account_id=account_id,
                run_id=run_id,
                wal_path=wal_path,
                since_ms=since_ms,
                live_state_last_intent_wal_seq=live_state_last_intent_wal_seq,
            )
        )
    return sort_lifecycle_events(projected)


def project_account_events(
    rows: Sequence[Mapping[str, Any]],
    *,
    account_id: str,
    start_file_position: int = 1,
) -> list[AccountEventProjection]:
    """Return tolerant typed projections for raw account-event rows."""

    return [
        normalize_account_event(row, account_id=account_id, file_position=index)
        for index, row in enumerate(rows, start=start_file_position)
    ]


def normalize_account_event(
    row: Mapping[str, Any],
    *,
    account_id: str,
    file_position: int,
) -> AccountEventProjection:
    """Normalize one raw account-event dict without backfilling history."""

    ts_ms, ts_field = resolve_account_event_ts_ms(row)
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

    mapping = _account_event_mapping(event.event_type)
    bot_id = _account_event_bot_id(event)
    run_id = _account_event_run_id(event)
    return BotLifecycleEvent(
        event_id=f"account_event:{event.account_id}:{event.seq or event.file_position}:{event.event_type}",
        bot_id=bot_id,
        run_id=run_id,
        account_id=event.account_id,
        event_type=event.event_type,
        category=mapping.category,
        node_id=mapping.node_id,
        status=mapping.status,
        status_label=lifecycle_status_label(mapping.status),
        severity=_severity_for_status(mapping.status),
        ts_ms=event.ts_ms,
        ts_ms_resolved=event.ts_ms_resolved,
        source=mapping.source,
        source_rank=SOURCE_RANKS[mapping.source],
        source_local_seq=event.seq or event.file_position,
        summary=event.summary,
        why=event.why,
        operator_next_step=_operator_next_step_for_account_event(event),
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
        rendered_template_id=mapping.template_id,
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


def _is_before_projection_window(ts_ms: int | None, since_ms: int | None) -> bool:
    if since_ms is None:
        return False
    return ts_ms is None or ts_ms < since_ms


def _stale_intent_wal_event(
    stale_events: Sequence[tuple[IntentEvent, IntentEventMapping, int | None, str | None]],
    *,
    node_id: str,
    bot_id: str | None,
    account_id: str | None,
    run_id: str | None,
    wal_path: Path | None,
    since_ms: int | None,
    live_state_last_intent_wal_seq: int | None,
) -> BotLifecycleEvent:
    newest = max(stale_events, key=lambda item: item[0].seq)
    newest_event = newest[0]
    max_seq = newest_event.seq
    stale_ts_values = [ts_ms for _event, _mapped, ts_ms, _source in stale_events if ts_ms is not None]
    stale_latest_ts_ms = max(stale_ts_values) if stale_ts_values else None
    cursor_text = (
        f" Live-state cursor last_intent_wal_seq={live_state_last_intent_wal_seq}."
        if live_state_last_intent_wal_seq is not None
        else ""
    )
    why = (
        f"Intent WAL rows through seq {max_seq} are outside the current live session window "
        f"(started_at_ms={since_ms}). Ignoring them instead of rendering stale submit evidence as current."
        f"{cursor_text}"
    )
    return BotLifecycleEvent(
        event_id=f"intent_wal_stale:{run_id or 'unknown'}:{node_id}:{max_seq}",
        bot_id=bot_id,
        run_id=run_id,
        account_id=account_id,
        event_type="IntentWalEvidenceStale",
        category="evidence",
        node_id=node_id,
        status="unknown",
        status_label=lifecycle_status_label("unknown"),
        severity="warning",
        ts_ms=since_ms,
        ts_ms_resolved=since_ms is not None,
        source="evidence",
        source_rank=SOURCE_RANKS["evidence"],
        source_local_seq=max_seq,
        summary="Intent WAL evidence is stale for the current live session.",
        why=why,
        operator_next_step="WAIT_FOR_CURRENT_SESSION_INTENT_EVIDENCE",
        evidence_refs=[
            LifecycleEvidenceRef(
                source="intent_wal",
                source_label="Intent WAL",
                source_local_seq=max_seq,
                path=str(wal_path) if wal_path is not None else None,
                row_id=newest_event.intent_id,
                summary="stale evidence outside current live session",
            )
        ],
        payload={
            "stale_intent_wal_count": len(stale_events),
            "stale_intent_wal_max_seq": max_seq,
            "stale_intent_wal_latest_ts_ms": stale_latest_ts_ms,
            "projection_since_ms": since_ms,
            "live_state_last_intent_wal_seq": live_state_last_intent_wal_seq,
        },
        rendered_template_id="lifecycle_projection.intent_wal.stale_evidence.v1",
    )


def _intent_event_mapping(
    event: IntentEvent,
) -> IntentEventMapping | None:
    if event.event_type is IntentEventType.PENDING_INTENT:
        return IntentEventMapping(
                event_type="BrokerOrderRequested",
                node_id="intent_wal",
                status="active",
                source="intent_pending",
                summary="Order intent persisted before broker submission.",
                operator_next_step="WAIT_FOR_BROKER_ACK",
                template_id="lifecycle_projection.intent_wal.pending_intent.v1",
            )
    if event.event_type is IntentEventType.SIZING_RESOLVED:
        return IntentEventMapping(
                event_type="RiskCheckPassed",
                node_id="submit_order",
                status="passed",
                source="risk_gate",
                summary="Order sizing resolved before submit.",
                operator_next_step="GATE_PASSING",
                template_id="lifecycle_projection.intent_wal.sizing_resolved.v1",
            )
    if event.event_type in {
        IntentEventType.SUBMITTED,
        IntentEventType.SUBMITTED_RECOVERED,
        IntentEventType.ADOPTED_BROKER_ORDER,
    }:
        return IntentEventMapping(
                event_type="BrokerOrderPlaced",
                node_id="place_order",
                status="passed",
                source="submit",
                summary="Order reached the broker submit boundary.",
                operator_next_step="WATCH_BROKER_ACK",
                template_id=f"lifecycle_projection.intent_wal.{event.event_type.value.lower()}.v1",
            )
    if event.event_type is IntentEventType.INTENT_NOT_ACCEPTED:
        return IntentEventMapping(
                event_type="BrokerOrderRejected",
                node_id="ack_or_reconcile",
                status="blocked",
                source="broker_ack",
                summary="Broker probe proved the intent was not accepted.",
                operator_next_step="RETRY_OR_RECONCILE",
                template_id="lifecycle_projection.intent_wal.intent_not_accepted.v1",
            )
    if event.event_type is IntentEventType.ACK_FAILED_UNCERTAIN:
        return IntentEventMapping(
                event_type="BrokerOrderUncertain",
                node_id="ack_or_reconcile",
                status="blocked",
                source="broker_ack",
                summary="Broker acknowledgment failed; submit outcome is uncertain.",
                operator_next_step="PROBE_BROKER_BEFORE_RETRY",
                template_id="lifecycle_projection.intent_wal.ack_failed_uncertain.v1",
            )
    if event.event_type is IntentEventType.SUBMIT_UNCERTAIN_HALTED:
        return IntentEventMapping(
                event_type="BrokerOrderUncertain",
                node_id="ack_or_reconcile",
                status="blocked",
                source="broker_ack",
                summary="Submit outcome could not be proven; the run halted.",
                operator_next_step="RECOVER_ACCOUNT_BEFORE_RESTART",
                template_id="lifecycle_projection.intent_wal.submit_uncertain_halted.v1",
            )
    if event.event_type is IntentEventType.INTENT_DROPPED_BEFORE_SUBMIT:
        return IntentEventMapping(
                event_type="BotBlocked",
                node_id="submit_order",
                status="blocked",
                source="submit",
                summary="Order intent was dropped before broker submission.",
                operator_next_step="CLEAR_SUBMISSION_GATE",
                template_id="lifecycle_projection.intent_wal.intent_dropped_before_submit.v1",
            )
    return None


def _intent_why(event: IntentEvent, *, fallback: str) -> str:
    if event.drop_reason is not None:
        return f"Submission gate dropped the intent: {event.drop_reason}."
    if event.reason:
        return event.reason
    return fallback


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
    if event_type == "account_owner_submit_prepared":
        return "AccountOwner intent persisted before broker submission."
    if event_type == "account_owner_submit_accepted":
        return "AccountOwner order reached the broker submit boundary."
    if event_type == "account_owner_submit_uncertain":
        return "AccountOwner submit outcome is uncertain."
    if event_type == "account_owner_submit_rejected":
        return "AccountOwner rejected the submit before broker placement."
    if event_type == "account_owner_generation_recorded":
        generation = row.get("generation")
        phase = row.get("phase")
        if generation is not None and phase:
            return (
                f"AccountOwner generation {generation} recorded ({phase}); this is R2 generation "
                "evidence, not R3 daemon/IPC writer authority."
            )
        return "AccountOwner generation recorded; this is R2 evidence, not R3 daemon/IPC writer authority."
    if event_type == "account_instance_binding_recorded":
        state = row.get("lifecycle_state")
        return f"Account instance binding recorded ({state})." if state else "Account instance binding recorded."
    return event_type.replace("_", " ").capitalize() + "."


def _account_event_mapping(event_type: str) -> AccountEventLifecycleMapping:
    return ACCOUNT_EVENT_MAPPINGS.get(event_type, UNKNOWN_ACCOUNT_EVENT_MAPPING)


def _operator_next_step_for_account_event(event: AccountEventProjection) -> str | None:
    direct = _string_or_none(event.payload.get("operator_next_step"))
    if direct is not None:
        return direct
    for field in ("final_gate_result", "gate_result"):
        value = event.payload.get(field)
        if isinstance(value, Mapping):
            next_step = _string_or_none(value.get("operator_next_step"))
            if next_step is not None:
                return next_step
    return None


def _account_event_bot_id(event: AccountEventProjection) -> str | None:
    value = _string_or_none(event.payload.get("strategy_instance_id"))
    if value is not None:
        return value
    diagnostics = event.payload.get("diagnostics")
    if isinstance(diagnostics, Mapping):
        return _string_or_none(diagnostics.get("strategy_instance_id"))
    return None


def _account_event_run_id(event: AccountEventProjection) -> str | None:
    value = _string_or_none(event.payload.get("run_id"))
    if value is not None:
        return value
    diagnostics = event.payload.get("diagnostics")
    if isinstance(diagnostics, Mapping):
        return _string_or_none(diagnostics.get("run_id"))
    return None


def _severity_for_status(status: LifecycleChartStatus) -> LifecycleEventSeverity:
    if status in {"poison", "freeze"}:
        return "critical"
    if status in {"blocked", "unknown"}:
        return "warning"
    return "info"
