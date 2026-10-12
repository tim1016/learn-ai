"""What each event of the timeline carries: the entry number it is about, and the reason it gives.

Minutes are counted after the open; the setups here enter at minute 5 and leave at minute 60. Each
expectation is worked by hand and written as a literal.
"""

from __future__ import annotations

from app.options_backtest.market import PickRefusal, PickRefused
from app.options_backtest.results import EventKind, SessionOutcome
from tests.options_backtest._market import (
    D,
    TableMarket,
    contract,
    flat,
    leg,
    minute_of,
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


def _numbered(outcome: SessionOutcome, leg_id: str) -> list[tuple[EventKind, int, int]]:
    """``(what happened, minute, entry number)`` for one leg, in order."""
    return [(e.kind, minute_of(e.at_ms), e.entry_number) for e in outcome.events if e.leg_id == leg_id]


def test_every_event_carries_the_entry_number_of_the_position_or_of_the_attempt_it_is_about() -> None:
    market = TableMarket(
        underlying={**steady("500", 1, 8), **steady("498", 9)},
        picks=picked(("call", "500", CALL), ("call", "498", CALL_498)),
        options={
            CALL: {
                5: ("2.00", "2.00"),  # the starting price is 2.00: the level is 2.00 * 1.25 = 2.50
                6: ("2.00", "2.50"),  # on the level: an order is decided
                7: ("2.60", "2.60"),  # bought at this open; the stop sits at 2.60 * 0.75 = 1.95
                8: ("2.60", "1.90"),  # 1.90 is under 1.95: stopped, and a re-entry is earned
                9: ("1.85", "1.85"),  # sold at this open
            },
            CALL_498: {
                9: ("1.00", "1.00"),  # the fresh contract's starting price: the level is 1.25
                10: ("1.00", "1.25"),  # on the level: an order is decided
                11: ("1.30", "1.30"),  # bought at this open; the stop sits at 1.30 * 0.75 = 0.975
                **flat("1.30", 12, 58),
                59: ("1.30", "0.90"),  # 0.90 is under 0.975: stopped, and a second re-entry is earned
                # nothing traded in minute 60: the sale is still pending when the exit time comes
                61: ("0.85", "0.85"),  # sold at this open
            },
        },
    )

    outcome = replay(
        setup(leg(momentum=("percent_up", "25"), stop=("percent", "25"), reentry=("on_momentum", 2))), market
    )

    assert [row(trade) for trade in outcome.trades] == [
        ("call", 0, 7, D("2.60"), D("2.60"), "stop", 9, D("1.85"), D("1.85")),
        ("call", 1, 11, D("1.30"), D("1.30"), "stop", 61, D("0.85"), D("0.85")),
    ]
    assert outcome.cash_change_usd == D("-120.00")  # -260.00 + 185.00 - 130.00 + 85.00
    # An event about a position carries that position's number, and one about an attempt to enter the number
    # that entry has or would have had: the stop of entry 0 earns entry 1, and the cancelled one would be entry 2.
    assert _numbered(outcome, "call") == [
        (EventKind.PICKED, 5, 0),
        (EventKind.STARTING_PRICE_TAKEN, 5, 0),
        (EventKind.MOMENTUM_HIT, 6, 0),
        (EventKind.ENTRY_FILLED, 7, 0),
        (EventKind.STOP_HIT, 8, 0),
        (EventKind.RE_ENTRY_EARNED, 8, 1),
        (EventKind.CLOSE_FILLED, 9, 0),
        (EventKind.PICKED, 9, 1),
        (EventKind.STARTING_PRICE_TAKEN, 9, 1),
        (EventKind.MOMENTUM_HIT, 10, 1),
        (EventKind.ENTRY_FILLED, 11, 1),
        (EventKind.STOP_HIT, 59, 1),
        (EventKind.RE_ENTRY_EARNED, 59, 2),
        (EventKind.LEG_CANCELLED, 60, 2),
        (EventKind.CLOSE_FILLED, 61, 1),
    ]


def test_an_entry_dropped_or_a_re_entry_lost_carries_the_number_of_the_entry_that_did_not_happen() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("call", "500", CALL)),
        options={
            CALL: {
                **flat("2.00", 5, 9),
                10: ("2.00", "1.40"),  # stopped: the stop sits at 2.00 * 0.75 = 1.50
                11: ("1.40", "1.40"),  # sold, and the re-entry is decided
                # nothing traded in minute 12: the re-entry, which would be entry 1, is dropped and lost
                **flat("1.50", 13, 61),
            }
        },
    )

    outcome = replay(setup(leg(stop=("percent", "25"), reentry=("at_once", 1))), market)

    assert _numbered(outcome, "call") == [
        (EventKind.PICKED, 5, 0),
        (EventKind.ENTRY_FILLED, 6, 0),
        (EventKind.STOP_HIT, 10, 0),
        (EventKind.RE_ENTRY_EARNED, 10, 1),
        (EventKind.CLOSE_FILLED, 11, 0),
        (EventKind.PICKED, 11, 1),
        (EventKind.ENTRY_DROPPED, 12, 1),
        (EventKind.RE_ENTRY_LOST, 12, 1),
    ]


def test_every_reason_an_event_gives_is_a_plain_string_whatever_kind_of_event_gives_it() -> None:
    refusal = PickRefused(PickRefusal.STRIKE_NOT_LISTED, "no strike is listed near 500")
    market = TableMarket(
        underlying=steady("500"),
        picks={("a", "500"): refusal, **picked(("b", "500", PUT), ("c", "500", CALL_505))},
        options={
            PUT: flat("2.00", 5, 61),  # 2.00 * 100 = 200.00 against 150.00 of cash
            CALL_505: flat("1.00", 5, 61),  # the level 1.25 is never reached: cancelled at the exit time
        },
    )

    outcome = replay(
        setup(leg("a", "call"), leg("b", "put"), leg("c", "call", momentum=("percent_up", "25"))), market, cash="150"
    )

    given = [(e.kind, e.leg_id, e.reason) for e in outcome.events if e.reason is not None]
    assert given == [
        (EventKind.PICK_REFUSED, "a", "strike_not_listed"),
        (EventKind.ENTRY_REFUSED_FOR_CASH, "b", "cash_short"),
        (EventKind.LEG_CANCELLED, "c", "exit_time"),
    ]
    # A string enum compares equal to its value, so only the type says it was stored as a plain string.
    assert all(type(e.reason) is str for e in outcome.events if e.reason is not None)
