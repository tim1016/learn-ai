"""IBKR-specific receipt-to-transaction projection helpers."""

from __future__ import annotations

import hashlib

from app.engine.live.account_clerk_journal_models import AccountClerkJournalEntry
from app.engine.live.account_owner import MANUAL_ORDER_INTENT_KIND
from app.schemas.clerk_transaction_projection import (
    ClerkOrderInstruction,
    ClerkTransactionEventRow,
    ClerkTransactionRow,
    TransactionOrigin,
)

_INTENT_EVENT_PRESENTATION: dict[str, tuple[str, str]] = {
    "recorded": ("recorded", "recorded"),
    "broker_submitting": ("submitting", "submitting"),
    "broker_uncertain": ("uncertain", "uncertain"),
    "broker_acked": ("submitted", "submitted"),
}
_ORIGINS: dict[str, TransactionOrigin] = {
    MANUAL_ORDER_INTENT_KIND: "manual",
    "STRATEGY": "strategy",
    "RECOVERY_FLATTEN": "recovery",
    "EMERGENCY_FLATTEN": "emergency",
    "SHUTDOWN_FLATTEN": "shutdown",
    "FORCE_FLAT": "force_flat",
}


def opaque_projection_id(prefix: str, *parts: object) -> str:
    raw = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(raw).hexdigest()[:32]}"


def transaction_origin(entry: AccountClerkJournalEntry) -> TransactionOrigin | None:
    if entry.intent is None or entry.intent.intent_kind == "CANCEL_NAMESPACE":
        return None
    return _ORIGINS.get(entry.intent.intent_kind, "other")


def _transaction_kind(origin: TransactionOrigin) -> str:
    return "manual_ibkr_acknowledgement" if origin == "manual" else f"{origin}_ibkr_order"


def _order_instruction(entry: AccountClerkJournalEntry) -> ClerkOrderInstruction:
    if entry.intent is None:
        raise ValueError("projected Clerk event is missing its intent")
    raw = entry.intent.order_spec

    def text(key: str) -> str | None:
        value = raw.get(key)
        return value if isinstance(value, str) else None

    def number(key: str) -> float | None:
        value = raw.get(key)
        return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None

    outside_rth = raw.get("outside_rth")
    return ClerkOrderInstruction(
        symbol=text("symbol"), sec_type=text("sec_type"), action=text("action"),
        quantity=number("quantity"), order_type=text("order_type"),
        limit_price=number("limit_price"), time_in_force=text("time_in_force"),
        outside_rth=outside_rth if isinstance(outside_rth, bool) else None,
    )


def row_from_event(
    account_id: str, entry: AccountClerkJournalEntry, event: ClerkTransactionEventRow
) -> ClerkTransactionRow:
    if entry.intent is None:
        raise ValueError("projected Clerk event is missing its intent")
    origin = transaction_origin(entry)
    if origin is None:
        raise ValueError("control-only Clerk intent cannot seed a transaction")
    return ClerkTransactionRow(
        transaction_id=opaque_projection_id("ctxn", account_id, entry.intent.order_ref),
        broker="ibkr", account_id=account_id, journal_seq=entry.seq,
        recorded_at_ms=entry.recorded_at_ms, transaction_kind=_transaction_kind(origin),
        transaction_origin=origin, order_instruction=_order_instruction(entry),
        strategy_instance_id=entry.intent.strategy_instance_id, run_id=entry.intent.run_id,
        intent_id=entry.intent.intent_id, order_ref=entry.intent.order_ref,
        order_id=entry.order_id, perm_id=entry.perm_id, exec_id=entry.exec_id,
        lifecycle_state=event.lifecycle_state,
        receipt=entry.model_dump(mode="json", exclude_none=True), events=[event],
    )


def _callback_state(entry: AccountClerkJournalEntry) -> tuple[str, str, str, float | None]:
    if entry.broker_event is None:
        raise ValueError("broker callback journal row is missing raw evidence")
    event = entry.broker_event
    callback = event.get("event_type")
    status = str(event.get("status") or "").lower()
    identity = entry.broker_callback_idempotency_key
    if not isinstance(identity, str) or not identity:
        raise ValueError("broker callback journal row is missing callback identity")
    fee = event.get("fee")
    if fee is not None and not isinstance(fee, int | float):
        raise ValueError("broker callback fee is not numeric")
    error_code = event.get("error_code")
    if error_code is not None and not isinstance(error_code, int):
        raise ValueError("broker callback error code is not an integer")
    if callback == "fill":
        state = "filled" if status == "filled" or event.get("remaining") == 0 else "partially_filled"
        return "execution", state, identity, float(fee) if fee is not None else None
    if callback == "cancel" or status in {"cancelled", "apicancelled"}:
        return "cancelled", "cancelled", identity, None
    if callback == "error":
        state = "rejected" if error_code == 201 or status in {"inactive", "rejected"} else "error"
        return state, state, identity, None
    if fee is not None:
        return "commission", "submitted", identity, float(fee)
    if status == "filled":
        return "filled", "filled", identity, None
    if status in {"partiallyfilled", "partialfilled"}:
        return "partial_fill", "partially_filled", identity, None
    if status in {"inactive", "rejected"}:
        return "rejected", "rejected", identity, None
    return "status", "submitted", identity, None


def event_from_journal(entry: AccountClerkJournalEntry) -> ClerkTransactionEventRow | None:
    if transaction_origin(entry) is None:
        return None
    if entry.entry_kind == "broker_event":
        event_kind, lifecycle_state, identity, fee = _callback_state(entry)
        return ClerkTransactionEventRow(
            event_id=opaque_projection_id("ctxnev", entry.intent.account_id, identity),
            event_kind=event_kind, callback_identity=identity, lifecycle_state=lifecycle_state,
            commission_status="reported" if fee is not None else "unknown", fee=fee,
            journal_seq=entry.seq, recorded_at_ms=entry.recorded_at_ms,
            receipt=entry.model_dump(mode="json", exclude_none=True),
        )
    presentation = _INTENT_EVENT_PRESENTATION.get(entry.entry_kind)
    if presentation is None or entry.intent is None:
        return None
    event_kind, lifecycle_state = presentation
    return ClerkTransactionEventRow(
        event_id=opaque_projection_id("ctxnev", entry.intent.account_id, entry.seq, entry.entry_kind),
        event_kind=event_kind, callback_identity=f"journal:{entry.seq}:{entry.entry_kind}",
        lifecycle_state=lifecycle_state, journal_seq=entry.seq, recorded_at_ms=entry.recorded_at_ms,
        receipt=entry.model_dump(mode="json", exclude_none=True),
    )


def event_order_ref(event: ClerkTransactionEventRow) -> str:
    if event.broker == "alpaca":
        value = event.receipt.get("order_ref")
        if isinstance(value, str) and value:
            return value
        raise ValueError("Alpaca event has no opaque order_ref")
    intent = event.receipt.get("intent")
    if isinstance(intent, dict):
        value = intent.get("order_ref")
        if isinstance(value, str) and value:
            return value
    broker_event = event.receipt.get("broker_event")
    if not isinstance(broker_event, dict) or not isinstance(broker_event.get("order_ref"), str):
        raise ValueError("IBKR Clerk event has no opaque order_ref")
    return broker_event["order_ref"]


def event_transaction_id(account_id: str, event: ClerkTransactionEventRow) -> str:
    if event.broker == "alpaca":
        return opaque_projection_id("ctxn", "alpaca", account_id, event_order_ref(event))
    return opaque_projection_id("ctxn", account_id, event_order_ref(event))
