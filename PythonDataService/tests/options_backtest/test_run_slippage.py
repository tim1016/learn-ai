"""The slippage a result survives, read from a replay of the whole run at each value on a ladder.

The scenes buy the 500 call at the open of minute 6 and sell it at the open of minute 61 (or at the open of the minute
after a stop). A buy fills at the price seen moved up by the slippage and rounded up to the cent, a sale at the price
seen moved down and rounded down to the cent. A stop of 25 percent is set from the fill and hit by a close at or below
it. Every figure is worked by hand and written as a literal.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.options_backtest import fills
from app.options_backtest.results import Fill
from app.options_backtest.run import (
    DEFAULT_SLIPPAGE_LADDER,
    SlippagePoint,
    SlippageTolerance,
    run_sessions,
    slippage_tolerance,
)
from app.options_backtest.setup import OptionsBacktestSetup
from tests.options_backtest._market import leg, setup
from tests.options_backtest._run_data import THU, WED, D, Day, FakeRunData, changed, fake_data, flat, steady

CALL_500 = ("call", 500)
STOP_25 = ("percent", "25")
LADDER = (D("0"), D("1"), D("5"), D("10"), D("20"))


def _profit_day() -> Day:
    # Seen: bought at 2.00 (minute 6's open), sold at 2.40 (minute 61's open). No close is anywhere near a stop.
    return Day(underlying=steady("500.00"), options={CALL_500: {**flat("2.00", 5, 60), 61: ("2.40", "2.40")}})


def _stop_day() -> Day:
    # Seen: bought at 2.00. Minute 6 closes at 1.55, which is under a stop set 25 percent below 2.10 or more (1.575)
    # and over one set below 2.02 (1.515). Minute 7 opens at 1.70 and the exit time's sale at minute 61 opens at 1.90.
    series = {5: ("2.00", "2.00"), 6: ("2.00", "1.55"), **flat("1.70", 7, 60), 61: ("1.90", "1.90")}
    return Day(underlying=steady("500.00"), options={CALL_500: series})


def _two_days_at_one_percent() -> tuple[OptionsBacktestSetup, FakeRunData]:
    return setup(leg(stop=STOP_25), slippage="1"), fake_data({WED: _profit_day(), THU: _stop_day()})


def test_the_ladder_replays_the_two_days_at_each_slippage_and_its_points_are_worked_by_hand() -> None:
    written, data = _two_days_at_one_percent()

    result = run_sessions(written, [WED, THU], data, slippage_ladder=LADDER)

    # Wednesday (no stop is near):
    #   0%: bought 2.00, sold 2.40: +40.00.            1%: bought 2.02, sold 2.376 down to 2.37: +35.00.
    #   5%: bought 2.10, sold 2.28: +18.00.             10%: bought 2.20, sold 2.16: -4.00.
    #   20%: bought 2.40, sold 1.92: -48.00.
    # Thursday:
    #   0%: stop 1.50 not reached, sold 1.90: (1.90 - 2.00) x 100 = -10.00.
    #   1%: bought 2.02, stop 1.515 not reached by 1.55, sold 1.881 down to 1.88: (1.88 - 2.02) x 100 = -14.00.
    #   5%: bought 2.10, stop 1.575, minute 6 closes at 1.55: stopped, sold at minute 7 for 1.70 x 0.95 = 1.615 down
    #       to 1.61: (1.61 - 2.10) x 100 = -49.00.
    #   10%: bought 2.20, stop 1.65, stopped, sold 1.70 x 0.90 = 1.53: (1.53 - 2.20) x 100 = -67.00.
    #   20%: bought 2.40, stop 1.80, stopped, sold 1.70 x 0.80 = 1.36: (1.36 - 2.40) x 100 = -104.00.
    # Net profit: 30.00, 21.00, -31.00, -71.00, -152.00. Winning sessions: Wednesday only, and only while it profits.
    assert [
        (p.slippage_percent, p.net_profit_usd, p.trades, p.traded_sessions, p.winning_sessions) for p in result.slippage
    ] == [
        (D("0"), D("30.00"), 2, 2, 1),
        (D("1"), D("21.00"), 2, 2, 1),
        (D("5"), D("-31.00"), 2, 2, 1),
        (D("10"), D("-71.00"), 2, 2, 0),
        (D("20"), D("-152.00"), 2, 2, 0),
    ], "the run is replayed again at each slippage and a stop that slippage turns is in the answer"
    assert [p.is_setups_own for p in result.slippage] == [False, True, False, False, False], (
        "only the point at the setup's own 1% is the setup's own"
    )

    # The run's own fills, seen at 2.00 -> 2.40 and 2.00 -> 1.90, re-priced at 5% without a new replay:
    #   Wednesday: bought 2.10, sold 2.28: +18.00.  Thursday: bought 2.10, sold 1.90 x 0.95 = 1.805 down to 1.80: -30.00.
    repriced = D("0")
    for session in result.sessions:
        for trade in session.outcome.trades:
            bought = fills.buy_fill_price(trade.entry.seen, slippage_percent=D("5"), price_step_usd=D("0.01"))
            assert isinstance(trade.exit, Fill), "every trade of the run was sold, none left marked"
            sold = fills.sell_fill_price(trade.exit.seen, slippage_percent=D("5"), price_step_usd=D("0.01"))
            repriced += (sold - bought) * 100
    five_percent = next(point for point in result.slippage if point.slippage_percent == D("5"))

    assert repriced == D("-12.00"), "re-pricing the fills of one replay gives another answer where a stop turns"
    # The replay at 5% stopped Thursday's leg out at minute 6 and lost 49.00 there: -31.00, not -12.00.
    assert five_percent.net_profit_usd == D("-31.00"), "the replay at 5% stopped Thursday's leg out and lost 49.00"

    # At 1% the run's own sessions made +35.00 and -14.00.
    assert [session.pnl_usd for session in result.sessions] == [D("35.00"), D("-14.00")], (
        "at 1% the run's own sessions made +35.00 and -14.00"
    )
    own = next(point for point in result.slippage if point.is_setups_own)
    assert own.net_profit_usd == sum((session.pnl_usd for session in result.sessions), start=D("0")) == D("21.00"), (
        "the setup's own point is the run's own result"
    )

    # 30.00 at 0, 21.00 at 1, then -31.00 at 5: it survives 1 and not 5.
    assert result.slippage_tolerance == SlippageTolerance(
        highest_profitable_percent=D("1"), first_unprofitable_percent=D("5")
    ), "the figure is the highest ladder value that still profits and the next one that does not"
    # Underlying, listing and the call, for two days.
    assert len(data.asked) == len(set(data.asked)) == 6, "the ladder replays read each day's data once"


def test_a_cash_refusal_that_slippage_causes_is_replayed_and_not_assumed_away() -> None:
    # 200.50 of cash. At no slippage the buy costs 2.00 x 100 = 200.00 and is taken, and the sale at 2.40 makes +40.00.
    # At 0.5% the buy fills at 2.00 x 1.005 = 2.01 and costs 201.00, which the cash cannot cover: no trade, no profit.
    # At 1% it costs 202.00: refused too.
    short_of_cash = changed(setup(leg(), slippage="0"), starting_cash_usd="200.50")

    result = run_sessions(
        short_of_cash, [WED], fake_data({WED: _profit_day()}), slippage_ladder=(D("0"), D("0.5"), D("1"))
    )

    assert [
        (p.slippage_percent, p.net_profit_usd, p.trades, p.traded_sessions, p.winning_sessions) for p in result.slippage
    ] == [(D("0"), D("40.00"), 1, 1, 1), (D("0.5"), D("0"), 0, 0, 0), (D("1"), D("0"), 0, 0, 0)]
    assert result.slippage_tolerance == SlippageTolerance(D("0"), D("0.5"))


def test_each_ladder_value_carries_its_own_cash_from_session_to_session() -> None:
    # 400.00 of cash. Thursday's contract is seen at 3.00 and held flat.
    # 0%: Wednesday buys 2.00 and sells 2.40: +40.00, so Thursday starts with 440.00 and buys at 3.00 (300.00): taken,
    #     and sold at 3.00 for nothing. Net +40.00 over two trades.
    # 20%: Wednesday buys 2.40 (240.00) and sells 1.92: -48.00, so Thursday starts with 352.00. It would buy at
    #     3.00 x 1.20 = 3.60 (360.00): refused. One trade, net -48.00. Had the 20% run been handed the run's own 440.00
    #     it would have bought, and lost (2.40 - 3.60) x 100 = 120.00 more.
    thursday = Day(underlying=steady("500.00"), options={CALL_500: {**flat("3.00", 5, 60), 61: ("3.00", "3.00")}})
    written = changed(setup(leg(), slippage="0"), starting_cash_usd="400")

    result = run_sessions(
        written, [WED, THU], fake_data({WED: _profit_day(), THU: thursday}), slippage_ladder=(D("0"), D("20"))
    )

    zero, twenty = result.slippage
    assert (zero.net_profit_usd, zero.trades) == (D("40.00"), 2)
    assert (twenty.net_profit_usd, twenty.trades) == (D("-48.00"), 1)


def test_a_point_counts_the_trades_and_not_the_sessions_that_traded_when_a_stop_turns_a_re_entry() -> None:
    # Thursday's contract and a leg that re-enters at once after a stop; the underlying is steady at 500.
    # 0%: bought 2.00, the stop 1.50 is not reached by minute 6's close of 1.55; sold at minute 61 for 1.90:
    #     (1.90 - 2.00) x 100 = -10.00. One trade.
    # 5%: bought 2.10, the stop 1.575 is reached by 1.55: sold at minute 7 for 1.70 x 0.95 = 1.615 down to 1.61:
    #     (1.61 - 2.10) x 100 = -49.00. The stop earns a re-entry, decided at minute 7 and bought at minute 8's open:
    #     1.70 x 1.05 = 1.785 up to 1.79. It is held to the exit time and sold at minute 61 for 1.90 x 0.95 = 1.805
    #     down to 1.80: (1.80 - 1.79) x 100 = +1.00. Two trades in the one session, net -49.00 + 1.00 = -48.00.
    written = setup(leg(stop=STOP_25, reentry=("at_once", 1)), slippage="5")

    result = run_sessions(written, [THU], fake_data({THU: _stop_day()}), slippage_ladder=(D("0"), D("5")))

    assert [
        (p.slippage_percent, p.net_profit_usd, p.trades, p.traded_sessions, p.winning_sessions) for p in result.slippage
    ] == [(D("0"), D("-10.00"), 1, 1, 0), (D("5"), D("-48.00"), 2, 1, 0)]
    # The setup's own slippage is 5%, so the session kept is that one: the first entry and its re-entry.
    assert [trade.entry_number for trade in result.sessions[0].outcome.trades] == [0, 1]


def test_the_default_ladder_the_setups_own_value_off_it_and_a_ladder_sorted_with_each_value_once() -> None:
    assert (
        tuple(D(value) for value in ("0", "0.1", "0.2", "0.3", "0.5", "0.75", "1", "1.5", "2", "3", "5"))
        == DEFAULT_SLIPPAGE_LADDER
    ), "the default ladder"
    # A session whose underlying did not trade at the entry time: skipped, so cheap, and every point is nothing.
    skipped = Day(underlying=steady("500.00", 6, 390))
    data = fake_data({WED: skipped})

    own_on_the_ladder = run_sessions(changed(setup(leg()), slippage_percent="0.30"), [WED], data)
    own_off_the_ladder = run_sessions(changed(setup(leg()), slippage_percent="0.4"), [WED], data)
    unsorted = run_sessions(setup(leg()), [WED], data, slippage_ladder=(D("5"), D("1"), D("1.0"), D("0")))

    assert [p.slippage_percent for p in own_on_the_ladder.slippage] == list(DEFAULT_SLIPPAGE_LADDER), (
        "a setup's own value on the ladder leaves the default ladder as it is"
    )
    assert [p.slippage_percent for p in own_on_the_ladder.slippage if p.is_setups_own] == [D("0.3")], (
        "the setup's own value on the ladder is the one point that is its own"
    )
    assert [p.slippage_percent for p in own_off_the_ladder.slippage] == [
        D(value) for value in ("0", "0.1", "0.2", "0.3", "0.4", "0.5", "0.75", "1", "1.5", "2", "3", "5")
    ], "a setup's own value off the ladder is added to it in its place"
    assert [p.slippage_percent for p in own_off_the_ladder.slippage if p.is_setups_own] == [D("0.4")], (
        "the setup's own value off the ladder is the one point that is its own"
    )
    assert [p.slippage_percent for p in unsorted.slippage] == [D("0"), D("1"), D("5")], (
        "a ladder is sorted and has each value once whatever order and spelling it arrives in"
    )


@pytest.mark.parametrize("bad", [D("100"), D("-1"), D("0.00001")])
def test_a_ladder_value_the_setup_could_not_carry_is_refused_before_anything_is_read(bad: Decimal) -> None:
    data = fake_data({WED: _profit_day()})

    with pytest.raises(ValueError, match="slippage_percent"):
        run_sessions(setup(leg()), [WED], data, slippage_ladder=(D("0"), bad))

    assert data.asked == []


def _point(percent: str, profit: str, trades: int = 1) -> SlippagePoint:
    return SlippagePoint(D(percent), False, D(profit), trades, min(trades, 1), 1)


@pytest.mark.parametrize(
    ("points", "highest_profitable", "first_unprofitable", "nothing_traded"),
    [
        pytest.param(
            (("0", "10"), ("1", "5"), ("2", "-1"), ("3", "8")),
            D("1"),
            D("2"),
            False,
            id="stops-at-the-first-value-that-does-not-profit-even-when-a-higher-one-does",
        ),
        pytest.param(
            (("0", "10"), ("1", "0")), D("0"), D("1"), False, id="a-profit-of-exactly-nothing-is-not-a-profit"
        ),
        pytest.param(
            (("0", "-0.01"), ("1", "50")), None, D("0"), False, id="a-run-that-loses-at-the-lowest-value-survives-none"
        ),
        pytest.param(
            (("0", "3"), ("5", "1")),
            D("5"),
            None,
            False,
            id="a-run-that-profits-at-every-value-survives-the-whole-ladder-and-not-beyond-it",
        ),
        pytest.param(
            (("0", "0", 0), ("1", "0", 0), ("5", "0", 0)),
            None,
            None,
            True,
            id="a-run-that-made-no-trade-at-any-value-says-so-and-is-not-a-loss-from-the-lowest-value",
        ),
        # Traded, and made exactly nothing at the lowest value: not a profit, and a trade was made.
        pytest.param(
            (("0", "0"), ("1", "-5")), None, D("0"), False, id="traded-and-made-nothing-at-the-lowest-value-is-a-trade"
        ),
        # Traded at one value only (the higher ones cost the trade its cash): the run did trade.
        pytest.param((("0", "-2"), ("1", "0", 0)), None, D("0"), False, id="traded-at-one-value-only-is-a-trade"),
        # Traded at every value and made exactly nothing at each: profit alone cannot tell it from a run with no trade.
        pytest.param(
            (("0", "0"), ("1", "0"), ("5", "0")),
            None,
            D("0"),
            False,
            id="traded-at-every-value-and-made-nothing-at-each-is-a-trade",
        ),
        pytest.param(
            (("2", "-1"), ("0", "10"), ("1", "5")),
            D("1"),
            D("2"),
            False,
            id="points-are-read-from-the-lowest-value-up-whatever-order-they-arrive-in",
        ),
    ],
)
def test_the_figure_is_read_from_the_points_of_the_ladder(
    points: tuple[tuple[Any, ...], ...],
    highest_profitable: Decimal | None,
    first_unprofitable: Decimal | None,
    nothing_traded: bool,
) -> None:
    tolerance = slippage_tolerance([_point(*each) for each in points])

    assert tolerance == SlippageTolerance(highest_profitable, first_unprofitable, nothing_traded=nothing_traded), (
        "the figure is the highest value that profits and the first that does not, or says nothing was traded"
    )
    assert tolerance.nothing_traded is nothing_traded, "only a run that made no trade at any value says so"


def test_a_whole_run_that_never_traded_has_no_tolerance_to_report_and_one_that_lost_everywhere_has_a_value() -> None:
    # Wednesday's contract was captured and never traded, so the entry is dropped: no trade at any value.
    never = fake_data({WED: Day(underlying=steady("500.00"), options={CALL_500: {}})})
    # Bought at 2.00 or more and sold at 1.90 or less at every ladder value: a loss at all of them.
    losing = Day(underlying=steady("500.00"), options={CALL_500: {**flat("2.00", 5, 60), 61: ("1.90", "1.90")}})

    quiet = run_sessions(setup(leg()), [WED], never, slippage_ladder=(D("0"), D("1")))
    lost = run_sessions(setup(leg()), [WED], fake_data({WED: losing}), slippage_ladder=(D("0"), D("1")))

    assert quiet.slippage_tolerance == SlippageTolerance(None, None, nothing_traded=True)
    assert [p.trades for p in quiet.slippage] == [0, 0]
    # (1.90 - 2.00) x 100 = -10.00 at 0%, and a larger loss at 1%.
    assert lost.slippage_tolerance == SlippageTolerance(None, D("0"))
    assert [p.trades for p in lost.slippage] == [1, 1]
