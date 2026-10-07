"""The exact body an option order is sent as, and how Alpaca's answer is read.

The bodies and answers these are held to are Alpaca's own published examples
(``tests/fixtures/alpaca_documented/option_orders``): documentation, not
recordings. What they leave unshown is listed in that folder's attribution.
"""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

import pytest
from alpaca.trading.enums import OrderClass, PositionIntent
from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest

from app.broker.alpaca.adapter import (
    alpaca_multi_leg_order_lacks_legs,
    from_alpaca_option_order,
    rfc3339_to_ms,
    to_alpaca_option_order_request,
)
from app.broker.contract.models import BrokerOrderLeg, OrderSide, OrderType
from app.broker.contract.options import (
    MultiLegOptionOrderRequest,
    OptionOrderRequest,
    OptionPositionIntent,
    SingleOptionOrderRequest,
)
from app.broker.contract.options import (
    OptionLegRequest as Leg,
)
from app.options.contract import OptionContract
from tests.broker.alpaca.documented_examples import documented
from tests.broker.alpaca.test_schema_drift import _known_names

_REF = "learn-ai/spy-options/v1:enter-1"
_OBSERVED = 1_700_000_000_000

_BUY, _SELL = OrderSide.BUY, OrderSide.SELL
_OPEN, _CLOSE = OptionPositionIntent.OPEN, OptionPositionIntent.CLOSE


def _single(
    symbol: str, side: OrderSide, quantity: int, *, limit: str | None = None, intent: OptionPositionIntent = _OPEN
) -> SingleOptionOrderRequest:
    return SingleOptionOrderRequest(
        contract=OptionContract.from_occ_symbol(symbol),
        side=side,
        intent=intent,
        quantity=quantity,
        order_type=OrderType.MARKET if limit is None else OrderType.LIMIT,
        limit_price=None if limit is None else Decimal(limit),
    )


def _multi(
    *legs: tuple[str, OrderSide, OptionPositionIntent], limit: str | None, quantity: int = 1, ratios: tuple[int, ...] = ()
) -> MultiLegOptionOrderRequest:
    return MultiLegOptionOrderRequest(
        legs=tuple(
            Leg(
                contract=OptionContract.from_occ_symbol(symbol),
                side=side,
                intent=intent,
                ratio=ratios[index] if ratios else 1,
            )
            for index, (symbol, side, intent) in enumerate(legs)
        ),
        quantity=quantity,
        order_type=OrderType.MARKET if limit is None else OrderType.LIMIT,
        net_limit_price=None if limit is None else Decimal(limit),
    )


# Each order Alpaca documents, as this repo's contract states it, with the
# ``position_intent`` Alpaca's single-leg examples leave out.
_DOCUMENTED_SINGLE_LEG: dict[str, tuple[SingleOptionOrderRequest, str]] = {
    "sell_a_covered_call": (_single("AAPL231201C00195000", _SELL, 2, limit="1.05"), "sell_to_open"),
    "sell_a_cash_secured_put": (_single("AAPL231201P00175000", _SELL, 1), "sell_to_open"),
    "buy_a_call": (_single("AAPL240119C00190000", _BUY, 1), "buy_to_open"),
    "buy_a_put": (_single("AAPL240119P00170000", _BUY, 1), "buy_to_open"),
}

_DOCUMENTED_MULTI_LEG: dict[str, MultiLegOptionOrderRequest] = {
    "how_to_submit_a_multi_leg_order": _multi(
        ("AAPL250117P00200000", _BUY, _OPEN), ("AAPL250117C00250000", _BUY, _OPEN), limit="0.6"
    ),
    "long_call_spread": _multi(
        ("AAPL250117C00190000", _BUY, _OPEN), ("AAPL250117C00210000", _SELL, _OPEN), limit="1.00"
    ),
    "long_put_spread": _multi(
        ("AAPL250117P00210000", _BUY, _OPEN), ("AAPL250117P00190000", _SELL, _OPEN), limit="1.25"
    ),
    "iron_condor": _multi(
        ("AAPL250117P00190000", _BUY, _OPEN),
        ("AAPL250117P00195000", _SELL, _OPEN),
        ("AAPL250117C00205000", _SELL, _OPEN),
        ("AAPL250117C00210000", _BUY, _OPEN),
        limit="1.80",
    ),
    "roll_a_call_spread_strike_price": _multi(
        ("AAPL250117C00200000", _BUY, _CLOSE),
        ("AAPL250117C00205000", _SELL, _CLOSE),
        ("AAPL250117C00210000", _SELL, _OPEN),
        ("AAPL250117C00215000", _BUY, _OPEN),
        limit="2.05",
    ),
    "roll_a_call_spread_expiration_date": _multi(
        ("AAPL250117C00200000", _BUY, _CLOSE),
        ("AAPL250117C00205000", _SELL, _CLOSE),
        ("AAPL250124C00200000", _SELL, _OPEN),
        ("AAPL250124C00205000", _BUY, _OPEN),
        limit="2.05",
    ),
}


def _requests(kind: str) -> dict[str, dict[str, Any]]:
    return documented("option_orders", "documented_requests.json")[kind]


def _responses(name: str) -> dict[str, Any]:
    return copy.deepcopy(documented("option_orders", "documented_responses.json")[name])


# ── The body sent ───────────────────────────────────────────────────────────


def test_every_documented_example_is_matched() -> None:
    """A new example on Alpaca's pages must be matched here, not passed over."""
    assert set(_requests("single_leg")) == set(_DOCUMENTED_SINGLE_LEG)
    assert set(_requests("multi_leg")) == set(_DOCUMENTED_MULTI_LEG)


@pytest.mark.parametrize("example", sorted(_DOCUMENTED_SINGLE_LEG))
def test_a_single_option_order_is_sent_as_alpaca_documents_it_with_its_intent(example: str) -> None:
    order, position_intent = _DOCUMENTED_SINGLE_LEG[example]

    body = to_alpaca_option_order_request(order, client_order_id=_REF)

    assert body == {**_requests("single_leg")[example], "position_intent": position_intent, "client_order_id": _REF}


@pytest.mark.parametrize("example", sorted(_DOCUMENTED_MULTI_LEG))
def test_a_multi_leg_order_is_sent_exactly_as_alpaca_documents_it(example: str) -> None:
    body = to_alpaca_option_order_request(_DOCUMENTED_MULTI_LEG[example], client_order_id=_REF)

    assert body == {**_requests("multi_leg")[example], "client_order_id": _REF}


def test_a_paid_spread_is_sent_with_a_positive_net_price() -> None:
    order = _multi(("SPY261218C00450000", _BUY, _OPEN), ("SPY261218C00455000", _SELL, _OPEN), limit="1.94", quantity=2)

    body = to_alpaca_option_order_request(order, client_order_id=_REF)

    assert body == {
        "order_class": "mleg",
        "qty": "2",
        "type": "limit",
        "limit_price": "1.94",
        "time_in_force": "day",
        "client_order_id": _REF,
        "legs": [
            {"symbol": "SPY261218C00450000", "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_open"},
            {"symbol": "SPY261218C00455000", "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"},
        ],
    }


def test_a_received_spread_is_sent_with_a_negative_net_price() -> None:
    """Alpaca reads a negative multi-leg limit price as a credit. No page shows such a body; the sign is its rule."""
    order = _multi(("SPY261218C00450000", _SELL, _OPEN), ("SPY261218C00455000", _BUY, _OPEN), limit="-3.07")

    body = to_alpaca_option_order_request(order, client_order_id=_REF)

    assert body == {
        "order_class": "mleg",
        "qty": "1",
        "type": "limit",
        "limit_price": "-3.07",
        "time_in_force": "day",
        "client_order_id": _REF,
        "legs": [
            {"symbol": "SPY261218C00450000", "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"},
            {"symbol": "SPY261218C00455000", "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_open"},
        ],
    }


def test_a_multi_leg_market_exit_carries_no_price_and_closes_every_leg() -> None:
    order = _multi(
        ("SPY261218C00450000", _BUY, _CLOSE), ("SPY261218C00455000", _SELL, _CLOSE), limit=None, quantity=3
    )

    body = to_alpaca_option_order_request(order, client_order_id=_REF)

    assert body == {
        "order_class": "mleg",
        "qty": "3",
        "type": "market",
        "time_in_force": "day",
        "client_order_id": _REF,
        "legs": [
            {"symbol": "SPY261218C00450000", "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_close"},
            {"symbol": "SPY261218C00455000", "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_close"},
        ],
    }


def test_each_legs_ratio_is_sent_beside_the_number_of_structures() -> None:
    butterfly = _multi(
        ("SPY261218C00445000", _BUY, _OPEN),
        ("SPY261218C00450000", _SELL, _OPEN),
        ("SPY261218C00455000", _BUY, _OPEN),
        limit="0.85",
        quantity=4,
        ratios=(1, 2, 1),
    )

    body = to_alpaca_option_order_request(butterfly, client_order_id=_REF)

    assert body["qty"] == "4"
    assert [leg["ratio_qty"] for leg in body["legs"]] == ["1", "2", "1"]


def test_a_single_leg_exit_is_a_plain_market_order_with_a_closing_intent() -> None:
    order = _single("SPY261218C00450000", _SELL, 2, intent=_CLOSE)

    body = to_alpaca_option_order_request(order, client_order_id=_REF)

    assert body == {
        "symbol": "SPY261218C00450000",
        "qty": "2",
        "side": "sell",
        "position_intent": "sell_to_close",
        "type": "market",
        "time_in_force": "day",
        "client_order_id": _REF,
    }


@pytest.mark.parametrize(
    "order",
    [
        _single("SPY261218C00450000", _BUY, 1, limit="4.10"),
        _single("SPY261218C00450000", _SELL, 1, intent=_CLOSE),
        *_DOCUMENTED_MULTI_LEG.values(),
    ],
)
def test_no_option_order_asks_for_extended_hours_or_any_time_in_force_but_day(order: OptionOrderRequest) -> None:
    body = to_alpaca_option_order_request(order, client_order_id=_REF)

    assert body["time_in_force"] == "day"
    assert "extended_hours" not in body


def test_a_price_is_sent_in_plain_digits_whatever_its_exponent() -> None:
    single = to_alpaca_option_order_request(
        SingleOptionOrderRequest(
            contract=OptionContract.from_occ_symbol("SPY261218C00450000"),
            side=_BUY,
            intent=_OPEN,
            quantity=1,
            order_type=OrderType.LIMIT,
            limit_price=Decimal("1E+2"),
        ),
        client_order_id=_REF,
    )

    assert single["limit_price"] == "100"


@pytest.mark.parametrize("client_order_id", ["", "   "])
def test_an_option_order_is_never_written_out_without_its_callers_client_order_id(client_order_id: str) -> None:
    """Alpaca makes an id up for an order sent without one, and no lookup of ours could then find the order."""
    with pytest.raises(ValueError, match="client order id"):
        to_alpaca_option_order_request(_DOCUMENTED_MULTI_LEG["long_call_spread"], client_order_id=client_order_id)


def test_a_stock_leg_is_never_written_out_as_an_option_order() -> None:
    stock_leg = BrokerOrderLeg(symbol="SPY", side="buy", quantity=1)

    with pytest.raises(TypeError, match="not BrokerOrderLeg"):
        to_alpaca_option_order_request(stock_leg, client_order_id=_REF)  # type: ignore[arg-type]


def test_every_key_and_word_sent_is_one_alpacas_sdk_defines() -> None:
    """The write-path drift guard: a renamed field or intent in a later alpaca-py surfaces here by name."""
    single = to_alpaca_option_order_request(_single("SPY261218C00450000", _BUY, 1, limit="4.10"), client_order_id=_REF)
    multi = to_alpaca_option_order_request(_DOCUMENTED_MULTI_LEG["iron_condor"], client_order_id=_REF)

    assert set(single) - _known_names(LimitOrderRequest) == set()
    assert set(multi) - _known_names(LimitOrderRequest) == set()
    assert {key for leg in multi["legs"] for key in leg} - _known_names(OptionLegRequest) == set()
    assert multi["order_class"] == OrderClass.MLEG.value
    sent_intents = {leg["position_intent"] for example in _DOCUMENTED_MULTI_LEG.values() for leg in to_alpaca_option_order_request(example, client_order_id=_REF)["legs"]}
    assert sent_intents == {intent.value for intent in PositionIntent}


# ── The answer read ─────────────────────────────────────────────────────────


def test_a_multi_leg_answer_is_a_parent_with_its_legs_nested() -> None:
    payload = _responses("MultilegOptionsOrderResponse")

    order = from_alpaca_option_order(payload, observed_at_ms=_OBSERVED)

    assert order.broker == "alpaca"
    assert order.multi_leg is True
    assert order.order_id == "83f37e9f-6b1f-49ed-8fc6-3e6af716323f"
    assert order.client_order_id == "646b1fe6-b212-4f54-94c6-429e7bcdee04"
    # The parent names no contract and no side: Alpaca writes both blank.
    assert (order.contract, order.side, order.intent) == (None, None, None)
    assert (order.order_type, order.time_in_force, order.status) == ("limit", "day", "filled")
    # Structures, and a net price a share.
    assert (order.quantity, order.filled_quantity) == (1, 1)
    assert (order.limit_price, order.filled_avg_price) == (Decimal("10"), Decimal("1.28"))
    assert order.submitted_at_ms == rfc3339_to_ms("2024-12-10T16:15:53.684952901Z")
    assert order.filled_at_ms == rfc3339_to_ms("2024-12-10T16:15:53.694Z")
    assert (order.canceled_at_ms, order.expired_at_ms) == (None, None)
    assert order.observed_at_ms == _OBSERVED


def test_each_leg_has_its_own_order_id_filled_quantity_average_price_and_status() -> None:
    order = from_alpaca_option_order(_responses("MultilegOptionsOrderResponse"), observed_at_ms=_OBSERVED)

    bought, sold = order.legs

    assert bought.order_id == "df4ff24a-c58a-4e37-8b9f-ef32b83a11f2"
    assert bought.contract == OptionContract.from_occ_symbol("AAPL241213C00250000")
    assert (bought.side, bought.intent) == (OrderSide.BUY, OptionPositionIntent.OPEN)
    # Contracts: three a structure, one structure.
    assert (bought.ratio, bought.quantity, bought.filled_quantity) == (3, 3, 3)
    assert (bought.filled_avg_price, bought.status) == (Decimal("0.43"), "filled")

    assert sold.order_id == "ecd91110-c34d-4e9d-a7bf-a9c27c40f8b5"
    assert sold.contract == OptionContract.from_occ_symbol("AAPL241213C00260000")
    assert (sold.side, sold.intent) == (OrderSide.SELL, OptionPositionIntent.OPEN)
    assert (sold.ratio, sold.quantity, sold.filled_quantity) == (1, 1, 1)
    assert (sold.filled_avg_price, sold.status) == (Decimal("0.01"), "filled")
    assert sold.updated_at_ms == rfc3339_to_ms("2024-12-10T16:15:53.708983759Z")

    # No leg borrows the parent's identity, or another leg's.
    assert len({order.order_id, bought.order_id, sold.order_id}) == 3
    assert len({order.client_order_id, bought.client_order_id, sold.client_order_id}) == 3


def test_the_documented_fill_is_the_legs_prices_netted_by_ratio() -> None:
    """Alpaca's own example agrees with the sign rule: three bought at 0.43 less one sold at 0.01 is 1.28 paid."""
    order = from_alpaca_option_order(_responses("MultilegOptionsOrderResponse"), observed_at_ms=_OBSERVED)

    netted = sum(
        (1 if leg.side is OrderSide.BUY else -1) * leg.ratio * leg.filled_avg_price  # type: ignore[operator]
        for leg in order.legs
    )

    assert netted == order.filled_avg_price == Decimal("1.28")


def test_a_single_option_answer_carries_its_own_contract_and_no_legs() -> None:
    order = from_alpaca_option_order(_responses("OptionOrderResponse"), observed_at_ms=_OBSERVED)

    assert order.multi_leg is False
    assert order.legs == ()
    assert order.order_id == "30a077fa-96f6-4f20-a052-4b921ee2f243"
    assert order.client_order_id == "58cd43a7-029e-457e-b77f-cd4f61f00f2a"
    assert order.contract == OptionContract.from_occ_symbol("AAPL250620C00100000")
    assert order.side is OrderSide.BUY
    # Alpaca's example answer states no intent, and none is supplied for it.
    assert order.intent is None
    assert (order.order_type, order.time_in_force, order.status) == ("limit", "day", "pending_new")
    assert (order.quantity, order.filled_quantity) == (2, 0)
    assert (order.limit_price, order.filled_avg_price) == (Decimal("10"), None)
    assert order.filled_at_ms is None


def test_a_credit_keeps_its_sign_when_read_back() -> None:
    """No page shows a credit's answer. This holds only that the reader drops no sign Alpaca might write."""
    payload = _responses("MultilegOptionsOrderResponse")
    payload["limit_price"] = "-3.07"
    payload["filled_avg_price"] = "-3.10"

    order = from_alpaca_option_order(payload, observed_at_ms=_OBSERVED)

    assert (order.limit_price, order.filled_avg_price) == (Decimal("-3.07"), Decimal("-3.10"))


def test_a_closing_intent_is_read_from_a_single_order_that_states_it() -> None:
    payload = _responses("OptionOrderResponse")
    payload.update(side="sell", position_intent="sell_to_close")

    order = from_alpaca_option_order(payload, observed_at_ms=_OBSERVED)

    assert (order.side, order.intent) == (OrderSide.SELL, OptionPositionIntent.CLOSE)


def test_a_plain_orders_class_may_be_blank_or_left_out() -> None:
    for order_class in ("", None):
        payload = _responses("OptionOrderResponse")
        payload["order_class"] = order_class
        assert from_alpaca_option_order(payload, observed_at_ms=_OBSERVED).multi_leg is False

    unstated = _responses("OptionOrderResponse")
    del unstated["order_class"]
    assert from_alpaca_option_order(unstated, observed_at_ms=_OBSERVED).multi_leg is False


def _multi_leg_with(**changes: Any) -> dict[str, Any]:
    payload = _responses("MultilegOptionsOrderResponse")
    payload.update(changes)
    return payload


def _first_leg_with(**changes: Any) -> dict[str, Any]:
    payload = _responses("MultilegOptionsOrderResponse")
    payload["legs"][0].update(changes)
    return payload


def _first_leg_without(field: str) -> dict[str, Any]:
    payload = _responses("MultilegOptionsOrderResponse")
    del payload["legs"][0][field]
    return payload


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(_multi_leg_with(filled_qty=None), id="parent filled quantity missing"),
        pytest.param(_multi_leg_with(filled_qty="one"), id="parent filled quantity not a number"),
        pytest.param(_multi_leg_with(filled_qty=True), id="parent filled quantity a boolean"),
        pytest.param(_multi_leg_with(qty="1.5"), id="a fraction of a structure"),
        pytest.param(_multi_leg_with(qty="-1"), id="a negative quantity"),
        pytest.param(_multi_leg_with(filled_avg_price="NaN"), id="a price that is not a number"),
        pytest.param(_multi_leg_with(status=""), id="no status"),
        pytest.param(_multi_leg_with(id=None), id="no order id"),
        pytest.param(_multi_leg_with(filled_at="yesterday"), id="a time that will not parse"),
        pytest.param(_multi_leg_with(legs=None), id="a multi-leg parent without its legs"),
        pytest.param(_multi_leg_with(legs=[]), id="a multi-leg parent with an empty leg list"),
        pytest.param(_multi_leg_with(order_class="bracket"), id="an order class that is not an option order's"),
        pytest.param(_first_leg_without("filled_qty"), id="a leg with no filled quantity"),
        pytest.param(_first_leg_with(filled_qty="2.5"), id="a leg filled in a fraction of a contract"),
        pytest.param(_first_leg_with(id=""), id="a leg with no order id"),
        pytest.param(_first_leg_with(symbol="AAPL"), id="a leg on a stock"),
        pytest.param(_first_leg_with(side=""), id="a leg with no side"),
        pytest.param(_first_leg_with(position_intent="sell_to_open"), id="a leg whose intent names the other side"),
        pytest.param(_first_leg_with(position_intent="hold"), id="a leg whose intent is no intent"),
        pytest.param(_first_leg_with(status=None), id="a leg with no status"),
        pytest.param(_first_leg_with(ratio_qty="0"), id="a leg with a ratio of zero"),
        pytest.param(
            _first_leg_with(id="83f37e9f-6b1f-49ed-8fc6-3e6af716323f"), id="a leg carrying the parent's order id"
        ),
        pytest.param([], id="an answer that is not an object"),
    ],
)
def test_an_answer_that_cannot_be_read_whole_is_refused_and_states_no_outcome(payload: Any) -> None:
    """A filled quantity that will not parse must not become zero contracts filled."""
    with pytest.raises((TypeError, ValueError)):
        from_alpaca_option_order(payload, observed_at_ms=_OBSERVED)


def test_a_stock_order_is_never_read_as_an_option_order(load_alpaca_fixture: Any) -> None:
    """The real captured stock orders: looked up by mistake, each is refused, not read as an option fill."""
    for stock_order in load_alpaca_fixture("orders", "orders.json"):
        with pytest.raises(ValueError, match="not an option contract symbol"):
            from_alpaca_option_order(stock_order, observed_at_ms=_OBSERVED)


def test_a_single_order_that_comes_back_with_legs_is_refused() -> None:
    payload = _responses("OptionOrderResponse")
    payload["legs"] = _responses("MultilegOptionsOrderResponse")["legs"]

    with pytest.raises(ValueError, match="came with legs"):
        from_alpaca_option_order(payload, observed_at_ms=_OBSERVED)


def test_a_multi_leg_parent_without_its_legs_is_told_apart_from_every_other_answer() -> None:
    nested = _responses("MultilegOptionsOrderResponse")

    assert alpaca_multi_leg_order_lacks_legs(nested) is False
    assert alpaca_multi_leg_order_lacks_legs({**nested, "legs": None}) is True
    assert alpaca_multi_leg_order_lacks_legs({**nested, "legs": []}) is True
    # A single order has no legs to lack.
    assert alpaca_multi_leg_order_lacks_legs(_responses("OptionOrderResponse")) is False
    assert alpaca_multi_leg_order_lacks_legs(None) is False
