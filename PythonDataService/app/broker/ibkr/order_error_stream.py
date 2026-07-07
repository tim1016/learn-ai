"""Adapt IBKR order-scoped error callbacks into order events."""

from __future__ import annotations

from collections.abc import Sequence

from app.broker.ibkr.models import IbkrOrderEvent
from app.broker.ibkr.order_evidence import build_status_event_evidence
from app.broker.ibkr.order_projection import (
    event_order_type,
    event_side,
    event_symbol,
    order_belongs_to_account,
)

OrderErrorEvent = tuple[int, int, str, int]


def drain_order_error_events(
    *,
    client: object,
    trades: Sequence[object],
    account_id: str,
) -> list[IbkrOrderEvent]:
    """Drain buffered IBKR order errors and enrich them from cached trades."""

    raw_errors = _drain_order_errors(client)
    if not raw_errors:
        return []

    trades_by_order_id = {
        int(trade.order.orderId): trade
        for trade in trades
        if order_belongs_to_account(trade, account_id)
    }
    out: list[IbkrOrderEvent] = []
    for req_id, error_code, error_message, error_ts_ms in raw_errors:
        trade = trades_by_order_id.get(req_id)
        if trade is None:
            out.append(
                IbkrOrderEvent(
                    account_id=account_id,
                    order_id=req_id,
                    req_id=req_id,
                    event_type="error",
                    error_code=error_code,
                    error_message=error_message,
                    ts_ms=error_ts_ms,
                )
            )
            continue
        out.append(
            _trade_to_error_event(
                trade,
                account_id,
                req_id=req_id,
                error_code=error_code,
                error_message=error_message,
                ts_ms=error_ts_ms,
            )
        )
    return out


def _trade_to_error_event(
    trade: object,
    account_id: str,
    *,
    req_id: int,
    error_code: int,
    error_message: str,
    ts_ms: int,
) -> IbkrOrderEvent:
    order_ref = getattr(trade.order, "orderRef", "") or None
    return IbkrOrderEvent(
        account_id=account_id,
        order_id=int(trade.order.orderId),
        req_id=req_id,
        perm_id=int(trade.order.permId) if trade.order.permId else None,
        con_id=int(trade.contract.conId) if trade.contract else None,
        event_type="error",
        status=getattr(trade.orderStatus, "status", None),
        order_ref=order_ref,
        symbol=event_symbol(trade),
        side=event_side(trade),
        order_type=event_order_type(trade),
        cumulative_filled=float(getattr(trade.orderStatus, "filled", 0.0) or 0.0),
        remaining=float(getattr(trade.orderStatus, "remaining", 0.0) or 0.0),
        error_code=error_code,
        error_message=error_message,
        ibkr_evidence=build_status_event_evidence(trade),
        ts_ms=ts_ms,
    )


def _drain_order_errors(client: object) -> list[OrderErrorEvent]:
    drain = getattr(client, "drain_order_errors", None)
    if not callable(drain):
        return []
    return list(drain())


__all__ = ["OrderErrorEvent", "drain_order_error_events"]
