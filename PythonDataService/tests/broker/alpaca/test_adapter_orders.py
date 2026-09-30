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
from app.broker.alpaca.clerk.sqlite.external_orders import (
    ExternalOrderObservationError,
    _observation_from_broker_order,
)
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
def test_a_boolean_fill_count_never_reads_as_a_quantity(
    load_alpaca_fixture: AlpacaFixtureLoader,
    filled_qty: bool,
) -> None:
    # ``payload.get("filled_qty") or 0`` once turned ``false`` into a quiet 0.0
    # while ``true`` became one filled share (#2606). A boolean now marks the
    # row unreadable (#2648): it never becomes a quantity, and the Clerk
    # contains the row by the field it names.
    payload = {**load_alpaca_fixture("orders", "orders.json")[1], "filled_qty": filled_qty}

    order = from_alpaca_order(payload, observed_at_ms=_OBSERVED)

    assert order.filled_quantity == 0.0
    assert order.unreadable_fields == ("filled_qty",)


def test_a_negative_price_keeps_its_sign_while_a_negative_share_count_is_unreadable(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    # Only share counts must be non-negative (#2648 review): a multi-leg
    # order's net limit price may be a credit, so a price keeps its sign.
    open_order = load_alpaca_fixture("orders", "orders.json")[1]

    priced = from_alpaca_order({**open_order, "limit_price": "-1.25"}, observed_at_ms=_OBSERVED)
    counted = from_alpaca_order({**open_order, "qty": "-1"}, observed_at_ms=_OBSERVED)

    assert priced.limit_price == -1.25 and priced.unreadable_fields == ()
    assert counted.quantity is None and counted.unreadable_fields == ("qty",)


def test_every_unreadable_value_on_a_row_is_named_in_order(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    payload = {
        **load_alpaca_fixture("orders", "orders.json")[1],
        "updated_at": "later",
        "qty": True,
        "extended_hours": "no",
    }

    order = from_alpaca_order(payload, observed_at_ms=_OBSERVED)

    assert order.unreadable_fields == ("extended_hours", "qty", "updated_at")
    assert order.status == "new"


_B = "00000000-0000-0000-0000-00000000000b"


@pytest.mark.parametrize(("field", "attribute"), [("replaced_by", "replaced_by"), ("replaces", "replaces")])
def test_a_replacement_link_maps_a_uuid_and_reads_blank_as_no_link(
    load_alpaca_fixture: AlpacaFixtureLoader, field: str, attribute: str,
) -> None:
    open_order = load_alpaca_fixture("orders", "orders.json")[1]

    linked = from_alpaca_order({**open_order, field: f"  {_B} "}, observed_at_ms=_OBSERVED)
    blank = from_alpaca_order({**open_order, field: " "}, observed_at_ms=_OBSERVED)

    assert getattr(linked, attribute) == _B and linked.unreadable_fields == ()
    assert getattr(blank, attribute) is None and blank.unreadable_fields == ()


@pytest.mark.parametrize("value", ["not-a-uuid", 7, True, {"id": _B}])
@pytest.mark.parametrize("field", ["replaced_by", "replaces"])
def test_a_replacement_link_that_is_not_a_uuid_string_is_unreadable_and_never_followed(
    load_alpaca_fixture: AlpacaFixtureLoader, field: str, value: object,
) -> None:
    """A non-string is refused, never ``str()``-coerced into an order id to follow (#2656)."""
    payload = {**load_alpaca_fixture("orders", "orders.json")[1], "status": "replaced", field: value}

    order = from_alpaca_order(payload, observed_at_ms=_OBSERVED)

    assert getattr(order, field) is None
    assert order.unreadable_fields == (field,)
    assert order.status == "replaced"


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


async def test_broker_names_a_malformed_order_as_unavailable_evidence(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    """A row that is not even an object once escaped as a raw error (#2627).

    There is no order to contain under any id, so the answer stays unavailable
    evidence. A row with unreadable *values* is no longer this path: it maps
    degraded and the Clerk contains it alone (#2648).
    """
    open_order = load_alpaca_fixture("orders", "orders.json")[1]
    broker = AlpacaBroker(client=_OrdersClient([open_order, None]))  # type: ignore[arg-type]

    with pytest.raises(BrokerEvidenceUnavailable, match="order data this app could not read") as info:
        await broker.list_orders(status="open", limit=500)

    assert info.value.http_status == 503
    assert info.value.detail is not None
    assert TypeError.__name__ not in info.value.detail
    assert isinstance(info.value.__cause__, TypeError)


# One order's unreadable value -> the broker field the row names. A boolean
# never becomes a quantity (#2606) and ``"false"`` never becomes ``True``
# (#2643); a number too large for a float, a non-finite number and a
# negative share count are unreadable too, never a raw error or a value.
_UNREADABLE_ORDER_VALUE_SHAPES: dict[str, dict[str, object]] = {
    "boolean-fill-count": {"filled_qty": True},
    "unparseable-submitted-at": {"submitted_at": "yesterday"},
    "unparseable-created-at": {"created_at": "not-a-time"},
    "boolean-quantity": {"qty": False},
    "non-numeric-limit-price": {"limit_price": "one dollar"},
    "non-boolean-extended-hours": {"extended_hours": "false"},
    "overflowing-quantity": {"qty": 10**400},
    "nan-fill-count": {"filled_qty": "NaN"},
    "infinite-limit-price": {"limit_price": "Infinity"},
    "negative-quantity": {"qty": "-5"},
    "negative-fill-count": {"filled_qty": -1},
}


@pytest.mark.parametrize("shape", sorted(_UNREADABLE_ORDER_VALUE_SHAPES))
async def test_one_order_with_unreadable_values_maps_degraded_and_the_clerk_contains_it(
    load_alpaca_fixture: AlpacaFixtureLoader,
    shape: str,
) -> None:
    """One row a value of which will not parse once refused every other order in the answer (#2648).

    That refusal held the account stale with no reductions (#2363). The row
    now maps degraded -- the unreadable value absent and named, the broker's
    own status kept -- and the rest of the answer maps fully, exactly as
    missing text already does (#2643). The Clerk's refusal names the real
    cause, not a status the broker did send (#2648 review).
    """
    open_order = load_alpaca_fixture("orders", "orders.json")[1]
    update = _UNREADABLE_ORDER_VALUE_SHAPES[shape]
    [field] = update
    poisoned = {**open_order, "id": "bad-order", **update}
    broker = AlpacaBroker(client=_OrdersClient([poisoned, open_order]))  # type: ignore[arg-type]

    degraded, readable = await broker.list_orders(status="open", limit=500)

    assert degraded.order_id == "bad-order"
    assert degraded.unreadable_fields == (field,)
    assert degraded.status == open_order["status"]
    assert degraded.filled_quantity == 0.0
    assert degraded.extended_hours is False
    assert degraded.events == []
    assert readable.order_id == open_order["id"]
    assert readable.unreadable_fields == ()
    with pytest.raises(ExternalOrderObservationError, match=f"could not read: {field}$"):
        _observation_from_broker_order(degraded)


@pytest.mark.parametrize("flag", ["false", "true", 0, 1], ids=repr)
async def test_a_non_boolean_extended_hours_flag_maps_degraded_never_true(
    load_alpaca_fixture: AlpacaFixtureLoader,
    flag: object,
) -> None:
    """``bool("false")`` once read a regular-hours order as extended-hours (#2643).

    A non-boolean flag now reads absent and marks the row unreadable (#2648):
    never ``True``, and the Clerk contains the row by the field it names.
    """
    open_order = load_alpaca_fixture("orders", "orders.json")[1]
    broker = AlpacaBroker(client=_OrdersClient([{**open_order, "extended_hours": flag}]))  # type: ignore[arg-type]

    mapped = await broker.list_orders(status="open", limit=500)

    [degraded] = mapped
    assert degraded.extended_hours is False
    assert degraded.unreadable_fields == ("extended_hours",)


_ABSENT = object()

# One order's missing text -> the reason the Clerk's per-order containment
# gives. alpaca-py's ``Order`` omits a multi-leg parent's symbol and side and
# a leg's type; the rest are required there, but one bad order still must not
# refuse the whole orders answer (#2363).
_PER_ORDER_TEXT_PROBLEMS: dict[str, tuple[dict[str, object], str]] = {
    "multi-leg-parent-omitted": (
        {"order_class": "mleg", "symbol": _ABSENT, "side": _ABSENT},
        "symbol must be non-empty",
    ),
    "multi-leg-parent-null": ({"order_class": "mleg", "symbol": None, "side": None}, "symbol must be non-empty"),
    "typeless-leg": ({"order_type": _ABSENT, "type": _ABSENT}, "type must be non-empty"),
    "null-symbol-valid-side": ({"symbol": None, "side": "buy"}, "symbol must be non-empty"),
    "null-id": ({"id": None}, "broker order id must be non-empty"),
    "omitted-id": ({"id": _ABSENT}, "broker order id must be non-empty"),
    "null-status": ({"status": None}, "status must be non-empty"),
    "null-time-in-force": ({"time_in_force": None}, "time in force must be non-empty"),
}


@pytest.mark.parametrize("shape", sorted(_PER_ORDER_TEXT_PROBLEMS))
async def test_one_orders_missing_text_maps_blank_and_the_clerk_contains_that_order(
    load_alpaca_fixture: AlpacaFixtureLoader,
    shape: str,
) -> None:
    """Missing text once became "None" -- a ticker, side or type -- or refused every order (#2643)."""
    open_order = load_alpaca_fixture("orders", "orders.json")[1]
    update, reason = _PER_ORDER_TEXT_PROBLEMS[shape]
    bad = {
        key: value
        for key, value in {**open_order, "id": "bad-order", **update}.items()
        if value is not _ABSENT
    }
    broker = AlpacaBroker(client=_OrdersClient([bad, open_order]))  # type: ignore[arg-type]

    mapped, readable = await broker.list_orders(status="all", limit=500)

    assert readable.order_id == open_order["id"]
    text = (mapped.order_id, mapped.symbol, mapped.side, mapped.order_type, mapped.time_in_force, mapped.status)
    assert "None" not in text
    with pytest.raises(ExternalOrderObservationError, match=reason):
        _observation_from_broker_order(mapped)
