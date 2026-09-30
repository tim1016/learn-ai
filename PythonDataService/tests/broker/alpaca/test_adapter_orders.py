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
from app.broker.alpaca.broker import AlpacaBroker
from app.broker.contract.errors import BrokerEvidenceUnavailable
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
    assert order.fill_latency_seconds == pytest.approx(0.772, abs=1e-12, rel=0)
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
    assert order.fill_latency_seconds is None
    assert order.events == []


@pytest.mark.parametrize("filled_qty", [None, "", "0", 0, "0.0"], ids=repr)
def test_an_order_with_no_fill_count_reads_as_zero_filled(
    load_alpaca_fixture: AlpacaFixtureLoader,
    filled_qty: object,
) -> None:
    payload = {**load_alpaca_fixture("orders", "orders.json")[1], "filled_qty": filled_qty}

    assert from_alpaca_order(payload, observed_at_ms=_OBSERVED).filled_quantity == 0.0


def test_an_order_missing_its_fill_count_reads_as_zero_filled(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    payload = {
        key: value
        for key, value in load_alpaca_fixture("orders", "orders.json")[1].items()
        if key != "filled_qty"
    }

    assert from_alpaca_order(payload, observed_at_ms=_OBSERVED).filled_quantity == 0.0


@pytest.mark.parametrize("filled_qty", [True, False])
def test_a_boolean_fill_count_is_refused_not_read_as_a_quantity(
    load_alpaca_fixture: AlpacaFixtureLoader,
    filled_qty: bool,
) -> None:
    # ``payload.get("filled_qty") or 0`` once turned ``false`` into a quiet 0.0
    # while ``true`` became one filled share (#2606).
    payload = {**load_alpaca_fixture("orders", "orders.json")[1], "filled_qty": filled_qty}

    with pytest.raises(TypeError, match="not a boolean"):
        from_alpaca_order(payload, observed_at_ms=_OBSERVED)


def test_fill_latency_is_unknown_until_both_broker_clocks_exist(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    filled = dict(load_alpaca_fixture("orders", "orders.json")[0])
    filled["submitted_at"] = None

    order = from_alpaca_order(filled, observed_at_ms=_OBSERVED)

    assert order.filled_at_ms is not None
    assert order.fill_latency_seconds is None


def test_to_alpaca_order_request_maps_equity_market_leg() -> None:
    leg = BrokerOrderLeg(symbol="SPY", side="buy", quantity=3)

    body = to_alpaca_order_request(leg, client_order_id="manual/inkant/v1:abc123")

    assert body == {
        "symbol": "SPY",
        "qty": "3.0",
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "extended_hours": False,
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
        "extended_hours": False,
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


def test_to_alpaca_order_request_forwards_extended_hours_on_a_limit_leg() -> None:
    leg = BrokerOrderLeg(
        symbol="SPY", side="buy", quantity=2, order_type="limit", limit_price=100.25, extended_hours=True
    )

    body = to_alpaca_order_request(leg, client_order_id="learn-ai/spy/v1:xh1")

    assert body["extended_hours"] is True
    assert body["type"] == "limit"
    assert body["time_in_force"] == "day"


def test_from_alpaca_order_reads_extended_hours_and_defaults_it_false(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    payload = load_alpaca_fixture("orders", "orders.json")[0]

    assert from_alpaca_order(payload, observed_at_ms=_OBSERVED).extended_hours is False
    assert from_alpaca_order({**payload, "extended_hours": True}, observed_at_ms=_OBSERVED).extended_hours is True


class _OrdersClient:
    """The client seam: returns the raw order rows the test built."""

    def __init__(self, payloads: list[object]) -> None:
        self.payloads = payloads

    async def list_orders(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
        after_ms: int | None = None,
    ) -> list[object]:
        return self.payloads


@pytest.mark.parametrize(
    ("shape", "cause_type"),
    [
        pytest.param("missing-id", KeyError, id="missing-id"),
        pytest.param("boolean-fill-count", TypeError, id="boolean-fill-count"),
        pytest.param("unparseable-submitted-at", ValueError, id="unparseable-submitted-at"),
        pytest.param("non-object-row", AttributeError, id="non-object-row"),
    ],
)
async def test_broker_names_a_malformed_order_as_unavailable_evidence(
    load_alpaca_fixture: AlpacaFixtureLoader,
    shape: str,
    cause_type: type[Exception],
) -> None:
    """A malformed row once escaped as a raw KeyError/TypeError/ValueError (#2627)."""
    open_order = load_alpaca_fixture("orders", "orders.json")[1]
    rows: dict[str, list[object]] = {
        "missing-id": [{key: value for key, value in open_order.items() if key != "id"}],
        "boolean-fill-count": [{**open_order, "filled_qty": True}],
        "unparseable-submitted-at": [{**open_order, "submitted_at": "yesterday"}],
        "non-object-row": [open_order, None],
    }
    broker = AlpacaBroker(client=_OrdersClient(rows[shape]))  # type: ignore[arg-type]

    with pytest.raises(BrokerEvidenceUnavailable, match="order data this app could not read") as info:
        await broker.list_orders(status="open", limit=500)

    assert info.value.http_status == 503
    assert info.value.detail is not None
    assert cause_type.__name__ not in info.value.detail
    assert isinstance(info.value.__cause__, cause_type)


@pytest.mark.parametrize("field", ["id", "status", "time_in_force"])
@pytest.mark.parametrize("value", [None, "", "   "], ids=["null", "blank", "whitespace"])
async def test_broker_refuses_an_order_whose_required_text_is_blank(
    load_alpaca_fixture: AlpacaFixtureLoader,
    field: str,
    value: object,
) -> None:
    """A null id once became order "None", a real-looking broker order (#2643)."""
    open_order = load_alpaca_fixture("orders", "orders.json")[1]
    broker = AlpacaBroker(client=_OrdersClient([{**open_order, field: value}]))  # type: ignore[arg-type]

    with pytest.raises(BrokerEvidenceUnavailable, match="order data this app could not read") as info:
        await broker.list_orders(status="open", limit=500)

    assert isinstance(info.value.__cause__, ValueError)
    assert f"'{field}'" in str(info.value.__cause__)


@pytest.mark.parametrize("flag", ["false", "true", 0, 1], ids=repr)
async def test_broker_refuses_an_extended_hours_flag_that_is_not_a_boolean(
    load_alpaca_fixture: AlpacaFixtureLoader,
    flag: object,
) -> None:
    """``bool("false")`` once read a regular-hours order as extended-hours (#2643)."""
    open_order = load_alpaca_fixture("orders", "orders.json")[1]
    broker = AlpacaBroker(client=_OrdersClient([{**open_order, "extended_hours": flag}]))  # type: ignore[arg-type]

    with pytest.raises(BrokerEvidenceUnavailable, match="order data this app could not read") as info:
        await broker.list_orders(status="open", limit=500)

    assert isinstance(info.value.__cause__, TypeError)


@pytest.mark.parametrize("blank", [None, ""], ids=["null", "blank"])
async def test_a_multi_leg_parent_order_still_maps_so_the_clerk_can_contain_it(
    load_alpaca_fixture: AlpacaFixtureLoader,
    blank: object,
) -> None:
    """Alpaca leaves a multi-leg parent's symbol and side blank (alpaca-py ``Order``).

    Refusing them would refuse the whole orders answer and freeze exits
    account-wide; the Clerk contains that one order instead (#2363).
    """
    open_order = load_alpaca_fixture("orders", "orders.json")[1]
    parent = {**open_order, "id": "mleg-parent", "order_class": "mleg", "symbol": blank, "side": blank}
    broker = AlpacaBroker(client=_OrdersClient([parent, open_order]))  # type: ignore[arg-type]

    orders = await broker.list_orders(status="open", limit=500)

    assert [order.order_id for order in orders] == ["mleg-parent", open_order["id"]]
