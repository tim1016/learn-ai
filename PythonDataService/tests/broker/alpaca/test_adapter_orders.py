"""Golden-fixture test: Alpaca order payloads → BrokerOrder (with fill events)."""

from __future__ import annotations

from app.broker.alpaca.adapter import from_alpaca_order, rfc3339_to_ms
from tests.broker.alpaca.conftest import AlpacaFixtureLoader

_OBSERVED = 1_700_000_000_000


def test_filled_order_maps_every_field_and_synthesizes_fill_event(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    filled = load_alpaca_fixture("orders", "orders.json")[0]

    order = from_alpaca_order(filled, observed_at_ms=_OBSERVED)

    assert order.broker == "alpaca"
    assert order.order_id == "61e69015-8549-4bfd-b9c3-01e75843f47d"
    assert order.client_order_id == "eb9e2aaa-f71a-4f51-b5b4-52a6c565dad4"
    assert order.symbol == "AAPL"
    assert order.asset_class == "us_equity"
    assert order.side == "buy"
    assert order.order_type == "market"
    assert order.time_in_force == "day"
    assert order.quantity == 10.0
    assert order.filled_quantity == 10.0
    assert order.limit_price is None
    assert order.filled_avg_price == 135.80
    assert order.status == "filled"
    assert order.submitted_at_ms == rfc3339_to_ms("2021-03-16T18:38:01.937734Z")
    assert order.filled_at_ms == rfc3339_to_ms("2021-03-16T18:38:02.123456Z")
    assert order.canceled_at_ms is None
    assert order.observed_at_ms == _OBSERVED

    assert len(order.events) == 1
    event = order.events[0]
    assert event.event_type == "fill"
    assert event.occurred_at_ms == rfc3339_to_ms("2021-03-16T18:38:02.123456Z")
    assert event.price == 135.80
    assert event.quantity == 10.0


def test_open_order_has_no_events_and_nullable_prices(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    open_order = load_alpaca_fixture("orders", "orders.json")[1]

    order = from_alpaca_order(open_order, observed_at_ms=_OBSERVED)

    assert order.status == "new"
    assert order.order_type == "limit"
    assert order.limit_price == 240.00
    assert order.filled_quantity == 0.0
    assert order.filled_avg_price is None
    assert order.filled_at_ms is None
    assert order.events == []
