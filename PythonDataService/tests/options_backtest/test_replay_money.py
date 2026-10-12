"""Cash, fees and what the same inputs give twice.

Minutes are counted after the open; the setups here enter at minute 5 and leave at minute 60. Each
expectation is worked by hand and written as a literal.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import ROUND_CEILING, Context, Decimal, Inexact, localcontext

import pytest

from app.options.contract import OptionContract
from app.options_backtest.market import MinuteBar, Picked, PickRefused
from app.options_backtest.results import EventKind, Fill, SessionOutcome, SessionStatus
from app.options_backtest.setup import LegSetup
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


def test_a_refused_entry_names_the_price_it_would_have_filled_at_not_the_price_seen() -> None:
    market = TableMarket(
        underlying=steady("500"), picks=picked(("call", "500", CALL)), options={CALL: flat("2.57", 5, 61)}
    )

    # 2.57 * 1.003 = 2.57771, up to 2.58: 258.00 against 257.50 of cash. At the price seen it would have fitted.
    outcome = replay(setup(leg(), slippage="0.3"), market, cash="257.50")

    assert outcome.trades == ()
    refused = [event for event in outcome.events if event.kind is EventKind.ENTRY_REFUSED_FOR_CASH]
    assert [(minute_of(e.at_ms), e.price) for e in refused] == [(6, D("2.58"))]


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
    # Both trips were sold, so no leg is worth anything at a mark and the profit is the cash that moved.
    assert (outcome.pnl_usd, outcome.marked_value_usd) == (D("-133.20"), D("0"))
    assert outcome.pnl_usd == outcome.cash_change_usd + outcome.marked_value_usd


def test_the_money_does_not_depend_on_the_ambient_decimal_precision() -> None:
    written = setup(leg(contracts=2, stop=STOP_25, reentry=("at_once", 1)), slippage="0.3")
    market = _a_stop_and_a_re_entry_at_two_contracts()

    with localcontext(Context(prec=4)):
        outcome = replay(written, market, fee_for=fee_per_contract("0.65"))

    assert [t.pnl_usd for t in outcome.trades] == [D("-146.60"), D("13.40")]
    assert outcome.cash_change_usd == D("-133.20")


def test_a_leg_marked_not_sold_is_in_the_profit_and_the_marked_value_and_not_in_the_cash_that_moved() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks={("call", "500"): Picked(CALL, multiplier=10)},
        options={CALL: {**flat("2.00", 5, 49), 50: ("2.00", "1.75")}},  # and never again: no sale can fill
    )

    outcome = replay(setup(leg(contracts=3)), market, fee_for=fee_per_contract("0.65"))

    # Three contracts of ten shares, bought at 2.00 with a fee of 0.65 * 3 = 1.95: 60.00 + 1.95 = 61.95 out, none back.
    assert outcome.cash_change_usd == D("-61.95")
    # Marked at 1.75: worth 1.75 * 10 * 3 = 52.50. Profit (1.75 - 2.00) * 30 - 1.95 = -9.45.
    assert outcome.marked_value_usd == D("52.50")
    assert outcome.pnl_usd == D("-9.45")
    assert outcome.pnl_usd == outcome.cash_change_usd + outcome.marked_value_usd


def test_cash_can_end_below_zero_by_the_fee_of_a_sale_that_fills_at_nothing() -> None:
    market = TableMarket(
        underlying=steady("500"), picks=picked(("call", "500", CALL)), options={CALL: flat("0.01", 5, 61)}
    )

    outcome = replay(setup(leg(), slippage="0.3"), market, cash="2.65", fee_for=fee_per_contract("0.65"))

    # Bought: 0.01 * 1.003 = 0.01003, up to 0.02; 0.02 * 100 + 0.65 = 2.65, exactly the cash, so it is taken.
    # Sold: 0.01 * 0.997 = 0.00997, down to 0.00; 0.00 * 100 - 0.65 = -0.65 comes in, and the sale is not refused.
    assert [row(trade) for trade in outcome.trades] == [
        ("call", 0, 6, D("0.01"), D("0.02"), "exit_time", 61, D("0.01"), D("0.00"))
    ]
    assert outcome.trades[0].pnl_usd == D("-3.30")  # (0.00 - 0.02) * 100 - 0.65 - 0.65
    assert outcome.cash_change_usd == D("-3.30")  # -2.65 out, -0.65 back
    assert D("2.65") + outcome.cash_change_usd == D("-0.65")


def _sale_fee_rounded_up_to_the_cent(fill: Fill) -> Decimal:
    """A buy owes nothing. A sale owes 0.0000278 of its value, rounded up to the cent."""
    if fill.side == "buy":
        return D(0)
    value = fill.filled * fill.multiplier * fill.contracts
    return (value * D("0.0000278")).quantize(D("0.01"), rounding=ROUND_CEILING)


def test_a_fee_that_rounds_to_the_cent_is_charged_and_does_not_crash_the_replay() -> None:
    market = TableMarket(
        underlying=steady("500"), picks=picked(("call", "500", CALL)), options={CALL: flat("1.50", 5, 61)}
    )

    outcome = replay(setup(leg()), market, fee_for=_sale_fee_rounded_up_to_the_cent)

    # Bought at 1.50 in minute 6 and sold at 1.50 in minute 61: 1.50 * 100 = 150.00 each way.
    # The sale's fee is 150.00 * 0.0000278 = 0.00417, which rounds up to 0.01.
    trade = outcome.trades[0]
    assert (trade.entry_fee_usd, trade.exit_fee_usd) == (D("0"), D("0.01"))
    assert trade.pnl_usd == D("-0.01")  # (1.50 - 1.50) * 100 - 0.01
    assert outcome.cash_change_usd == D("-0.01")  # -150.00 out, 149.99 back


class _MarketThatRounds:
    """A market whose three methods round, as a picker that snaps to a strike or a vendor adapter might."""

    def __init__(self, inner: TableMarket) -> None:
        self._inner = inner
        self.session_open_ms = inner.session_open_ms
        self.session_close_ms = inner.session_close_ms

    def underlying_close(self, close_ms: int) -> Decimal | None:
        price = self._inner.underlying_close(close_ms)
        return None if price is None else (price + D("0.0049")).quantize(D("0.01"))

    def pick(self, leg: LegSetup, *, at_ms: int, underlying_price: Decimal) -> Picked | PickRefused:
        return self._inner.pick(leg, at_ms=at_ms, underlying_price=underlying_price.quantize(D("1")))

    def option_bar(self, option: OptionContract, close_ms: int) -> MinuteBar | None:
        bar = self._inner.option_bar(option, close_ms)
        return None if bar is None else replace(bar, open=(bar.open + D("0.0049")).quantize(D("0.01")))


def test_a_market_that_rounds_what_it_returns_does_not_crash_the_replay() -> None:
    inner = TableMarket(
        underlying=steady("500.40"), picks=picked(("call", "500", CALL)), options={CALL: flat("2.00", 5, 61)}
    )

    outcome = replay(setup(leg()), _MarketThatRounds(inner))

    # The underlying 500.40 + 0.0049 rounds to 500.40, and the pick rounds that to the 500 strike: the call.
    # Each bar opens at 2.00 + 0.0049, which rounds to 2.00: bought and sold at 2.00, so no profit.
    assert [row(trade) for trade in outcome.trades] == [
        ("call", 0, 6, D("2.00"), D("2.00"), "exit_time", 61, D("2.00"), D("2.00"))
    ]
    assert outcome.trades[0].pnl_usd == D("0.00")


def _a_bar_opening_at(digits: int) -> TableMarket:
    """An option whose bar in the entry fill's minute opens at 2.111..., that many significant digits long."""
    return TableMarket(
        underlying=steady("500"),
        picks=picked(("call", "500", CALL)),
        options={CALL: {5: ("2.00", "2.00"), 6: ("2." + "1" * (digits - 1), "2.00"), **flat("2.00", 7, 61)}},
    )


def test_a_price_of_eighty_digits_is_held_exactly_and_filled_at_the_next_cent_up() -> None:
    outcome = replay(setup(leg()), _a_bar_opening_at(80))

    # 2.1111... up to the cent is 2.12; sold at 2.00 in minute 61: (2.00 - 2.12) * 100 = -12.00.
    assert outcome.trades[0].entry.seen == D("2." + "1" * 79)
    assert (outcome.trades[0].entry.filled, outcome.trades[0].pnl_usd) == (D("2.12"), D("-12.00"))


def test_a_price_too_long_to_hold_exactly_raises_instead_of_being_rounded() -> None:
    with pytest.raises(Inexact):
        replay(setup(leg()), _a_bar_opening_at(120))


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
