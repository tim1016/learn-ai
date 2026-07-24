"""Golden-fixture test: Alpaca order payloads → BrokerOrder (with fill events),
plus the outbound BrokerOrderLeg → Alpaca request-body mapper.

Fixture layout (orders.json):
  [0] — real filled SPY market buy (HITL #1178)
  [1] — synthetic open limit SPY buy at $730.00
"""

from __future__ import annotations

import pytest

from app.broker.alpaca.adapter import (
    from_alpaca_order,
    rfc3339_to_ms,
    to_alpaca_order_request,
)
from app.broker.contract.models import BrokerOrderLeg
from tests.broker.alpaca.conftest import AlpacaFixtureLoader

_OBSERVED = 1_700_000_000_000


def test_filled_order_maps_every_field_and_synthesizes_fill_event(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    filled = load_alpaca_fixture("orders", "orders.json")[0]

    order = from_alpaca_order(filled, observed_at_ms=_OBSERVED)

    assert order.broker == "alpaca"
    assert order.order_id == "00000000-0000-0000-0000-000000000001"
    assert order.client_order_id == "manual/hitl-gate/v1:SANITIZED0000000000001"
    assert order.symbol == "SPY"
    assert order.asset_class == "us_equity"
    assert order.side == "buy"
    assert order.order_type == "market"
    assert order.time_in_force == "day"
    assert order.quantity == 1.0
    assert order.filled_quantity == 1.0
    assert order.limit_price is None
    assert order.filled_avg_price == 737.91
    assert order.status == "filled"
    assert order.submitted_at_ms == rfc3339_to_ms("2026-07-24T14:42:49.356957709Z")
    assert order.filled_at_ms == rfc3339_to_ms("2026-07-24T14:42:50.129256359Z")
    assert order.canceled_at_ms is None
    assert order.observed_at_ms == _OBSERVED

    assert len(order.events) == 1
    event = order.events[0]
    assert event.event_type == "fill"
    assert event.occurred_at_ms == rfc3339_to_ms("2026-07-24T14:42:50.129256359Z")
    assert event.price == 737.91
    assert event.quantity == 1.0


def test_open_order_has_no_events_and_nullable_prices(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    open_order = load_alpaca_fixture("orders", "orders.json")[1]

    order = from_alpaca_order(open_order, observed_at_ms=_OBSERVED)

    assert order.status == "new"
    assert order.order_type == "limit"
    assert order.limit_price == 730.00
    assert order.filled_quantity == 0.0
    assert order.filled_avg_price is None
    assert order.filled_at_ms is None
    assert order.events == []


def test_to_alpaca_order_request_maps_equity_market_leg() -> None:
    leg = BrokerOrderLeg(symbol="SPY", side="buy", quantity=3)

    body = to_alpaca_order_request(leg, client_order_id="manual/inkant/v1:abc123")

    assert body == {
        "symbol": "SPY",
        "qty": "3.0",
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "client_order_id": "manual/inkant/v1:abc123",
    }
    # A market leg never carries a limit_price on the wire.
    assert "limit_price" not in body


def test_to_alpaca_order_request_maps_limit_leg_with_price_and_tif() -> None:
    leg = BrokerOrderLeg(
        symbol="SPY",
        side="sell",
        quantity=2,
        order_type="limit",
        limit_price=240.5,
        time_in_force="gtc",
    )

    body = to_alpaca_order_request(leg, client_order_id="manual/inkant/v1:def456")

    assert body == {
        "symbol": "SPY",
        "qty": "2.0",
        "side": "sell",
        "type": "limit",
        "time_in_force": "gtc",
        "limit_price": "240.5",
        "client_order_id": "manual/inkant/v1:def456",
    }


@pytest.mark.parametrize(
    ("quantity", "expected"),
    [
        (0.00001, "0.00001"),
        (1e20, "100000000000000000000"),
    ],
)
def test_to_alpaca_order_request_never_uses_scientific_quantity_notation(
    quantity: float,
    expected: str,
) -> None:
    leg = BrokerOrderLeg(symbol="SPY", side="buy", quantity=quantity)

    body = to_alpaca_order_request(leg, client_order_id="manual/inkant/v1:decimal")

    assert body["qty"] == expected
