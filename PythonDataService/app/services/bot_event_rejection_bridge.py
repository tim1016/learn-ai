"""IBKR rejection bridge into ADR-0024 bot events."""

from __future__ import annotations

import hashlib
import logging

from app.broker.ibkr.event_codes import IBKR_CODE_MEANINGS
from app.broker.ibkr.models import IbkrOrderEvent
from app.operator.incidents.store import IncidentStore
from app.operator.notices.schema import (
    OperatorIncident,
    OperatorNotice,
    OperatorNoticeAction,
)
from app.schemas.bot_events import (
    BotEventIdentity,
    BotEventRaw,
    BotEventRawType,
    IncidentDedupeKey,
    SourceAuthority,
    TerminalError,
    TerminalErrorCode,
    TerminalErrorSource,
)
from app.schemas.broker_activity import BrokerActivityRow, ReasonCode
from app.services.bot_event_wal import BotEventRawWal
from app.services.broker_activity_reconciler import EngineIntent, parse_order_ref

logger = logging.getLogger(__name__)


def is_order_rejection_row(row: BrokerActivityRow) -> bool:
    return ReasonCode.REJECTION in row.reason_codes


def build_order_rejected_raw_event(
    *,
    seq: int,
    strategy_instance_id: str,
    run_id: str,
    event: IbkrOrderEvent,
    row: BrokerActivityRow,
    intent: EngineIntent | None,
) -> BotEventRaw:
    terminal_error = _terminal_error(event=event, row=row)
    return BotEventRaw(
        seq=seq,
        ts_ms=row.ts_ms,
        strategy_instance_id=strategy_instance_id,
        run_id=run_id,
        event_type=BotEventRawType.ORDER_REJECTED,
        source_authority=SourceAuthority.BROKER_SESSION,
        identity=_identity(event=event, row=row, intent=intent),
        terminal_error=terminal_error,
        facts={
            "symbol": row.symbol,
            "side": row.side,
            "quantity": row.quantity,
            "order_type": row.order_type,
        },
    )


def append_order_rejection_capture(
    *,
    bot_event_wal: BotEventRawWal,
    incident_store: IncidentStore | None,
    strategy_instance_id: str,
    run_id: str,
    event: IbkrOrderEvent,
    row: BrokerActivityRow,
    intent: EngineIntent | None,
) -> None:
    raw_event = build_order_rejected_raw_event(
        seq=bot_event_wal.allocate_seq(),
        strategy_instance_id=strategy_instance_id,
        run_id=run_id,
        event=event,
        row=row,
        intent=intent,
    )
    bot_event_wal.append_event(raw_event)
    if incident_store is None:
        logger.warning(
            "order rejection captured but no IncidentStore wired; incident not persisted",
            extra={
                "strategy_instance_id": strategy_instance_id,
                "bot_event_seq": raw_event.seq,
            },
        )
        return
    incident_store.append(build_order_rejected_incident(raw_event))


def build_order_rejected_incident(raw_event: BotEventRaw) -> OperatorIncident:
    terminal_error = raw_event.terminal_error
    if terminal_error is None:
        raise ValueError("order rejection incident requires terminal_error")
    identity = raw_event.identity
    incident_id = order_rejected_incident_id(
        IncidentDedupeKey(
            strategy_instance_id=raw_event.strategy_instance_id,
            terminal_code=TerminalErrorCode.ORDER_REJECTED,
            order_ref=identity.order_ref,
            evaluation_id=identity.evaluation_id,
            req_id=identity.req_id,
            order_id=identity.order_id,
            perm_id=identity.perm_id,
        )
    )
    external_message = terminal_error.external_message or terminal_error.message
    return OperatorIncident(
        incident_id=incident_id,
        category="order",
        notice=OperatorNotice(
            code="order.rejected",
            tier="critical",
            title="IBKR rejected the order",
            message=(
                "IBKR rejected an order from this bot. Review the broker "
                f"message before retrying: {external_message}"
            ),
            source_codes=[str(terminal_error.external_code)]
            if terminal_error.external_code is not None
            else [],
            forensic_facts={
                "bot_event_seq": raw_event.seq,
                "order_ref": identity.order_ref,
                "req_id": identity.req_id,
                "order_id": identity.order_id,
                "perm_id": identity.perm_id,
                "external_code": terminal_error.external_code,
                "external_message": terminal_error.external_message,
            },
            action=OperatorNoticeAction(
                kind="external_manual_check",
                label="Review in IBKR",
                target="ibkr_order_rejection",
            ),
            runbook_slug="ibkr-order-rejection",
            occurred_at_ms=raw_event.ts_ms,
        ),
        started_at_ms=raw_event.ts_ms,
        evidence={
            "bot_event_seq": raw_event.seq,
            "strategy_instance_id": raw_event.strategy_instance_id,
            "run_id": raw_event.run_id,
            "order_ref": identity.order_ref,
            "req_id": identity.req_id,
            "order_id": identity.order_id,
            "perm_id": identity.perm_id,
            "terminal_code": terminal_error.code.value,
        },
    )


def order_rejected_incident_id(key: IncidentDedupeKey) -> str:
    raw = key.model_dump_json()
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"order-rejected-{digest}"


def _identity(
    *,
    event: IbkrOrderEvent,
    row: BrokerActivityRow,
    intent: EngineIntent | None,
) -> BotEventIdentity:
    parsed = parse_order_ref(row.order_ref)
    intent_id = intent.intent_id if intent is not None else (parsed[1] if parsed else None)
    req_id = event.req_id if event.req_id is not None else event.order_id
    return BotEventIdentity(
        intent_id=intent_id,
        order_ref=row.order_ref,
        req_id=req_id,
        order_id=event.order_id,
        perm_id=event.perm_id or row.perm_id,
        exec_id=event.exec_id or row.exec_id,
    )


def _terminal_error(*, event: IbkrOrderEvent, row: BrokerActivityRow) -> TerminalError:
    meaning = IBKR_CODE_MEANINGS.get(event.error_code) if event.error_code is not None else None
    message = meaning.label if meaning is not None else "IBKR order rejected"
    detail = event.error_message or row.narrative
    return TerminalError(
        code=TerminalErrorCode.ORDER_REJECTED,
        source=TerminalErrorSource.IBKR,
        gate_id="broker.place_order",
        message=message,
        detail=detail,
        external_code=event.error_code,
        external_message=event.error_message,
        forensic_facts={
            "req_id": event.req_id if event.req_id is not None else event.order_id,
            "order_id": event.order_id,
            "perm_id": event.perm_id,
        },
    )


__all__ = [
    "append_order_rejection_capture",
    "build_order_rejected_incident",
    "build_order_rejected_raw_event",
    "is_order_rejection_row",
    "order_rejected_incident_id",
]
