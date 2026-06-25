"""Projection helpers for IBKR order domain models."""

from __future__ import annotations

from typing import Literal

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
