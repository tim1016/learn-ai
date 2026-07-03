"""Broker order-cancel decision service."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.broker.ibkr.account_truth import (
    build_account_truth_collection_context,
    cancel_action_for_open_order,
)
from app.broker.ibkr.client import IbkrClient
from app.broker.ibkr.models import IbkrConnectionHealth
from app.broker.ibkr.orders import OrderNotFoundError, OrderRefusedError, list_open_orders
from app.schemas.account_truth import AccountTruthOrderCancelAction


@dataclass(frozen=True)
class AccountTruthCancelDecision:
    order_id: int
    action: AccountTruthOrderCancelAction

    def raise_if_blocked(self) -> None:
        if self.action.enabled:
            return
        reason = self.action.reason_code or "UNKNOWN"
        raise OrderRefusedError(
            f"Refusing to cancel order_id={self.order_id}: {reason}. {self.action.detail}"
        )


async def account_truth_cancel_decision(
    client: IbkrClient,
    *,
    health: IbkrConnectionHealth,
    artifacts_root: Path,
    order_id: int,
) -> AccountTruthCancelDecision:
    """Evaluate cancelability for one current open order without a full account sweep."""

    collection_context = build_account_truth_collection_context(
        artifacts_root=artifacts_root,
        account_id=health.account_id,
        context="order cancel",
    )
    open_orders = await list_open_orders(client)
    order = next(
        (order for order in open_orders if int(order.order_id) == int(order_id)),
        None,
    )
    if order is None:
        raise OrderNotFoundError(f"No open order with order_id={order_id} on this client.")
    return AccountTruthCancelDecision(
        order_id=order_id,
        action=cancel_action_for_open_order(
            order,
            health=health,
            collection_context=collection_context,
        ),
    )


__all__ = ["AccountTruthCancelDecision", "account_truth_cancel_decision"]
