"""Adapt IBKR order-scoped error callbacks into order events."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, NamedTuple

from app.broker.ibkr.models import IbkrOrderEvent
from app.broker.ibkr.order_evidence import build_status_event_evidence
from app.broker.ibkr.order_projection import (
    order_belongs_to_account,
    trade_order_event_fields,
)

if TYPE_CHECKING:
    from app.broker.ibkr.client import IbkrClient


class OrderErrorEvent(NamedTuple):
    seq: int
    req_id: int
    error_code: int
    error_message: str
    ts_ms: int


def read_order_error_events(
    *,
    client: IbkrClient,
    trades: Sequence[object],
    account_id: str,
    after_seq: int,
) -> tuple[list[IbkrOrderEvent], int]:
    """Read buffered IBKR order errors after ``after_seq`` and enrich them."""

    raw_errors = client.order_errors_after(after_seq)
    if not raw_errors:
        return [], after_seq

    trades_by_order_id = {
        int(trade.order.orderId): trade
        for trade in trades
        if order_belongs_to_account(trade, account_id)
    }
    out: list[IbkrOrderEvent] = []
    for raw_error in raw_errors:
        trade = trades_by_order_id.get(raw_error.req_id)
        if trade is None:
            out.append(
                IbkrOrderEvent(
                    account_id=account_id,
                    order_id=raw_error.req_id,
                    req_id=raw_error.req_id,
                    event_type="error",
                    error_code=raw_error.error_code,
                    error_message=raw_error.error_message,
                    ts_ms=raw_error.ts_ms,
                )
            )
            continue
        out.append(
            _trade_to_error_event(
                trade,
                account_id,
                req_id=raw_error.req_id,
                error_code=raw_error.error_code,
                error_message=raw_error.error_message,
                ts_ms=raw_error.ts_ms,
            )
        )
    return out, raw_errors[-1].seq


def _trade_to_error_event(
    trade: object,
    account_id: str,
    *,
    req_id: int,
    error_code: int,
    error_message: str,
    ts_ms: int,
) -> IbkrOrderEvent:
    return IbkrOrderEvent(
        **trade_order_event_fields(trade, account_id),
        req_id=req_id,
        event_type="error",
        error_code=error_code,
        error_message=error_message,
        ibkr_evidence=build_status_event_evidence(trade),
        ts_ms=ts_ms,
    )


__all__ = ["OrderErrorEvent", "read_order_error_events"]
