"""Live-engine Bot event spine identity and facts helpers."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

from app.broker.ibkr.models import IbkrOrderAck, IbkrOrderEvent
from app.engine.execution.order import Order, OrderEvent
from app.engine.live.bot_event_capture import BotEventSpineRecorder
from app.engine.live.live_portfolio import LivePortfolio
from app.engine.live.order_identity import (
    build_bot_order_namespace,
    build_order_ref,
    order_ref_namespace_matches,
    parse_order_ref,
)
from app.engine.strategy.base import Strategy
from app.schemas.bot_events import BotEventIdentity, FactValue


@dataclass(frozen=True)
class PendingBotEventIdentity:
    evaluation_id: str | None
    intent_id: str | None
    order_ref: str | None


def record_evaluation_spine_after_strategy(
    recorder: BotEventSpineRecorder | None,
    strategy: Strategy,
    portfolio: LivePortfolio,
    *,
    evaluation_id: str,
    ts_ms: int,
    decision_emitted: bool,
) -> None:
    if recorder is None:
        return
    pending_orders = list(portfolio.pending_orders)
    if not pending_orders:
        recorder.append_evaluation_idle(
            ts_ms=ts_ms,
            evaluation_id=evaluation_id,
            facts=decision_spine_facts(strategy) if decision_emitted else {},
        )
        return

    first_order_identity = pending_bot_event_identity(
        portfolio,
        order_id=pending_orders[0].order_id,
        evaluation_id=evaluation_id,
    )
    recorder.append_signal_fired(
        ts_ms=ts_ms,
        evaluation_id=evaluation_id,
        intent_id=first_order_identity.intent_id,
        order_ref=first_order_identity.order_ref,
        facts={
            **(decision_spine_facts(strategy) if decision_emitted else {}),
            "pending_count": len(pending_orders),
            "pending_orders": [pending_order_facts(order) for order in pending_orders],
        },
    )


def decision_spine_facts(strategy: Strategy) -> dict[str, FactValue]:
    snap = strategy.last_decision_snapshot
    if snap is None:
        return {}
    return {
        "decision_bar_close_ms": int(snap.bar_close_ms),
        "decision_signal": str(snap.signal),
        "intended_price": float(snap.intended_price),
    }


def pending_order_facts(order: Order) -> dict[str, FactValue]:
    return {
        "order_id": int(order.order_id),
        "symbol": order.symbol,
        "side": "BUY" if order.quantity > 0 else "SELL",
        "quantity": abs(int(order.quantity)),
        "order_type": order.order_type.value,
        "tag": order.tag,
        "created_at_ms": int(order.time.timestamp() * 1000),
    }


def pending_bot_event_identity(
    portfolio: LivePortfolio,
    *,
    order_id: int,
    evaluation_id: str | None,
) -> PendingBotEventIdentity:
    intent_id = portfolio.intent_id_for_order(order_id)
    order_ref = None
    if intent_id is not None and portfolio.bot_order_namespace:
        order_ref = build_order_ref(portfolio.bot_order_namespace, intent_id)
    return PendingBotEventIdentity(
        evaluation_id=evaluation_id,
        intent_id=intent_id,
        order_ref=order_ref,
    )


def submitted_bot_event_identity(
    ack: IbkrOrderAck,
    pending_identity: PendingBotEventIdentity | None,
) -> BotEventIdentity:
    order_ref = ack.order_ref or (pending_identity.order_ref if pending_identity is not None else None)
    intent_id = pending_identity.intent_id if pending_identity is not None else None
    if intent_id is None:
        intent_id = intent_id_from_order_ref(order_ref)
    return BotEventIdentity(
        evaluation_id=pending_identity.evaluation_id if pending_identity is not None else None,
        intent_id=intent_id,
        order_ref=order_ref,
        order_id=int(ack.order_id),
        perm_id=ack.perm_id,
    )


def intent_id_from_order_ref(order_ref: str | None) -> str | None:
    if not order_ref:
        return None
    with contextlib.suppress(Exception):
        _namespace, intent_id = parse_order_ref(order_ref)
        return intent_id
    return None


def order_submitted_facts(order: Order, ack: IbkrOrderAck) -> dict[str, FactValue]:
    return {
        "symbol": ack.symbol,
        "side": ack.action,
        "quantity": float(ack.quantity),
        "order_type": ack.order_type,
        "status": ack.status,
        "tag": order.tag,
        "client_order_id": f"live-{order.order_id}",
        "order_ref": ack.order_ref,
        "perm_id": ack.perm_id,
    }


def is_owned_broker_event(
    event: IbkrOrderEvent,
    *,
    order_ids: set[int],
    owned_perm_ids: set[int],
    strategy_instance_id: str,
) -> bool:
    if int(event.order_id) in order_ids:
        return True
    if event.perm_id is not None and int(event.perm_id) in owned_perm_ids:
        return True
    if event.order_ref is None or not strategy_instance_id:
        return False
    return order_ref_namespace_matches(
        event.order_ref,
        {build_bot_order_namespace(strategy_instance_id)},
    )


def broker_event_identity(
    event: IbkrOrderEvent,
    *,
    evaluation_id: str | None = None,
    intent_id: str | None = None,
    order_ref: str | None = None,
    perm_id: int | None = None,
) -> BotEventIdentity:
    effective_order_ref = event.order_ref or order_ref
    return BotEventIdentity(
        evaluation_id=evaluation_id,
        intent_id=intent_id or intent_id_from_order_ref(effective_order_ref),
        order_ref=effective_order_ref,
        req_id=event.req_id,
        order_id=int(event.order_id),
        perm_id=event.perm_id or perm_id,
        exec_id=event.exec_id,
    )


def order_event_identity(
    *,
    order_id: int,
    evaluation_id: str | None = None,
    intent_id: str | None = None,
    order_ref: str | None = None,
    perm_id: int | None = None,
    exec_id: str | None = None,
) -> BotEventIdentity:
    return BotEventIdentity(
        evaluation_id=evaluation_id,
        intent_id=intent_id,
        order_ref=order_ref,
        order_id=order_id,
        perm_id=perm_id,
        exec_id=exec_id,
    )


def order_filled_facts(event: OrderEvent) -> dict[str, FactValue]:
    return {
        "symbol": event.symbol,
        "fill_quantity": int(event.fill_quantity),
        "fill_price": float(event.fill_price),
        "fee": float(event.fee),
        "tag": event.tag,
        "execution_source": event.execution_source,
        "fill_model": event.fill_model,
        "client_order_id": event.client_order_id,
        "exec_time_ms": event.exec_time_ms,
    }


def broker_order_event_facts(event: IbkrOrderEvent) -> dict[str, FactValue]:
    return {
        "broker_event_type": event.event_type,
        "status": event.status,
        "symbol": event.symbol,
        "side": event.side,
        "order_type": event.order_type,
        "fill_quantity": event.fill_quantity,
        "avg_fill_price": event.avg_fill_price,
        "last_fill_price": event.last_fill_price,
        "cumulative_filled": event.cumulative_filled,
        "remaining": event.remaining,
        "error_code": event.error_code,
        "error_message": event.error_message,
    }
