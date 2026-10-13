"""Stops, protection and re-entry on sold legs (#3107).

Every scene is worked by hand from the rules of #3095 and never taken from the code's output. Minutes are counted after
the open: a setup here enters at minute 5, so its orders fill at the open of minute 6, and leaves at minute 60, so its
closes fill at the open of minute 61. A stop decided at minute ``m`` fills at the open of minute ``m + 1``. There is no
slippage and no fee unless a scene says so, and money is exact.

A sold leg's stop level is what it was sold for times (1 + percent / 100), or plus the points, and a minute that closes
at or above it hits it. When a bought leg's close is decided by its stop, the sold legs the cover book names go with it
in the same minute and exit group, with the reason ``closed_with_its_protection``.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.options_backtest import replay as replay_module
from app.options_backtest.cover_book import Held
from app.options_backtest.describe import describe_setup
from app.options_backtest.results import (
    EntryRefusal,
    EventKind,
    ExitReason,
    FillGroup,
    MoneyStep,
    SessionStatus,
)
from app.options_backtest.session_clock import SessionClock
from tests.options_backtest._market import (
    D,
    TableMarket,
    after_open,
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

PUT_495 = contract("put", "495")
PUT_500 = contract("put", "500")
PUT_505 = contract("put", "505")
CALL_500 = contract("call", "500")
CALL_675 = contract("call", "675")
CALL_680 = contract("call", "680")
CALL_685 = contract("call", "685")
CALL_690 = contract("call", "690")
CALL_695 = contract("call", "695")
CALL_700 = contract("call", "700")
CALL_710 = contract("call", "710")
STOP_25 = ("percent", "25")
AT_ONCE_1 = ("at_once", 1)


def _put_spread(
    *, long: dict[str, Any] | None = None, short: dict[str, Any] | None = None
) -> tuple[dict[str, Any], ...]:
    """A put 495 bought and a put 500 sold, one contract each, with the rules a scene gives each."""
    return (
        leg("long", "put", **(long or {})),
        leg("short", "put", side="sell", **(short or {})),
    )


def _events(outcome: Any, kind: EventKind) -> list[tuple[int, str]]:
    """``(minute, leg)`` of every event of one kind, in the order they were recorded."""
    return [(minute_of(event.at_ms), event.leg_id) for event in outcome.events if event.kind is kind]


# ---- A stop on the sold leg alone ---------------------------------------------------


def test_a_stop_on_the_sold_leg_alone_buys_it_back_and_the_bought_leg_stays_to_the_exit_time() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("long", "500", PUT_495), ("short", "500", PUT_500)),
        options={
            PUT_495: flat("1.00", 5, 61),
            # Sold at 2.00 at minute 6: the stop sits at 2.00 x 1.25 = 2.50. Minute 9 closes under it, and minute 10
            # closes on it, which is a hit. The buy-back fills at the open of minute 11.
            PUT_500: {**flat("2.00", 5, 8), 9: ("2.00", "2.49"), 10: ("2.49", "2.50"), **flat("2.60", 11, 61)},
        },
    )

    outcome = replay(setup(*_put_spread(short={"stop": STOP_25})), market)

    assert [row(trade) for trade in outcome.trades] == [
        ("long", 0, 6, D("1.00"), D("1.00"), "exit_time", 61, D("1.00"), D("1.00")),
        ("short", 0, 6, D("2.00"), D("2.00"), "stop", 11, D("2.60"), D("2.60")),
    ]
    short = outcome.trades[1]
    assert (short.stop_level, short.exit_group, short.exit_decided_at_ms) == (D("2.50"), 1, at(10))
    # The stop event carries the close that reached the level, and nothing was closed with a protection.
    assert [(e.leg_id, e.price) for e in outcome.events if e.kind is EventKind.STOP_HIT] == [("short", D("2.50"))]
    assert EventKind.CLOSED_WITH_ITS_PROTECTION not in outcome.event_counts
    # The bought leg is a different exit: its own group, decided by the exit time.
    assert (outcome.trades[0].exit_group, outcome.trades[0].exit_decided_at_ms) == (2, at(60))
    # Sold for 2.00 and bought back at 2.60: (2.00 - 2.60) x 100 = -60.00, which is all the session made.
    assert [trade.pnl_usd for trade in outcome.trades] == [D("0.00"), D("-60.00")]
    assert outcome.cash_change_usd == D("-60.00")
    # A buy-back with the bought put still held leaves the collateral at nothing.
    assert outcome.money_steps[1] == MoneyStep(
        at(11), FillGroup.CLOSING, D("10100.00"), D("9840.00"), D("500.00"), D("0"), D("0"), True
    )


def test_a_percent_stop_above_a_hundred_on_a_sold_leg_lets_the_premium_double_before_it_is_hit() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("long", "500", PUT_495), ("short", "500", PUT_500)),
        options={
            PUT_495: flat("1.00", 5, 61),
            # Sold at 2.00: 150 percent puts the stop at 2.00 x 2.5 = 5.00. Minute 10 closes at 4.90 and holds, even
            # past double the sale price. Minute 11 closes on 5.00 and hits, and the buy-back fills at the open of 12.
            PUT_500: {**flat("2.00", 5, 9), 10: ("2.00", "4.90"), 11: ("4.90", "5.00"), **flat("5.20", 12, 61)},
        },
    )

    outcome = replay(setup(*_put_spread(short={"stop": ("percent", "150")})), market)

    short = outcome.trades[1]
    assert (short.stop_level, short.exit_reason, short.exit_decided_at_ms) == (D("5.00"), ExitReason.STOP, at(11))
    assert (minute_of(short.exit.bar_close_ms), short.exit_price) == (12, D("5.20"))
    # (2.00 - 5.20) x 100 = -320.00
    assert short.pnl_usd == D("-320.00")


def test_a_points_stop_on_a_sold_leg_sits_that_many_points_above_the_sale_and_is_hit_by_a_close_on_it() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("long", "500", PUT_495), ("short", "500", PUT_500)),
        options={
            PUT_495: flat("1.00", 5, 61),
            # Sold at 2.00 with a stop of 0.50 points: the level is 2.50.
            PUT_500: {**flat("2.00", 5, 8), 9: ("2.00", "2.49"), 10: ("2.49", "2.50"), **flat("2.60", 11, 61)},
        },
    )

    outcome = replay(setup(*_put_spread(short={"stop": ("points", "0.50")})), market)

    short = outcome.trades[1]
    assert (short.stop_level, short.exit_decided_at_ms) == (D("2.50"), at(10))


def test_a_high_above_a_sold_legs_stop_with_a_close_under_it_does_not_buy_it_back() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("long", "500", PUT_495), ("short", "500", PUT_500)),
        options={
            PUT_495: flat("1.00", 5, 61),
            # The stop sits at 2.50. Minute 10 printed up to 3.10 and closed at 2.20.
            PUT_500: {**flat("2.00", 5, 9), 10: ("2.00", "3.10", "1.90", "2.20"), **flat("2.20", 11, 61)},
        },
    )

    outcome = replay(setup(*_put_spread(short={"stop": STOP_25})), market)

    assert EventKind.STOP_HIT not in outcome.event_counts
    assert [trade.exit_reason for trade in outcome.trades] == [ExitReason.EXIT_TIME, ExitReason.EXIT_TIME]


def test_a_fill_far_from_the_price_the_stop_was_read_at_is_the_open_of_the_next_minute_and_a_close_is_never_refused() -> (
    None
):
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("long", "500", PUT_495), ("short", "500", PUT_500)),
        options={
            PUT_495: flat("1.00", 5, 61),
            # The stop sits at 2.50 and is read at the close of minute 10, 2.55. The next minute opens at 8.00.
            PUT_500: {**flat("2.00", 5, 9), 10: ("2.00", "2.55"), **flat("8.00", 11, 61)},
        },
    )

    # 450.00 less the bought put's 100.00 and plus the sale's 200.00 is 550.00, which carries the 500.00 collateral.
    outcome = replay(setup(*_put_spread(short={"stop": STOP_25})), market, cash="450")

    short = outcome.trades[1]
    assert [e.price for e in outcome.events if e.kind is EventKind.STOP_HIT] == [D("2.55")]
    assert short.exit_price == D("8.00")
    # (2.00 - 8.00) x 100 = -600.00 on the leg, and the cash goes below nothing: a close is never refused for cash.
    assert short.pnl_usd == D("-600.00")
    closing = outcome.money_steps[1]
    assert (closing.cash_before_usd, closing.cash_after_usd) == (D("550.00"), D("-250.00"))
    assert closing.cash_after_usd < 0 <= closing.collateral_before_usd


# ---- A stop on the protection --------------------------------------------------------


def _a_spread_whose_cover_is_stopped() -> TableMarket:
    """The long 495 put is bought at 1.00 and stopped at minute 10 (its stop sits at 0.75, minute 10 closes at 0.70).
    The short 500 put is sold at 2.20 and comes down to 2.10 at the open of minute 11."""
    return TableMarket(
        underlying=steady("500"),
        picks=picked(("long", "500", PUT_495), ("short", "500", PUT_500)),
        options={
            PUT_495: {**flat("1.00", 5, 9), 10: ("1.00", "0.70"), **flat("0.65", 11, 61)},
            PUT_500: {**flat("2.20", 5, 10), **flat("2.10", 11, 61)},
        },
    )


def test_a_stop_on_the_protection_takes_the_sold_leg_with_it_in_one_exit_group_with_its_own_reason() -> None:
    outcome = replay(setup(*_put_spread(long={"stop": STOP_25})), _a_spread_whose_cover_is_stopped())

    assert [row(trade) for trade in outcome.trades] == [
        ("long", 0, 6, D("1.00"), D("1.00"), "stop", 11, D("0.65"), D("0.65")),
        ("short", 0, 6, D("2.20"), D("2.20"), "closed_with_its_protection", 11, D("2.10"), D("2.10")),
    ]
    assert [trade.exit_group for trade in outcome.trades] == [1, 1]
    assert [trade.exit_decided_at_ms for trade in outcome.trades] == [at(10), at(10)]
    # Decided at minute 10 in this order, and filled at minute 11 with the buy-back first.
    assert [(minute_of(e.at_ms), e.leg_id, e.kind) for e in outcome.events if e.at_ms in (at(10), at(11))] == [
        (10, "long", EventKind.STOP_HIT),
        (10, "short", EventKind.CLOSED_WITH_ITS_PROTECTION),
        (11, "short", EventKind.CLOSE_FILLED),
        (11, "long", EventKind.CLOSE_FILLED),
    ]
    protected = next(e for e in outcome.events if e.kind is EventKind.CLOSED_WITH_ITS_PROTECTION)
    assert (protected.contract, protected.price, protected.reason, protected.entry_number) == (PUT_500, None, None, 0)
    assert outcome.exit_counts == {ExitReason.STOP: 1, ExitReason.CLOSED_WITH_ITS_PROTECTION: 1}
    # Long: (0.65 - 1.00) x 100 = -35.00. Short: (2.20 - 2.10) x 100 = +10.00.
    assert [trade.pnl_usd for trade in outcome.trades] == [D("-35.00"), D("10.00")]
    assert outcome.cash_change_usd == D("-25.00")
    assert outcome.money_steps[1] == MoneyStep(
        at(11), FillGroup.CLOSING, D("10120.00"), D("9975.00"), D("500.00"), D("0"), D("0"), False
    )


def test_the_protections_sale_fills_only_once_the_sold_legs_buy_back_has_a_bar() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("long", "500", PUT_495), ("short", "500", PUT_500)),
        options={
            # Stopped at minute 10; the cover trades in every minute after it.
            PUT_495: {
                **flat("1.00", 5, 9),
                10: ("1.00", "0.70"),
                11: ("0.65", "0.65"),
                12: ("0.60", "0.60"),
                **flat("0.55", 13, 61),
            },
            # The sold put did not trade in minutes 11 and 12.
            PUT_500: {**flat("2.20", 5, 10), **flat("2.30", 13, 61)},
        },
    )

    outcome = replay(setup(*_put_spread(long={"stop": STOP_25})), market)

    # Minutes 11 and 12: the cover has a bar and the sold put has none, so the sale waits. Minute 13: both fill.
    assert [row(trade) for trade in outcome.trades] == [
        ("long", 0, 6, D("1.00"), D("1.00"), "stop", 13, D("0.55"), D("0.55")),
        ("short", 0, 6, D("2.20"), D("2.20"), "closed_with_its_protection", 13, D("2.30"), D("2.30")),
    ]
    assert [trade.exit_decided_at_ms for trade in outcome.trades] == [at(10), at(10)]
    assert len({trade.exit_group for trade in outcome.trades}) == 1
    assert [minute_of(step.at_ms) for step in outcome.money_steps] == [6, 13]
    # Cash: -100.00 + 220.00 = 10120.00, then -230.00 + 55.00 = 9945.00.
    assert outcome.cash_change_usd == D("-55.00")
    assert outcome.status is SessionStatus.TRADED


def test_a_protection_whose_buy_back_never_finds_a_bar_is_marked_with_the_group_its_stop_decided() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("long", "500", PUT_495), ("short", "500", PUT_500)),
        options={
            PUT_495: {**flat("1.00", 5, 9), 10: ("1.00", "0.70"), **flat("0.65", 11, 390)},
            # The sold put's last bar is minute 10, closing at 2.20.
            PUT_500: flat("2.20", 5, 10),
        },
    )

    outcome = replay(setup(*_put_spread(long={"stop": STOP_25})), market)

    assert outcome.status is SessionStatus.INCOMPLETE
    assert [(trade.leg_id, trade.exit_reason, trade.exit_price) for trade in outcome.trades] == [
        ("long", ExitReason.MARKED_NOT_SOLD, D("0.65")),
        ("short", ExitReason.MARKED_NOT_BOUGHT_BACK, D("2.20")),
    ]
    # Both were decided by the stop at minute 10, and neither ever filled.
    assert [(trade.exit_group, trade.exit_decided_at_ms) for trade in outcome.trades] == [(1, at(10)), (1, at(10))]
    # Long: (0.65 - 1.00) x 100 = -35.00. Short: sold at 2.20 and owed at 2.20: 0.00.
    assert [trade.pnl_usd for trade in outcome.trades] == [D("-35.00"), D("0.00")]
    assert outcome.cash_change_usd == D("120.00")
    assert outcome.marked_value_usd == D("-155.00")  # +65.00 held and -220.00 owed
    assert [minute_of(step.at_ms) for step in outcome.money_steps] == [6]


# ---- The collateral would rise ---------------------------------------------------------


def _calls_680_690_and_675(*, stopped: str) -> TableMarket:
    """A call 680 and a call 690 are bought and a call 675 sold. ``stopped`` is the bought call whose premium falls
    through its stop at minute 10 and is sold at the open of minute 11."""
    falling = {"680": ("3.00", "2.00", "1.90"), "690": ("1.00", "0.70", "0.65")}
    steady_680 = flat("3.00", 5, 61)
    steady_690 = flat("1.00", 5, 61)
    s_open, s_mid, s_late = falling[stopped]
    stopped_bars = {**flat(s_open, 5, 9), 10: (s_open, s_mid), **flat(s_late, 11, 61)}
    return TableMarket(
        underlying=steady("500"),
        picks=picked(("a", "500", CALL_680), ("b", "500", CALL_690), ("s", "500", CALL_675)),
        options={
            CALL_680: stopped_bars if stopped == "680" else steady_680,
            CALL_690: stopped_bars if stopped == "690" else steady_690,
            CALL_675: {**flat("4.20", 5, 10), **flat("4.00", 11, 61)},
        },
    )


def test_a_stop_on_the_680_call_takes_the_675_call_with_it_because_the_collateral_would_rise() -> None:
    # Held: bought 680 and 690, sold 675. The summed payoff has its lowest point, -5, between the 680 and the 690: 500.00.
    # With the 680 gone, one bought contract still covers the one sold, and the lowest point is -15: 1,500.00. So the
    # 680 is protection by the collateral, though a bought call remains.
    written = setup(leg("a", "call", stop=STOP_25), leg("b", "call"), leg("s", "call", side="sell"))

    outcome = replay(written, _calls_680_690_and_675(stopped="680"))

    assert [row(trade) for trade in outcome.trades] == [
        ("a", 0, 6, D("3.00"), D("3.00"), "stop", 11, D("1.90"), D("1.90")),
        ("b", 0, 6, D("1.00"), D("1.00"), "exit_time", 61, D("1.00"), D("1.00")),
        ("s", 0, 6, D("4.20"), D("4.20"), "closed_with_its_protection", 11, D("4.00"), D("4.00")),
    ]
    assert [trade.exit_group for trade in outcome.trades] == [1, 2, 1]
    assert _events(outcome, EventKind.CLOSED_WITH_ITS_PROTECTION) == [(10, "s")]
    # Opening: -300.00 - 100.00 + 420.00 = 10020.00 against 500.00. Closing: -400.00 + 190.00 = 9810.00, with the
    # bought 690 still held, so the buy-back was made under cover.
    assert outcome.money_steps[0].cash_after_usd == D("10020.00")
    assert outcome.money_steps[1] == MoneyStep(
        at(11), FillGroup.CLOSING, D("10020.00"), D("9810.00"), D("500.00"), D("0"), D("0"), True
    )
    # a: (1.90 - 3.00) x 100 = -110.00; b: 0; s: (4.20 - 4.00) x 100 = +20.00.
    assert [trade.pnl_usd for trade in outcome.trades] == [D("-110.00"), D("0.00"), D("20.00")]
    assert outcome.cash_change_usd == D("-90.00")


def test_a_bought_leg_that_protects_nothing_is_sold_freely_when_it_is_stopped() -> None:
    # The same holdings with the 690 stopped: with it gone the 680 still covers the 675 and the lowest point is -5, as
    # it was. It protects nothing, so the sold leg stays.
    written = setup(leg("a", "call"), leg("b", "call", stop=STOP_25), leg("s", "call", side="sell"))

    outcome = replay(written, _calls_680_690_and_675(stopped="690"))

    assert [row(trade) for trade in outcome.trades] == [
        ("a", 0, 6, D("3.00"), D("3.00"), "exit_time", 61, D("3.00"), D("3.00")),
        ("b", 0, 6, D("1.00"), D("1.00"), "stop", 11, D("0.65"), D("0.65")),
        ("s", 0, 6, D("4.20"), D("4.20"), "exit_time", 61, D("4.00"), D("4.00")),
    ]
    assert EventKind.CLOSED_WITH_ITS_PROTECTION not in outcome.event_counts
    # The sale at minute 11 leaves 500.00 pledged, as before it.
    assert (outcome.money_steps[1].collateral_before_usd, outcome.money_steps[1].collateral_after_usd) == (
        D("500.00"),
        D("500.00"),
    )


# ---- The order is the book's, and it is a policy -----------------------------------------


def test_two_sold_legs_that_depend_on_one_leaving_bought_leg_are_closed_last_listed_first() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("b", "500", CALL_700), ("s1", "500", CALL_695), ("s2", "500", CALL_690)),
        options={
            # Two contracts bought at 2.00: the stop sits at 1.50, and minute 10 closes at 1.40.
            CALL_700: {**flat("2.00", 5, 9), 10: ("2.00", "1.40"), **flat("1.30", 11, 61)},
            CALL_695: flat("3.00", 5, 61),
            CALL_690: flat("4.00", 5, 61),
        },
    )
    written = setup(
        leg("b", "call", contracts=2, stop=STOP_25), leg("s1", "call", side="sell"), leg("s2", "call", side="sell")
    )

    outcome = replay(written, market)

    # The bought leg leaves, which would leave both sold legs bare: the one listed last is closed first.
    assert _events(outcome, EventKind.CLOSED_WITH_ITS_PROTECTION) == [(10, "s2"), (10, "s1")]
    assert [trade.exit_reason for trade in outcome.trades] == [
        ExitReason.STOP,
        ExitReason.CLOSED_WITH_ITS_PROTECTION,
        ExitReason.CLOSED_WITH_ITS_PROTECTION,
    ]
    assert {trade.exit_group for trade in outcome.trades} == {1}
    # The buy-backs fill in the setup's listed order and the sale after them.
    assert _events(outcome, EventKind.CLOSE_FILLED) == [(11, "s1"), (11, "s2"), (11, "b")]
    # Bought 400.00, sold 300.00 and 400.00: 10300.00 against 1,500.00. Closed: -300.00 - 400.00 + 260.00.
    assert outcome.money_steps[0].cash_after_usd == D("10300.00")
    assert outcome.money_steps[0].collateral_after_usd == D("1500.00")
    assert outcome.money_steps[1].cash_after_usd == D("9860.00")


def test_the_plan_is_a_policy_by_listed_order_and_closes_a_sold_leg_a_smaller_plan_would_keep() -> None:
    # Two covered pairs: the 680 covers the 675 and the 700 covers the 695, 1,000.00 together. The 680 leaves. Closing
    # only the 675 would leave a pair that pledges 500.00, which is enough. The book closes the sold leg listed last
    # first, the 695, and the 675 after it: it can close more legs than another policy would.
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(
            ("b1", "500", CALL_680), ("s1", "500", CALL_675), ("b2", "500", CALL_700), ("s2", "500", CALL_695)
        ),
        options={
            CALL_680: {**flat("3.00", 5, 9), 10: ("3.00", "2.00"), **flat("1.90", 11, 61)},
            CALL_675: flat("4.20", 5, 61),
            CALL_700: flat("1.00", 5, 61),
            CALL_695: flat("2.00", 5, 61),
        },
    )
    written = setup(
        leg("b1", "call", stop=STOP_25),
        leg("s1", "call", side="sell"),
        leg("b2", "call"),
        leg("s2", "call", side="sell"),
    )

    outcome = replay(written, market)

    assert _events(outcome, EventKind.CLOSED_WITH_ITS_PROTECTION) == [(10, "s2"), (10, "s1")]
    assert [(trade.leg_id, trade.exit_reason.value) for trade in outcome.trades] == [
        ("b1", "stop"),
        ("s1", "closed_with_its_protection"),
        ("b2", "exit_time"),
        ("s2", "closed_with_its_protection"),
    ]


def test_the_book_is_asked_again_until_the_stopped_legs_sale_is_allowed_in_the_minute_the_buy_backs_fill() -> None:
    # Bought 700 and 710, sold 675 and 680: 5,500.00. The 700 is stopped. The book's plan against 5,500.00 closes the 680
    # alone, which leaves the 710 against the 675 at 3,500.00. But once the 680 is bought back what is pledged is the
    # 700 against the 675, 2,500.00, and the 700's sale would take it to 3,500.00: it would wait for the exit time, with
    # its stop already decided. So the 675 goes too, and the sale fills with the buy-backs.
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(
            ("b700", "500", CALL_700), ("b710", "500", CALL_710), ("s675", "500", CALL_675), ("s680", "500", CALL_680)
        ),
        options={
            CALL_700: {**flat("2.00", 5, 9), 10: ("2.00", "1.40"), **flat("1.30", 11, 61)},
            CALL_710: flat("1.00", 5, 61),
            CALL_675: flat("4.20", 5, 61),
            CALL_680: flat("3.60", 5, 61),
        },
    )
    written = setup(
        leg("b700", "call", stop=STOP_25),
        leg("b710", "call"),
        leg("s675", "call", side="sell"),
        leg("s680", "call", side="sell"),
    )

    outcome = replay(written, market)

    assert [row(trade) for trade in outcome.trades] == [
        ("b700", 0, 6, D("2.00"), D("2.00"), "stop", 11, D("1.30"), D("1.30")),
        ("b710", 0, 6, D("1.00"), D("1.00"), "exit_time", 61, D("1.00"), D("1.00")),
        ("s675", 0, 6, D("4.20"), D("4.20"), "closed_with_its_protection", 11, D("4.20"), D("4.20")),
        ("s680", 0, 6, D("3.60"), D("3.60"), "closed_with_its_protection", 11, D("3.60"), D("3.60")),
    ]
    # Last listed first, and then the leg the first answer left behind.
    assert _events(outcome, EventKind.CLOSED_WITH_ITS_PROTECTION) == [(10, "s680"), (10, "s675")]
    assert _events(outcome, EventKind.CLOSE_FILLED) == [(11, "s675"), (11, "s680"), (11, "b700"), (61, "b710")]
    # Opening: -200.00 - 100.00 + 420.00 + 360.00 = 10480.00 against 5,500.00. Closing: -420.00 - 360.00 + 130.00.
    assert outcome.money_steps[:2] == (
        MoneyStep(at(6), FillGroup.OPENING, D("10000"), D("10480.00"), D("0"), D("5500.00"), D("135500"), False),
        MoneyStep(at(11), FillGroup.CLOSING, D("10480.00"), D("9830.00"), D("5500.00"), D("0"), D("0"), True),
    )


def test_a_second_stop_while_a_pair_is_still_waiting_to_close_is_asked_of_what_is_held_and_not_closing() -> None:
    # Two covered pairs, (680, 675) and (700, 695). The 680 is stopped at minute 10 and takes both sold legs with it
    # (the plan above), and neither sold leg has a bar in minutes 11 and 12, so nothing fills. The 700 is stopped at
    # minute 12. What is then held and not closing is the 700 alone: asked of a book that took the closing 680 out
    # and kept its sold legs, the replay would hand the book bare sold legs and raise. Here it takes all four out.
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(
            ("b1", "500", CALL_680), ("s1", "500", CALL_675), ("b2", "500", CALL_700), ("s2", "500", CALL_695)
        ),
        options={
            CALL_680: {**flat("3.00", 5, 9), 10: ("3.00", "2.00"), **flat("1.90", 11, 61)},
            CALL_675: {**flat("4.20", 5, 10), **flat("4.00", 13, 61)},
            CALL_700: {**flat("1.00", 5, 11), 12: ("1.00", "0.70"), **flat("0.65", 13, 61)},
            CALL_695: {**flat("2.00", 5, 10), **flat("2.10", 13, 61)},
        },
    )
    written = setup(
        leg("b1", "call", stop=STOP_25),
        leg("s1", "call", side="sell"),
        leg("b2", "call", stop=STOP_25),
        leg("s2", "call", side="sell"),
    )

    outcome = replay(written, market)

    assert _events(outcome, EventKind.STOP_HIT) == [(10, "b1"), (12, "b2")]
    assert _events(outcome, EventKind.CLOSED_WITH_ITS_PROTECTION) == [(10, "s2"), (10, "s1")]
    # Everything fills at minute 13, the first minute the sold legs trade again: buy-backs, then both sales.
    assert minutes(outcome, EventKind.CLOSE_FILLED) == [13, 13, 13, 13]
    assert [(trade.leg_id, trade.exit_reason.value, trade.exit_group) for trade in outcome.trades] == [
        ("b1", "stop", 1),
        ("s1", "closed_with_its_protection", 1),
        ("b2", "stop", 2),
        ("s2", "closed_with_its_protection", 1),
    ]
    assert [minute_of(step.at_ms) for step in outcome.money_steps] == [6, 13]


# ---- A bought leg and a sold leg stopped in the same minute -----------------------------


def test_a_bought_leg_and_a_sold_leg_stopped_in_the_same_minute_are_one_group_and_each_has_its_own_stop() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("long", "500", PUT_495), ("short", "500", PUT_500)),
        options={
            # The long's stop sits at 0.75 and minute 10 closes at 0.70. The short's sits at 2.20 x 1.25 = 2.75 and the
            # same minute closes at 2.80.
            PUT_495: {**flat("1.00", 5, 9), 10: ("1.00", "0.70"), **flat("0.65", 11, 61)},
            PUT_500: {**flat("2.20", 5, 9), 10: ("2.20", "2.80"), **flat("2.90", 11, 61)},
        },
    )

    outcome = replay(setup(*_put_spread(long={"stop": STOP_25}, short={"stop": STOP_25})), market)

    assert [row(trade) for trade in outcome.trades] == [
        ("long", 0, 6, D("1.00"), D("1.00"), "stop", 11, D("0.65"), D("0.65")),
        ("short", 0, 6, D("2.20"), D("2.20"), "stop", 11, D("2.90"), D("2.90")),
    ]
    assert [trade.exit_group for trade in outcome.trades] == [1, 1]
    assert outcome.event_counts[EventKind.STOP_HIT] == 2
    # The sold leg was stopped by its own rule, and is not closed a second time with its protection.
    assert EventKind.CLOSED_WITH_ITS_PROTECTION not in outcome.event_counts
    assert EventKind.SQUARED_OFF not in outcome.event_counts


def test_a_sold_leg_stopped_in_the_minute_its_protection_is_stopped_does_not_ask_the_book_for_a_second_close() -> None:
    # Three legs: the 495 put bought for two contracts (stopped) and two sold 500 puts, one of them stopped itself. The
    # one with no stop of its own goes with the protection: the stopped sold leg is already leaving.
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("long", "500", PUT_495), ("a", "500", PUT_500), ("b", "500", PUT_505)),
        options={
            PUT_495: {**flat("1.00", 5, 9), 10: ("1.00", "0.70"), **flat("0.65", 11, 61)},
            PUT_500: {**flat("2.20", 5, 9), 10: ("2.20", "2.80"), **flat("2.90", 11, 61)},
            PUT_505: flat("3.00", 5, 61),
        },
    )
    written = setup(
        leg("long", "put", contracts=2, stop=STOP_25),
        leg("a", "put", side="sell", stop=STOP_25),
        leg("b", "put", side="sell"),
    )

    outcome = replay(written, market)

    assert [(trade.leg_id, trade.exit_reason.value, trade.exit_group) for trade in outcome.trades] == [
        ("long", "stop", 1),
        ("a", "stop", 1),
        ("b", "closed_with_its_protection", 1),
    ]
    assert _events(outcome, EventKind.CLOSED_WITH_ITS_PROTECTION) == [(10, "b")]


# ---- Re-entry of a sold leg ---------------------------------------------------------------


def test_a_sold_leg_closed_by_its_own_stop_re_enters_with_its_cover_held_and_is_judged_as_a_first_entry() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("long", "500", PUT_495), ("short", "500", PUT_500)),
        options={
            PUT_495: flat("1.00", 5, 61),
            # Sold at 2.00 (stop at 2.50), stopped at minute 10, bought back at the open of 11 at 2.90. The re-entry is
            # decided at minute 11 and sold at the open of 12 at 2.40, with a stop of its own at 2.40 x 1.25 = 3.00.
            PUT_500: {**flat("2.00", 5, 9), 10: ("2.00", "2.80"), 11: ("2.90", "2.90"), **flat("2.40", 12, 61)},
        },
    )

    outcome = replay(setup(*_put_spread(short={"stop": STOP_25, "reentry": AT_ONCE_1})), market)

    assert [row(trade) for trade in outcome.trades] == [
        ("long", 0, 6, D("1.00"), D("1.00"), "exit_time", 61, D("1.00"), D("1.00")),
        ("short", 0, 6, D("2.00"), D("2.00"), "stop", 11, D("2.90"), D("2.90")),
        ("short", 1, 12, D("2.40"), D("2.40"), "exit_time", 61, D("2.40"), D("2.40")),
    ]
    assert [trade.stop_level for trade in outcome.trades] == [None, D("2.50"), D("3.00")]
    assert minutes(outcome, EventKind.RE_ENTRY_EARNED, "short") == [10]
    # Held first, then the re-entry's sale was taken in a group of its own at minute 12.
    assert [(minute_of(step.at_ms), step.group.value) for step in outcome.money_steps] == [
        (6, "opening"),
        (11, "closing"),
        (12, "opening"),
        (61, "closing"),
    ]
    # Cash: 10000 - 100 + 200 - 290 + 240 = 10050.00 against the collateral of 500.00 held again.
    assert (outcome.money_steps[2].cash_after_usd, outcome.money_steps[2].collateral_after_usd) == (
        D("10050.00"),
        D("500.00"),
    )


def test_a_sold_leg_closed_with_its_protection_re_enters_with_its_cover_when_the_cover_re_enters_too() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("long", "500", PUT_495), ("short", "500", PUT_500)),
        options={
            # Stopped at minute 10 and sold at the open of 11 at 0.65; the fresh buy is at 0.80 at the open of 12.
            PUT_495: {**flat("1.00", 5, 9), 10: ("1.00", "0.70"), 11: ("0.65", "0.65"), **flat("0.80", 12, 61)},
            PUT_500: {**flat("2.20", 5, 10), 11: ("2.10", "2.10"), **flat("2.00", 12, 61)},
        },
    )
    written = setup(*_put_spread(long={"stop": STOP_25, "reentry": AT_ONCE_1}, short={"reentry": AT_ONCE_1}))

    outcome = replay(written, market)

    assert [row(trade) for trade in outcome.trades] == [
        ("long", 0, 6, D("1.00"), D("1.00"), "stop", 11, D("0.65"), D("0.65")),
        ("short", 0, 6, D("2.20"), D("2.20"), "closed_with_its_protection", 11, D("2.10"), D("2.10")),
        ("long", 1, 12, D("0.80"), D("0.80"), "exit_time", 61, D("0.80"), D("0.80")),
        ("short", 1, 12, D("2.00"), D("2.00"), "exit_time", 61, D("2.00"), D("2.00")),
    ]
    # Both earned it at the minute the stop was decided, the protected one after the stopped one.
    assert _events(outcome, EventKind.RE_ENTRY_EARNED) == [(10, "long"), (10, "short")]
    # The two entries are one opening group: the bought leg is the cover the sold leg stands on.
    assert [(minute_of(step.at_ms), step.group.value) for step in outcome.money_steps] == [
        (6, "opening"),
        (11, "closing"),
        (12, "opening"),
        (61, "closing"),
    ]


def test_a_sold_leg_closed_with_its_protection_is_refused_for_cover_when_its_cover_does_not_come_back() -> None:
    # The setup of the carried review finding: a partial square-off, a bought put with a stop and no re-entry, and a
    # sold put with a re-entry and no stop of its own. The validator takes it because the bought put's stop closes the
    # sold leg with it; the sold leg earns its re-entry, and the cover it needs is gone.
    market = _a_spread_whose_cover_is_stopped()
    written = setup(*_put_spread(long={"stop": STOP_25}, short={"reentry": AT_ONCE_1}))

    outcome = replay(written, market)

    assert [row(trade) for trade in outcome.trades] == [
        ("long", 0, 6, D("1.00"), D("1.00"), "stop", 11, D("0.65"), D("0.65")),
        ("short", 0, 6, D("2.20"), D("2.20"), "closed_with_its_protection", 11, D("2.10"), D("2.10")),
    ]
    assert minutes(outcome, EventKind.RE_ENTRY_EARNED, "short") == [10]
    refused = [event for event in outcome.events if event.kind is EventKind.ENTRY_REFUSED_FOR_COVER]
    # Decided at minute 11 and refused where it would fill, minute 12, with the price it would have filled at.
    assert [(minute_of(e.at_ms), e.leg_id, e.reason, e.price, e.entry_number) for e in refused] == [
        (12, "short", EntryRefusal.COVER_NOT_YET_HELD.value, D("2.10"), 1)
    ]
    assert kinds(outcome, "short")[-2:] == [EventKind.PICKED, EventKind.ENTRY_REFUSED_FOR_COVER]


@pytest.mark.parametrize(("stop_minute", "earned"), [(20, True), (21, False)])
def test_a_stop_after_the_no_re_entry_after_time_earns_a_sold_leg_closed_with_its_protection_none(
    stop_minute: int, earned: bool
) -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("long", "500", PUT_495), ("short", "500", PUT_500)),
        options={
            PUT_495: {
                **flat("1.00", 5, stop_minute - 1),
                stop_minute: ("1.00", "0.70"),
                **flat("0.65", stop_minute + 1, 61),
            },
            PUT_500: flat("2.20", 5, 61),
        },
    )
    written = setup(*_put_spread(long={"stop": STOP_25}, short={"reentry": AT_ONCE_1}), no_reentry_after=after_open(20))

    outcome = replay(written, market)

    assert [trade.exit_reason for trade in outcome.trades] == [
        ExitReason.STOP,
        ExitReason.CLOSED_WITH_ITS_PROTECTION,
    ]
    assert minutes(outcome, EventKind.RE_ENTRY_EARNED, "short") == ([stop_minute] if earned else [])
    assert minutes(outcome, EventKind.RE_ENTRY_BLOCKED_BY_CUTOFF, "short") == ([] if earned else [stop_minute])


def test_a_sold_legs_own_stop_after_the_cutoff_earns_no_re_entry_either() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("long", "500", PUT_495), ("short", "500", PUT_500)),
        options={
            PUT_495: flat("1.00", 5, 61),
            PUT_500: {**flat("2.00", 5, 20), 21: ("2.00", "2.80"), **flat("2.90", 22, 61)},
        },
    )
    written = setup(*_put_spread(short={"stop": STOP_25, "reentry": AT_ONCE_1}), no_reentry_after=after_open(20))

    outcome = replay(written, market)

    assert minutes(outcome, EventKind.RE_ENTRY_BLOCKED_BY_CUTOFF, "short") == [21]
    assert [(trade.leg_id, trade.entry_number) for trade in outcome.trades] == [("long", 0), ("short", 0)]


# ---- A complete square-off --------------------------------------------------------------


def test_a_complete_square_off_closes_bought_and_sold_legs_together_and_cancels_what_waits() -> None:
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(
            ("long", "500", PUT_495), ("short", "500", PUT_500), ("c", "500", CALL_500), ("late", "500", PUT_505)
        ),
        options={
            # Two puts bought at 1.00: the stop sits at 0.75, and minute 10 closes at 0.70.
            PUT_495: {**flat("1.00", 5, 9), 10: ("1.00", "0.70"), **flat("0.65", 11, 61)},
            PUT_500: flat("2.20", 5, 61),
            CALL_500: flat("2.00", 5, 61),
            # The momentum level 3.00 x 1.25 = 3.75 is never reached.
            PUT_505: flat("3.00", 5, 61),
        },
    )
    written = setup(
        leg("long", "put", contracts=2, stop=STOP_25),
        leg("short", "put", side="sell"),
        leg("c", "call"),
        leg("late", "put", side="sell", momentum=("percent_up", "25")),
        square_off="complete",
    )

    outcome = replay(written, market)

    assert [(trade.leg_id, trade.exit_reason.value, trade.exit_group) for trade in outcome.trades] == [
        ("long", "stop", 1),
        ("short", "square_off", 1),
        ("c", "square_off", 1),
    ]
    # Everything was decided by the stop and the square-off, so no leg is closed with a protection, and the leg still
    # waiting for its momentum is cancelled for the day.
    assert EventKind.CLOSED_WITH_ITS_PROTECTION not in outcome.event_counts
    assert [(e.leg_id, e.reason) for e in outcome.events if e.kind is EventKind.LEG_CANCELLED] == [
        ("late", "square_off")
    ]
    # The buy-back fills first, then the sales in the order the legs are listed.
    assert _events(outcome, EventKind.CLOSE_FILLED) == [(11, "short"), (11, "long"), (11, "c")]


# ---- What the replay asks the book -------------------------------------------------------


def test_the_replay_never_asks_the_book_for_a_plan_when_no_bought_leg_of_a_setup_that_sells_is_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked = 0
    real_plan = replay_module.leaving_plan

    def counting(held: Held, leaving: Any) -> tuple[str, ...]:
        nonlocal asked
        asked += 1
        return real_plan(held, leaving)

    monkeypatch.setattr(replay_module, "leaving_plan", counting)

    # A stop on the sold leg alone, and a setup that buys only.
    replay(setup(*_put_spread(short={"stop": STOP_25})), _stopped_sold_leg_market())
    bought_only = TableMarket(
        underlying=steady("500"),
        picks=picked(("call", "500", CALL_500)),
        options={CALL_500: {**flat("2.00", 5, 9), 10: ("2.00", "1.40"), **flat("1.40", 11, 61)}},
    )
    replay(setup(leg("call", stop=STOP_25)), bought_only)

    assert asked == 0


def _stopped_sold_leg_market() -> TableMarket:
    return TableMarket(
        underlying=steady("500"),
        picks=picked(("long", "500", PUT_495), ("short", "500", PUT_500)),
        options={
            PUT_495: flat("1.00", 5, 61),
            PUT_500: {**flat("2.00", 5, 9), 10: ("2.00", "2.80"), **flat("2.90", 11, 61)},
        },
    )


# ---- One sentence, one validator, one replay ---------------------------------------------


def _clock() -> SessionClock:
    return SessionClock(open_minute_et=570, close_minute_et=960)


def test_a_partial_square_off_that_takes_a_protected_sold_leg_along_says_so_and_the_replay_does_it() -> None:
    # The carried review finding on #3105: a bought call with a stop and a sold call with a re-entry and no stop, under
    # a partial square-off. The validator takes the setup (the sold leg is reached through the bought leg's stop), the
    # sentences say that a stopped bought leg takes the sold legs it protects with it, and the replay does exactly that.
    written = setup(
        leg("long_call", "call", stop=STOP_25),
        leg("short_call", "call", side="sell", reentry=AT_ONCE_1),
    )

    sentences = describe_setup(written, full_session=_clock())
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("long_call", "500", CALL_700), ("short_call", "500", CALL_695)),
        options={
            CALL_700: {**flat("2.00", 5, 9), 10: ("2.00", "1.40"), **flat("1.30", 11, 61)},
            CALL_695: flat("3.00", 5, 61),
        },
    )
    outcome = replay(written, market)

    assert sentences.square_off == (
        "Square off is partial: a stop closes only the leg that was stopped, and a stopped bought leg takes with it "
        "the sold legs it protects, closed in the same minute and exit group, last listed first, which can close more "
        "legs than strictly needed.",
    )
    assert [trade.exit_reason for trade in outcome.trades[:2]] == [
        ExitReason.STOP,
        ExitReason.CLOSED_WITH_ITS_PROTECTION,
    ]
    assert minutes(outcome, EventKind.RE_ENTRY_EARNED, "short_call") == [10]
