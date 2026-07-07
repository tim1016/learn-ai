"""IBKR rejection bridge into ADR-0024 bot events."""

from __future__ import annotations

import logging

from app.broker.ibkr.event_codes import IBKR_CODE_MEANINGS
from app.broker.ibkr.models import IbkrOrderEvent
from app.operator.incidents.store import IncidentStore
from app.operator.notices.schema import OperatorIncident
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
from app.services.bot_event_incidents import (
    append_terminal_incident,
    build_terminal_incident,
    terminal_incident_id,
)
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
    append_terminal_incident(incident_store, raw_event)


def build_order_rejected_incident(raw_event: BotEventRaw) -> OperatorIncident:
    if (
        raw_event.terminal_error is None
        or raw_event.terminal_error.code is not TerminalErrorCode.ORDER_REJECTED
    ):
        raise ValueError("order rejection incident requires order_rejected terminal_error")
    return build_terminal_incident(raw_event)


def order_rejected_incident_id(key: IncidentDedupeKey) -> str:
    if key.terminal_code is not TerminalErrorCode.ORDER_REJECTED:
        raise ValueError("order rejection incident id requires order_rejected terminal code")
    return terminal_incident_id(key)


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
