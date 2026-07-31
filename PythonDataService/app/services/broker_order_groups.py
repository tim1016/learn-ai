"""Build the broker desk's symbol-grouped recent-order read model.

Formula: gross requested = Σ qᵢ; gross filled = Σ fᵢ; gross working =
  Σ max(qᵢ − fᵢ, 0) for non-terminal orders only. Every quantity is an
  absolute broker-reported share count across both BUY and SELL orders; these
  are activity totals, never net exposure.
Reference: Python ``decimal`` arithmetic over broker-reported quantities; this
  is a product read-model aggregation, not a port from an external engine.
Canonical implementation: this file.
Validated against: tests/services/test_broker_order_groups.py.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from app.broker.contract.models import BrokerOrder, BrokerOrderGroup

_TERMINAL_STATUSES = frozenset(
    {
        "filled",
        "canceled",
        "expired",
        "rejected",
        "replaced",
    }
)


def _quantity(value: float | None) -> Decimal:
    return Decimal("0") if value is None else Decimal(str(value))


def _submitted_at_ms(order: BrokerOrder) -> int:
    """Choose the best available submission instant for display ordering."""
    for timestamp in (order.submitted_at_ms, order.created_at_ms, order.updated_at_ms):
        if timestamp is not None:
            return timestamp
    return -1


def group_orders_by_symbol(orders: list[BrokerOrder]) -> list[BrokerOrderGroup]:
    """Group orders newest first while retaining that order within each symbol."""
    grouped: dict[str, list[BrokerOrder]] = defaultdict(list)
    for order in sorted(orders, key=_submitted_at_ms, reverse=True):
        grouped[order.symbol].append(order)

    result: list[BrokerOrderGroup] = []
    for symbol, symbol_orders in grouped.items():
        requested = sum((_quantity(order.quantity) for order in symbol_orders), Decimal("0"))
        filled = sum((_quantity(order.filled_quantity) for order in symbol_orders), Decimal("0"))
        working_orders = [
            order
            for order in symbol_orders
            if order.status.lower() not in _TERMINAL_STATUSES
        ]
        working = sum(
            (
                max(
                    _quantity(order.quantity) - _quantity(order.filled_quantity),
                    Decimal("0"),
                )
                for order in working_orders
            ),
            Decimal("0"),
        )
        result.append(
            BrokerOrderGroup(
                symbol=symbol,
                orders=symbol_orders,
                order_count=len(symbol_orders),
                working_order_count=len(working_orders),
                gross_requested_quantity=float(requested),
                gross_filled_quantity=float(filled),
                gross_working_quantity=float(working),
            )
        )
    return result
