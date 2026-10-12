"""Cash, fees and what the same inputs give twice.

Minutes are counted after the open; the setups here enter at minute 5 and leave at minute 60. Each
expectation is worked by hand and written as a literal.
"""

from __future__ import annotations

from decimal import Context, localcontext

import pytest

from app.options_backtest.market import Picked
from app.options_backtest.results import EventKind, SessionOutcome, SessionStatus
from tests.options_backtest._market import (
    FULL_CLOSE_MS,
    D,
    TableMarket,
    contract,
    fee_per_contract,
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


def _call_at_two_and_put_at_three() -> TableMarket:
    return TableMarket(
        underlying=steady("500"),
        picks=picked(("call", "500", CALL), ("put", "500", PUT)),
        options={CALL: flat("2.00", 5, 61), PUT: flat("3.00", 5, 61)},
    )


def test_one_entry_the_cash_cannot_cover_is_refused_and_counted() -> None:
    # 2.00 * 100 = 200.00 against 150.00.
    outcome = replay(setup(leg()), _call_at_two_and_put_at_three(), cash="150")

    assert outcome.status is SessionStatus.NO_TRADE
    assert outcome.trades == ()
    assert outcome.cash_change_usd == D("0")
    refused = [event for event in outcome.events if event.kind is EventKind.ENTRY_REFUSED_FOR_CASH]
    assert [(minute_of(e.at_ms), e.leg_id, e.reason, e.price) for e in refused] == [
        (6, "call", "cash_short", D("2.00"))
    ]


def test_an_entry_costing_exactly_the_cash_is_taken() -> None:
    outcome = replay(setup(leg()), _call_at_two_and_put_at_three(), cash="200")

    assert [trade.leg_id for trade in outcome.trades] == ["call"]
    assert outcome.cash_change_usd == D("0")  # 200.00 out, 200.00 back


def test_the_fee_counts_toward_what_an_entry_costs() -> None:
    # 200.00 + 0.65 = 200.65 against 200.00.
    outcome = replay(setup(leg()), _call_at_two_and_put_at_three(), cash="200", fee_for=fee_per_contract("0.65"))

    assert outcome.trades == ()
    assert minutes(outcome, EventKind.ENTRY_REFUSED_FOR_CASH) == [6]


def test_two_entries_in_one_minute_that_together_exceed_the_cash_are_both_refused() -> None:
    # 200.00 + 300.00 = 500.00 against 450.00: the call alone would fit, and is refused with the put.
    written = setup(leg("call", "call"), leg("put", "put"))

    outcome = replay(written, _call_at_two_and_put_at_three(), cash="450")

    assert outcome.trades == ()
    refused = [event for event in outcome.events if event.kind is EventKind.ENTRY_REFUSED_FOR_CASH]
    assert [(minute_of(e.at_ms), e.leg_id) for e in refused] == [(6, "call"), (6, "put")]
    assert outcome.cash_change_usd == D("0")


def test_two_entries_in_one_minute_that_together_equal_the_cash_are_both_taken() -> None:
    outcome = replay(setup(leg("call", "call"), leg("put", "put")), _call_at_two_and_put_at_three(), cash="500")

    assert [trade.leg_id for trade in outcome.trades] == ["call", "put"]


def test_a_leg_refused_for_cash_is_done_for_the_day_even_when_cash_comes_back() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("a", "500", CALL), ("b", "500", PUT)),
        options={
            CALL: {**flat("2.00", 5, 19), 20: ("2.00", "1.40"), 21: ("1.40", "1.40")},  # bought for 200, sold for 140
            PUT: {
                **flat("3.00", 5, 7),
                8: ("3.00", "3.75"),  # the level 3.75 is reached
                9: ("3.80", "3.80"),  # 380.00 against the 250.00 left after A's 200.00: refused here
                **flat("3.80", 10, 61),
            },
        },
    )
    written = setup(leg("a", "call", stop=STOP_25), leg("b", "put", momentum=("percent_up", "25")))

    outcome = replay(written, market, cash="450")

    # By minute 21 the cash is 250 + 140 = 390, enough for B, but B's day is over.
    assert [trade.leg_id for trade in outcome.trades] == ["a"]
    assert minutes(outcome, EventKind.ENTRY_REFUSED_FOR_CASH, "b") == [9]
    assert minutes(outcome, EventKind.MOMENTUM_HIT, "b") == [8]


def test_an_entry_is_checked_against_the_cash_before_the_sales_filling_in_the_same_minute() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("a", "500", CALL), ("b", "500", PUT)),
        options={
            # A is stopped at 9; no trade in minute 10; its sale fills at 11 for 140.00.
            CALL: {**flat("2.00", 5, 8), 9: ("2.00", "1.40"), 11: ("1.40", "1.40")},
            # B's level is reached at 10, a minute after the exit was decided, so its order is not suppressed.
            PUT: {**flat("1.00", 5, 9), 10: ("1.00", "1.30"), **flat("1.30", 11, 61)},
        },
    )
    written = setup(leg("a", "call", stop=STOP_25), leg("b", "put", momentum=("percent_up", "25")))

    outcome = replay(written, market, cash="250")

    # At minute 11 B costs 130.00 and the cash is 250 - 200 = 50; A's 140.00 comes in after the entries are checked.
    assert [trade.leg_id for trade in outcome.trades] == ["a"]
    assert minutes(outcome, EventKind.ENTRY_REFUSED_FOR_CASH, "b") == [11]


def test_the_multiplier_of_the_picked_contract_prices_the_order() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks={("call", "500"): Picked(CALL, multiplier=10)},
        options={CALL: {5: ("2.00", "2.00"), 6: ("2.00", "2.00"), **flat("2.10", 7, 61)}},
    )

    outcome = replay(setup(leg()), market)

    # Bought at 2.00 and sold at 2.10, ten shares to the contract: 0.10 * 10 = 1.00.
    assert outcome.trades[0].multiplier == 10
    assert outcome.trades[0].pnl_usd == D("1.00")
    assert outcome.cash_change_usd == D("1.00")


def _a_stop_and_a_re_entry_at_two_contracts() -> TableMarket:
    return TableMarket(
        underlying=steady("500"),
        picks=picked(("call", "500", CALL)),
        options={
            CALL: {
                **flat("2.57", 5, 9),
                10: ("2.57", "1.90"),  # bought at 2.58, so the stop is 1.935: 1.90 is under it
                11: ("1.87", "1.87"),
                12: ("1.50", "1.50"),
                **flat("1.50", 13, 60),
                61: ("1.60", "1.60"),
            }
        },
    )


def test_fees_are_charged_on_every_fill_re_entries_included_and_profit_plus_cash_reconcile_to_the_cent() -> None:
    written = setup(leg(contracts=2, stop=STOP_25, reentry=("at_once", 1)), slippage="0.3")

    outcome = replay(written, _a_stop_and_a_re_entry_at_two_contracts(), fee_for=fee_per_contract("0.65"))

    # First trip: bought 2.57 * 1.003 = 2.57771 up to 2.58, sold 1.87 * 0.997 = 1.86439 down to 1.86.
    # Second trip: bought 1.50 * 1.003 = 1.5045 up to 1.51, sold 1.60 * 0.997 = 1.5952 down to 1.59.
    assert [row(trade) for trade in outcome.trades] == [
        ("call", 0, 6, D("2.57"), D("2.58"), "stop", 11, D("1.87"), D("1.86")),
        ("call", 1, 12, D("1.50"), D("1.51"), "exit_time", 61, D("1.60"), D("1.59")),
    ]
    # Each fill costs 0.65 * 2 = 1.30, and there are four.
    assert [(t.entry_fee_usd, t.exit_fee_usd) for t in outcome.trades] == [(D("1.30"), D("1.30"))] * 2
    # (1.86 - 2.58) * 200 - 2.60 = -146.60 and (1.59 - 1.51) * 200 - 2.60 = 13.40.
    assert [t.pnl_usd for t in outcome.trades] == [D("-146.60"), D("13.40")]
    # Cash: -517.30 + 370.70 - 303.30 + 316.70.
    assert outcome.cash_change_usd == D("-133.20")
    assert outcome.cash_change_usd == sum((t.pnl_usd for t in outcome.trades), start=D(0))


def test_the_money_does_not_depend_on_the_ambient_decimal_precision() -> None:
    written = setup(leg(contracts=2, stop=STOP_25, reentry=("at_once", 1)), slippage="0.3")
    market = _a_stop_and_a_re_entry_at_two_contracts()

    with localcontext(Context(prec=4)):
        outcome = replay(written, market, fee_for=fee_per_contract("0.65"))

    assert [t.pnl_usd for t in outcome.trades] == [D("-146.60"), D("13.40")]
    assert outcome.cash_change_usd == D("-133.20")


def _rich_session() -> SessionOutcome:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("a", "500", CALL), ("b", "500", PUT), ("c", "500", CALL_505)),
        options={
            CALL: {**flat("2.00", 5, 9), 10: ("2.00", "1.40"), 11: ("1.40", "1.40"), **flat("1.60", 12, 61)},
            PUT: {**flat("3.00", 5, 11), **flat("3.20", 12, 61)},
            CALL_505: {**flat("1.00", 5, 11), 12: ("1.00", "1.30"), **flat("1.30", 13, 61)},
        },
    )
    written = setup(
        leg("a", "call", stop=STOP_25, reentry=("at_once", 1)),
        leg("b", "put", reentry=("at_once", 1)),
        leg("c", "call", momentum=("percent_up", "25")),
        square_off="complete",
        slippage="0.3",
    )
    return replay(written, market, fee_for=fee_per_contract("0.65"))


def test_the_same_inputs_give_an_equal_outcome_twice_over() -> None:
    first, second = _rich_session(), _rich_session()

    assert first == second
    assert repr(first) == repr(second)
    # The scene is not trivial: it has trades, events and a cancelled leg.
    assert len(first.trades) == 4
    assert EventKind.LEG_CANCELLED in kinds(first)


def test_a_session_that_does_not_open_and_close_on_a_whole_minute_is_refused() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("call", "500", CALL)),
        options={CALL: flat("2.00", 1, 390)},
        session_close_ms=FULL_CLOSE_MS + 1_000,
    )

    with pytest.raises(ValueError, match="whole minute"):
        replay(setup(leg()), market)
