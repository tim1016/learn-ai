"""Re-entry after a stop: when it is earned, when it is blocked, and when it is bought.

Minutes are counted after the open; the setups here enter at minute 5 and leave at minute 60. Each
expectation is worked by hand and written as a literal. A trade reads as
``(leg, entry number, entry minute, seen, filled, exit reason, exit minute, seen, filled)``.
"""

from __future__ import annotations

import pytest

from app.options_backtest.market import PickRefusal, PickRefused
from app.options_backtest.results import EventKind, SessionStatus
from tests.options_backtest._market import (
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
CALL_498 = contract("call", "498")
CALL_505 = contract("call", "505")
PUT = contract("put", "500")
STOP_25 = ("percent", "25")
AT_ONCE_1 = ("at_once", 1)


def test_a_re_entry_at_once_picks_a_fresh_contract_from_that_minutes_underlying_and_buys_with_no_wait() -> None:
    market = TableMarket(
        underlying={**steady("500", 1, 10), **steady("498", 11)},
        picks=picked(("call", "500", CALL), ("call", "498", CALL_498)),
        options={
            CALL: {**flat("2.00", 5, 9), 10: ("2.00", "1.40"), 11: ("1.40", "1.40")},  # stopped at 10, sold at 11
            CALL_498: {
                12: ("3.00", "3.00"),  # the fresh contract: bought at this open, stop at 3.00 * 0.75 = 2.25
                13: ("3.00", "3.00"),
                14: ("3.00", "2.20"),  # 2.20 is under 2.25, though well above the first stop's 1.50
                15: ("2.20", "2.20"),
            },
        },
    )

    outcome = replay(setup(leg(stop=STOP_25, reentry=AT_ONCE_1)), market)

    assert [row(trade) for trade in outcome.trades] == [
        ("call", 0, 6, D("2.00"), D("2.00"), "stop", 11, D("1.40"), D("1.40")),
        ("call", 1, 12, D("3.00"), D("3.00"), "stop", 15, D("2.20"), D("2.20")),
    ]
    reentry = outcome.trades[1]
    assert (reentry.contract, reentry.entry_decided_at_ms, reentry.stop_level) == (CALL_498, at(11), D("2.25"))
    assert (reentry.starting_price, reentry.momentum_level) == (None, None)
    assert minutes(outcome, EventKind.RE_ENTRY_EARNED) == [10]
    assert minutes(outcome, EventKind.PICKED) == [5, 11]


def test_a_re_entry_on_momentum_takes_a_new_starting_price_and_buys_only_when_its_level_is_reached() -> None:
    market = TableMarket(
        underlying={**steady("500", 1, 9), **steady("498", 10)},
        picks=picked(("call", "500", CALL), ("call", "498", CALL_498)),
        options={
            CALL: {
                5: ("2.00", "2.00"),
                6: ("2.00", "2.50"),  # 2.00 * 1.25 = 2.50 is reached
                7: ("2.50", "2.50"),  # bought: the stop sits at 2.50 * 0.75 = 1.875
                8: ("2.50", "2.50"),
                9: ("2.50", "1.80"),  # stopped
                10: ("1.80", "1.80"),  # sold
            },
            CALL_498: {
                10: ("0.95", "1.00"),  # the fresh contract's new starting price is this close; the level is 1.25
                11: ("1.00", "1.20"),  # short
                12: ("1.20", "1.25"),  # on the level: an order is decided
                13: ("1.30", "1.30"),  # bought here, with a stop at 1.30 * 0.75 = 0.975
                **flat("1.30", 14, 61),
            },
        },
    )

    outcome = replay(setup(leg(momentum=("percent_up", "25"), stop=STOP_25, reentry=("on_momentum", 1))), market)

    assert [row(trade) for trade in outcome.trades] == [
        ("call", 0, 7, D("2.50"), D("2.50"), "stop", 10, D("1.80"), D("1.80")),
        ("call", 1, 13, D("1.30"), D("1.30"), "exit_time", 61, D("1.30"), D("1.30")),
    ]
    reentry = outcome.trades[1]
    assert (reentry.contract, reentry.starting_price, reentry.momentum_level) == (CALL_498, D("1.00"), D("1.25"))
    assert (reentry.entry_decided_at_ms, reentry.stop_level) == (at(12), D("0.975"))
    assert minutes(outcome, EventKind.MOMENTUM_HIT) == [6, 12]
    assert minutes(outcome, EventKind.STARTING_PRICE_TAKEN) == [5, 10]


def test_a_re_entry_at_once_does_not_wait_on_the_legs_momentum_rule() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("call", "500", CALL)),
        options={
            CALL: {
                5: ("2.00", "2.00"),
                6: ("2.00", "2.50"),  # the first entry waited for this
                7: ("2.50", "2.50"),
                8: ("2.50", "2.50"),
                9: ("2.50", "1.80"),  # stopped
                10: ("1.80", "1.80"),  # sold
                **flat("1.90", 11, 61),  # bought at 1.90 in the next minute, 1.90 being nowhere near a level
            }
        },
    )

    outcome = replay(setup(leg(momentum=("percent_up", "25"), stop=STOP_25, reentry=AT_ONCE_1)), market)

    assert [row(trade) for trade in outcome.trades] == [
        ("call", 0, 7, D("2.50"), D("2.50"), "stop", 10, D("1.80"), D("1.80")),
        ("call", 1, 11, D("1.90"), D("1.90"), "exit_time", 61, D("1.90"), D("1.90")),
    ]
    assert (outcome.trades[1].starting_price, outcome.trades[1].momentum_level) == (None, None)
    assert minutes(outcome, EventKind.MOMENTUM_HIT) == [6]


def _one_stop_at(stop_minute: int) -> TableMarket:
    """Bought at 2.00 in minute 6 (stop at 1.50), stopped at ``stop_minute``, a fresh buy 1.50 two minutes after."""
    return TableMarket(
        underlying=steady("500"),
        picks=picked(("call", "500", CALL)),
        options={
            CALL: {
                **flat("2.00", 5, stop_minute - 1),
                stop_minute: ("2.00", "1.40"),
                stop_minute + 1: ("1.40", "1.40"),
                stop_minute + 2: ("1.50", "1.50"),
                **flat("1.50", stop_minute + 3, 61),
            }
        },
    )


@pytest.mark.parametrize(
    ("stop_minute", "trades", "earned", "blocked"),
    [
        pytest.param(
            20,
            (
                ("call", 0, 6, D("2.00"), D("2.00"), "stop", 21, D("1.40"), D("1.40")),
                ("call", 1, 22, D("1.50"), D("1.50"), "exit_time", 61, D("1.50"), D("1.50")),
            ),
            (20,),
            (),
            id="a_stop_at_exactly_the_cutoff_earns_a_re_entry",
        ),
        pytest.param(
            21,
            (("call", 0, 6, D("2.00"), D("2.00"), "stop", 22, D("1.40"), D("1.40")),),
            (),
            (21,),
            id="a_stop_one_minute_after_the_cutoff_earns_none_and_says_so",
        ),
    ],
)
def test_a_stop_earns_a_re_entry_up_to_the_cutoff_and_none_after_it(
    stop_minute: int,
    trades: tuple[tuple[object, ...], ...],
    earned: tuple[int, ...],
    blocked: tuple[int, ...],
) -> None:
    outcome = replay(
        setup(leg(stop=STOP_25, reentry=AT_ONCE_1), no_reentry_after=after_open(20)), _one_stop_at(stop_minute)
    )

    assert tuple(row(trade) for trade in outcome.trades) == trades, "the trades"
    assert tuple(minutes(outcome, EventKind.RE_ENTRY_EARNED)) == earned, "the minutes a re-entry was earned"
    assert tuple(minutes(outcome, EventKind.RE_ENTRY_BLOCKED_BY_CUTOFF)) == blocked, (
        "the minutes a re-entry was blocked by the cutoff"
    )


def _stopped_at_on_a_half_day(stop_minute: int) -> TableMarket:
    """The same stop and fresh buy as ``_one_stop_at``, in a session that closes at minute 210."""
    return TableMarket(
        underlying=steady("500", 1, 210),
        picks=picked(("call", "500", CALL)),
        options={
            CALL: {
                **flat("2.00", 5, stop_minute - 1),
                stop_minute: ("2.00", "1.40"),
                stop_minute + 1: ("1.40", "1.40"),
                stop_minute + 2: ("1.50", "1.50"),
                **flat("1.50", stop_minute + 3, 210),
            }
        },
        session_close_ms=HALF_CLOSE_MS,
    )


def test_a_cutoff_counted_from_the_close_of_a_full_day_lets_a_stop_on_it_re_enter_and_not_one_minute_later() -> None:
    # 390 - 375 = minute 15.
    written = setup(leg(stop=STOP_25, reentry=AT_ONCE_1), no_reentry_after=before_close(375))

    on_it = replay(written, _one_stop_at(15))
    after_it = replay(written, _one_stop_at(16))

    assert [row(trade) for trade in on_it.trades] == [
        ("call", 0, 6, D("2.00"), D("2.00"), "stop", 16, D("1.40"), D("1.40")),
        ("call", 1, 17, D("1.50"), D("1.50"), "exit_time", 61, D("1.50"), D("1.50")),
    ]
    assert minutes(on_it, EventKind.RE_ENTRY_EARNED) == [15]
    assert minutes(on_it, EventKind.RE_ENTRY_BLOCKED_BY_CUTOFF) == []
    assert [row(trade) for trade in after_it.trades] == [
        ("call", 0, 6, D("2.00"), D("2.00"), "stop", 17, D("1.40"), D("1.40"))
    ]
    assert minutes(after_it, EventKind.RE_ENTRY_BLOCKED_BY_CUTOFF) == [16]
    assert minutes(after_it, EventKind.RE_ENTRY_EARNED) == []


def test_a_cutoff_counted_from_the_close_of_a_half_day_is_that_far_before_its_earlier_close() -> None:
    # The session closes at minute 210: the exit, 10 minutes before it, is minute 200, and the cutoff, 30 minutes
    # before it, is minute 180. A cutoff counted from the open would be minute 30, or a full day's minute 360.
    written = setup(leg(stop=STOP_25, reentry=AT_ONCE_1), exit=before_close(10), no_reentry_after=before_close(30))

    on_it = replay(written, _stopped_at_on_a_half_day(180))
    after_it = replay(written, _stopped_at_on_a_half_day(181))

    # A stop closing at 180 is sold at 181, and its re-entry is decided at 181, bought at 182, and sold by the
    # exit time: decided at 200, filled at 201.
    assert [row(trade) for trade in on_it.trades] == [
        ("call", 0, 6, D("2.00"), D("2.00"), "stop", 181, D("1.40"), D("1.40")),
        ("call", 1, 182, D("1.50"), D("1.50"), "exit_time", 201, D("1.50"), D("1.50")),
    ]
    assert on_it.trades[1].exit_decided_at_ms == at(200)
    assert minutes(on_it, EventKind.RE_ENTRY_EARNED) == [180]
    assert minutes(on_it, EventKind.RE_ENTRY_BLOCKED_BY_CUTOFF) == []
    # A stop closing at 181 is one minute past the cutoff: sold at 182, and no re-entry.
    assert [row(trade) for trade in after_it.trades] == [
        ("call", 0, 6, D("2.00"), D("2.00"), "stop", 182, D("1.40"), D("1.40"))
    ]
    assert minutes(after_it, EventKind.RE_ENTRY_BLOCKED_BY_CUTOFF) == [181]
    assert minutes(after_it, EventKind.RE_ENTRY_EARNED) == []


def test_under_complete_every_leg_closed_after_the_cutoff_is_blocked_from_re_entering() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("a", "500", CALL), ("b", "500", PUT)),
        options={
            CALL: {**flat("2.00", 5, 20), 21: ("2.00", "1.40"), 22: ("1.40", "1.40")},
            PUT: {**flat("3.00", 5, 21), 22: ("3.10", "3.10")},
        },
    )
    written = setup(
        leg("a", "call", stop=STOP_25, reentry=AT_ONCE_1),
        leg("b", "put", reentry=AT_ONCE_1),
        square_off="complete",
        no_reentry_after=after_open(20),
    )

    outcome = replay(written, market)

    assert [row(trade) for trade in outcome.trades] == [
        ("a", 0, 6, D("2.00"), D("2.00"), "stop", 22, D("1.40"), D("1.40")),
        ("b", 0, 6, D("3.00"), D("3.00"), "square_off", 22, D("3.10"), D("3.10")),
    ]
    assert minutes(outcome, EventKind.RE_ENTRY_BLOCKED_BY_CUTOFF, "a") == [21]
    assert minutes(outcome, EventKind.RE_ENTRY_BLOCKED_BY_CUTOFF, "b") == [21]
    assert minutes(outcome, EventKind.RE_ENTRY_EARNED) == []


def test_a_first_entry_after_the_cutoff_is_still_taken() -> None:
    market = TableMarket(
        underlying=steady("500"), picks=picked(("call", "500", CALL)), options={CALL: flat("2.00", 1, 61)}
    )

    outcome = replay(setup(leg(), entry=after_open(25), no_reentry_after=after_open(20)), market)

    assert [row(trade) for trade in outcome.trades] == [
        ("call", 0, 26, D("2.00"), D("2.00"), "exit_time", 61, D("2.00"), D("2.00"))
    ]


def test_a_re_entry_earned_before_the_cutoff_may_be_decided_and_fill_after_it() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("call", "500", CALL)),
        options={
            CALL: {
                **flat("2.00", 5, 18),
                19: ("2.00", "1.40"),  # stopped one minute before the cutoff at 20
                # nothing traded in minutes 20 and 21: the sale waits
                22: ("1.40", "1.40"),  # sold; the re-entry is decided in this minute, past the cutoff
                23: ("1.60", "1.60"),  # and bought here
                **flat("1.60", 24, 61),
            }
        },
    )

    outcome = replay(setup(leg(stop=STOP_25, reentry=AT_ONCE_1), no_reentry_after=after_open(20)), market)

    assert [row(trade) for trade in outcome.trades] == [
        ("call", 0, 6, D("2.00"), D("2.00"), "stop", 22, D("1.40"), D("1.40")),
        ("call", 1, 23, D("1.60"), D("1.60"), "exit_time", 61, D("1.60"), D("1.60")),
    ]
    assert minutes(outcome, EventKind.RE_ENTRY_EARNED) == [19]


def _stopped_twice() -> TableMarket:
    """Bought at 2.00 and stopped at 10; bought again at 2.00 in minute 12 and stopped at 15."""
    return TableMarket(
        underlying=steady("500"),
        picks=picked(("call", "500", CALL)),
        options={
            CALL: {
                **flat("2.00", 5, 9),
                10: ("2.00", "1.40"),
                11: ("1.40", "1.40"),
                **flat("2.00", 12, 14),
                15: ("2.00", "1.40"),
                **flat("1.40", 16, 61),
            }
        },
    )


def test_a_leg_with_no_re_entry_left_is_not_counted_as_blocked_by_the_cutoff_and_one_with_a_re_entry_left_is() -> None:
    # Stopped at 10, which is before the cutoff at 12, so it earns its re-entry and buys again at 12; stopped
    # again at 15, which is after the cutoff.
    spent = replay(setup(leg(stop=STOP_25, reentry=("at_once", 1)), no_reentry_after=after_open(12)), _stopped_twice())
    one_left = replay(
        setup(leg(stop=STOP_25, reentry=("at_once", 2)), no_reentry_after=after_open(12)), _stopped_twice()
    )

    # With a count of one the re-entry is used up by 15: nothing is left for the cutoff to block.
    assert minutes(spent, EventKind.RE_ENTRY_EARNED) == [10]
    assert minutes(spent, EventKind.RE_ENTRY_BLOCKED_BY_CUTOFF) == []
    # With a count of two one is left, and the cutoff is what stops it.
    assert minutes(one_left, EventKind.RE_ENTRY_EARNED) == [10]
    assert minutes(one_left, EventKind.RE_ENTRY_BLOCKED_BY_CUTOFF) == [15]
    # Both took the same two trades.
    assert [trade.entry_number for trade in spent.trades] == [trade.entry_number for trade in one_left.trades] == [0, 1]


@pytest.mark.parametrize(
    ("count", "trades", "earned", "exit_groups"),
    [
        pytest.param(
            1,
            (
                ("call", 0, 6, D("2.00"), D("2.00"), "stop", 11, D("1.40"), D("1.40")),
                ("call", 1, 12, D("2.00"), D("2.00"), "stop", 16, D("1.40"), D("1.40")),
            ),
            (10,),
            (1, 2),
            id="with_a_count_of_one_a_second_stop_earns_nothing",
        ),
        pytest.param(
            2,
            # The third entry fills at the 1.40 open of minute 17; its stop is at 1.05, which 1.40 never reaches.
            (
                ("call", 0, 6, D("2.00"), D("2.00"), "stop", 11, D("1.40"), D("1.40")),
                ("call", 1, 12, D("2.00"), D("2.00"), "stop", 16, D("1.40"), D("1.40")),
                ("call", 2, 17, D("1.40"), D("1.40"), "exit_time", 61, D("1.40"), D("1.40")),
            ),
            (10, 15),
            # Each stop is its own group, and the exit time is the next.
            (1, 2, 3),
            id="with_a_count_of_two_the_second_stop_earns_a_second_re_entry_and_no_third",
        ),
    ],
)
def test_a_second_stop_earns_a_re_entry_only_while_the_count_has_one_left(
    count: int,
    trades: tuple[tuple[object, ...], ...],
    earned: tuple[int, ...],
    exit_groups: tuple[int, ...],
) -> None:
    outcome = replay(setup(leg(stop=STOP_25, reentry=("at_once", count))), _stopped_twice())

    assert tuple(row(trade) for trade in outcome.trades) == trades, "the trades"
    assert tuple(minutes(outcome, EventKind.RE_ENTRY_EARNED)) == earned, "the minutes a re-entry was earned"
    assert tuple(trade.exit_group for trade in outcome.trades) == exit_groups, "each stop is its own exit group"


def _three_legs_one_stop_at_ten() -> TableMarket:
    """A stops at minute 10, B has no stop, C waits on momentum and would hit at minute 12. All can re-enter once."""
    return TableMarket(
        underlying=steady("500"),
        picks=picked(("a", "500", CALL), ("b", "500", PUT), ("c", "500", CALL_505)),
        options={
            CALL: {**flat("2.00", 5, 9), 10: ("2.00", "1.40"), 11: ("1.40", "1.40"), **flat("1.60", 12, 61)},
            PUT: {**flat("3.00", 5, 11), **flat("3.20", 12, 61)},
            CALL_505: {**flat("1.00", 5, 11), 12: ("1.00", "1.30"), **flat("1.30", 13, 61)},
        },
    )


def test_under_complete_a_leg_closed_by_the_others_stop_may_re_enter_and_a_leg_that_never_filled_stays_cancelled() -> (
    None
):
    written = setup(
        leg("a", "call", stop=STOP_25, reentry=AT_ONCE_1),
        leg("b", "put", reentry=AT_ONCE_1),
        leg("c", "call", momentum=("percent_up", "25"), reentry=AT_ONCE_1),
        square_off="complete",
    )

    outcome = replay(written, _three_legs_one_stop_at_ten())

    assert [row(trade) for trade in outcome.trades] == [
        ("a", 0, 6, D("2.00"), D("2.00"), "stop", 11, D("1.40"), D("1.40")),
        ("b", 0, 6, D("3.00"), D("3.00"), "square_off", 11, D("3.00"), D("3.00")),
        ("a", 1, 12, D("1.60"), D("1.60"), "exit_time", 61, D("1.60"), D("1.60")),
        ("b", 1, 12, D("3.20"), D("3.20"), "exit_time", 61, D("3.20"), D("3.20")),
    ]
    assert minutes(outcome, EventKind.RE_ENTRY_EARNED, "a") == minutes(outcome, EventKind.RE_ENTRY_EARNED, "b") == [10]
    assert kinds(outcome, "c") == [EventKind.PICKED, EventKind.STARTING_PRICE_TAKEN, EventKind.LEG_CANCELLED]


def test_under_complete_no_leg_re_enters_until_every_close_of_the_group_has_filled() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("a", "500", CALL), ("b", "500", PUT)),
        options={
            CALL: {**flat("2.00", 5, 9), 10: ("2.00", "1.40"), 11: ("1.40", "1.40"), **flat("1.60", 14, 61)},
            # B's square-off finds no trade in minutes 11 and 12, and fills in minute 13.
            PUT: {**flat("3.00", 5, 10), 13: ("3.10", "3.10"), **flat("3.20", 14, 61)},
        },
    )
    written = setup(
        leg("a", "call", stop=STOP_25, reentry=AT_ONCE_1), leg("b", "put", reentry=AT_ONCE_1), square_off="complete"
    )

    outcome = replay(written, market)

    # A was sold at minute 11 but waits for B; both are decided at minute 13 and bought at 14.
    assert [row(trade) for trade in outcome.trades] == [
        ("a", 0, 6, D("2.00"), D("2.00"), "stop", 11, D("1.40"), D("1.40")),
        ("b", 0, 6, D("3.00"), D("3.00"), "square_off", 13, D("3.10"), D("3.10")),
        ("a", 1, 14, D("1.60"), D("1.60"), "exit_time", 61, D("1.60"), D("1.60")),
        ("b", 1, 14, D("3.20"), D("3.20"), "exit_time", 61, D("3.20"), D("3.20")),
    ]
    assert minutes(outcome, EventKind.PICKED) == [5, 5, 13, 13]


def test_under_partial_a_close_still_pending_holds_back_only_its_own_legs_re_entry() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("a", "500", CALL), ("b", "500", PUT)),
        options={
            # A is stopped at 10 and finds no trade in minutes 11 and 12: its sale fills at 13.
            CALL: {**flat("2.00", 5, 9), 10: ("2.00", "1.40"), 13: ("1.30", "1.30"), **flat("1.50", 14, 61)},
            # B is stopped at 10 too, sold at 11, and goes on at once.
            PUT: {**flat("3.00", 5, 9), 10: ("3.00", "2.20"), 11: ("2.20", "2.20"), **flat("3.30", 12, 61)},
        },
    )
    written = setup(leg("a", "call", stop=STOP_25, reentry=AT_ONCE_1), leg("b", "put", stop=STOP_25, reentry=AT_ONCE_1))

    outcome = replay(written, market)

    assert [row(trade) for trade in outcome.trades] == [
        ("a", 0, 6, D("2.00"), D("2.00"), "stop", 13, D("1.30"), D("1.30")),
        ("b", 0, 6, D("3.00"), D("3.00"), "stop", 11, D("2.20"), D("2.20")),
        ("b", 1, 12, D("3.30"), D("3.30"), "exit_time", 61, D("3.30"), D("3.30")),
        ("a", 1, 14, D("1.50"), D("1.50"), "exit_time", 61, D("1.50"), D("1.50")),
    ]


def test_an_exit_decided_in_a_minute_suppresses_an_entry_in_that_minute_and_it_is_asked_again_next_minute() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("a", "500", CALL), ("c", "500", CALL_505)),
        options={
            CALL: {**flat("2.00", 5, 9), 10: ("2.00", "1.40"), 11: ("1.40", "1.40")},
            CALL_505: {
                **flat("1.00", 5, 9),
                10: ("1.00", "1.30"),  # the level 1.25 is reached in the same minute A is stopped: no entry
                11: ("1.30", "1.35"),  # and is reached again here: an order is decided
                12: ("1.40", "1.40"),  # filled here, not a minute earlier
                **flat("1.40", 13, 61),
            },
        },
    )
    written = setup(leg("a", "call", stop=STOP_25), leg("c", "call", momentum=("percent_up", "25")))

    outcome = replay(written, market)

    assert [row(trade) for trade in outcome.trades] == [
        ("a", 0, 6, D("2.00"), D("2.00"), "stop", 11, D("1.40"), D("1.40")),
        ("c", 0, 12, D("1.40"), D("1.40"), "exit_time", 61, D("1.40"), D("1.40")),
    ]
    assert minutes(outcome, EventKind.MOMENTUM_HIT) == [11]


def test_an_exit_decided_in_a_minute_holds_a_re_entry_to_the_next_minute() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("a", "500", CALL), ("b", "500", PUT)),
        options={
            # A is stopped at 10 and sold at 11, where its re-entry would start.
            CALL: {**flat("2.00", 5, 9), 10: ("2.00", "1.40"), 11: ("1.40", "1.40"), **flat("1.60", 13, 61)},
            # B is stopped at 11, the same minute: nothing enters in it.
            PUT: {**flat("3.00", 5, 10), 11: ("3.00", "2.20"), 12: ("2.20", "2.20")},
        },
    )
    written = setup(leg("a", "call", stop=STOP_25, reentry=AT_ONCE_1), leg("b", "put", stop=STOP_25))

    outcome = replay(written, market)

    assert [row(trade) for trade in outcome.trades] == [
        ("a", 0, 6, D("2.00"), D("2.00"), "stop", 11, D("1.40"), D("1.40")),
        ("b", 0, 6, D("3.00"), D("3.00"), "stop", 12, D("2.20"), D("2.20")),
        ("a", 1, 13, D("1.60"), D("1.60"), "exit_time", 61, D("1.60"), D("1.60")),
    ]
    assert minutes(outcome, EventKind.PICKED, "a") == [5, 12]


def test_a_re_entry_waits_a_minute_when_the_underlying_did_not_trade_in_the_minute_it_would_start() -> None:
    market = TableMarket(
        underlying={minute: price for minute, price in steady("500").items() if minute != 11},
        picks=picked(("call", "500", CALL)),
        options={
            CALL: {**flat("2.00", 5, 9), 10: ("2.00", "1.40"), 11: ("1.40", "1.40"), **flat("1.60", 13, 61)},
        },
    )

    outcome = replay(setup(leg(stop=STOP_25, reentry=AT_ONCE_1)), market)

    assert [row(trade) for trade in outcome.trades][1:] == [
        ("call", 1, 13, D("1.60"), D("1.60"), "exit_time", 61, D("1.60"), D("1.60"))
    ]
    assert minutes(outcome, EventKind.PICKED) == [5, 12]


def test_a_re_entry_at_once_whose_contract_does_not_trade_in_the_next_minute_is_lost_and_the_leg_is_done() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("call", "500", CALL)),
        options={
            CALL: {
                **flat("2.00", 5, 9),
                10: ("2.00", "1.40"),
                11: ("1.40", "1.40"),
                # nothing traded in minute 12: the re-entry is dropped
                **flat("1.50", 13, 61),
            }
        },
    )

    outcome = replay(setup(leg(stop=STOP_25, reentry=("at_once", 2))), market)

    assert [row(trade) for trade in outcome.trades] == [
        ("call", 0, 6, D("2.00"), D("2.00"), "stop", 11, D("1.40"), D("1.40"))
    ]
    assert kinds(outcome) == [
        EventKind.PICKED,
        EventKind.ENTRY_FILLED,
        EventKind.STOP_HIT,
        EventKind.RE_ENTRY_EARNED,
        EventKind.CLOSE_FILLED,
        EventKind.PICKED,
        EventKind.ENTRY_DROPPED,
        EventKind.RE_ENTRY_LOST,
    ]


def test_a_re_entry_on_momentum_whose_fresh_contract_has_no_starting_price_is_lost() -> None:
    market = TableMarket(
        underlying={**steady("500", 1, 9), **steady("498", 10)},
        picks=picked(("call", "500", CALL), ("call", "498", CALL_498)),
        options={
            CALL: {
                5: ("2.00", "2.00"),
                6: ("2.00", "2.50"),
                7: ("2.50", "2.50"),
                8: ("2.50", "2.50"),
                9: ("2.50", "1.80"),
                10: ("1.80", "1.80"),
            },
            CALL_498: flat("1.30", 11, 61),  # nothing traded in minute 10, then a price that would have bought
        },
    )

    outcome = replay(setup(leg(momentum=("percent_up", "25"), stop=STOP_25, reentry=("on_momentum", 1))), market)

    assert [trade.entry_number for trade in outcome.trades] == [0]
    assert kinds(outcome)[-3:] == [EventKind.PICKED, EventKind.NO_STARTING_PRICE, EventKind.RE_ENTRY_LOST]
    assert minutes(outcome, EventKind.RE_ENTRY_LOST) == [10]


def test_a_re_entry_whose_pick_is_refused_is_lost_with_the_reason_carried() -> None:
    refusal = PickRefused(PickRefusal.CHAIN_INCOMPLETE, "the chain at minute 11 lists too few strikes")
    market = TableMarket(
        underlying={**steady("500", 1, 10), **steady("498", 11)},
        picks={**picked(("call", "500", CALL)), ("call", "498"): refusal},
        options={CALL: {**flat("2.00", 5, 9), 10: ("2.00", "1.40"), 11: ("1.40", "1.40"), **flat("1.50", 12, 61)}},
    )

    outcome = replay(setup(leg(stop=STOP_25, reentry=AT_ONCE_1)), market)

    assert [trade.entry_number for trade in outcome.trades] == [0]
    refused = [e for e in outcome.events if e.kind is EventKind.PICK_REFUSED]
    assert [(minute_of(e.at_ms), e.reason, e.detail) for e in refused] == [
        (11, "chain_incomplete", "the chain at minute 11 lists too few strikes")
    ]
    assert minutes(outcome, EventKind.RE_ENTRY_LOST) == [11]


@pytest.mark.parametrize(
    ("sale_minute", "sale_price"),
    [
        (60, "1.40"),  # the sale fills in the exit minute itself
        (61, "1.30"),  # the sale is still pending when the exit time comes
    ],
)
def test_the_exit_time_cancels_a_re_entry_that_was_earned_and_not_yet_started(
    sale_minute: int, sale_price: str
) -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("call", "500", CALL)),
        options={
            CALL: {
                **flat("2.00", 5, 58),
                59: ("2.00", "1.40"),  # stopped, and a re-entry is earned
                sale_minute: (sale_price, sale_price),
                # The contract trades on after the exit time, so a re-entry wrongly started would buy and be seen.
                **flat(sale_price, sale_minute + 1, 100),
            }
        },
    )

    outcome = replay(setup(leg(stop=STOP_25, reentry=AT_ONCE_1)), market)

    assert outcome.status is SessionStatus.TRADED
    assert [row(trade) for trade in outcome.trades] == [
        ("call", 0, 6, D("2.00"), D("2.00"), "stop", sale_minute, D(sale_price), D(sale_price))
    ]
    # No fresh contract is picked after the entry minute: the re-entry never starts.
    assert minutes(outcome, EventKind.PICKED) == [5]
    # The cancelled re-entry would have been entry 1, whether or not its sale had filled by the exit time.
    cancelled = [e for e in outcome.events if e.kind is EventKind.LEG_CANCELLED]
    assert [(e.at_ms, e.reason, e.entry_number) for e in cancelled] == [(at(60), "exit_time", 1)]
