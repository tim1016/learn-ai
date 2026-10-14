"""What a real Alpaca Paper account answered to one bought SPY call and one bull call spread, read by this repo's own readers (#3103).

The recordings (``tests/fixtures/alpaca/option_paper_recordings``) are the
capture journal of the first option orders ever sent from this system, made on
2026-10-13 and 2026-10-14 under the qualification runbook and sanitised by the
kit. They sit beside the documented examples and replace none of them: each
shows one account on one day, for **one bought contract, or one bull call
spread bought and closed (alone, and twice at once by two bots), every order
filled inside its wait**. A credit placed by hand, a resting or part-filled
order, a cancel and an exercise are not in them. Regulatory and clearing fees
are: Alpaca Paper posts them the morning after the fills, and the 2026-10-14
steps' activity feed carries 2026-10-13's.

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
from app.broker.contract.options import (
    MultiLegOptionOrderRequest,
    OptionLegRequest,
    OptionPositionIntent,
    SingleOptionOrderRequest,
)
from app.options.contract import OptionContract
from app.options.expiry import option_expiry_anchor_ms

_RECORDINGS = Path(__file__).resolve().parents[2] / "fixtures" / "alpaca" / "option_paper_recordings"

# step -> (the contract, its strike, the limit price each entry was sent at, the price each order filled at as
# Alpaca states it). Entries and exits alternate, entry first, in every step.
_STEPS: dict[str, tuple[str, str, tuple[str, ...], tuple[str, ...]]] = {
    "A1-long-call-flat": ("SPY261016C00778000", "778", ("3.47",), ("3.47", "3.4")),
    "A2-long-call-flat-repeat": (
        "SPY261016C00778000", "778", ("3.34", "3.46", "3.60"), ("3.34", "3.45", "3.46", "3.41", "3.6", "3.67"),
    ),
    # The first entry after #3136's fix: limit 3.37, filled at 3.36 (a cent better), exited at 3.52.
    "A4-long-call-flat-fix-landed": ("SPY261016C00772000", "772", ("3.37",), ("3.36", "3.52")),
    # One entry with the shortest wait, then a Clerk restart with it held, then the bot page's Flatten at 3.16.
    "A5-1s-wait-restart-held-flatten": ("SPY261016C00772000", "772", ("3.04",), ("3.04", "3.16")),
}
_STEP = pytest.mark.parametrize("step", sorted(_STEPS))


def _lines(step: str, family: str) -> list[dict[str, Any]]:
    path = _RECORDINGS / step / f"{family}.jsonl"
    return [json.loads(text) for text in path.read_text(encoding="utf-8").splitlines()]


def _body(line: dict[str, Any]) -> Any:
    return json.loads(line["raw_body"])


def _frames(step: str) -> list[dict[str, Any]]:
    """The order events of the stream: its authorization and listening acknowledgements are not events."""
    return [data for line in _lines(step, "stream") if "event" in (data := _body(line).get("data", {}))]


@_STEP
def test_each_order_the_bot_sent_is_the_body_the_writer_produces_and_its_answer_reads_as_sent(step: str) -> None:
    _, _, entry_prices, _ = _STEPS[step]
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
    _, _, _, filled_at = _STEPS[step]
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
    symbol = _STEPS[step][0]
    rows: dict[str, Any] = {}
    for line in _lines(step, "activities"):
        for row in _body(line):
            if row.get("symbol") == symbol and row["activity_type"] == "FILL":
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
    symbol, strike, _, _ = _STEPS[step]
    # The contract the bot traded: a step's journal also holds the listings of contracts only previewed.
    contract = next(
        read
        for read in (from_alpaca_option_contract(_body(line)) for line in _lines(step, "option_contracts"))
        if read.symbol == symbol
    )
    account = from_alpaca_option_account(_body(_lines(step, "account")[0]), account_mode="paper")
    held = [from_alpaca_position(position) for line in _lines(step, "positions") for position in _body(line)]
    polls_flat = [not _body(line) for line in _lines(step, "positions")]

    assert (contract.symbol, contract.tradable, contract.status) == (symbol, True, "active")
    assert (contract.right, contract.strike_price, contract.multiplier) == ("call", Decimal(strike), Decimal("100"))
    assert [(d.kind, d.symbol, d.amount) for d in contract.deliverables] == [("equity", "SPY", Decimal("100"))]
    assert contract.expiry_ms == option_expiry_anchor_ms(date(2026, 10, 16))
    assert (account.account_mode, account.options_approved_level, account.options_trading_level) == ("paper", 3, 3)
    assert account.options_buying_power is not None and account.options_buying_power > 0
    assert held and {(p.symbol, p.asset_class, p.side, p.quantity) for p in held} == {
        (symbol, "us_option", "long", 1.0)
    }
    # The account is flat when the run starts and when it ends.
    assert polls_flat[0] and polls_flat[-1]


def _account_figures(step: str) -> list[tuple[Decimal, Decimal]]:
    """The account's (cash, option buying power) as each distinct answer stated them, in order."""
    figures: list[tuple[Decimal, Decimal]] = []
    for line in _lines(step, "account"):
        body = _body(line)
        read = from_alpaca_option_account(body, account_mode="paper")
        assert read.options_buying_power is not None
        figure = (Decimal(body["cash"]), read.options_buying_power)
        if not figures or figures[-1] != figure:
            figures.append(figure)
    return figures


def test_a_resting_limit_holds_option_buying_power_and_the_fill_takes_the_premium_and_the_clearing_fee_from_cash() -> None:
    """2026-10-14, the first entry after #3136's fix: how Alpaca moved the figure the Clerk compares.

    Before the order cash and option buying power were one number. While the limit order rested Alpaca held its
    limit (3.37 x 100) plus a cent out of option buying power and left cash alone. Once it filled at 3.36 both
    were cash less the premium ($336.00) and Alpaca's $0.03 clearing fee: $336.03 below where they began.
    """
    figures = _account_figures("A4-long-call-flat-fix-landed")

    before = figures[0]
    assert before[0] == before[1]
    holding = (before[0], before[1] - Decimal("337.01"))
    settled = (before[0] - Decimal("336.03"), before[1] - Decimal("336.03"))
    assert holding in figures and settled in figures
    assert figures.index(before) < figures.index(holding) < figures.index(settled)


def test_option_buying_power_can_sit_below_cash_after_a_sale_until_the_next_order_moves_it() -> None:
    """2026-10-14, the second entry: the figure the Clerk read before it was already $2.95 under cash.

    After the earlier step's sell to close, cash rose by the whole sale and option buying power by $2.95 less,
    and the two stayed apart until this entry's order made Alpaca state them again. The entry did not cause the
    $2.95; a comparison that expects the fall to equal the premium reads it as an entry that cost less.
    """
    figures = _account_figures("A5-1s-wait-restart-held-flatten")

    cash, buying_power = figures[0]
    assert cash - buying_power == Decimal("2.95")
    # The order rests (its limit 3.04 x 100 and a cent held), then the fill brings the two figures together again.
    assert (cash, buying_power - Decimal("304.01")) in figures
    settled = cash - Decimal("304.03")
    assert (settled, settled) in figures


@pytest.mark.parametrize("step", ["A4-long-call-flat-fix-landed", "A5-1s-wait-restart-held-flatten"])
def test_the_morning_after_alpaca_paper_posts_the_options_fees_of_the_day_as_fee_activities(step: str) -> None:
    """The 2026-10-13 option orders' regulatory and clearing fees, in the 2026-10-14 steps' activity feed.

    Paper does charge them, but not on the day: they are non-trade activities of type FEE dated the trading
    day, one per clearing fee and one each for ORF, OPT REG, OPT TAF and CAT, $0.42 for eight option orders.
    """
    fees: dict[str, Any] = {}
    for line in _lines(step, "activities"):
        for row in _body(line):
            if row["activity_type"] == "FEE" and row.get("date") == "2026-10-13":
                fees[row["id"]] = row

    read = [from_alpaca_activity(row) for row in fees.values()]

    assert read and all(a.category == "non_trade_activity" and a.symbol is None for a in read)
    assert sum(Decimal(str(a.net_amount)) for a in read) == Decimal("-0.42")
    descriptions = sorted(row["description"].split(" fee")[0].split(" for ")[0] for row in fees.values())
    assert descriptions == sorted(["CAT"] + ["OCC Clearing Fee"] * 8 + ["OPT REG", "OPT TAF", "ORF"])


# ── The first several-leg order: a bull call spread, 2026-10-14 ──────────────

_SPREAD = "B1-bull-call-spread-flat"
_LONG_LEG, _SHORT_LEG = "SPY261016C00771000", "SPY261016C00776000"


def _leg_request(leg: dict[str, Any]) -> OptionLegRequest:
    return OptionLegRequest(
        contract=OptionContract.from_occ_symbol(leg["symbol"]),
        side=OrderSide(leg["side"]),
        intent=OptionPositionIntent.OPEN if leg["position_intent"].endswith("_to_open") else OptionPositionIntent.CLOSE,
        ratio=int(leg["ratio_qty"]),
    )


def test_the_spread_sent_is_the_body_the_writer_produces_and_a_debit_is_a_positive_net_limit() -> None:
    posts = [line for line in _lines(_SPREAD, "orders") if line["method"] == "POST"]
    assert len(posts) == 1
    sent = json.loads(posts[0]["request_body"])
    request = MultiLegOptionOrderRequest(
        legs=tuple(_leg_request(leg) for leg in sent["legs"]),
        quantity=int(sent["qty"]),
        order_type=OrderType(sent["type"]),
        net_limit_price=Decimal(sent["limit_price"]),
    )

    assert to_alpaca_option_order_request(request, client_order_id=sent["client_order_id"]) == sent

    answer = from_alpaca_option_order(_body(posts[0]))
    assert (answer.status, answer.filled_quantity, answer.time_in_force) == ("pending_new", 0, "day")
    assert [(leg.contract.occ_symbol, leg.side, leg.intent) for leg in answer.legs] == [
        (_LONG_LEG, OrderSide.BUY, OptionPositionIntent.OPEN),
        (_SHORT_LEG, OrderSide.SELL, OptionPositionIntent.OPEN),
    ]
    # Paying 3.31 for the lower strike and receiving 1.18 for the higher is a net debit, and a debit is positive.
    assert request.net_limit_price == Decimal("2.14") > 0


def test_the_parents_fill_frame_has_the_executions_of_its_legs_and_none_of_its_own() -> None:
    frames = _frames(_SPREAD)
    events = [from_alpaca_trade_update(data) for data in frames]

    assert [event.event_type for event in events] == ["pending_new", "new", "fill"]
    fill = frames[-1]
    # Alpaca states the net at the top level and each leg's execution under ``legs``.
    assert "execution_id" not in fill and fill["order"]["order_class"] == "mleg"
    assert (events[-1].execution_id, events[-1].price, events[-1].quantity) == (None, None, None)
    legs = {leg["symbol"]: leg for leg in fill["legs"]}
    assert set(legs) == {_LONG_LEG, _SHORT_LEG}
    assert Decimal(legs[_LONG_LEG]["price"]) - Decimal(legs[_SHORT_LEG]["price"]) == Decimal(fill["price"]) == Decimal("2.13")


def test_each_leg_execution_on_the_parents_frame_is_in_the_activity_feed_as_that_legs_fill() -> None:
    fill = _frames(_SPREAD)[-1]
    on_the_frame = {(leg["execution_id"], leg["symbol"], Decimal(leg["price"])) for leg in fill["legs"]}
    in_the_feed: set[tuple[str, str | None, Decimal]] = set()
    for line in _lines(_SPREAD, "activities"):
        for row in _body(line):
            if row["activity_type"] == "FILL" and row.get("symbol") in {_LONG_LEG, _SHORT_LEG}:
                activity = from_alpaca_activity(row)
                in_the_feed.add(
                    (execution_id_from_activity_id(activity.activity_id), activity.symbol, Decimal(str(activity.price)))
                )

    assert on_the_frame <= in_the_feed


def test_a_spreads_debit_moves_cash_and_option_buying_power_together_and_the_account_holds_both_legs() -> None:
    figures = _account_figures(_SPREAD)
    cash, buying_power = figures[0]
    # The state a sale had left it in: option buying power under cash until the next order states it again.
    assert cash - buying_power == Decimal("5.88")
    # $213.00 net premium and $0.05 of fees leave cash; option buying power ends level with cash.
    settled = (cash - Decimal("213.05"), cash - Decimal("213.05"))
    assert settled in figures

    held = {
        position.symbol: (position.side, position.quantity)
        for line in _lines(_SPREAD, "positions")
        for position in map(from_alpaca_position, _body(line))
    }
    assert held == {_LONG_LEG: ("long", 1.0), _SHORT_LEG: ("short", -1.0)}


# ── The spread closed, and two bots' spreads held at once, 2026-10-14 ────────

_CLEAN_PASS = "B2-bull-call-spread-clean-pass"
_OVERLAP = "B3-overlap-spread"
# step -> (long leg, short leg, the net limit each order was sent at (None: a market order), the net price each
# order filled at as Alpaca states it). Entries and exits are in the order they were sent.
_SPREAD_PASSES: dict[str, tuple[str, str, tuple[str | None, ...], tuple[str, ...]]] = {
    _CLEAN_PASS: ("SPY261016C00769000", "SPY261016C00774000", ("2.13", None), ("2.12", "-2.07")),
    # Two bots, the same two contracts: the second entered a minute after the first, and left a minute after it.
    _OVERLAP: (
        "SPY261016C00768000", "SPY261016C00773000", ("2.20", "2.24", None, None), ("2.19", "2.23", "-2.26", "-2.28"),
    ),
}
_SPREAD_PASS = pytest.mark.parametrize("step", sorted(_SPREAD_PASSES))


def _fill_frames(step: str) -> list[dict[str, Any]]:
    return [data for data in _frames(step) if data["event"] == "fill"]


@_SPREAD_PASS
def test_each_spread_order_sent_is_the_body_the_writer_produces_a_limit_debit_in_and_a_market_order_out(step: str) -> None:
    long_leg, short_leg, limits, _ = _SPREAD_PASSES[step]
    posts = [line for line in _lines(step, "orders") if line["method"] == "POST"]
    assert len(posts) == len(limits)

    for post, limit in zip(posts, limits, strict=True):
        sent = json.loads(post["request_body"])
        request = MultiLegOptionOrderRequest(
            legs=tuple(_leg_request(leg) for leg in sent["legs"]),
            quantity=int(sent["qty"]),
            order_type=OrderType(sent["type"]),
            net_limit_price=None if limit is None else Decimal(limit),
        )

        assert to_alpaca_option_order_request(request, client_order_id=sent["client_order_id"]) == sent
        assert sent.get("limit_price") == limit
        # The exit is the same two legs, each the other way round, as ONE order.
        entry = [(long_leg, "buy", "buy_to_open"), (short_leg, "sell", "sell_to_open")]
        exit_ = [(long_leg, "sell", "sell_to_close"), (short_leg, "buy", "buy_to_close")]
        assert [(leg["symbol"], leg["side"], leg["position_intent"]) for leg in sent["legs"]] == (
            entry if limit is not None else exit_
        )
        answer = from_alpaca_option_order(_body(post))
        assert (answer.status, answer.filled_quantity, answer.time_in_force) == ("pending_new", 0, "day")


@_SPREAD_PASS
def test_a_spread_fill_states_what_it_paid_as_positive_and_what_it_received_as_negative_whatever_its_parents_side(
    step: str,
) -> None:
    _, _, _, nets = _SPREAD_PASSES[step]
    fills = _fill_frames(step)

    assert [Decimal(data["price"]) for data in fills] == [Decimal(net) for net in nets]
    for data in fills:
        sides = {leg["symbol"]: leg["side"] for leg in data["order"]["legs"]}
        bought = sum(Decimal(leg["price"]) for leg in data["legs"] if sides[leg["symbol"]] == "buy")
        sold = sum(Decimal(leg["price"]) for leg in data["legs"] if sides[leg["symbol"]] == "sell")
        # The net is what the legs bought less what they sold, and the order carries the same figure.
        assert bought - sold == Decimal(data["price"]) == Decimal(data["order"]["filled_avg_price"])
        # The stream's parent states ``side: buy`` on an exit as well as an entry (the REST parent states none), so
        # the direction of a spread is its legs' and its net's sign, never the parent's side.
        assert data["order"]["side"] == "buy"
        assert data["order"]["order_class"] == "mleg" and "execution_id" not in data


@_SPREAD_PASS
def test_each_leg_execution_names_its_own_leg_order_and_is_in_the_activity_feed_as_that_legs_fill(step: str) -> None:
    long_leg, short_leg, _, _ = _SPREAD_PASSES[step]
    fills = _fill_frames(step)
    on_the_frames: list[tuple[str, str, str, Decimal]] = []
    for data in fills:
        # The leg executions name the legs of the parent they came on, and no other order.
        assert {leg["order_id"] for leg in data["legs"]} == {leg["id"] for leg in data["order"]["legs"]}
        on_the_frames += [(leg["execution_id"], leg["order_id"], leg["symbol"], Decimal(leg["price"])) for leg in data["legs"]]

    assert len({execution_id for execution_id, *_ in on_the_frames}) == len(on_the_frames) == 2 * len(fills)
    assert {symbol for _, _, symbol, _ in on_the_frames} == {long_leg, short_leg}
    in_the_feed: set[tuple[str, str | None, str | None, Decimal]] = set()
    for line in _lines(step, "activities"):
        for row in _body(line):
            if row["activity_type"] == "FILL" and row.get("symbol") in {long_leg, short_leg}:
                activity = from_alpaca_activity(row)
                in_the_feed.add(
                    (
                        execution_id_from_activity_id(activity.activity_id),
                        activity.native_order_id,
                        activity.symbol,
                        Decimal(str(activity.price)),
                    )
                )

    assert set(on_the_frames) <= in_the_feed


@pytest.mark.parametrize(
    ("step", "moves"),
    [
        # Entry: the premium and $0.05 of fees leave cash. Exit: the credit less $0.05 comes back.
        (_CLEAN_PASS, ("-212.05", "206.95")),
        # The second entry took its own premium and $0.05 exactly, with the first spread held: no requirement added.
        (_OVERLAP, ("-219.05", "-223.05", "225.95", "227.95")),
    ],
)
def test_a_spreads_debit_and_credit_move_cash_and_option_buying_power_together_by_premium_and_fees_alone(
    step: str, moves: tuple[str, ...]
) -> None:
    figures = _account_figures(step)

    assert all(cash == buying_power for cash, buying_power in figures)
    assert [later[0] - earlier[0] for earlier, later in zip(figures, figures[1:], strict=False)] == [
        Decimal(move) for move in moves
    ]


def test_two_bots_spreads_held_at_once_are_one_account_position_of_two_of_each_leg() -> None:
    long_leg, short_leg, _, _ = _SPREAD_PASSES[_OVERLAP]
    held_over_time = [
        {position.symbol: position.quantity for position in map(from_alpaca_position, _body(line))}
        for line in _lines(_OVERLAP, "positions")
    ]

    assert held_over_time[0] == {} and held_over_time[-1] == {}
    # One structure, then both, then the first one's exit leaves the second: the account does not say whose.
    assert {(held.get(long_leg), held.get(short_leg)) for held in held_over_time} == {
        (None, None),
        (1.0, -1.0),
        (2.0, -2.0),
    }
