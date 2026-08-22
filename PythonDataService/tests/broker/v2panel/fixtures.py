"""Shared journal-fixture builders for the broker-v2 panel tests (S1)."""

from __future__ import annotations

from app.broker.alpaca.clerk.models import ClerkEntryKind, OrderJournalEntry
from app.broker.alpaca.clerk.sqlite.decision_receipts import DecisionOutcome
from app.broker.contract.models import (
    BrokerOrder,
    BrokerOrderEvent,
    BrokerOrderLeg,
    OrderSide,
)
from app.engine.live.order_identity import NAMESPACE_ROOT
from app.services.broker_v2_panel.sqlite_panel_source import CausalDecisionReceipt

ACCT = "test-acct"
SID = "bot-alpha"
OTHER_SID = "bot-beta"


def order_ref(sid: str, intent: str) -> str:
    return f"{NAMESPACE_ROOT}/{sid}/v1:{intent}"


def intent_entry(
    *,
    sid: str,
    intent: str,
    ts_ms: int,
    symbol: str = "SPY",
    side: OrderSide = OrderSide.BUY,
    qty: float = 100.0,
    account_id: str = ACCT,
) -> OrderJournalEntry:
    return OrderJournalEntry(
        kind=ClerkEntryKind.INTENT_RECORDED,
        account_id=account_id,
        order_ref=order_ref(sid, intent),
        client_order_id=order_ref(sid, intent),
        operator=sid,
        intent_id=intent,
        recorded_at_ms=ts_ms,
        leg=BrokerOrderLeg(symbol=symbol, side=side, quantity=qty),
    )


def submit_acked_entry(
    *, sid: str, intent: str, ts_ms: int, account_id: str = ACCT
) -> OrderJournalEntry:
    ref = order_ref(sid, intent)
    return OrderJournalEntry(
        kind=ClerkEntryKind.SUBMIT_ACKED,
        account_id=account_id,
        order_ref=ref,
        client_order_id=ref,
        operator=sid,
        intent_id=intent,
        recorded_at_ms=ts_ms,
        order=BrokerOrder(
            broker="alpaca",
            order_id="ord-1",
            client_order_id=ref,
            symbol="SPY",
            asset_class="us_equity",
            side="buy",
            order_type="market",
            time_in_force="day",
            quantity=100.0,
            filled_quantity=0.0,
            limit_price=None,
            stop_price=None,
            filled_avg_price=None,
            status="accepted",
            submitted_at_ms=ts_ms,
            created_at_ms=ts_ms,
            updated_at_ms=ts_ms,
            filled_at_ms=None,
            canceled_at_ms=None,
            expired_at_ms=None,
            observed_at_ms=ts_ms,
        ),
    )


def submit_failed_entry(
    *, sid: str, intent: str, ts_ms: int, account_id: str = ACCT
) -> OrderJournalEntry:
    ref = order_ref(sid, intent)
    return OrderJournalEntry(
        kind=ClerkEntryKind.SUBMIT_FAILED,
        account_id=account_id,
        order_ref=ref,
        client_order_id=ref,
        operator=sid,
        intent_id=intent,
        recorded_at_ms=ts_ms,
        error_message="rejected by broker",
        error_detail="insufficient buying power",
    )


def fill_entry(
    *,
    sid: str,
    intent: str,
    symbol: str = "SPY",
    side: OrderSide = OrderSide.BUY,
    qty: float = 100.0,
    price: float = 500.0,
    ts_ms: int = 1_000,
    event_key: str | None = None,
    account_id: str = ACCT,
) -> OrderJournalEntry:
    ref = order_ref(sid, intent)
    return OrderJournalEntry(
        kind=ClerkEntryKind.ORDER_EVENT,
        account_id=account_id,
        order_ref=ref,
        client_order_id=ref,
        operator=sid,
        intent_id=intent,
        owned=True,
        recorded_at_ms=ts_ms,
        leg=BrokerOrderLeg(symbol=symbol, side=side, quantity=qty),
        event=BrokerOrderEvent(
            event_type="fill", occurred_at_ms=ts_ms, price=price, quantity=qty
        ),
        event_key=event_key or f"exec:{ts_ms}:{intent}",
    )


def reconciliation_entry(
    *, verdict: str, ts_ms: int, account_id: str = ACCT
) -> OrderJournalEntry:
    return OrderJournalEntry(
        kind=ClerkEntryKind.RECONCILIATION,
        account_id=account_id,
        recorded_at_ms=ts_ms,
        verdict=verdict,  # type: ignore[arg-type]
        operator="op",
    )


def hold_set_entry(
    *, reason_code: str, ts_ms: int, account_id: str = ACCT
) -> OrderJournalEntry:
    return OrderJournalEntry(
        kind=ClerkEntryKind.HOLD_SET,
        account_id=account_id,
        recorded_at_ms=ts_ms,
        reason_code=reason_code,
        reason="fixture hold",
    )


def decision_receipt(
    *,
    seq: int,
    ts_ms: int,
    outcome: DecisionOutcome,
    reason_code: str,
    bar_ref: str = "bar-1",
    order_ref: str = "",
    intent_id: str = "",
    decision_id: str | None = None,
    effect_operation_id: str | None = None,
) -> CausalDecisionReceipt:
    return CausalDecisionReceipt(
        seq=seq,
        ts_ms=ts_ms,
        bar_ref=bar_ref,
        outcome=outcome,
        reason_code=reason_code,
        order_ref=order_ref,
        intent_id=intent_id,
        decision_id=decision_id,
        effect_operation_id=effect_operation_id,
    )
