"""Stops, square-off, the exit time and a leg that is never sold.

Minutes are counted after the open; the setups here enter at minute 5 and leave at minute 60. Each
expectation is worked by hand and written as a literal. A trade reads as
``(leg, entry number, entry minute, seen, filled, exit reason, exit minute, seen, filled)``.
"""

from __future__ import annotations

import pytest

from app.options_backtest.results import EventKind, ExitReason, SessionStatus
from tests.options_backtest._market import (
    FULL_CLOSE_MS,
    D,
    TableMarket,
    at,
    contract,
    flat,
    kinds,
    leg,
    minute_of,
    minutes,
    picked,
    replay,
    row,
    setup,
    steady,
)

CALL = contract("call", "500")
PUT = contract("put", "500")
CALL_505 = contract("call", "505")
STOP_25 = ("percent", "25")


def test_a_stop_holds_on_a_close_above_its_level_and_sells_at_the_next_open_once_a_close_reaches_it() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("call", "500", CALL)),
        options={
            CALL: {
                5: ("2.57", "2.57"),
                6: (
                    "2.57",
                    "2.57",
                ),  # bought at 2.57 * 1.003 = 2.57771, up to 2.58: the stop sits at 2.58 * 0.75 = 1.935
                7: ("2.57", "1.94"),  # 1.94 is above 1.935: holds
                8: ("1.94", "1.93"),  # 1.93 is at or below it: a sale is decided
                9: ("1.92", "1.90"),  # and fills at this open: 1.92 * 0.997 = 1.91424, down to 1.91
            }
        },
    )

    outcome = replay(setup(leg(stop=STOP_25), slippage="0.3"), market)

    assert [row(trade) for trade in outcome.trades] == [
        ("call", 0, 6, D("2.57"), D("2.58"), "stop", 9, D("1.92"), D("1.91"))
    ]
    trade = outcome.trades[0]
    assert (trade.stop_level, trade.exit_group, trade.exit_decided_at_ms) == (D("1.935"), 1, at(8))
    assert trade.pnl_usd == D("-67.00")  # (1.91 - 2.58) * 100
    assert minutes(outcome, EventKind.STOP_HIT) == [8]
    # The stop event carries the close that reached the level (1.93), not that bar's open (1.94).
    assert [e.price for e in outcome.events if e.kind is EventKind.STOP_HIT] == [D("1.93")]


def test_a_points_stop_sits_that_many_points_below_the_buy_fill_and_is_hit_by_a_close_on_it() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("call", "500", CALL)),
        options={
            CALL: {
                5: ("2.00", "2.00"),
                6: ("2.00", "2.00"),  # bought at 2.00: a stop of 0.50 points sits at 1.50
                7: ("2.00", "1.51"),  # holds
                8: ("1.51", "1.50"),  # a close on the level is a hit
                9: ("1.45", "1.45"),
            }
        },
    )

    outcome = replay(setup(leg(stop=("points", "0.50"))), market)

    assert [row(trade) for trade in outcome.trades] == [
        ("call", 0, 6, D("2.00"), D("2.00"), "stop", 9, D("1.45"), D("1.45"))
    ]
    assert (outcome.trades[0].stop_level, outcome.trades[0].exit_decided_at_ms) == (D("1.50"), at(8))


def test_a_low_below_the_stop_with_a_close_above_it_does_not_sell() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("call", "500", CALL)),
        options={
            CALL: {
                5: ("2.00", "2.00"),
                6: ("2.00", "2.00"),  # the stop sits at 2.00 * 0.75 = 1.50
                7: ("2.00", "2.10", "1.40", "1.60"),  # the low went through it; the close did not
                **flat("1.60", 8, 61),
            }
        },
    )

    outcome = replay(setup(leg(stop=STOP_25)), market)

    assert [row(trade) for trade in outcome.trades] == [
        ("call", 0, 6, D("2.00"), D("2.00"), "exit_time", 61, D("1.60"), D("1.60"))
    ]
    assert minutes(outcome, EventKind.STOP_HIT) == []


def test_a_minute_without_a_trade_cannot_hit_a_stop_and_the_next_one_that_traded_does() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("call", "500", CALL)),
        options={
            CALL: {
                **flat("2.00", 5, 9),
                # nothing traded in minute 10: no price is carried forward to decide on
                11: ("1.45", "1.40"),  # 1.40 is at or below 1.50: decided here
                12: ("1.35", "1.35"),
            }
        },
    )

    outcome = replay(setup(leg(stop=STOP_25)), market)

    assert [row(trade) for trade in outcome.trades] == [
        ("call", 0, 6, D("2.00"), D("2.00"), "stop", 12, D("1.35"), D("1.35"))
    ]
    assert outcome.trades[0].exit_decided_at_ms == at(11)


def test_an_entry_that_fills_at_a_bars_open_can_be_stopped_by_that_same_bars_close() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("call", "500", CALL)),
        options={CALL: {5: ("2.00", "2.00"), 6: ("2.00", "1.40"), 7: ("1.40", "1.40")}},
    )

    outcome = replay(setup(leg(stop=STOP_25)), market)

    # Filled at 2.00, the open of minute 6, whose close of 1.40 is at or below the stop at 1.50.
    assert [row(trade) for trade in outcome.trades] == [
        ("call", 0, 6, D("2.00"), D("2.00"), "stop", 7, D("1.40"), D("1.40"))
    ]
    assert outcome.trades[0].pnl_usd == D("-60.00")
    assert kinds(outcome) == [EventKind.PICKED, EventKind.ENTRY_FILLED, EventKind.STOP_HIT, EventKind.CLOSE_FILLED]
    assert minutes(outcome, EventKind.ENTRY_FILLED) == minutes(outcome, EventKind.STOP_HIT) == [6]


def _call_put_and_a_waiting_call() -> TableMarket:
    """A stops at minute 10. B has no stop. C waits on momentum, and its level is reached at minute 12."""
    return TableMarket(
        underlying=steady("500"),
        picks=picked(("a", "500", CALL), ("b", "500", PUT), ("c", "500", CALL_505)),
        options={
            CALL: {**flat("2.00", 5, 9), 10: ("2.00", "1.40"), 11: ("1.40", "1.40")},
            PUT: {**flat("3.00", 5, 10), **flat("3.10", 11, 61)},
            CALL_505: {**flat("1.00", 5, 11), 12: ("1.00", "1.30"), **flat("1.30", 13, 61)},
        },
    )


def _three_legs(square_off: str):
    return setup(
        leg("a", "call", stop=STOP_25),
        leg("b", "put"),
        leg("c", "call", momentum=("percent_up", "25")),
        square_off=square_off,
    )


def test_under_complete_one_legs_stop_closes_the_other_and_cancels_a_leg_still_waiting() -> None:
    outcome = replay(_three_legs("complete"), _call_put_and_a_waiting_call())

    assert [row(trade) for trade in outcome.trades] == [
        ("a", 0, 6, D("2.00"), D("2.00"), "stop", 11, D("1.40"), D("1.40")),
        ("b", 0, 6, D("3.00"), D("3.00"), "square_off", 11, D("3.10"), D("3.10")),
    ]
    # One group, decided at minute 10. C never traded though its level was reached at minute 12.
    assert {trade.exit_group for trade in outcome.trades} == {1}
    assert {trade.exit_decided_at_ms for trade in outcome.trades} == {at(10)}
    assert outcome.exit_counts == {ExitReason.STOP: 1, ExitReason.SQUARE_OFF: 1}
    assert minutes(outcome, EventKind.LEG_CANCELLED, "c") == [10]
    assert minutes(outcome, EventKind.MOMENTUM_HIT) == []
    # C is said to be cancelled by the square-off, not by the exit time, and it is its first entry that never came.
    cancelled = [e for e in outcome.events if e.kind is EventKind.LEG_CANCELLED]
    assert [(e.leg_id, e.reason, e.entry_number) for e in cancelled] == [("c", "square_off", 0)]


def test_under_partial_one_legs_stop_leaves_the_other_leg_and_the_waiting_leg_to_carry_on() -> None:
    outcome = replay(_three_legs("partial"), _call_put_and_a_waiting_call())

    assert [row(trade) for trade in outcome.trades] == [
        ("a", 0, 6, D("2.00"), D("2.00"), "stop", 11, D("1.40"), D("1.40")),
        ("b", 0, 6, D("3.00"), D("3.00"), "exit_time", 61, D("3.10"), D("3.10")),
        ("c", 0, 13, D("1.30"), D("1.30"), "exit_time", 61, D("1.30"), D("1.30")),
    ]
    assert minutes(outcome, EventKind.MOMENTUM_HIT) == [12]


@pytest.mark.parametrize("square_off", ["partial", "complete"])
def test_two_stops_in_one_minute_are_one_group_and_each_leg_is_sold_once(square_off: str) -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("a", "500", CALL), ("b", "500", PUT)),
        options={
            CALL: {**flat("2.00", 5, 9), 10: ("2.00", "1.40"), 11: ("1.40", "1.40")},  # stop at 1.50
            PUT: {**flat("3.00", 5, 9), 10: ("3.00", "2.20"), 11: ("2.20", "2.20")},  # stop at 3.00 * 0.75 = 2.25
        },
    )

    outcome = replay(
        setup(leg("a", "call", stop=STOP_25), leg("b", "put", stop=STOP_25), square_off=square_off), market
    )

    assert [row(trade) for trade in outcome.trades] == [
        ("a", 0, 6, D("2.00"), D("2.00"), "stop", 11, D("1.40"), D("1.40")),
        ("b", 0, 6, D("3.00"), D("3.00"), "stop", 11, D("2.20"), D("2.20")),
    ]
    assert [trade.exit_group for trade in outcome.trades] == [1, 1]
    assert outcome.event_counts[EventKind.STOP_HIT] == 2
    assert outcome.event_counts[EventKind.CLOSE_FILLED] == 2
    assert EventKind.SQUARED_OFF not in outcome.event_counts


def test_the_exit_time_closes_every_open_leg_cancels_the_waiting_and_nothing_enters_from_then_on() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("a", "500", CALL), ("b", "500", PUT)),
        options={
            CALL: flat("2.00", 5, 61),
            # B's level, 3.75, is first reached by the close at minute 60 and stays reached.
            PUT: {**flat("3.00", 5, 59), 60: ("3.00", "3.80"), **flat("3.80", 61, 70)},
        },
    )

    outcome = replay(setup(leg("a", "call"), leg("b", "put", momentum=("percent_up", "25"))), market)

    assert [row(trade) for trade in outcome.trades] == [
        ("a", 0, 6, D("2.00"), D("2.00"), "exit_time", 61, D("2.00"), D("2.00"))
    ]
    assert (outcome.trades[0].exit_group, outcome.trades[0].exit_decided_at_ms) == (1, at(60))
    assert kinds(outcome, "b") == [EventKind.PICKED, EventKind.STARTING_PRICE_TAKEN, EventKind.LEG_CANCELLED]
    assert minutes(outcome, EventKind.LEG_CANCELLED, "b") == [60]
    cancelled = [e for e in outcome.events if e.kind is EventKind.LEG_CANCELLED]
    assert [(e.leg_id, e.reason, e.entry_number) for e in cancelled] == [("b", "exit_time", 0)]


def test_the_legs_the_exit_time_closes_are_one_exit_group_each_with_its_own_exit_time_event() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("a", "500", CALL), ("b", "500", PUT), ("c", "500", CALL_505)),
        options={
            CALL: {**flat("2.00", 5, 9), 10: ("2.00", "1.40"), 11: ("1.40", "1.40")},  # A's stop sits at 1.50
            PUT: flat("3.00", 5, 61),
            CALL_505: flat("1.00", 5, 61),
        },
    )

    outcome = replay(setup(leg("a", "call", stop=STOP_25), leg("b", "put"), leg("c", "call")), market)

    assert [row(trade) for trade in outcome.trades] == [
        ("a", 0, 6, D("2.00"), D("2.00"), "stop", 11, D("1.40"), D("1.40")),
        ("b", 0, 6, D("3.00"), D("3.00"), "exit_time", 61, D("3.00"), D("3.00")),
        ("c", 0, 6, D("1.00"), D("1.00"), "exit_time", 61, D("1.00"), D("1.00")),
    ]
    # A's stop is the first group and the exit time is the second, and B and C are in it together.
    assert [trade.exit_group for trade in outcome.trades] == [1, 2, 2]
    assert [trade.exit_decided_at_ms for trade in outcome.trades] == [at(10), at(60), at(60)]
    exit_events = [(e.leg_id, minute_of(e.at_ms)) for e in outcome.events if e.kind is EventKind.EXIT_TIME]
    assert exit_events == [("b", 60), ("c", 60)]


def test_an_entry_decided_just_before_the_exit_time_fills_at_it_and_is_closed_in_the_same_minute() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("a", "500", CALL)),
        options={
            CALL: {
                **flat("2.00", 5, 58),
                59: ("2.00", "2.50"),  # the level 2.50 is reached: an order is decided
                60: ("2.60", "2.60"),  # it fills at this open, and the exit time arrives in this same minute
                61: ("2.70", "2.70"),  # the sale fills here
            }
        },
    )

    outcome = replay(setup(leg("a", "call", momentum=("percent_up", "25"))), market)

    assert [row(trade) for trade in outcome.trades] == [
        ("a", 0, 60, D("2.60"), D("2.60"), "exit_time", 61, D("2.70"), D("2.70"))
    ]
    assert outcome.trades[0].pnl_usd == D("10.00")  # (2.70 - 2.60) * 100


def test_a_close_with_no_trade_in_the_next_minute_is_tried_again_and_fills_at_a_later_bar() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("call", "500", CALL)),
        options={
            CALL: {
                **flat("2.00", 5, 9),
                10: ("2.00", "1.40"),  # the stop is hit
                # nothing traded in minutes 11 and 12
                13: ("1.30", "1.30"),  # the sale fills here
            }
        },
    )

    outcome = replay(setup(leg(stop=STOP_25)), market)

    assert [row(trade) for trade in outcome.trades] == [
        ("call", 0, 6, D("2.00"), D("2.00"), "stop", 13, D("1.30"), D("1.30"))
    ]
    assert outcome.trades[0].exit_decided_at_ms == at(10)
    assert minutes(outcome, EventKind.CLOSE_FILLED) == [13]


def test_a_close_at_the_exit_time_is_tried_again_after_the_exit_time() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("call", "500", CALL)),
        options={CALL: {**flat("2.00", 5, 60), 63: ("1.90", "1.90")}},  # nothing traded in minutes 61 and 62
    )

    outcome = replay(setup(leg()), market)

    assert [row(trade) for trade in outcome.trades] == [
        ("call", 0, 6, D("2.00"), D("2.00"), "exit_time", 63, D("1.90"), D("1.90"))
    ]


def test_a_sale_that_first_finds_a_trade_in_the_sessions_last_minute_is_filled_not_marked() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("call", "500", CALL)),
        options={CALL: {**flat("2.00", 5, 60), 390: ("1.90", "1.90")}},  # nothing traded from minute 61 to 389
    )

    outcome = replay(setup(leg()), market)

    assert outcome.status is SessionStatus.TRADED
    assert [row(trade) for trade in outcome.trades] == [
        ("call", 0, 6, D("2.00"), D("2.00"), "exit_time", 390, D("1.90"), D("1.90"))
    ]


def test_a_leg_never_sold_by_the_close_is_marked_at_its_last_seen_close_and_the_session_is_incomplete() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("a", "500", CALL), ("b", "500", PUT)),
        options={
            CALL: {**flat("2.00", 5, 49), 50: ("2.00", "1.75")},  # and never again: no sale can fill
            PUT: flat("3.00", 5, 61),
        },
    )

    outcome = replay(setup(leg("a", "call"), leg("b", "put")), market)

    assert outcome.status is SessionStatus.INCOMPLETE
    assert [row(trade) for trade in outcome.trades] == [
        ("a", 0, 6, D("2.00"), D("2.00"), "marked_not_sold", 50, D("1.75"), D("1.75")),
        ("b", 0, 6, D("3.00"), D("3.00"), "exit_time", 61, D("3.00"), D("3.00")),
    ]
    marked = outcome.trades[0]
    assert (marked.exit_group, marked.exit_decided_at_ms, marked.exit_fee_usd) == (1, at(60), D("0"))
    assert marked.pnl_usd == D("-25.00")  # (1.75 - 2.00) * 100
    # A bought for 200 and was not paid back; B was bought for 300 and sold for 300.
    assert outcome.cash_change_usd == D("-200.00")
    # A's mark is worth 1.75 * 100 = 175.00. The profit is A's -25.00 and B's 0.00, and it is the cash plus the mark.
    assert (outcome.pnl_usd, outcome.marked_value_usd) == (D("-25.00"), D("175.00"))
    assert outcome.pnl_usd == outcome.cash_change_usd + outcome.marked_value_usd
    assert outcome.exit_counts == {ExitReason.MARKED_NOT_SOLD: 1, ExitReason.EXIT_TIME: 1}
    marked_events = [e for e in outcome.events if e.kind is EventKind.MARKED_NOT_SOLD]
    assert [(e.leg_id, e.at_ms, e.price) for e in marked_events] == [("a", FULL_CLOSE_MS, D("1.75"))]


def test_a_leg_whose_stop_was_hit_but_whose_sale_never_filled_is_marked_at_the_close_that_hit_it() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("call", "500", CALL)),
        options={CALL: {**flat("2.00", 5, 39), 40: ("2.00", "1.40")}},  # then nothing traded for the rest of the day
    )

    outcome = replay(setup(leg(stop=STOP_25)), market)

    assert outcome.status is SessionStatus.INCOMPLETE
    assert [row(trade) for trade in outcome.trades] == [
        ("call", 0, 6, D("2.00"), D("2.00"), "marked_not_sold", 40, D("1.40"), D("1.40"))
    ]
    assert (outcome.trades[0].exit_group, outcome.trades[0].exit_decided_at_ms) == (1, at(40))
    assert outcome.trades[0].pnl_usd == D("-60.00")  # (1.40 - 2.00) * 100
