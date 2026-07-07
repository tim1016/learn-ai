"""Legacy broker-activity to BotEventRow replacement map.

Slice -1 of PRD #928 defines the contract only. This pure helper proves how
existing ``BrokerActivityRow`` records map into the Bot event stream tail
without wiring the stream into production yet.
"""

from __future__ import annotations

from app.schemas.bot_events import (
    BotEventIdentity,
    BotEventRow,
    BotEventSeverity,
    BotEventType,
    SourceAuthority,
    TerminalError,
    TerminalErrorCode,
    TerminalErrorSource,
)
from app.schemas.broker_activity import BrokerActivityRow, ReasonCode, Verdict
from app.services.broker_activity_reconciler import parse_order_ref


def broker_activity_row_to_bot_event_row(row: BrokerActivityRow) -> BotEventRow:
    """Map one ADR-0014 broker row into the ADR-0024 stream tail."""

    event_type = _event_type_for(row)
    terminal_error = _terminal_error_for(row, event_type)

    return BotEventRow(
        seq=row.seq,
        ts_ms=row.ts_ms,
        event_type=event_type,
        source_authority=_source_authority_for(row),
        identity=_identity_for(row),
        severity=_severity_for(row, event_type),
        headline=row.headline,
        narrative=row.narrative,
        terminal_error=terminal_error,
        facts=_facts_for(row),
    )


def _identity_for(row: BrokerActivityRow) -> BotEventIdentity:
    parsed = parse_order_ref(row.order_ref)
    parsed_intent_id = parsed[1] if parsed is not None else None
    overlay_intent_id = row.engine_overlay.intent_id if row.engine_overlay is not None else None
    if not any((overlay_intent_id, parsed_intent_id, row.order_ref, row.perm_id is not None, row.exec_id)):
        return BotEventIdentity(evaluation_id=f"legacy-broker-activity-seq:{row.seq}")
    return BotEventIdentity(
        intent_id=overlay_intent_id or parsed_intent_id,
        order_ref=row.order_ref,
        order_id=None,
        perm_id=row.perm_id,
        exec_id=row.exec_id,
    )


def _source_authority_for(row: BrokerActivityRow) -> SourceAuthority:
    if row.verdict is Verdict.ENGINE_ONLY_PENDING:
        return SourceAuthority.ENGINE_LOOP
    return SourceAuthority.BROKER_SESSION


def _facts_for(row: BrokerActivityRow) -> dict[str, object]:
    facts: dict[str, object] = {
        "legacy_schema": "BrokerActivityRow",
        "legacy_verdict": row.verdict.value,
        "legacy_reason_codes": [reason.value for reason in row.reason_codes],
        "legacy_template_key": row.template_key,
        "legacy_template_version": row.template_version,
        "broker": {
            "exec_id": row.exec_id,
            "perm_id": row.perm_id,
            "order_ref": row.order_ref,
            "symbol": row.symbol,
            "side": row.side,
            "quantity": row.quantity,
            "price": row.price,
            "commission": row.commission,
            "net_amount": row.net_amount,
            "order_type": row.order_type,
            "exec_ts_ms": row.exec_ts_ms,
        },
        "migration": {
            "source_run_id": row.source_run_id,
            "source_seq": row.source_seq,
            "recovery_provenance": row.recovery_provenance,
            "recovery_reason": row.recovery_reason,
        },
    }
    if row.engine_overlay is not None:
        facts["engine_overlay"] = row.engine_overlay.model_dump(mode="json")
    if row.divergence_facts is not None:
        facts["divergence_facts"] = row.divergence_facts.model_dump(mode="json")
    if not any((row.order_ref, row.perm_id is not None, row.exec_id)):
        facts["legacy_identity_fallback"] = True
    return facts


def _event_type_for(row: BrokerActivityRow) -> BotEventType:
    reasons = set(row.reason_codes)
    if ReasonCode.REJECTION in reasons:
        return BotEventType.ORDER_REJECTED
    if ReasonCode.CANCELLATION in reasons:
        return BotEventType.ORDER_CANCELLED
    if row.verdict is Verdict.ENGINE_ONLY_PENDING:
        return BotEventType.ORDER_SUBMITTED
    return BotEventType.ORDER_FILLED


def _severity_for(row: BrokerActivityRow, event_type: BotEventType) -> BotEventSeverity:
    if event_type is BotEventType.ORDER_REJECTED or row.verdict is Verdict.UNEXPECTED:
        return BotEventSeverity.CRITICAL
    if row.verdict is Verdict.EXPECTED_WITH_CAVEAT:
        return BotEventSeverity.WARNING
    return BotEventSeverity.INFO


def _terminal_error_for(row: BrokerActivityRow, event_type: BotEventType) -> TerminalError | None:
    if event_type is not BotEventType.ORDER_REJECTED:
        return None
    return TerminalError(
        code=TerminalErrorCode.ORDER_REJECTED,
        source=TerminalErrorSource.IBKR,
        gate_id="broker.place_order",
        message=row.headline,
        detail=row.narrative,
        forensic_facts={
            "legacy_template_key": row.template_key,
            "legacy_template_version": row.template_version,
        },
    )
