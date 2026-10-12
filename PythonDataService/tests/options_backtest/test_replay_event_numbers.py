"""Which entry each event of a replay is about, and two sums that must not bend to their reader.

The shared setup trades from minute 5 to minute 60 with no slippage, so a price seen is the price filled.
"""

from __future__ import annotations

from decimal import Context, localcontext

from app.options_backtest.market import Picked
from app.options_backtest.results import EventKind, SessionOutcome
from tests.options_backtest._market import (
    D,
    TableMarket,
    after_open,
    contract,
    fee_per_contract,
    flat,
    leg,
    minute_of,
    minutes,
    picked,
    replay,
    setup,
    steady,
)

CALL = contract("call", "500")
PUT = contract("put", "500")
STOP_25 = ("percent", "25")


def _numbers(outcome: SessionOutcome, kind: EventKind) -> list[tuple[str, int, int]]:
    return [(event.leg_id, minute_of(event.at_ms), event.entry_number) for event in outcome.events if event.kind is kind]


def test_the_exit_time_event_names_the_position_it_closes() -> None:
    # Bought at minute 6, held to the exit time at minute 60: the leg's first entry, number 0.
    market = TableMarket(underlying=steady("500"), picks=picked(("a", "500", CALL)), options={CALL: flat("2.00", 5, 61)})

    outcome = replay(setup(leg("a")), market)

    assert _numbers(outcome, EventKind.EXIT_TIME) == [("a", 60, 0)]


def test_a_squared_off_event_names_the_position_it_closes() -> None:
    # Leg a paid 2.00, so its 25% stop is 1.50; minute 10 closes at 1.40 and stops it. Under complete
    # square-off leg b, also in its first entry, is closed by that stop in the same minute.
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("a", "500", CALL), ("b", "500", PUT)),
        options={CALL: {**flat("2.00", 5, 9), 10: ("2.00", "1.40"), 11: ("1.40", "1.40")}, PUT: flat("3.00", 5, 61)},
    )

    outcome = replay(setup(leg("a", stop=STOP_25), leg("b", "put"), square_off="complete"), market)

    assert _numbers(outcome, EventKind.SQUARED_OFF) == [("b", 10, 0)]


def test_a_marked_event_names_the_position_it_marks() -> None:
    # The contract's last bar closes at minute 60, so the exit's sale at minute 61 never fills and the
    # leg's first entry is marked at the session's close, minute 390.
    market = TableMarket(underlying=steady("500"), picks=picked(("a", "500", CALL)), options={CALL: flat("2.00", 5, 60)})

    outcome = replay(setup(leg("a")), market)

    assert _numbers(outcome, EventKind.MARKED_NOT_SOLD) == [("a", 390, 0)]


def test_a_re_entry_blocked_by_the_cutoff_names_the_entry_that_was_refused() -> None:
    # The stop at minute 15 comes after the cutoff at minute 12. What is refused is the re-entry, entry 1.
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("a", "500", CALL)),
        options={CALL: {**flat("2.00", 5, 14), 15: ("2.00", "1.40"), **flat("1.40", 16, 61)}},
    )

    outcome = replay(setup(leg("a", stop=STOP_25, reentry=("at_once", 1)), no_reentry_after=after_open(12)), market)

    assert _numbers(outcome, EventKind.RE_ENTRY_BLOCKED_BY_CUTOFF) == [("a", 15, 1)]


def test_profit_and_marked_value_do_not_depend_on_the_readers_decimal_context() -> None:
    # 3 contracts of ten shares at a fee of 0.67 a contract: the buy's fee is 3 x 0.67 = 2.01.
    # Bought at 2.00; the last bar seen closes at 1.75 and the exit's sale never fills, so it is marked there.
    # Profit = (1.75 - 2.00) x 10 x 3 - 2.01 = -7.50 - 2.01 = -9.51. Marked value = 1.75 x 10 x 3 = 52.50.
    # A reader working to two digits would round both.
    market = TableMarket(
        underlying=steady("500"),
        picks={("a", "500"): Picked(CALL, multiplier=10)},
        options={CALL: {**flat("2.00", 5, 49), 50: ("2.00", "1.75")}},
    )

    outcome = replay(setup(leg("a", contracts=3)), market, fee_for=fee_per_contract("0.67"))

    with localcontext(Context(prec=2)):
        assert outcome.pnl_usd == D("-9.51")
        assert outcome.marked_value_usd == D("52.50")


def test_a_pending_sale_that_earned_no_re_entry_is_not_said_to_cancel_one() -> None:
    # No re-entry rule. The stop at minute 59 has its sale still pending at the exit time (no bar at 60):
    # nothing was earned, so nothing is cancelled.
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("a", "500", CALL)),
        options={CALL: {**flat("2.00", 5, 58), 59: ("2.00", "1.40"), 61: ("1.40", "1.40"), **flat("1.40", 62, 80)}},
    )

    outcome = replay(setup(leg("a", stop=STOP_25)), market)

    assert minutes(outcome, EventKind.LEG_CANCELLED) == []
