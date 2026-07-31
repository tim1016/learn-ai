from __future__ import annotations

from typing import Any

import pytest

from app.broker.contract.models import BrokerOrder
from app.services.broker_order_groups import group_orders_by_symbol


def _order(**overrides: Any) -> BrokerOrder:
    values: dict[str, Any] = {
        "broker": "alpaca",
        "order_id": "order-1",
        "client_order_id": None,
        "symbol": "SPY",
        "asset_class": "us_equity",
        "side": "buy",
        "order_type": "market",
        "time_in_force": "day",
        "quantity": 10.0,
        "filled_quantity": 0.0,
        "limit_price": None,
        "stop_price": None,
        "filled_avg_price": None,
        "status": "new",
        "submitted_at_ms": 1_700_000_000_000,
        "created_at_ms": 1_700_000_000_000,
        "updated_at_ms": 1_700_000_000_000,
        "filled_at_ms": None,
        "canceled_at_ms": None,
        "expired_at_ms": None,
        "events": [],
        "observed_at_ms": 1_700_000_000_000,
    }
    values.update(overrides)
    return BrokerOrder(**values)


def test_group_orders_by_symbol_preserves_recency_and_aggregates_quantities() -> None:
    orders = [
        _order(order_id="spy-new", quantity=10.25, filled_quantity=2.25),
        _order(
            order_id="aapl-filled",
            symbol="AAPL",
            quantity=3.0,
            filled_quantity=3.0,
            status="filled",
        ),
        _order(
            order_id="spy-filled",
            quantity=1.5,
            filled_quantity=1.5,
            status="filled",
        ),
    ]

    groups = group_orders_by_symbol(orders)

    assert [group.symbol for group in groups] == ["SPY", "AAPL"]
    assert [order.order_id for order in groups[0].orders] == ["spy-new", "spy-filled"]
    assert groups[0].order_count == 2
    assert groups[0].working_order_count == 1
    assert groups[0].gross_requested_quantity == pytest.approx(11.75, abs=1e-12, rel=0)
    assert groups[0].gross_filled_quantity == pytest.approx(3.75, abs=1e-12, rel=0)
    assert groups[0].gross_working_quantity == pytest.approx(8.0, abs=1e-12, rel=0)


def test_group_orders_by_symbol_excludes_canceled_remainder_from_working_quantity() -> None:
    groups = group_orders_by_symbol(
        [
            _order(
                quantity=10.0,
                filled_quantity=4.0,
                status="canceled",
            )
        ]
    )

    assert groups[0].gross_filled_quantity == pytest.approx(4.0, abs=1e-12, rel=0)
    assert groups[0].gross_working_quantity == pytest.approx(0.0, abs=1e-12, rel=0)


def test_group_orders_by_symbol_sorts_unordered_input_by_submission_time() -> None:
    groups = group_orders_by_symbol(
        [
            _order(order_id="aapl-old", symbol="AAPL", submitted_at_ms=100),
            _order(order_id="spy-new", symbol="SPY", submitted_at_ms=300),
            _order(order_id="aapl-new", symbol="AAPL", submitted_at_ms=200),
        ]
    )

    assert [group.symbol for group in groups] == ["SPY", "AAPL"]
    assert [order.order_id for order in groups[1].orders] == ["aapl-new", "aapl-old"]


def test_group_orders_by_symbol_labels_mixed_side_activity_as_gross() -> None:
    groups = group_orders_by_symbol(
        [
            _order(order_id="buy", quantity=10.0, filled_quantity=4.0, side="buy"),
            _order(order_id="sell", quantity=10.0, filled_quantity=2.0, side="sell"),
        ]
    )

    assert groups[0].gross_requested_quantity == pytest.approx(20.0, abs=1e-12, rel=0)
    assert groups[0].gross_filled_quantity == pytest.approx(6.0, abs=1e-12, rel=0)
    assert groups[0].gross_working_quantity == pytest.approx(14.0, abs=1e-12, rel=0)
