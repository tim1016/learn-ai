"""What a real Alpaca Paper account answered to bought and spread orders, read by this repo's own readers (#3103, #3225).

The recordings (``tests/fixtures/alpaca/option_paper_recordings``) are the
capture journal of the first option orders ever sent from this system, made on
2026-10-13 through 2026-10-16 under the qualification runbook and sanitised by the
kit. They sit beside the documented examples and replace none of them: each
shows one account on one day, for **one bought contract, or one bull call
spread bought and closed (alone, and twice at once by two bots), every order
filled inside its wait**. The #3225 recordings add outside and bot credit spreads and an iron condor
opened with negative limits and closed by market orders. The dashboard step
also records a resting credit order cancelled without a fill; it does not
exercise the bot's entry deadline. Part fills and exercise are not in them. Regulatory and clearing fees
are: the 2026-10-14 steps' activity feed carries 2026-10-13's fees. The
2026-10-16 recording carries the outside credit spread's four leg fills and
eight fee rows. The capture proves their presence then, not their first posting time.

What these tests prove is that what Alpaca really answered is read whole by
the readers the Clerk uses, and that the body the Clerk sent is the body the
writer produces. They do not say the Clerk's books agree: that is the bundle
and its offline judgment.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.adapter import (
    execution_id_from_activity_id,
    from_alpaca_activity,
    from_alpaca_option_account,
    from_alpaca_option_contract,
    from_alpaca_option_order,
    from_alpaca_order,
    from_alpaca_position,
    from_alpaca_trade_update,
    to_alpaca_option_order_request,
)
from app.broker.alpaca.option_fees import fees_for_option_fill, settle_option_session
from app.broker.contract.models import OrderSide, OrderType
from app.broker.contract.options import (
    MultiLegOptionOrderRequest,
    OptionLegRequest,
    OptionPositionIntent,
    SingleOptionOrderRequest,
)
from app.options.contract import OptionContract
from app.options.expiry import option_expiry_anchor_ms
from app.utils.session_anchors import et_date_at_ms

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


@pytest.mark.parametrize(
    ("step", "trade_date", "clearing_rows", "total"),
    [
        ("A4-long-call-flat-fix-landed", "2026-10-13", 8, "-0.42"),
        ("A5-1s-wait-restart-held-flatten", "2026-10-13", 8, "-0.42"),
        ("3225-credit-fees-20261016", "2026-10-15", 4, "-0.21"),
    ],
)
def test_recorded_option_fees_are_read_as_non_trade_activities_for_the_fill_day(
    step: str, trade_date: str, clearing_rows: int, total: str,
) -> None:
    """Bought-option and outside credit-spread fees, present in the later activity feed.

    Paper does charge them: they are non-trade activities of type FEE dated the trading
    day, one per leg's clearing fee and one each for ORF, OPT REG, OPT TAF and CAT. These are observed
    postings, not an endorsement of the fee model: #3306 tracks the OCC and OPT TAF discrepancies.
    """
    fees: dict[str, Any] = {}
    for line in _lines(step, "activities"):
        for row in _body(line):
            if row["activity_type"] == "FEE" and row.get("date") == trade_date:
                fees[row["id"]] = row

    read = [from_alpaca_activity(row) for row in fees.values()]

    assert read and all(a.category == "non_trade_activity" and a.symbol is None for a in read)
    assert sum(Decimal(str(a.net_amount)) for a in read) == Decimal(total)
    descriptions = sorted(row["description"].split(" fee")[0].split(" for ")[0] for row in fees.values())
    assert descriptions == sorted(["CAT"] + ["OCC Clearing Fee"] * clearing_rows + ["OPT REG", "OPT TAF", "ORF"])


def test_credit_spread_clearing_fees_link_to_each_leg_execution() -> None:
    """#3225: OCC gives an execution ID even though its fee row has no order ID."""
    rows = {
        row["id"]: row
        for line in _lines("3225-credit-fees-20261016", "activities")
        for row in _body(line)
    }
    fills = {
        execution_id_from_activity_id(activity.activity_id): activity
        for activity in map(from_alpaca_activity, rows.values())
        if activity.activity_type == "FILL" and activity.occurred_at_ms is not None
        and et_date_at_ms(activity.occurred_at_ms) == date(2026, 10, 15)
    }
    clearing = [
        row for row in rows.values()
        if row["activity_type"] == "FEE" and row.get("date") == "2026-10-15"
        and row.get("activity_sub_type") == "OCC"
    ]

    assert len(clearing) == len(fills) == 4
    assert {row["execution_id"] for row in clearing} == set(fills)
    assert len({fill.native_order_id for fill in fills.values()}) == 4
    assert all(fill.native_order_id is not None for fill in fills.values())
    assert {fill.symbol for fill in fills.values()} == {"SPY261016C00776000", "SPY261016C00777000"}
    assert {fill.side for fill in fills.values()} == {"buy", "sell", "sell_short"}
    assert all(Decimal(row["net_amount"]) == Decimal("-0.03") for row in clearing)


def test_credit_spread_recorded_fills_model_eighteen_cents_before_the_posted_three_cent_difference() -> None:
    """#3306: canonical option_fees.py predicts OCC $0.02 and TAF $0.01 less than Paper posted."""
    evidence = json.loads((_RECORDINGS / "3225-credit-sign-check" / "outside_fee_evidence.json").read_text())
    contracts = {contract.symbol: contract for contract in map(from_alpaca_option_contract, evidence["contracts"])}
    fills = list(map(from_alpaca_activity, evidence["activities"]))
    assert len(fills) == 4
    assert {fill.side for fill in fills} == {"buy", "sell", "sell_short"}
    accrued = []
    for fill in fills:
        assert fill.occurred_at_ms is not None and et_date_at_ms(fill.occurred_at_ms) == date(2026, 10, 15)
        assert fill.symbol is not None and fill.side is not None
        assert fill.quantity is not None and fill.price is not None
        multiplier = contracts[fill.symbol].multiplier
        assert multiplier == multiplier.to_integral_value()
        accrued.append(fees_for_option_fill(
            trade_date_ms=fill.occurred_at_ms,
            side=OrderSide.SELL if fill.side == "sell_short" else OrderSide(fill.side),
            contracts=Decimal(str(fill.quantity)),
            premium=Decimal(str(fill.price)),
            multiplier=int(multiplier),
        ))

    model = settle_option_session(accrued)
    assert (model.sec, model.taf, model.cat, model.orf, model.occ, model.total) == tuple(
        map(Decimal, ("0.01", "0.00", "0.01", "0.06", "0.10", "0.18"))
    )
    rows = {
        row["id"]: from_alpaca_activity(row)
        for line in _lines("3225-credit-fees-20261016", "activities")
        for row in _body(line)
    }
    posted = -sum(
        Decimal(str(activity.net_amount)) for activity in rows.values()
        if activity.activity_type == "FEE" and activity.occurred_at_ms is not None
        and et_date_at_ms(activity.occurred_at_ms) == date(2026, 10, 15)
    )
    assert posted - model.total == Decimal("0.03")


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
_RESTARTED = "B4-spread-restart-scheduled-close"
_CREDIT_FLAT = "3225-C1-credit-flat-20261016"
_CREDIT_OVERLAP = "3225-C2-credit-overlap-20261016"
_CONDOR_FLAT = "3225-D1-condor-flat-20261016"
_CONDOR_BEFORE_OVERLAP = "3225-D2a-condor-before-overlap-20261016"
_CONDOR_OVERLAP = "3225-D2-condor-overlap-20261016"
_BUYBACK_MISSED = "C3-buyback-window-missed"
_BUYBACK_RETRY_MISSED = "3225-C4-buyback-retry-window-missed-20261016"
_SPREAD_LONG, _SPREAD_SHORT = "SPY261016C00768000", "SPY261016C00773000"
# step -> (long legs, short legs, the net limit each order was sent at (None: a market order), the net price each
# order filled at as Alpaca states it). Entries and exits are in the order they were sent.
_STRUCTURE_PASSES: dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[str | None, ...], tuple[str, ...]]] = {
    _CLEAN_PASS: (("SPY261016C00769000",), ("SPY261016C00774000",), ("2.13", None), ("2.12", "-2.07")),
    # Two bots, the same two contracts: the second entered a minute after the first, and left a minute after it.
    _OVERLAP: (
        ("SPY261016C00768000",), ("SPY261016C00773000",), ("2.20", "2.24", None, None), ("2.19", "2.23", "-2.26", "-2.28"),
    ),
    # Bought at 12:29 CDT, the Clerk restarted with it held, and its own scheduled close sent the exit at 15:50 ET.
    _RESTARTED: (("SPY261016C00768000",), ("SPY261016C00773000",), ("2.31", None), ("2.31", "-2.43")),
    _CREDIT_FLAT: (("SPY261019C00771000",), ("SPY261019C00770000",), ("-0.31", None), ("-0.31", "0.32")),
    _CREDIT_OVERLAP: (
        ("SPY261019C00771000",), ("SPY261019C00770000",), ("-0.28", "-0.28", None, None), ("-0.28", "-0.28", "0.28", "0.28"),
    ),
    _CONDOR_FLAT: (
        ("SPY261019P00758000", "SPY261019C00769000"), ("SPY261019P00759000", "SPY261019C00768000"),
        ("-0.52", None), ("-0.52", "0.56"),
    ),
    # The second bot had no entry signal before this first cycle closed; this window proves no overlap.
    _CONDOR_BEFORE_OVERLAP: (
        ("SPY261019P00758000", "SPY261019C00768000"), ("SPY261019P00759000", "SPY261019C00767000"),
        ("-0.58", None), ("-0.58", "0.62"),
    ),
    _CONDOR_OVERLAP: (
        ("SPY261019P00757000", "SPY261019C00767000"), ("SPY261019P00758000", "SPY261019C00766000"),
        ("-0.58", "-0.58", None, None), ("-0.58", "-0.58", "0.61", "0.61"),
    ),
    # The owner did not submit the isolated buy-back before the strategy's exit; both legs closed together.
    _BUYBACK_MISSED: (("SPY261019C00770000",), ("SPY261019C00769000",), ("-0.29", None), ("-0.29", "0.3")),
    _BUYBACK_RETRY_MISSED: (("SPY261019C00767000",), ("SPY261019C00766000",), ("-0.32", None), ("-0.32", "0.35")),
}
_STRUCTURE_PASS = pytest.mark.parametrize("step", sorted(_STRUCTURE_PASSES))
# The steps whose recordings keep the activity feed. B4's was left out on purpose (6 MB of polls).
_STRUCTURE_PASS_WITH_FEED = pytest.mark.parametrize("step", [step for step in _STRUCTURE_PASSES if step != _RESTARTED])


def _fill_frames(step: str) -> list[dict[str, Any]]:
    return [data for data in _frames(step) if data["event"] == "fill"]


@_STRUCTURE_PASS
def test_each_structure_order_sent_is_the_body_the_writer_produces_a_signed_limit_in_and_a_market_order_out(step: str) -> None:
    long_legs, short_legs, limits, _ = _STRUCTURE_PASSES[step]
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
        assert request.quantity == 1 and all(leg.ratio == 1 for leg in request.legs)
        # The exit is every leg, each the other way round, as ONE order.
        entry = [(leg, "buy", "buy_to_open") for leg in long_legs] + [(leg, "sell", "sell_to_open") for leg in short_legs]
        exit_ = [(leg, "sell", "sell_to_close") for leg in long_legs] + [(leg, "buy", "buy_to_close") for leg in short_legs]
        assert sorted((leg["symbol"], leg["side"], leg["position_intent"]) for leg in sent["legs"]) == sorted(
            entry if limit is not None else exit_
        )
        answer = from_alpaca_option_order(_body(post))
        assert (answer.status, answer.filled_quantity, answer.time_in_force) == ("pending_new", 0, "day")


@_STRUCTURE_PASS
def test_a_structure_fill_states_what_it_paid_as_positive_and_what_it_received_as_negative_whatever_its_parents_side(
    step: str,
) -> None:
    _, _, _, nets = _STRUCTURE_PASSES[step]
    fills = _fill_frames(step)

    assert [Decimal(data["price"]) for data in fills] == [Decimal(net) for net in nets]
    for data in fills:
        assert from_alpaca_trade_update(data).event_type == "fill"
        read = from_alpaca_order(data["order"])
        assert read.unreadable_fields == () and read.status == "filled"
        assert read.quantity == read.filled_quantity == 1
        assert Decimal(str(read.filled_avg_price)) == Decimal(data["price"])
        sides = {leg["symbol"]: leg["side"] for leg in data["order"]["legs"]}
        bought = sum(Decimal(leg["price"]) for leg in data["legs"] if sides[leg["symbol"]] == "buy")
        sold = sum(Decimal(leg["price"]) for leg in data["legs"] if sides[leg["symbol"]] == "sell")
        # The net is what the legs bought less what they sold, and the order carries the same figure.
        assert bought - sold == Decimal(data["price"]) == Decimal(data["order"]["filled_avg_price"])
        # The stream's parent states ``side: buy`` on an exit as well as an entry (the REST parent states none), so
        # the direction of a spread is its legs' and its net's sign, never the parent's side.
        assert data["order"]["side"] == "buy"
        assert data["order"]["order_class"] == "mleg" and "execution_id" not in data


@_STRUCTURE_PASS_WITH_FEED
def test_each_leg_execution_names_its_own_leg_order_and_is_in_the_activity_feed_as_that_legs_fill(step: str) -> None:
    long_legs, short_legs, _, _ = _STRUCTURE_PASSES[step]
    symbols = set(long_legs + short_legs)
    fills = _fill_frames(step)
    on_the_frames: list[tuple[str, str, str, Decimal]] = []
    for data in fills:
        # The leg executions name the legs of the parent they came on, and no other order.
        assert {leg["order_id"] for leg in data["legs"]} == {leg["id"] for leg in data["order"]["legs"]}
        on_the_frames += [(leg["execution_id"], leg["order_id"], leg["symbol"], Decimal(leg["price"])) for leg in data["legs"]]

    assert len({execution_id for execution_id, *_ in on_the_frames}) == len(on_the_frames) == len(symbols) * len(fills)
    assert {symbol for _, _, symbol, _ in on_the_frames} == symbols
    in_the_feed: set[tuple[str, str | None, str | None, Decimal]] = set()
    for line in _lines(step, "activities"):
        for row in _body(line):
            if row["activity_type"] == "FILL" and row.get("symbol") in symbols:
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
    assert [later[0] - earlier[0] for earlier, later in pairwise(figures)] == [
        Decimal(move) for move in moves
    ]


@pytest.mark.parametrize(
    ("step", "observed_quantities"),
    [(_OVERLAP, (1.0, 2.0)), (_CREDIT_OVERLAP, (1.0, 2.0)), (_CONDOR_OVERLAP, (2.0,))],
)
def test_two_bots_structures_held_at_once_are_one_account_position_of_two_of_each_leg(
    step: str, observed_quantities: tuple[float, ...],
) -> None:
    long_legs, short_legs, _, _ = _STRUCTURE_PASSES[step]
    held_over_time = [
        {position.symbol: position.quantity for position in map(from_alpaca_position, _body(line))}
        for line in _lines(step, "positions")
    ]

    assert held_over_time[0] == {} and held_over_time[-1] == {}
    # The condor polls captured both held together, without an intermediate one-structure account reading.
    expected = {
        tuple(sorted({**dict.fromkeys(long_legs, qty), **dict.fromkeys(short_legs, -qty)}.items()))
        for qty in observed_quantities
    }
    expected.add(())
    assert {tuple(sorted(held.items())) for held in held_over_time} == expected


@pytest.mark.parametrize(
    ("step", "starting", "cash_credit", "buying_power_fall", "round_trip_cost"),
    [
        (_CREDIT_FLAT, "99923.44", "30.95", "69.05", "1.10"),
        (_CONDOR_FLAT, "99923.04", "51.90", "48.10", "4.20"),
        (_CONDOR_BEFORE_OVERLAP, "99917.74", "57.90", "42.10", "4.20"),
        (_BUYBACK_MISSED, "99918.84", "28.95", "71.05", "1.10"),
        (_BUYBACK_RETRY_MISSED, "99907.14", "31.95", "68.05", "3.10"),
    ],
)
def test_a_credit_structure_adds_premium_to_cash_and_holds_its_width_until_the_whole_structure_closes(
    step: str, starting: str, cash_credit: str, buying_power_fall: str, round_trip_cost: str,
) -> None:
    """#3225: each $1-wide structure holds $100, released on close; cash includes observed charges."""
    before, held, closed = _account_figures(step)

    assert before == (Decimal(starting), Decimal(starting))
    assert held == (before[0] + Decimal(cash_credit), before[1] - Decimal(buying_power_fall))
    assert held[0] - held[1] == Decimal("100.00")
    assert closed == (before[0] - Decimal(round_trip_cost), before[1] - Decimal(round_trip_cost))
    positions = [
        {position.symbol: position.quantity for position in map(from_alpaca_position, _body(line))}
        for line in _lines(step, "positions")
    ]
    assert positions[0] == positions[-1] == {}
    long_legs, short_legs, _, _ = _STRUCTURE_PASSES[step]
    assert {**dict.fromkeys(long_legs, 1.0), **dict.fromkeys(short_legs, -1.0)} in positions


def test_the_condor_contracts_read_as_two_calls_and_two_puts_with_the_same_expiry_and_multiplier() -> None:
    contracts = [from_alpaca_option_contract(_body(line)) for line in _lines(_CONDOR_FLAT, "option_contracts")]

    assert {(contract.symbol, contract.right, contract.strike_price) for contract in contracts} == {
        ("SPY261019P00758000", "put", Decimal("758")),
        ("SPY261019P00759000", "put", Decimal("759")),
        ("SPY261019C00768000", "call", Decimal("768")),
        ("SPY261019C00769000", "call", Decimal("769")),
    }
    assert all(contract.expiry_ms == option_expiry_anchor_ms(date(2026, 10, 19)) for contract in contracts)
    assert all(contract.multiplier == Decimal("100") and contract.tradable for contract in contracts)


def test_two_credit_spreads_each_add_premium_and_requirement_then_release_the_whole_requirement() -> None:
    """#3225: two $28 credits, two $28 closes, and $0.05 cash effect per parent order."""
    before, first, both, closed = _account_figures(_CREDIT_OVERLAP)

    assert before == (Decimal("99923.24"), Decimal("99923.24"))
    assert first == (before[0] + Decimal("27.95"), before[1] - Decimal("72.05"))
    assert both == (first[0] + Decimal("27.95"), first[1] - Decimal("72.05"))
    assert both[0] - both[1] == Decimal("200.00")
    assert closed == (before[0] - Decimal("0.20"), before[1] - Decimal("0.20"))


def test_two_condors_add_their_combined_premium_and_requirement_then_release_the_whole_requirement() -> None:
    """#3225: two $58 credits, two $61 closes, and $0.10 observed cash charge per parent order."""
    before, both, closed = _account_figures(_CONDOR_OVERLAP)

    assert before == (Decimal("99913.54"), Decimal("99913.54"))
    assert both == (before[0] + Decimal("115.80"), before[1] - Decimal("84.20"))
    assert both[0] - both[1] == Decimal("200.00")
    assert closed == (before[0] - Decimal("6.40"), before[1] - Decimal("6.40"))


def test_the_clerks_own_scheduled_close_sends_a_spread_held_through_a_restart_out_at_ten_to_four_as_one_order() -> None:
    """2026-10-14: the run had ended at the restart; the close owed by what is held was sent on time anyway."""
    records = [_body(line) for line in _lines(_RESTARTED, "stream")]
    kinds = [
        data["event"] if "event" in (data := body.get("data", {})) else body["stream"] for body in records
    ]
    # Entry, then the stream authorises and listens again (the Clerk came back up), then the exit's three frames.
    assert kinds == ["pending_new", "new", "fill", "authorization", "listening", "pending_new", "new", "fill"]

    entry_fill, close_fill = records[2]["data"], records[7]["data"]
    assert entry_fill["position_qtys"] == {_SPREAD_LONG: "1", _SPREAD_SHORT: "-1"}
    assert records[5]["data"]["order"]["type"] == "market"
    # 15:50:00 ET, ten minutes before the session's close: the frame stamps the close within its first second.
    assert records[5]["data"]["timestamp"].startswith("2026-10-14T19:50:00.")
    assert Decimal(close_fill["price"]) == Decimal("-2.43")
    # Both legs are flat after the one closing order.
    assert close_fill["position_qtys"] == {_SPREAD_LONG: "0", _SPREAD_SHORT: "0"}


@pytest.mark.parametrize(
    "step,short,long,opening_net,closing_net,opening_legs,closing_legs,preceding_events",
    [
        ("3225-credit-sign-check", "SPY261016C00776000", "SPY261016C00777000",
         "-0.14", "0.16", ("0.60", "0.46"), ("0.61", "0.45"), ()),
        ("3225-dashboard-credit-sign-check-20261016", "SPY261019C00774000", "SPY261019C00775000",
         "-0.15", "0.18", ("0.51", "0.36"), ("0.55", "0.37"), ("pending_new", "new", "canceled")),
        ("3225-C1-credit-flat-20261016", "SPY261019C00770000", "SPY261019C00771000",
         "-0.31", "0.32", ("1.25", "0.94"), ("1.21", "0.89"), ()),
    ],
)
def test_paper_accepts_a_negative_limit_as_a_credit_and_a_positive_market_close_as_a_debit(
    step: str, short: str, long: str, opening_net: str, closing_net: str,
    opening_legs: tuple[str, str], closing_legs: tuple[str, str], preceding_events: tuple[str, ...],
) -> None:
    """#3225: outside orders and the bot's flat pass preserve the credit/debit sign."""
    frames = _frames(step)
    assert [frame["event"] for frame in frames] == [*preceding_events, *(["pending_new", "new", "fill"] * 2)]
    opening, closing = [frame for frame in frames if frame["event"] == "fill"]

    assert [(frame["order"]["order_class"], frame["order"]["type"]) for frame in (opening, closing)] == [
        ("mleg", "limit"), ("mleg", "market")
    ]
    assert Decimal(opening["order"]["limit_price"]) == Decimal(opening_net) < 0
    assert Decimal(opening["price"]) == Decimal(opening["order"]["filled_avg_price"]) == Decimal(opening_net)
    assert Decimal(closing["price"]) == Decimal(closing["order"]["filled_avg_price"]) == Decimal(closing_net) > 0

    for frame, sides, amounts in (
        (opening, ((short, "sell", "sell_to_open"), (long, "buy", "buy_to_open")), opening_legs),
        (closing, ((short, "buy", "buy_to_close"), (long, "sell", "sell_to_close")), closing_legs),
    ):
        # The trade-update consumer reads its embedded parent with this reader. Alpaca's stream
        # states a misleading parent side, so the stricter REST option-order reader does not apply.
        read_order = from_alpaca_order(frame["order"])
        assert read_order.unreadable_fields == ()
        assert read_order.status == "filled"
        assert read_order.quantity == read_order.filled_quantity == 1
        assert read_order.limit_price == (float(opening_net) if frame is opening else None)
        assert read_order.filled_avg_price == float(opening_net if frame is opening else closing_net)
        assert frame["order"]["status"] == "filled"
        assert frame["order"]["filled_qty"] == frame["order"]["qty"] == "1"
        assert [(leg["symbol"], leg["side"], leg["position_intent"]) for leg in frame["order"]["legs"]] == list(sides)
        assert [Decimal(leg["price"]) for leg in frame["legs"]] == [Decimal(amount) for amount in amounts]
        assert from_alpaca_trade_update(frame).event_type == "fill"

    assert opening["position_qtys"] == {short: "-1", long: "1"}
    assert closing["position_qtys"] == {short: "0", long: "0"}
    assert _body(_lines(step, "positions")[-1]) == []


def test_dashboard_credit_order_can_rest_and_be_cancelled_without_a_fill() -> None:
    """#3225: this is a dashboard cancellation, not the bot's entry-deadline path."""
    step = "3225-dashboard-credit-sign-check-20261016"
    frames = _frames(step)
    assert len(frames) == 9
    first_attempt = frames[:3]
    assert [frame["event"] for frame in first_attempt] == ["pending_new", "new", "canceled"]
    assert len({frame["order"]["id"] for frame in first_attempt}) == 1
    assert first_attempt[-1]["order"]["id"] != frames[3]["order"]["id"]
    for frame in first_attempt:
        read = from_alpaca_order(frame["order"])
        assert read.unreadable_fields == ()
        assert read.quantity == 1 and read.filled_quantity == 0
        assert read.limit_price == -0.33 and read.filled_avg_price is None
        assert from_alpaca_trade_update(frame).event_type == frame["event"]
    assert from_alpaca_order(first_attempt[-1]["order"]).status == "canceled"
    assert _body(_lines(step, "orders")[0])[0]["status"] == "new"
    assert _body(_lines(step, "orders")[-1]) == []


def test_dashboard_credit_round_trip_cash_change_is_recorded_separately_from_net_premiums() -> None:
    """#3225: the extra cash reduction is observed, not a final posted-fee attribution."""
    step = "3225-dashboard-credit-sign-check-20261016"
    figures = _account_figures(step)
    assert figures[0] == (Decimal("99926.54"), Decimal("99926.54"))
    assert figures[-1] == (Decimal("99923.44"), Decimal("99923.44"))
    opening, closing = [frame for frame in _frames(step) if frame["event"] == "fill"]
    contracts = [from_alpaca_option_contract(_body(line)) for line in _lines(step, "option_contracts")]
    assert {contract.symbol for contract in contracts} == {leg["symbol"] for leg in opening["legs"]}
    assert all(contract.multiplier == Decimal("100") for contract in contracts)
    premium_cost = (Decimal(opening["price"]) + Decimal(closing["price"])) * contracts[0].multiplier
    cash_cost = figures[0][0] - figures[-1][0]
    assert premium_cost == Decimal("3.00")
    assert cash_cost == Decimal("3.10")
    assert cash_cost - premium_cost == Decimal("0.10")
