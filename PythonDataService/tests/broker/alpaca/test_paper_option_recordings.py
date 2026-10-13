"""What a real Alpaca Paper account answered to one bought SPY call, read by this repo's own readers (#3103).

The recordings (``tests/fixtures/alpaca/option_paper_recordings``) are the
capture journal of the first option orders ever sent from this system, made on
2026-10-13 under the qualification runbook and sanitised by the kit. They sit
beside the documented examples and replace none of them: each shows one
account on one day, for **one bought contract, filled inside its wait**. A
spread, a credit, a resting or part-filled order, a cancel, an exercise and a
regulatory fee are not in them.

What these tests prove is that what Alpaca really answered is read whole by
the readers the Clerk uses, and that the body the Clerk sent is the body the
writer produces. They do not say the Clerk's books agree: that is the bundle
and its offline judgment.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.adapter import (
    execution_id_from_activity_id,
    from_alpaca_activity,
    from_alpaca_option_account,
    from_alpaca_option_contract,
    from_alpaca_option_order,
    from_alpaca_position,
    from_alpaca_trade_update,
    to_alpaca_option_order_request,
)
from app.broker.contract.models import OrderSide, OrderType
from app.broker.contract.options import OptionPositionIntent, SingleOptionOrderRequest
from app.options.contract import OptionContract
from app.options.expiry import option_expiry_anchor_ms

_RECORDINGS = Path(__file__).resolve().parents[2] / "fixtures" / "alpaca" / "option_paper_recordings"
_SYMBOL = "SPY261016C00778000"

# step -> (the limit price each entry was sent at, the price each order filled at as Alpaca states it).
# Entries and exits alternate, entry first, in both.
_STEPS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "A1-long-call-flat": (("3.47",), ("3.47", "3.4")),
    "A2-long-call-flat-repeat": (("3.34", "3.46", "3.60"), ("3.34", "3.45", "3.46", "3.41", "3.6", "3.67")),
}
_STEP = pytest.mark.parametrize("step", sorted(_STEPS))


def _lines(step: str, family: str) -> list[dict[str, Any]]:
    path = _RECORDINGS / step / f"{family}.jsonl"
    return [json.loads(text) for text in path.read_text(encoding="utf-8").splitlines()]


def _body(line: dict[str, Any]) -> Any:
    return json.loads(line["raw_body"])


def _frames(step: str) -> list[dict[str, Any]]:
    return [_body(line)["data"] for line in _lines(step, "stream")]


@_STEP
def test_each_order_the_bot_sent_is_the_body_the_writer_produces_and_its_answer_reads_as_sent(step: str) -> None:
    entry_prices, _ = _STEPS[step]
    sent_and_answered = [
        (json.loads(line["request_body"]), from_alpaca_option_order(_body(line)))
        for line in _lines(step, "orders")
        if line["method"] == "POST"
    ]

    terms = []
    for sent, answer in sent_and_answered:
        request = SingleOptionOrderRequest(
            contract=OptionContract.from_occ_symbol(sent["symbol"]),
            side=OrderSide(sent["side"]),
            intent=OptionPositionIntent.OPEN if sent["position_intent"].endswith("_to_open") else OptionPositionIntent.CLOSE,
            quantity=int(sent["qty"]),
            order_type=OrderType(sent["type"]),
            limit_price=Decimal(sent["limit_price"]) if "limit_price" in sent else None,
        )
        assert to_alpaca_option_order_request(request, client_order_id=sent["client_order_id"]) == sent
        # Alpaca answers an order just sent as accepted and nothing filled, with the terms it was sent under.
        assert (answer.status, answer.filled_quantity, answer.time_in_force) == ("pending_new", 0, "day")
        assert answer.contract is not None and answer.contract.occ_symbol == sent["symbol"]
        assert (answer.side, answer.intent, answer.quantity) == (request.side, request.intent, request.quantity)
        assert answer.limit_price == request.limit_price
        terms.append((sent["position_intent"], sent["type"], sent.get("limit_price")))

    # Every entry is a limit buy to open at the price the Clerk chose; every exit a market sell to close.
    assert terms == [
        pair
        for price in entry_prices
        for pair in (("buy_to_open", "limit", price), ("sell_to_close", "market", None))
    ]


@_STEP
def test_each_order_goes_pending_new_then_new_then_a_fill_at_the_price_its_own_order_states(step: str) -> None:
    _, filled_at = _STEPS[step]
    events_by_order: dict[str, list[str]] = {}
    fills: list[tuple[OrderSide, Decimal, str]] = []

    for data in _frames(step):
        event = from_alpaca_trade_update(data)
        order = from_alpaca_option_order(data["order"])
        events_by_order.setdefault(order.order_id, []).append(event.event_type)
        if event.event_type == "fill":
            assert order.status == "filled" and order.filled_quantity == order.quantity == 1
            # The execution's price is the order's own average: one contract, one fill.
            assert order.filled_avg_price == Decimal(str(event.price))
            assert event.execution_id is not None
            assert order.side is not None
            fills.append((order.side, order.filled_avg_price, order.order_id))

    assert set(map(tuple, events_by_order.values())) == {("pending_new", "new", "fill")}
    assert [(side, str(price)) for side, price, _ in fills] == [
        (OrderSide.BUY if number % 2 == 0 else OrderSide.SELL, price) for number, price in enumerate(filled_at)
    ]


@_STEP
def test_each_fill_on_the_stream_is_in_the_activity_feed_under_the_same_execution_and_order(step: str) -> None:
    rows: dict[str, Any] = {}
    for line in _lines(step, "activities"):
        for row in _body(line):
            if row.get("symbol") == _SYMBOL and row["activity_type"] == "FILL":
                rows[row["id"]] = row
    in_the_feed = {
        (execution_id_from_activity_id(activity.activity_id), activity.native_order_id, activity.price, activity.quantity)
        for activity in map(from_alpaca_activity, rows.values())
    }

    on_the_stream = {
        (data["execution_id"], data["order"]["id"], float(data["price"]), float(data["qty"]))
        for data in _frames(step)
        if data["event"] == "fill"
    }

    # The feed is the whole account's day: a later step also lists the earlier step's fills.
    assert on_the_stream and on_the_stream <= in_the_feed


@_STEP
def test_the_contract_the_account_and_the_position_read_as_a_standard_call_held_and_then_flat(step: str) -> None:
    contract = from_alpaca_option_contract(_body(_lines(step, "option_contracts")[0]))
    account = from_alpaca_option_account(_body(_lines(step, "account")[0]), account_mode="paper")
    held = [from_alpaca_position(position) for line in _lines(step, "positions") for position in _body(line)]
    polls_flat = [not _body(line) for line in _lines(step, "positions")]

    assert (contract.symbol, contract.tradable, contract.status) == (_SYMBOL, True, "active")
    assert (contract.right, contract.strike_price, contract.multiplier) == ("call", Decimal("778"), Decimal("100"))
    assert [(d.kind, d.symbol, d.amount) for d in contract.deliverables] == [("equity", "SPY", Decimal("100"))]
    assert contract.expiry_ms == option_expiry_anchor_ms(date(2026, 10, 16))
    assert (account.account_mode, account.options_approved_level, account.options_trading_level) == ("paper", 3, 3)
    assert account.options_buying_power is not None and account.options_buying_power > 0
    assert held and {(p.symbol, p.asset_class, p.side, p.quantity) for p in held} == {
        (_SYMBOL, "us_option", "long", 1.0)
    }
    # The account is flat when the run starts and when it ends.
    assert polls_flat[0] and polls_flat[-1]
