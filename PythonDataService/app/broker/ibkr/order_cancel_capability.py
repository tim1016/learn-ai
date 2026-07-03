"""Pure order-cancel capability policy for IBKR paper orders."""

from __future__ import annotations

from app.broker.ibkr.models import IbkrConnectionHealth
from app.schemas.account_truth import (
    AccountTruthFactOwner,
    AccountTruthOrderCancelAction,
    AccountTruthOrderCancelReasonCode,
)

_TERMINAL_LIFECYCLES = frozenset({"filled", "cancelled", "rejected"})


def disabled_order_cancel_action(
    *,
    visible: bool,
    reason_code: AccountTruthOrderCancelReasonCode,
    detail: str,
) -> AccountTruthOrderCancelAction:
    return AccountTruthOrderCancelAction(
        visible=visible,
        enabled=False,
        reason_code=reason_code,
        label="Cannot cancel",
        detail=detail,
    )


def evaluate_order_cancel_capability(
    *,
    health: IbkrConnectionHealth,
    fact_kind: str,
    owner: AccountTruthFactOwner,
    lifecycle: str,
    remaining: float,
    account_freeze_active: bool = False,
) -> AccountTruthOrderCancelAction:
    """Return the canonical cancel affordance for one broker order row."""

    if fact_kind != "open_order":
        return disabled_order_cancel_action(
            visible=False,
            reason_code="NOT_OPEN_ORDER",
            detail="Only live open broker orders can be cancelled.",
        )
    if account_freeze_active:
        return disabled_order_cancel_action(
            visible=True,
            reason_code="ACCOUNT_FROZEN",
            detail="Account recovery is frozen; cancel requires account recovery evidence first.",
        )
    if health.connected is not True or health.is_paper is not True:
        return disabled_order_cancel_action(
            visible=True,
            reason_code="BROKER_NOT_PAPER_CONNECTED",
            detail="Disabled until IBKR is connected to a paper account (DU account).",
        )
    if owner.owner_class == "foreign_or_unclaimed":
        return disabled_order_cancel_action(
            visible=True,
            reason_code="FOREIGN_OR_UNCLAIMED",
            detail="Foreign or unclaimed orders require explicit adoption before app-side cancel.",
        )
    if remaining <= 0 or lifecycle in _TERMINAL_LIFECYCLES:
        return disabled_order_cancel_action(
            visible=True,
            reason_code="ORDER_TERMINAL",
            detail="Order is already terminal at IBKR.",
        )
    return AccountTruthOrderCancelAction(
        visible=True,
        enabled=True,
        reason_code=None,
        label="Cancel",
        detail="Sends an IBKR cancel request for this live open order.",
    )


__all__ = ["disabled_order_cancel_action", "evaluate_order_cancel_capability"]
