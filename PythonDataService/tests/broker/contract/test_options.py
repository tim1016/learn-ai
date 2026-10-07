"""The broker contract's option types: what an order may say, and which contracts a bot may trade."""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Context, Decimal, localcontext

import pytest
from pydantic import ValidationError

from app.broker.alpaca.clerk.sqlite.facts import leg_instruction_payload
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.contract.capabilities import BrokerCapabilities
from app.broker.contract.models import BrokerOrderLeg, OrderSide, OrderType
from app.broker.contract.options import (
    BrokerOptionAccountSnapshot,
    BrokerOptionContract,
    BrokerOptionDeliverable,
    BrokerOptionOrder,
    BrokerOptionOrderLeg,
    MultiLegOptionOrderRequest,
    OptionContractRefusal,
    OptionLegRequest,
    OptionPositionIntent,
    SingleOptionOrderRequest,
    closing_order,
    only_closes,
    opening_order,
    option_contract_refusal,
)
from app.options.contract import OptionContract
from app.options.requirement import LegQuote, natural_net_price
from app.options.structure import ResolvedLeg, ResolvedStructure

_EXPIRY = date(2026, 12, 18)


@pytest.mark.parametrize("order", [MultiLegOptionOrderRequest.model_construct(legs=()), object()])
def test_an_empty_or_unexpected_request_does_not_qualify_as_a_close(order: object) -> None:
    assert only_closes(order) is False  # type: ignore[arg-type]


def _contract(right: str, strike: str, *, root: str = "SPY", expiry: date = _EXPIRY) -> OptionContract:
    return OptionContract.of(root=root, expiry_date=expiry, right=right, strike=Decimal(strike))


def _leg(
    contract: OptionContract,
    side: OrderSide,
    *,
    intent: OptionPositionIntent = OptionPositionIntent.OPEN,
    ratio: int = 1,
) -> OptionLegRequest:
    return OptionLegRequest(contract=contract, side=side, intent=intent, ratio=ratio)


def _spread(**overrides: object) -> MultiLegOptionOrderRequest:
    order: dict[str, object] = {
        "legs": (_leg(_contract("call", "450"), OrderSide.BUY), _leg(_contract("call", "455"), OrderSide.SELL)),
        "quantity": 1,
        "order_type": OrderType.LIMIT,
        "net_limit_price": Decimal("1.94"),
    }
    order.update(overrides)
    return MultiLegOptionOrderRequest(**order)  # type: ignore[arg-type]


def _single(**overrides: object) -> SingleOptionOrderRequest:
    order: dict[str, object] = {
        "contract": _contract("call", "450"),
        "side": OrderSide.BUY,
        "intent": OptionPositionIntent.OPEN,
        "quantity": 1,
        "order_type": OrderType.LIMIT,
        "limit_price": Decimal("4.10"),
    }
    order.update(overrides)
    return SingleOptionOrderRequest(**order)  # type: ignore[arg-type]


def _resolved(position: str, right: str, strike: str, *, leg_id: str, ratio: int = 1) -> ResolvedLeg:
    return ResolvedLeg(leg_id=leg_id, contract=_contract(right, strike), position=position, ratio=ratio)


def _bought_call_spread() -> ResolvedStructure:
    return ResolvedStructure(
        legs=(_resolved("long", "call", "450", leg_id="bought"), _resolved("short", "call", "455", leg_id="sold"))
    )


def _sold_call_spread() -> ResolvedStructure:
    return ResolvedStructure(
        legs=(_resolved("short", "call", "450", leg_id="sold"), _resolved("long", "call", "455", leg_id="bought"))
    )


# ── Stock orders are what they were ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("leg", "stored_hash"),
    [
        (
            BrokerOrderLeg(symbol="SPY", side="buy", quantity=3),
            "478c3f7b33e99477b88e49df81417fb00bab55d12913b9e7803a68202cb17ceb",
        ),
        (
            BrokerOrderLeg(
                symbol="SPY", side="sell", quantity=2, order_type="limit", limit_price=240.5, time_in_force="gtc"
            ),
            "f2c1d998f54a166516fc555e61040aebb2a4aced738e8741da72106651c12283",
        ),
        (
            BrokerOrderLeg(
                symbol="SPY", side="buy", quantity=2, order_type="limit", limit_price=100.25, extended_hours=True
            ),
            "73b5057d4e599f75060e8a01bcee2aec16619d8d821520ff473a1ff2845691fe",
        ),
    ],
)
def test_a_stock_leg_hashes_as_it_did_before_the_contract_learned_options(
    leg: BrokerOrderLeg, stored_hash: str
) -> None:
    """Each hash was taken on master before the option types existed; a stored stock order must still match."""
    payload = canonicalize(leg_instruction_payload(leg))

    assert hashlib.sha256(payload.encode("utf-8")).hexdigest() == stored_hash


@pytest.mark.parametrize("root", ["F", "SPY", "GOOGL", "ABCDEF"])
def test_no_symbol_an_option_order_carries_is_taken_in_a_stock_leg(root: str) -> None:
    """Whatever the root's length, the symbol the option types write is still refused where a stock goes."""
    option_symbol = _contract("call", "450", root=root).occ_symbol

    with pytest.raises(ValidationError):
        BrokerOrderLeg(symbol=option_symbol, side="buy", quantity=1)


# ── What an option order may say ────────────────────────────────────────────


def test_a_multi_leg_order_carries_two_to_four_legs() -> None:
    one_leg = (_leg(_contract("call", "450"), OrderSide.BUY),)
    five_legs = tuple(_leg(_contract("call", strike), OrderSide.BUY) for strike in ("440", "445", "450", "455", "460"))

    for legs in (one_leg, five_legs):
        with pytest.raises(ValidationError):
            _spread(legs=legs)

    four = tuple(_leg(_contract("call", strike), OrderSide.BUY) for strike in ("440", "445", "450", "455"))
    assert len(_spread(legs=four).legs) == 4


def test_a_multi_leg_order_refuses_a_contract_in_two_legs() -> None:
    same = _contract("call", "450")

    with pytest.raises(ValidationError, match="each contract in one leg"):
        _spread(legs=(_leg(same, OrderSide.BUY), _leg(same, OrderSide.SELL)))


def test_a_multi_leg_order_refuses_legs_on_two_underlyings() -> None:
    with pytest.raises(ValidationError, match="one underlying"):
        _spread(
            legs=(_leg(_contract("call", "450"), OrderSide.BUY), _leg(_contract("call", "455", root="QQQ"), OrderSide.SELL))
        )


def test_leg_ratios_that_share_a_divisor_are_refused_as_alpaca_refuses_them() -> None:
    def ratios(first: int, second: int) -> tuple[OptionLegRequest, ...]:
        return (
            _leg(_contract("call", "450"), OrderSide.BUY, ratio=first),
            _leg(_contract("call", "455"), OrderSide.SELL, ratio=second),
        )

    with pytest.raises(ValidationError, match="no divisor above one"):
        _spread(legs=ratios(2, 4))

    assert [leg.ratio for leg in _spread(legs=ratios(1, 2)).legs] == [1, 2]


@pytest.mark.parametrize("quantity", [0, -1, 1.5, True, "2"])
def test_an_option_order_is_for_whole_contracts_or_structures(quantity: object) -> None:
    with pytest.raises(ValidationError):
        _single(quantity=quantity)
    with pytest.raises(ValidationError):
        _spread(quantity=quantity)


def test_a_price_is_an_exact_decimal_and_never_a_float() -> None:
    with pytest.raises(ValidationError):
        _single(limit_price=4.10)
    with pytest.raises(ValidationError):
        _spread(net_limit_price=1.94)


def test_a_limit_order_needs_its_price_and_a_market_order_carries_none() -> None:
    with pytest.raises(ValidationError, match="limit order requires"):
        _single(limit_price=None)
    with pytest.raises(ValidationError, match="market order has no price limit"):
        _single(order_type=OrderType.MARKET)
    with pytest.raises(ValidationError, match="limit order requires"):
        _spread(net_limit_price=None)
    with pytest.raises(ValidationError, match="market order has no price limit"):
        _spread(order_type=OrderType.MARKET)

    assert _single(order_type=OrderType.MARKET, limit_price=None).limit_price is None
    assert _spread(order_type=OrderType.MARKET, net_limit_price=None).net_limit_price is None


@pytest.mark.parametrize("price", [Decimal("0"), Decimal("-4.10")])
def test_a_single_options_limit_price_is_above_zero(price: Decimal) -> None:
    with pytest.raises(ValidationError, match="above zero"):
        _single(limit_price=price)


def test_a_net_price_may_be_a_debit_or_a_credit_and_never_zero() -> None:
    assert _spread(net_limit_price=Decimal("1.94")).net_limit_price == Decimal("1.94")
    assert _spread(net_limit_price=Decimal("-3.07")).net_limit_price == Decimal("-3.07")

    with pytest.raises(ValidationError, match="never zero"):
        _spread(net_limit_price=Decimal("0.00"))


def test_an_order_that_only_buys_cannot_claim_a_credit_nor_one_that_only_sells_a_debit() -> None:
    """A straddle's two bought legs cost money; a sign that says they pay us is a sign error, not an order."""
    bought = (_leg(_contract("call", "450"), OrderSide.BUY), _leg(_contract("put", "450"), OrderSide.BUY))
    sold = tuple(
        _leg(leg.contract, OrderSide.SELL, intent=OptionPositionIntent.CLOSE) for leg in bought
    )

    with pytest.raises(ValidationError, match="only buys"):
        _spread(legs=bought, net_limit_price=Decimal("-6.00"))
    with pytest.raises(ValidationError, match="only sells"):
        _spread(legs=sold, net_limit_price=Decimal("6.00"))

    assert _spread(legs=bought, net_limit_price=Decimal("6.00")).net_limit_price == Decimal("6.00")
    assert _spread(legs=sold, net_limit_price=Decimal("-6.00")).net_limit_price == Decimal("-6.00")


@pytest.mark.parametrize(
    "price", [Decimal("1E+20"), Decimal("-1E+20"), Decimal("1E-50"), Decimal("NaN"), Decimal("Infinity")]
)
def test_a_price_too_large_too_fine_or_not_a_number_is_refused(price: Decimal) -> None:
    with pytest.raises(ValidationError):
        _single(limit_price=price)
    with pytest.raises(ValidationError):
        _spread(net_limit_price=price)


@pytest.mark.parametrize("ratio", [0, -1, True, 1.0, "1"])
def test_a_legs_ratio_is_a_whole_number_one_or_more(ratio: object) -> None:
    """A ratio of zero would pass the divisor rule beside a ratio of one, and send a leg of no contracts."""
    with pytest.raises(ValidationError):
        _leg(_contract("call", "450"), OrderSide.BUY, ratio=ratio)  # type: ignore[arg-type]


def test_an_order_cannot_be_changed_once_it_is_built() -> None:
    single, spread = _single(), _spread()

    with pytest.raises(ValidationError):
        single.quantity = 5
    with pytest.raises(ValidationError):
        spread.net_limit_price = Decimal("-1.94")
    with pytest.raises(ValidationError):
        spread.legs[0].ratio = 2

    assert (single.quantity, spread.net_limit_price, spread.legs[0].ratio) == (1, Decimal("1.94"), 1)


@pytest.mark.parametrize("asked", [{"extended_hours": True}, {"time_in_force": "gtc"}, {"time_in_force": "day"}])
def test_extended_hours_and_a_time_in_force_cannot_be_asked_for(asked: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _single(**asked)
    with pytest.raises(ValidationError):
        _spread(**asked)


def test_a_stored_request_reads_back_as_the_order_it_was() -> None:
    for order in (_single(), _spread(net_limit_price=Decimal("-3.07"))):
        assert type(order).model_validate_json(order.model_dump_json()) == order


# ── An order built from a resolved structure ────────────────────────────────


def test_a_bought_spread_opens_as_one_limit_order_at_the_price_its_quotes_come_to() -> None:
    structure = _bought_call_spread()
    quotes = {
        "bought": LegQuote(Decimal("4.06"), Decimal("4.10")),
        "sold": LegQuote(Decimal("2.16"), Decimal("2.20")),
    }

    order = opening_order(structure, units=2, net_limit_price=natural_net_price(structure, quotes))

    assert isinstance(order, MultiLegOptionOrderRequest)
    assert order.order_type is OrderType.LIMIT
    assert order.quantity == 2
    # Paid: the bought leg's ask less the sold leg's bid.
    assert order.net_limit_price == Decimal("1.94")
    assert [(leg.contract.occ_symbol, leg.side, leg.intent, leg.ratio) for leg in order.legs] == [
        ("SPY261218C00450000", OrderSide.BUY, OptionPositionIntent.OPEN, 1),
        ("SPY261218C00455000", OrderSide.SELL, OptionPositionIntent.OPEN, 1),
    ]


def test_a_received_spread_opens_with_a_negative_net_price() -> None:
    structure = _sold_call_spread()
    quotes = {
        "sold": LegQuote(Decimal("5.20"), Decimal("5.26")),
        "bought": LegQuote(Decimal("2.09"), Decimal("2.13")),
    }

    order = opening_order(structure, units=1, net_limit_price=natural_net_price(structure, quotes))

    assert isinstance(order, MultiLegOptionOrderRequest)
    assert order.net_limit_price == Decimal("-3.07")
    assert [(leg.side, leg.intent) for leg in order.legs] == [
        (OrderSide.SELL, OptionPositionIntent.OPEN),
        (OrderSide.BUY, OptionPositionIntent.OPEN),
    ]


def test_an_order_keeps_each_legs_ratio_and_counts_structures() -> None:
    butterfly = ResolvedStructure(
        legs=(
            _resolved("long", "call", "445", leg_id="lower"),
            _resolved("short", "call", "450", leg_id="body", ratio=2),
            _resolved("long", "call", "455", leg_id="upper"),
        )
    )

    order = opening_order(butterfly, units=3, net_limit_price=Decimal("0.85"))

    assert isinstance(order, MultiLegOptionOrderRequest)
    assert order.quantity == 3
    assert [leg.ratio for leg in order.legs] == [1, 2, 1]


def test_a_structure_of_one_leg_is_a_plain_option_order_never_a_multi_leg_one() -> None:
    long_call = ResolvedStructure(legs=(_resolved("long", "call", "450", leg_id="call"),))

    opened = opening_order(long_call, units=2, net_limit_price=Decimal("4.10"))
    closed = closing_order(long_call, units=2)

    assert opened == SingleOptionOrderRequest(
        contract=_contract("call", "450"),
        side=OrderSide.BUY,
        intent=OptionPositionIntent.OPEN,
        quantity=2,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("4.10"),
    )
    assert closed == SingleOptionOrderRequest(
        contract=_contract("call", "450"),
        side=OrderSide.SELL,
        intent=OptionPositionIntent.CLOSE,
        quantity=2,
        order_type=OrderType.MARKET,
    )


def test_a_lone_sold_option_opens_at_the_price_it_receives() -> None:
    short_put = ResolvedStructure(legs=(_resolved("short", "put", "440", leg_id="put"),))

    order = opening_order(short_put, units=1, net_limit_price=Decimal("-2.50"))

    assert isinstance(order, SingleOptionOrderRequest)
    assert (order.side, order.limit_price) == (OrderSide.SELL, Decimal("2.50"))


def test_a_lone_sold_options_price_keeps_every_digit_whatever_the_decimal_context() -> None:
    """Unary minus rounds to the context in force; a caller working in two digits must not shorten the price."""
    short_put = ResolvedStructure(legs=(_resolved("short", "put", "440", leg_id="put"),))

    with localcontext(Context(prec=2)):
        order = opening_order(short_put, units=1, net_limit_price=Decimal("-12.3456"))

    assert isinstance(order, SingleOptionOrderRequest)
    assert order.limit_price == Decimal("12.3456")


def test_a_lone_leg_whose_price_has_the_wrong_sign_is_refused() -> None:
    long_call = ResolvedStructure(legs=(_resolved("long", "call", "450", leg_id="call"),))
    short_put = ResolvedStructure(legs=(_resolved("short", "put", "440", leg_id="put"),))

    with pytest.raises(ValueError, match="pays its premium"):
        opening_order(long_call, units=1, net_limit_price=Decimal("-4.10"))
    with pytest.raises(ValueError, match="receives its premium"):
        opening_order(short_put, units=1, net_limit_price=Decimal("2.50"))


def test_a_lone_leg_with_a_ratio_above_one_is_refused() -> None:
    doubled = ResolvedStructure(legs=(_resolved("long", "call", "450", leg_id="call", ratio=2),))

    with pytest.raises(ValueError, match="one contract a structure"):
        opening_order(doubled, units=1, net_limit_price=Decimal("4.10"))
    with pytest.raises(ValueError, match="one contract a structure"):
        closing_order(doubled, units=1)


def test_the_exit_is_one_market_order_that_reverses_and_closes_every_leg() -> None:
    order = closing_order(_sold_call_spread(), units=2)

    assert isinstance(order, MultiLegOptionOrderRequest)
    assert order.order_type is OrderType.MARKET
    assert order.net_limit_price is None
    assert order.quantity == 2
    assert [(leg.contract.occ_symbol, leg.side, leg.intent, leg.ratio) for leg in order.legs] == [
        ("SPY261218C00450000", OrderSide.BUY, OptionPositionIntent.CLOSE, 1),
        ("SPY261218C00455000", OrderSide.SELL, OptionPositionIntent.CLOSE, 1),
    ]


def test_the_exit_keeps_each_legs_ratio_so_no_contract_is_left_open() -> None:
    """A butterfly closed one-for-one would leave one of its two sold calls open and uncovered."""
    butterfly = ResolvedStructure(
        legs=(
            _resolved("long", "call", "445", leg_id="lower"),
            _resolved("short", "call", "450", leg_id="body", ratio=2),
            _resolved("long", "call", "455", leg_id="upper"),
        )
    )

    order = closing_order(butterfly, units=3)

    assert isinstance(order, MultiLegOptionOrderRequest)
    assert order.quantity == 3
    assert [(leg.contract.occ_symbol, leg.side, leg.intent, leg.ratio) for leg in order.legs] == [
        ("SPY261218C00445000", OrderSide.SELL, OptionPositionIntent.CLOSE, 1),
        ("SPY261218C00450000", OrderSide.BUY, OptionPositionIntent.CLOSE, 2),
        ("SPY261218C00455000", OrderSide.SELL, OptionPositionIntent.CLOSE, 1),
    ]


@pytest.mark.parametrize("units", [0, -1, 1.0, True])
def test_an_order_is_built_for_whole_structures(units: object) -> None:
    with pytest.raises(ValueError, match="whole number of structures"):
        opening_order(_bought_call_spread(), units=units, net_limit_price=Decimal("1.94"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="whole number of structures"):
        closing_order(_bought_call_spread(), units=units)  # type: ignore[arg-type]


# ── What a broker says back ─────────────────────────────────────────────────

_TIMES: dict[str, object] = {
    "submitted_at_ms": 1_700_000_000_000,
    "created_at_ms": 1_700_000_000_000,
    "updated_at_ms": 1_700_000_000_000,
    "filled_at_ms": None,
    "canceled_at_ms": None,
    "expired_at_ms": None,
}


def _reported_leg(order_id: str, strike: str, side: OrderSide, **overrides: object) -> BrokerOptionOrderLeg:
    leg: dict[str, object] = {
        "order_id": order_id,
        "client_order_id": None,
        "contract": _contract("call", strike),
        "side": side,
        "intent": OptionPositionIntent.OPEN,
        "ratio": 1,
        "quantity": 2,
        "filled_quantity": 0,
        "filled_avg_price": None,
        "status": "new",
        **_TIMES,
    }
    leg.update(overrides)
    return BrokerOptionOrderLeg(**leg)  # type: ignore[arg-type]


def _reported(**overrides: object) -> BrokerOptionOrder:
    order: dict[str, object] = {
        "broker": "alpaca",
        "order_id": "parent",
        "client_order_id": "learn-ai/spy-options/v1:enter-1",
        "order_type": "limit",
        "time_in_force": "day",
        "quantity": 2,
        "filled_quantity": 0,
        "limit_price": Decimal("1.94"),
        "filled_avg_price": None,
        "status": "new",
        **_TIMES,
        "observed_at_ms": 1_700_000_000_000,
    }
    order.update(overrides)
    return BrokerOptionOrder(**order)  # type: ignore[arg-type]


def _reported_spread(**overrides: object) -> BrokerOptionOrder:
    spread: dict[str, object] = {
        "multi_leg": True,
        "contract": None,
        "side": None,
        "intent": None,
        "legs": (_reported_leg("bought", "450", OrderSide.BUY), _reported_leg("sold", "455", OrderSide.SELL)),
    }
    spread.update(overrides)
    return _reported(**spread)


def _reported_single(**overrides: object) -> BrokerOptionOrder:
    single: dict[str, object] = {
        "multi_leg": False,
        "contract": _contract("call", "450"),
        "side": OrderSide.BUY,
        "intent": OptionPositionIntent.OPEN,
    }
    single.update(overrides)
    return _reported(**single)


def test_a_reported_spread_and_a_reported_single_order_are_read() -> None:
    spread, single = _reported_spread(), _reported_single()

    assert (spread.multi_leg, len(spread.legs), spread.contract) == (True, 2, None)
    assert (single.multi_leg, single.legs, single.side) == (False, (), OrderSide.BUY)


@pytest.mark.parametrize(
    "change",
    [
        {"quantity": True},
        {"quantity": "2"},
        {"quantity": 2.0},
        {"filled_quantity": "0"},
        {"limit_price": 0.1 + 0.2},
        {"limit_price": "1.94"},
        {"filled_avg_price": 1},
        {"side": "buy"},
    ],
)
def test_what_a_broker_reported_is_never_coerced_into_a_quantity_a_price_or_a_side(change: dict[str, object]) -> None:
    """A float price would carry the float's own digits: 0.1 + 0.2 is not thirty cents."""
    with pytest.raises(ValidationError):
        _reported_single(**change)


@pytest.mark.parametrize("field", ["ratio", "quantity"])
def test_a_reported_leg_states_its_ratio_and_its_quantity_or_says_it_has_none(field: str) -> None:
    """Left out is an adapter that forgot the field; ``None`` is the broker's answer not saying."""
    stated = _reported_leg("bought", "450", OrderSide.BUY, **{field: None})
    assert getattr(stated, field) is None

    fields = {**stated.model_dump(), "contract": stated.contract}
    assert BrokerOptionOrderLeg.model_validate(fields) == stated
    del fields[field]
    with pytest.raises(ValidationError):
        BrokerOptionOrderLeg.model_validate(fields)


def test_a_single_order_may_state_no_intent_and_a_multi_leg_parent_never_states_one() -> None:
    """Alpaca's one published single-leg answer carries no intent; a parent's intents are its legs'."""
    assert _reported_single(intent=None).intent is None
    assert _reported_single(intent=OptionPositionIntent.CLOSE).intent is OptionPositionIntent.CLOSE

    with pytest.raises(ValidationError, match="no contract, side or intent"):
        _reported_spread(intent=OptionPositionIntent.OPEN)


def _legs_on(*strikes: str) -> tuple[BrokerOptionOrderLeg, ...]:
    return tuple(_reported_leg(f"leg-{strike}", strike, OrderSide.BUY) for strike in strikes)


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"legs": _legs_on("450")}, id="one leg"),
        pytest.param({"legs": _legs_on("440", "445", "450", "455", "460")}, id="five legs"),
        pytest.param({"legs": ()}, id="no legs"),
        pytest.param({"contract": _contract("call", "450")}, id="a contract of its own"),
        pytest.param({"side": OrderSide.BUY}, id="a side of its own"),
        pytest.param(
            {"legs": (_reported_leg("parent", "450", OrderSide.BUY), _reported_leg("sold", "455", OrderSide.SELL))},
            id="a leg under the parent's order id",
        ),
        pytest.param(
            {"legs": (_reported_leg("same", "450", OrderSide.BUY), _reported_leg("same", "455", OrderSide.SELL))},
            id="two legs under one order id",
        ),
        pytest.param(
            {"legs": (_reported_leg("bought", "450", OrderSide.BUY), _reported_leg("sold", "450", OrderSide.SELL))},
            id="two legs on one contract",
        ),
        pytest.param(
            {
                "legs": (
                    _reported_leg("bought", "450", OrderSide.BUY),
                    _reported_leg("sold", "455", OrderSide.SELL, contract=_contract("call", "455", root="QQQ")),
                )
            },
            id="legs on two underlyings",
        ),
        pytest.param({"quantity": 0}, id="no structures"),
    ],
)
def test_a_reported_multi_leg_order_that_cannot_be_one_is_refused(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _reported_spread(**change)


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"legs": _legs_on("450", "455")}, id="legs"),
        pytest.param({"contract": None}, id="no contract"),
        pytest.param({"side": None}, id="no side"),
        pytest.param({"quantity": 0}, id="no contracts"),
    ],
)
def test_a_reported_single_order_that_cannot_be_one_is_refused(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _reported_single(**change)


def test_nothing_reported_has_filled_more_than_was_ordered() -> None:
    """True whatever a quantity counts, so a wrong guess about the units reads as no answer, not as a fill."""
    with pytest.raises(ValidationError, match="filled 3 of a quantity of 2"):
        _reported_single(filled_quantity=3)
    with pytest.raises(ValidationError, match="filled 7 of a quantity of 2"):
        _reported_spread(filled_quantity=7)
    with pytest.raises(ValidationError, match="filled 99 of a quantity of 3"):
        _reported_leg("bought", "450", OrderSide.BUY, quantity=3, filled_quantity=99)

    assert _reported_single(filled_quantity=2).filled_quantity == 2
    # A leg whose quantity the broker did not state has no quantity to exceed.
    assert _reported_leg("bought", "450", OrderSide.BUY, quantity=None, filled_quantity=5).filled_quantity == 5


def test_a_leg_is_not_held_to_its_ratio_times_the_parents_quantity() -> None:
    """Alpaca's published request and answer disagree on what a leg's quantity counts; it is read, not judged."""
    legs = (
        _reported_leg("bought", "450", OrderSide.BUY, ratio=3, quantity=3),
        _reported_leg("sold", "455", OrderSide.SELL, ratio=1, quantity=1),
    )

    assert [leg.quantity for leg in _reported_spread(quantity=3, legs=legs).legs] == [3, 1]


def test_only_a_multi_leg_net_price_may_be_below_zero() -> None:
    """A net price is negative when the structure receives. One contract's price never is."""
    received = _reported_spread(limit_price=Decimal("-3.07"), filled_avg_price=Decimal("-3.10"))
    assert (received.limit_price, received.filled_avg_price) == (Decimal("-3.07"), Decimal("-3.10"))

    with pytest.raises(ValidationError):
        _reported_single(limit_price=Decimal("-4.10"))
    with pytest.raises(ValidationError):
        _reported_single(filled_avg_price=Decimal("-4.10"))
    with pytest.raises(ValidationError):
        _reported_leg("bought", "450", OrderSide.BUY, filled_avg_price=Decimal("-0.43"))

    # Zero is not below zero.
    assert _reported_leg("bought", "450", OrderSide.BUY, filled_avg_price=Decimal("0")).filled_avg_price == 0


# ── The account's option fields ─────────────────────────────────────────────


def _option_account(*, approved: int | None, in_force: int | None) -> BrokerOptionAccountSnapshot:
    return BrokerOptionAccountSnapshot(
        broker="alpaca",
        account_id="PA0SANITIZED00001",
        account_mode="paper",
        options_buying_power=Decimal("100000"),
        options_approved_level=approved,
        options_trading_level=in_force,
        observed_at_ms=1_700_000_000_000,
    )


@pytest.mark.parametrize(("approved", "in_force"), [(0, 1), (1, 2), (2, 3), (0, 3)])
def test_a_trading_level_above_the_approved_level_is_refused(approved: int, in_force: int) -> None:
    """Alpaca's account reference on ``options_trading_level``, read 2026-10-07: "This is the minimum between
    account options_approved_level and account configurations max_options_trading_level." So an answer whose
    level in force is above the level approved contradicts itself, and is not read as approval for either."""
    with pytest.raises(ValidationError):
        _option_account(approved=approved, in_force=in_force)


@pytest.mark.parametrize(
    ("approved", "in_force"),
    [(3, 3), (3, 1), (2, 0), (0, 0), (None, 3), (1, None), (None, None)],
)
def test_a_trading_level_within_the_approved_level_or_beside_an_unknown_one_is_read(
    approved: int | None, in_force: int | None
) -> None:
    """A level the broker did not state stays unknown: there is nothing to hold the other one to."""
    account = _option_account(approved=approved, in_force=in_force)

    assert (account.options_approved_level, account.options_trading_level) == (approved, in_force)


# ── Which contracts a bot may trade ─────────────────────────────────────────


def _listing(contract: OptionContract, **overrides: object) -> BrokerOptionContract:
    listing: dict[str, object] = {
        "broker": "alpaca",
        "contract_id": "98359ef7-5124-49f3-85ea-5cf02df6defa",
        "symbol": contract.occ_symbol,
        "name": None,
        "status": "active",
        "tradable": True,
        "expiry_ms": contract.expiry_ms,
        "root_symbol": contract.root,
        "underlying_symbol": contract.root,
        "underlying_asset_id": None,
        "right": contract.right,
        "style": "american",
        "strike_price": contract.strike,
        "multiplier": Decimal("100"),
        "shares_per_contract": Decimal("100"),
        "penny_program": True,
        "deliverables": None,
        "observed_at_ms": 1_700_000_000_000,
    }
    listing.update(overrides)
    return BrokerOptionContract(**listing)  # type: ignore[arg-type]


def test_a_listed_active_tradable_american_100_share_contract_may_be_traded() -> None:
    contract = _contract("call", "450")

    assert option_contract_refusal(contract, _listing(contract)) is None


def test_a_contract_the_broker_does_not_list_is_refused_by_name() -> None:
    contract = _contract("call", "450")

    refusal = option_contract_refusal(contract, None)

    assert refusal is not None
    assert refusal.reason is OptionContractRefusal.NOT_LISTED
    assert refusal.symbol == "SPY261218C00450000"
    assert "SPY261218C00450000" in str(refusal)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"status": "inactive"}, OptionContractRefusal.NOT_ACTIVE),
        ({"tradable": False}, OptionContractRefusal.NOT_TRADABLE),
        ({"style": "european"}, OptionContractRefusal.NOT_AMERICAN),
        ({"multiplier": Decimal("10")}, OptionContractRefusal.NOT_STANDARD),
        ({"shares_per_contract": Decimal("150")}, OptionContractRefusal.NOT_STANDARD),
    ],
)
def test_a_listed_contract_a_bot_may_not_trade_is_refused_with_its_reason(
    change: dict[str, object], reason: OptionContractRefusal
) -> None:
    contract = _contract("put", "440")

    refusal = option_contract_refusal(contract, _listing(contract, **change))

    assert refusal is not None
    assert (refusal.reason, refusal.symbol) == (reason, contract.occ_symbol)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"status": "ACTIVE"}, OptionContractRefusal.NOT_ACTIVE),
        ({"status": "Active"}, OptionContractRefusal.NOT_ACTIVE),
        ({"style": "American"}, OptionContractRefusal.NOT_AMERICAN),
        ({"style": "AMERICAN"}, OptionContractRefusal.NOT_AMERICAN),
        (
            {"deliverables": (BrokerOptionDeliverable(kind="EQUITY", symbol="SPY", amount=Decimal("100")),)},
            OptionContractRefusal.NOT_STANDARD,
        ),
    ],
)
def test_a_word_the_brokers_reference_does_not_list_is_not_taken_for_the_one_it_resembles(
    change: dict[str, object], reason: OptionContractRefusal
) -> None:
    """Alpaca's reference lists ``active``, ``american`` and ``equity`` in lowercase and nothing else."""
    contract = _contract("call", "450")

    refusal = option_contract_refusal(contract, _listing(contract, **change))

    assert refusal is not None
    assert refusal.reason is reason


@pytest.mark.parametrize(
    "deliverables",
    [
        (),
        (BrokerOptionDeliverable(kind="equity", symbol="SPY", amount=Decimal("150")),),
        (BrokerOptionDeliverable(kind="equity", symbol="SPYX", amount=Decimal("100")),),
        (BrokerOptionDeliverable(kind="cash", symbol="SPY", amount=Decimal("100")),),
        (BrokerOptionDeliverable(kind="equity", symbol="SPY", amount=None),),
        (
            BrokerOptionDeliverable(kind="equity", symbol="SPY", amount=Decimal("100")),
            BrokerOptionDeliverable(kind="cash", symbol="USD", amount=Decimal("12.50")),
        ),
    ],
)
def test_a_contract_that_delivers_anything_but_100_shares_of_its_underlying_is_not_standard(
    deliverables: tuple[BrokerOptionDeliverable, ...],
) -> None:
    """An adjusted contract can keep a multiplier of 100 and deliver something else."""
    contract = _contract("call", "450")

    refusal = option_contract_refusal(contract, _listing(contract, deliverables=deliverables))

    assert refusal is not None
    assert refusal.reason is OptionContractRefusal.NOT_STANDARD


def test_a_contract_listed_as_delivering_100_shares_of_its_underlying_is_standard() -> None:
    contract = _contract("call", "450")
    standard = (BrokerOptionDeliverable(kind="equity", symbol="SPY", amount=Decimal("100")),)

    assert option_contract_refusal(contract, _listing(contract, deliverables=standard)) is None


@pytest.mark.parametrize(
    "change",
    [
        {"symbol": "SPY261218C00455000"},
        {"strike_price": Decimal("455")},
        {"right": "put"},
        {"expiry_ms": _contract("call", "450", expiry=date(2027, 1, 15)).expiry_ms},
        {"root_symbol": "SPYX"},
    ],
)
def test_an_answer_about_a_different_contract_is_refused(change: dict[str, object]) -> None:
    contract = _contract("call", "450")

    refusal = option_contract_refusal(contract, _listing(contract, **change))

    assert refusal is not None
    assert refusal.reason is OptionContractRefusal.NOT_THE_CONTRACT_ASKED_FOR


def test_a_listing_keeps_its_root_and_underlying_as_the_broker_wrote_them() -> None:
    """A share-class ticker's contracts are written without the dot; both spellings are carried, neither matched."""
    contract = _contract("call", "450", root="BRKB")

    listing = _listing(contract, underlying_symbol="BRK.B")

    assert (listing.root_symbol, listing.underlying_symbol) == ("BRKB", "BRK.B")
    assert option_contract_refusal(contract, listing) is None


# ── Capabilities ────────────────────────────────────────────────────────────


def _capabilities(**overrides: object) -> BrokerCapabilities:
    base: dict[str, object] = {
        "broker": "x",
        "paper_only": True,
        "supports_fractional": True,
        "supports_extended_hours": False,
        "supported_order_types": ("market", "limit"),
        "data_feed": "test",
        "bars_may_gap": False,
        "max_stream_symbols": 1,
        "max_concurrent_streams": 1,
        "rest_rate_limit_per_min": 1,
    }
    base.update(overrides)
    return BrokerCapabilities(**base)  # type: ignore[arg-type]


def test_a_broker_takes_no_option_orders_unless_it_says_so() -> None:
    silent = _capabilities()

    assert (silent.supports_option_orders, silent.supports_multi_leg_option_orders) == (False, False)


def test_a_broker_cannot_take_multi_leg_orders_without_taking_option_orders() -> None:
    with pytest.raises(ValidationError, match="supports_option_orders"):
        _capabilities(supports_multi_leg_option_orders=True)

    single_only = _capabilities(supports_option_orders=True)
    assert (single_only.supports_option_orders, single_only.supports_multi_leg_option_orders) == (True, False)
