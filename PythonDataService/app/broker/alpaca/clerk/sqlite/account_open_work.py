"""The account's open orders and positions, read whole from the broker.

The read, and the three things every reader of it asks of its answer: the
signed position per symbol, which orders may still act, and whether the
open-order page can be complete. A leaf module, so account reconciliation
(``reconcile``) and the EXIT machine it drives (``exit_resolution``) share
one copy without importing each other.
"""

from __future__ import annotations

import asyncio

from app.broker.alpaca.clerk.sqlite.order_projection import (
    ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES,
    signed_broker_position_quantity,
)
from app.broker.contract.models import BrokerOrder, BrokerPosition
from app.broker.contract.ports import BrokerReadPort

MAX_OPEN_ORDER_SNAPSHOT = 500


def broker_quantity_by_symbol(broker_positions: list[BrokerPosition]) -> dict[str, float]:
    """The broker's signed position per upper-cased symbol."""
    broker_by_symbol: dict[str, float] = {}
    for position in broker_positions:
        symbol = position.symbol.upper()
        broker_by_symbol[symbol] = broker_by_symbol.get(symbol, 0.0) + signed_broker_position_quantity(
            position
        )
    return broker_by_symbol


def broker_order_in_flight(order: BrokerOrder) -> bool:
    """Whether the broker may still act on this order."""
    return order.status.lower() not in ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES


def open_order_snapshot_is_full(broker_orders: list[BrokerOrder]) -> bool:
    """Whether the open-order read reached its row limit, so more orders may exist than it shows."""
    return len(broker_orders) >= MAX_OPEN_ORDER_SNAPSHOT


async def read_account_open_work(
    read: BrokerReadPort,
) -> tuple[list[BrokerOrder], list[BrokerPosition]]:
    """The account's open orders and positions, as the broker reports them.

    Whole-account broker truth, read together: reconciliation folds it into
    custody and the lane-quiet observation (#2154) asks only whether it is
    empty. A ``BrokerError`` propagates, because what an unreadable broker
    means is the caller's to decide. The two lists are gathered concurrently
    with no consistency fence between them, so a caller that needs them to
    describe one instant owes its own re-read rule.
    """
    broker_orders, broker_positions = await asyncio.gather(
        read.list_orders(status="open", limit=MAX_OPEN_ORDER_SNAPSHOT),
        read.list_positions(),
    )
    return broker_orders, broker_positions


async def read_open_orders_then_positions(
    read: BrokerReadPort,
) -> tuple[list[BrokerOrder], list[BrokerPosition]]:
    """The account's open orders, then its positions: one read after the other (#2839).

    For a caller that sets a position against what the open orders may still
    take from it. Read in this sequence, an order that fills between the two
    reads is counted twice -- as the open order it was, and in the position it
    already changed -- and never missed; read the other way round, or
    together as :func:`read_account_open_work` reads them, it can show in
    neither. A ``BrokerError`` propagates.
    """
    broker_orders = await read.list_orders(status="open", limit=MAX_OPEN_ORDER_SNAPSHOT)
    broker_positions = await read.list_positions()
    return broker_orders, broker_positions


__all__ = [
    "MAX_OPEN_ORDER_SNAPSHOT",
    "broker_order_in_flight",
    "broker_quantity_by_symbol",
    "open_order_snapshot_is_full",
    "read_account_open_work",
    "read_open_orders_then_positions",
]
