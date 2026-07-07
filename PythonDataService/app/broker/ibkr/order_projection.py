"""Projection helpers for IBKR order domain models."""

from __future__ import annotations

from typing import Any, Literal

from app.broker.ibkr.models import OrderEventType


def event_symbol(trade: object) -> str | None:
    contract = getattr(trade, "contract", None)
    if contract is None:
        return None
    symbol = getattr(contract, "symbol", None)
    return str(symbol) if symbol else None


def event_side(trade: object) -> Literal["BUY", "SELL"] | None:
    action = getattr(getattr(trade, "order", None), "action", None)
    if action == "BUY":
        return "BUY"
    if action == "SELL":
        return "SELL"
    return None


def event_order_type(trade: object | None) -> str | None:
    if trade is None:
        return None
    order_type = getattr(getattr(trade, "order", None), "orderType", None)
    return str(order_type) if order_type else None


def trade_order_event_fields(trade: object, account_id: str) -> dict[str, Any]:
    """Fields shared by status/error order events sourced from an IBKR Trade."""

    order = getattr(trade, "order", None)
    contract = getattr(trade, "contract", None)
    order_status = getattr(trade, "orderStatus", None)
    order_ref = getattr(order, "orderRef", "") if order is not None else ""
    if order is None:
        raise AttributeError("trade has no order")
    return {
        "account_id": account_id,
        "order_id": int(order.orderId),
        "perm_id": int(order.permId) if order.permId else None,
        "con_id": int(contract.conId) if contract else None,
        "status": getattr(order_status, "status", None),
        "order_ref": order_ref or None,
        "symbol": event_symbol(trade),
        "side": event_side(trade),
        "order_type": event_order_type(trade),
        "cumulative_filled": float(getattr(order_status, "filled", 0.0) or 0.0),
        "remaining": float(getattr(order_status, "remaining", 0.0) or 0.0),
    }


def order_belongs_to_account(trade: object, account_id: str) -> bool:
    """Whether ``trade`` belongs to the connected account.

    Orders placed by this single-account client may have ``order.account == ""``.
    A non-empty account that differs is genuinely foreign.
    """
    order_account = getattr(getattr(trade, "order", None), "account", "") or ""
    return order_account in ("", account_id)


def resolve_event_type(trade: object, *, is_fill: bool) -> OrderEventType:
    if is_fill:
        return "fill"
    status = getattr(getattr(trade, "orderStatus", None), "status", "")
    if status in {"Cancelled", "ApiCancelled"}:
        return "cancel"
    return "status"
