"""What decides whether a session trades at all, and when each leg buys.

Every expectation is worked by hand from the rules and written as a literal. Minutes are counted
after the open: minute ``k`` is the bar closing at ``at(k)``, an order decided at minute ``k`` fills at
the open of minute ``k + 1``, and the setups here enter at minute 5 and leave at minute 60.
"""

from __future__ import annotations

import pytest

from app.options_backtest.market import PickRefusal, PickRefused
from app.options_backtest.results import EventKind, SessionStatus, SkipReason
from tests.options_backtest._market import (
    FULL_CLOSE_MS,
    HALF_CLOSE_MS,
    D,
    TableMarket,
    after_open,
    at,
    before_close,
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


@pytest.mark.parametrize(
    ("entry_minute", "exit_minute", "fits"),
    [
        (0, 60, False),  # the open's own minute has no closed bar to read yet
        (1, 60, True),  # one minute after the open, to the second
        (5, 389, True),  # one minute before the close, to the second
        (5, 390, False),  # an exit at the close has no next minute to fill in
        (60, 60, False),  # one instant cannot be both
        (61, 60, False),  # leaving before entering
        (60, 61, True),
    ],
)
def test_a_window_fits_only_between_a_minute_after_the_open_and_a_minute_before_the_close(
    entry_minute: int, exit_minute: int, fits: bool
) -> None:
    market = TableMarket(
        underlying=steady("500"), picks=picked(("call", "500", CALL)), options={CALL: flat("2.00", 1, 390)}
    )

    outcome = replay(setup(leg(), entry=after_open(entry_minute), exit=after_open(exit_minute)), market)

    if fits:
        assert outcome.skip_reason is None
        assert outcome.status is SessionStatus.TRADED
    else:
        assert outcome.skip_reason is SkipReason.WINDOW_DOES_NOT_FIT
        assert (outcome.status, outcome.trades, outcome.events, outcome.cash_change_usd) == (
            SessionStatus.SKIPPED,
            (),
            (),
            D(0),
        )


def test_a_session_where_the_underlying_did_not_trade_at_the_entry_time_is_skipped() -> None:
    market = TableMarket(
        underlying=steady("500", 6),  # nothing closed at minute 5
        picks=picked(("call", "500", CALL)),
        options={CALL: flat("2.00", 1, 390)},
    )

    outcome = replay(setup(leg()), market)

    assert outcome.status is SessionStatus.SKIPPED
    assert outcome.skip_reason is SkipReason.UNDERLYING_DID_NOT_TRADE
    assert outcome.trades == ()


def test_a_half_day_keeps_the_exit_a_set_distance_before_its_earlier_close() -> None:
    # The exit is 46 minutes before the close: minute 344 of a full session, minute 164 of one that closes at 13:00.
    written = setup(leg(), exit=before_close(46))
    options = {CALL: flat("2.00", 1, 390)}
    full = replay(written, TableMarket(underlying=steady("500"), picks=picked(("call", "500", CALL)), options=options))
    half = replay(
        written,
        TableMarket(
            underlying=steady("500"),
            picks=picked(("call", "500", CALL)),
            options=options,
            session_close_ms=HALF_CLOSE_MS,
        ),
    )

    assert [row(trade) for trade in full.trades] == [
        ("call", 0, 6, D("2.00"), D("2.00"), "exit_time", 345, D("2.00"), D("2.00"))
    ]
    assert [row(trade) for trade in half.trades] == [
        ("call", 0, 6, D("2.00"), D("2.00"), "exit_time", 165, D("2.00"), D("2.00"))
    ]
    assert full.trades[0].exit_decided_at_ms == FULL_CLOSE_MS - 46 * 60_000
    assert half.trades[0].exit_decided_at_ms == HALF_CLOSE_MS - 46 * 60_000


def test_a_window_that_fits_a_full_day_can_miss_a_half_day() -> None:
    # Enter at minute 190, leave 30 minutes before the close: minute 360 of a full day, minute 180 of a half day.
    written = setup(leg(), entry=after_open(190), exit=before_close(30))
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("call", "500", CALL)),
        options={CALL: flat("2.00", 1, 390)},
        session_close_ms=HALF_CLOSE_MS,
    )

    outcome = replay(written, market)

    assert outcome.skip_reason is SkipReason.WINDOW_DOES_NOT_FIT


def test_momentum_buys_at_the_next_opening_price_with_slippage_never_at_the_trigger_level() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("call", "500", CALL)),
        options={
            CALL: {
                5: ("1.95", "2.00"),  # opened lower: the starting price is the close, taken at the entry time
                6: ("2.00", "2.49"),  # 2.49 is short of 2.00 * 1.25 = 2.50: no buy
                7: ("2.49", "2.50"),  # closes on the level: an order is decided
                8: ("2.60", "2.55"),  # and fills at this bar's open
                **flat("2.55", 9, 61),
            }
        },
    )

    outcome = replay(setup(leg(momentum=("percent_up", "25")), slippage="0.3"), market)

    # Bought: 2.60 * 1.003 = 2.6078, up to 2.61. Sold at minute 61: 2.55 * 0.997 = 2.54235, down to 2.54.
    assert [row(trade) for trade in outcome.trades] == [
        ("call", 0, 8, D("2.60"), D("2.61"), "exit_time", 61, D("2.55"), D("2.54"))
    ]
    trade = outcome.trades[0]
    assert (trade.starting_price, trade.momentum_level) == (D("2.00"), D("2.50"))
    assert (trade.entry_decided_at_ms, trade.pnl_usd) == (at(7), D("-7.00"))  # (2.54 - 2.61) * 100
    assert minutes(outcome, EventKind.MOMENTUM_HIT) == [7]


def test_slippage_of_a_third_of_a_percent_buys_2_57_at_2_58_and_sells_1_87_at_1_86() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("call", "500", CALL)),
        options={CALL: {5: ("2.57", "2.57"), 6: ("2.57", "2.57"), **flat("1.87", 7, 61)}},
    )

    outcome = replay(setup(leg(), slippage="0.3"), market)

    # 2.57 * 1.003 = 2.57771 up to 2.58; 1.87 * 0.997 = 1.86439 down to 1.86.
    assert [row(trade) for trade in outcome.trades] == [
        ("call", 0, 6, D("2.57"), D("2.58"), "exit_time", 61, D("1.87"), D("1.86"))
    ]
    assert outcome.trades[0].pnl_usd == D("-72.00")  # (1.86 - 2.58) * 100


def test_a_high_above_the_momentum_level_with_a_close_below_it_does_not_buy() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("call", "500", CALL)),
        options={
            CALL: {
                5: ("2.00", "2.00"),
                6: ("2.00", "2.80", "1.90", "2.40"),  # high 2.80 is past the level 2.50; the close is not
                **flat("2.40", 7, 61),
            }
        },
    )

    outcome = replay(setup(leg(momentum=("percent_up", "25"))), market)

    assert outcome.status is SessionStatus.NO_TRADE
    assert outcome.trades == ()
    assert minutes(outcome, EventKind.MOMENTUM_HIT) == []
    # The day had a starting price and no momentum hit: that is how a caller counts a day without momentum.
    assert kinds(outcome, "call") == [EventKind.PICKED, EventKind.STARTING_PRICE_TAKEN, EventKind.LEG_CANCELLED]


def test_a_momentum_down_leg_buys_once_a_close_falls_to_its_level() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("call", "500", CALL)),
        options={
            CALL: {
                5: ("2.00", "2.00"),  # 25% down from 2.00 is 1.50
                6: ("2.00", "1.51"),  # short
                7: ("1.51", "1.50"),  # on the level: an order is decided
                8: ("1.45", "1.45"),  # and fills at this open
                **flat("1.45", 9, 61),
            }
        },
    )

    outcome = replay(setup(leg(momentum=("percent_down", "25"))), market)

    assert [row(trade) for trade in outcome.trades] == [
        ("call", 0, 8, D("1.45"), D("1.45"), "exit_time", 61, D("1.45"), D("1.45"))
    ]
    assert outcome.trades[0].momentum_level == D("1.50")


@pytest.mark.parametrize(
    ("call_hits", "put_hits", "entered"),
    [
        (True, False, ["call"]),
        (False, True, ["put"]),
        (True, True, ["call", "put"]),
        (False, False, []),
    ],
)
def test_only_the_legs_whose_momentum_is_reached_enter(call_hits: bool, put_hits: bool, entered: list[str]) -> None:
    # The call starts at 2.00 and needs 2.50; the put starts at 3.00 and needs 3.75.
    call_price = "2.50" if call_hits else "2.40"
    put_price = "3.75" if put_hits else "3.70"
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("call", "500", CALL), ("put", "500", PUT)),
        options={
            CALL: {5: ("2.00", "2.00"), 6: ("2.00", call_price), **flat(call_price, 7, 61)},
            PUT: {5: ("3.00", "3.00"), 6: ("3.00", put_price), **flat(put_price, 7, 61)},
        },
    )

    outcome = replay(
        setup(
            leg("call", "call", momentum=("percent_up", "25")),
            leg("put", "put", momentum=("percent_up", "25")),
        ),
        market,
    )

    assert [trade.leg_id for trade in outcome.trades] == entered
    assert outcome.status is (SessionStatus.TRADED if entered else SessionStatus.NO_TRADE)


def test_a_leg_with_no_momentum_rule_buys_at_the_entry_time() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("call", "500", CALL)),
        options={CALL: {5: ("2.10", "2.00"), 6: ("2.05", "2.10"), **flat("2.10", 7, 61)}},
    )

    outcome = replay(setup(leg()), market)

    trade = outcome.trades[0]
    assert (trade.entry_decided_at_ms, minute_of(trade.entry.bar_close_ms)) == (at(5), 6)
    assert (trade.starting_price, trade.momentum_level, trade.stop_level) == (None, None, None)
    assert trade.entry.seen == D("2.05")


def test_an_entry_with_no_trade_in_the_next_minute_is_dropped_and_the_leg_is_done() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("call", "500", CALL)),
        options={CALL: {5: ("2.00", "2.00"), **flat("2.00", 7, 61)}},  # nothing traded in minute 6
    )

    outcome = replay(setup(leg()), market)

    assert outcome.status is SessionStatus.NO_TRADE
    assert outcome.trades == ()
    assert kinds(outcome) == [EventKind.PICKED, EventKind.ENTRY_DROPPED]
    assert minutes(outcome, EventKind.ENTRY_DROPPED) == [6]


def test_a_momentum_entry_with_no_trade_in_the_next_minute_goes_back_to_waiting_and_buys_later() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("call", "500", CALL)),
        options={
            CALL: {
                5: ("2.00", "2.00"),
                6: ("2.00", "2.50"),  # the level is reached: an order is decided
                # nothing traded in minute 7, so the order is dropped
                8: ("2.55", "2.60"),  # still past the level: asked again
                9: ("2.70", "2.70"),  # and filled here
                **flat("2.70", 10, 61),
            }
        },
    )

    outcome = replay(setup(leg(momentum=("percent_up", "25"))), market)

    assert [row(trade) for trade in outcome.trades] == [
        ("call", 0, 9, D("2.70"), D("2.70"), "exit_time", 61, D("2.70"), D("2.70"))
    ]
    assert minutes(outcome, EventKind.MOMENTUM_HIT) == [6, 8]
    assert minutes(outcome, EventKind.ENTRY_DROPPED) == [7]


def test_a_leg_with_no_starting_price_at_the_entry_time_does_not_trade() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("call", "500", CALL)),
        options={CALL: flat("3.00", 6, 61)},  # nothing traded in minute 5, then a rise that would have bought
    )

    outcome = replay(setup(leg(momentum=("percent_up", "25"))), market)

    assert outcome.status is SessionStatus.NO_TRADE
    assert kinds(outcome) == [EventKind.PICKED, EventKind.NO_STARTING_PRICE]
    assert outcome.events[1].at_ms == at(5)


def test_a_refused_pick_leaves_the_leg_out_for_the_day_and_carries_the_reason() -> None:
    refusal = PickRefused(PickRefusal.STRIKE_NOT_LISTED, "no strike is listed near 500")
    market = TableMarket(
        underlying=steady("500"),
        picks={("call", "500"): refusal, **picked(("put", "500", PUT))},
        options={PUT: flat("3.00", 5, 61)},
    )

    outcome = replay(setup(leg("call", "call"), leg("put", "put")), market)

    assert [trade.leg_id for trade in outcome.trades] == ["put"]
    refused = [event for event in outcome.events if event.kind is EventKind.PICK_REFUSED]
    assert [(e.leg_id, minute_of(e.at_ms), e.reason, e.detail) for e in refused] == [
        ("call", 5, "strike_not_listed", "no strike is listed near 500")
    ]
