"""Every figure of a run's summary, worked by hand from seven sessions written down in ``_scene``.

Each trade is a leg round trip of one or more contracts of 100 shares. A trade's profit is (sold - bought) x 100 x
contracts less its two fees, bought and sold being the prices it filled at. The seven trades and their profits:

    A  2 contracts  bought 2.02  sold 1.48 (stop)         (1.48 - 2.02) x 200 - 1.30 - 1.30 = -108.00 - 2.60 = -110.60
    B  1 contract   bought 3.01  sold 3.58 (exit time)    (3.58 - 3.01) x 100 - 0.65 - 0.65 =   57.00 - 1.30 =   55.70
    E  1 contract   bought 1.00  sold 1.00 (exit time)    0.00                                                =    0.00
    F  1 contract   bought 2.00  marked 2.50              (2.50 - 2.00) x 100 - 0.65 - 0     =   50.00 - 0.65 =   49.35
    C  1 contract   bought 4.04  sold 4.46 (exit time)    (4.46 - 4.04) x 100 - 0.65 - 0.65 =   42.00 - 1.30 =   40.70
    D  3 contracts  bought 1.01  sold 0.79 (square off)   (0.79 - 1.01) x 300 - 1.95 - 1.95 =  -66.00 - 3.90 =  -69.90
    G  1 contract   bought 2.00  sold 3.00 (exit time)    (3.00 - 2.00) x 100                  =  100.00

and the sessions they sit in, with the cash each starts with (10,000.00 to begin) and the equity it ends on:

    1  Sep 29  A, B        -54.90   9,945.10        5  Oct 5   F (marked)     +49.35   9,994.45
    2  Sep 30  none (no trade)        0   9,945.10    6  Oct 6   C, D           -29.20   9,965.25
    3  Oct 1   none (skipped)         0   9,945.10    7  Oct 7   G             +100.00  10,065.25
    4  Oct 2   E                      0   9,945.10
"""

from __future__ import annotations

from datetime import date
from decimal import Context, Decimal, localcontext

import pytest

from app.options_backtest.market import PickRefusal
from app.options_backtest.results import EventKind, ExitReason, SessionStatus, SkipReason
from app.options_backtest.session_result import SessionResult
from app.options_backtest.statistics import Drawdown, Ratio, summarize_run
from tests.options_backtest._results import a_session, a_trade, an_event
from tests.options_backtest._run_data import D, open_ms

SEP_29, SEP_30 = date(2026, 9, 29), date(2026, 9, 30)
OCT_1, OCT_2, OCT_5, OCT_6, OCT_7 = (date(2026, 10, day) for day in (1, 2, 5, 6, 7))
STARTING = D("10000.00")


def _scene() -> list[SessionResult]:
    return [
        a_session(
            SEP_29,
            "10000.00",
            status=SessionStatus.TRADED,
            trades=(
                a_trade(
                    "a",
                    entry=("2.00", "2.02"),
                    exit=("1.50", "1.48"),
                    reason=ExitReason.STOP,
                    contracts=2,
                    fees=("1.30", "1.30"),
                ),
                a_trade(
                    "b",
                    entry=("3.00", "3.01"),
                    exit=("3.60", "3.58"),
                    reason=ExitReason.EXIT_TIME,
                    fees=("0.65", "0.65"),
                ),
            ),
            events=(an_event(EventKind.STARTING_PRICE_TAKEN, "b"), an_event(EventKind.MOMENTUM_HIT, "b")),
        ),
        a_session(
            SEP_30,
            "9945.10",
            status=SessionStatus.NO_TRADE,
            events=(
                an_event(EventKind.PICK_REFUSED, "a", PickRefusal.STRIKE_NOT_LISTED.value),
                an_event(EventKind.PICK_REFUSED, "c", PickRefusal.EXPIRY_NOT_LISTED.value),
                an_event(EventKind.STARTING_PRICE_TAKEN, "b"),
                an_event(EventKind.NO_STARTING_PRICE, "c"),
            ),
        ),
        a_session(OCT_1, "9945.10", status=SessionStatus.SKIPPED, skip_reason=SkipReason.UNDERLYING_DID_NOT_TRADE),
        a_session(
            OCT_2,
            "9945.10",
            status=SessionStatus.TRADED,
            trades=(a_trade("a", entry=("1.00", "1.00"), exit=("1.00", "1.00"), reason=ExitReason.EXIT_TIME),),
            events=(an_event(EventKind.ENTRY_DROPPED, "b"), an_event(EventKind.RE_ENTRY_BLOCKED_BY_CUTOFF, "a")),
        ),
        a_session(
            OCT_5,
            "9945.10",
            status=SessionStatus.INCOMPLETE,
            trades=(
                a_trade(
                    "a", entry=("2.00", "2.00"), mark="2.50", reason=ExitReason.MARKED_NOT_SOLD, fees=("0.65", "0")
                ),
            ),
            events=(an_event(EventKind.ENTRY_REFUSED_FOR_CASH, "b", "cash_short"),),
        ),
        a_session(
            OCT_6,
            "9994.45",
            status=SessionStatus.TRADED,
            trades=(
                a_trade(
                    "c",
                    entry=("4.00", "4.04"),
                    exit=("4.50", "4.46"),
                    reason=ExitReason.EXIT_TIME,
                    fees=("0.65", "0.65"),
                ),
                a_trade(
                    "d",
                    entry=("1.00", "1.01"),
                    exit=("0.80", "0.79"),
                    reason=ExitReason.SQUARE_OFF,
                    contracts=3,
                    fees=("1.95", "1.95"),
                ),
            ),
            events=(an_event(EventKind.RE_ENTRY_LOST, "c"),),
        ),
        a_session(
            OCT_7,
            "9965.25",
            status=SessionStatus.TRADED,
            trades=(a_trade("g", entry=("2.00", "2.00"), exit=("3.00", "3.00"), reason=ExitReason.EXIT_TIME),),
        ),
    ]


def test_the_sessions_are_counted_by_what_happened_in_them() -> None:
    summary = summarize_run(_scene(), starting_cash_usd=STARTING)

    # Seven tested = five traded (1, 4, 5, 6, 7) + one no trade (2) + one skipped (3).
    assert summary.sessions_tested == 7
    assert summary.sessions_traded == 5
    assert summary.sessions_no_trade == 1
    assert summary.sessions_skipped == 1
    assert summary.skipped_by_reason == (
        (SkipReason.WINDOW_DOES_NOT_FIT, 0),
        (SkipReason.UNDERLYING_DID_NOT_TRADE, 1),
    )
    assert summary.sessions_incomplete == 1  # session 5, a leg marked and not sold
    # Of the five traded: 5 (+49.35) and 7 (+100.00) profit, 1 (-54.90) and 6 (-29.20) lose, 4 (0.00) breaks even.
    assert (summary.sessions_profitable, summary.sessions_losing, summary.sessions_break_even) == (2, 2, 1)


def test_the_daily_win_rate_is_profitable_sessions_over_traded_sessions_and_ends_in_a_decimal() -> None:
    summary = summarize_run(_scene(), starting_cash_usd=STARTING)

    assert summary.daily_win_rate == Ratio(D("2"), D("5"))
    assert summary.daily_win_rate.exact() == D("0.4")
    assert summary.daily_win_rate.rounded_to_6_places() == D("0.400000")


def test_the_trades_are_counted_won_lost_and_break_even_by_their_profit_after_fees() -> None:
    summary = summarize_run(_scene(), starting_cash_usd=STARTING)

    # B (55.70), F (49.35), C (40.70), G (100.00) won; A (-110.60) and D (-69.90) lost; E (0.00) broke even.
    assert summary.trades == 7
    assert (summary.trades_won, summary.trades_lost, summary.trades_break_even) == (4, 2, 1)
    # Four sevenths is a decimal that never ends: the two numbers, and a rounded one that says so.
    assert summary.trade_win_rate == Ratio(D("4"), D("7"))
    assert summary.trade_win_rate.exact() is None
    assert summary.trade_win_rate.rounded_to_6_places() == D("0.571429")  # 0.571428571...


def test_the_trades_are_counted_by_the_reason_they_left_with_every_reason_listed() -> None:
    summary = summarize_run(_scene(), starting_cash_usd=STARTING)

    # Stop: A. Square off: D. Exit time: B, E, C, G. Marked, not sold: F.
    assert summary.exits_by_reason == (
        (ExitReason.STOP, 1),
        (ExitReason.SQUARE_OFF, 1),
        (ExitReason.EXIT_TIME, 4),
        (ExitReason.MARKED_NOT_SOLD, 1),
    )


def test_the_money_adds_up_trade_profit_after_fees_with_the_marked_leg_at_its_mark() -> None:
    summary = summarize_run(_scene(), starting_cash_usd=STARTING)

    # Gross profit 55.70 + 49.35 + 40.70 + 100.00 = 245.75. Gross loss 110.60 + 69.90 = 180.50.
    # Net profit 245.75 - 180.50 = 65.25, which is also -54.90 + 0 + 0 + 0 + 49.35 - 29.20 + 100.00.
    assert summary.gross_profit_usd == D("245.75")
    assert summary.gross_loss_usd == D("180.50")
    assert summary.net_profit_usd == D("65.25")
    assert summary.starting_cash_usd == STARTING
    assert summary.ending_cash_usd == D("10065.25")


def test_the_profit_factor_is_gross_profit_over_gross_loss_in_dollars() -> None:
    summary = summarize_run(_scene(), starting_cash_usd=STARTING)

    # 245.75 / 180.50 = 983 / 722 = 1.36149584..., which never ends (722 has a factor of 19). The seventh place is
    # an 8, so six places round up to 1.361496.
    assert summary.profit_factor == Ratio(D("245.75"), D("180.50"))
    assert summary.profit_factor.exact() is None
    assert summary.profit_factor.rounded_to_6_places() == D("1.361496")


def test_the_costs_are_the_fees_of_every_fill_and_what_slippage_and_the_price_step_took_from_every_fill() -> None:
    summary = summarize_run(_scene(), starting_cash_usd=STARTING)

    # Fees: A 2.60 + B 1.30 + F 0.65 + C 1.30 + D 3.90 = 9.75.
    assert summary.total_fees_usd == D("9.75")
    # Slippage paid, |filled - seen| x 100 x contracts at each fill:
    #   A bought 0.02 x 200 = 4.00, sold 0.02 x 200 = 4.00;  B bought 0.01 x 100 = 1.00, sold 0.02 x 100 = 2.00;
    #   C bought 0.04 x 100 = 4.00, sold 0.04 x 100 = 4.00;  D bought 0.01 x 300 = 3.00, sold 0.01 x 300 = 3.00.
    #   E, G filled at the price seen; F's mark is not a fill. 4 + 4 + 1 + 2 + 4 + 4 + 3 + 3 = 25.00.
    assert summary.slippage_paid_usd == D("25.00")


def test_the_drawdown_is_the_worst_fall_of_equity_from_its_peak_measured_at_session_ends() -> None:
    summary = summarize_run(_scene(), starting_cash_usd=STARTING)

    # Equity: 10,000.00 at the start, then 9,945.10 after session 1 (and unchanged through 4), 9,994.45, 9,965.25,
    # 10,065.25. The deepest fall is the first: 10,000.00 - 9,945.10 = 54.90, which is 54.90 / 10,000.00 of the peak.
    # The peak is the starting cash, before any session, so it has no date.
    expected = Drawdown(
        peak_equity_usd=D("10000.00"),
        trough_equity_usd=D("9945.10"),
        fall_usd=D("54.90"),
        fall_ratio=Ratio(D("54.90"), D("10000.00")),
        peak_date=None,
        peak_session_open_ms=None,
        trough_date=SEP_29,
        trough_session_open_ms=open_ms(SEP_29),
    )
    assert summary.drawdown_by_fraction == expected
    assert summary.drawdown_by_dollars == expected
    assert summary.drawdown_by_fraction.measured_at == "session_end"
    assert summary.drawdown_by_fraction.fall_ratio.exact() == D("0.00549")


def test_what_did_not_trade_is_counted_from_the_events() -> None:
    summary = summarize_run(_scene(), starting_cash_usd=STARTING)

    # Session 2 took a starting price (leg b) and no momentum was reached; session 1 took one and reached it.
    assert summary.sessions_without_momentum == 1
    assert summary.entries_refused_for_cash == 1  # session 5
    assert summary.re_entries_blocked_by_cutoff == 1  # session 4
    assert summary.entries_dropped == 1  # session 4
    assert summary.picks_refused_by_reason == (
        (PickRefusal.CHAIN_INCOMPLETE, 0),
        (PickRefusal.EXPIRY_NOT_LISTED, 1),
        (PickRefusal.EXPIRY_NOT_A_SESSION, 0),
        (PickRefusal.STRIKE_NOT_LISTED, 1),
        (PickRefusal.NOT_STANDARD, 0),
    )
    assert summary.legs_without_a_starting_price == 1  # session 2
    assert summary.re_entries_lost == 1  # session 6


def test_the_by_day_table_has_a_row_for_every_session_in_order() -> None:
    summary = summarize_run(_scene(), starting_cash_usd=STARTING)

    # (date, status, trades, net profit, fees, wins, losses, break even)
    assert [
        (
            row.trading_date,
            row.status,
            row.trades,
            row.net_profit_usd,
            row.fees_usd,
            row.wins,
            row.losses,
            row.break_even,
        )
        for row in summary.by_day
    ] == [
        (SEP_29, SessionStatus.TRADED, 2, D("-54.90"), D("3.90"), 1, 1, 0),
        (SEP_30, SessionStatus.NO_TRADE, 0, D("0"), D("0"), 0, 0, 0),
        (OCT_1, SessionStatus.SKIPPED, 0, D("0"), D("0"), 0, 0, 0),
        (OCT_2, SessionStatus.TRADED, 1, D("0"), D("0"), 0, 0, 1),
        (OCT_5, SessionStatus.INCOMPLETE, 1, D("49.35"), D("0.65"), 1, 0, 0),
        (OCT_6, SessionStatus.TRADED, 2, D("-29.20"), D("5.20"), 1, 1, 0),
        (OCT_7, SessionStatus.TRADED, 1, D("100.00"), D("0"), 1, 0, 0),
    ]
    assert summary.by_day[2].skip_reason is SkipReason.UNDERLYING_DID_NOT_TRADE
    assert summary.by_day[0].skip_reason is None
    assert [row.session_open_ms for row in summary.by_day] == [
        open_ms(day) for day in (SEP_29, SEP_30, OCT_1, OCT_2, OCT_5, OCT_6, OCT_7)
    ]


def test_the_by_month_table_adds_up_the_sessions_of_each_month() -> None:
    summary = summarize_run(_scene(), starting_cash_usd=STARTING)

    september, october = summary.by_month
    # September: sessions 1 and 2. Two sessions, one traded; two trades (B won, A lost); -54.90; fees 3.90.
    assert (september.year, september.month, september.sessions, september.sessions_traded) == (2026, 9, 2, 1)
    assert (september.trades, september.net_profit_usd, september.fees_usd) == (2, D("-54.90"), D("3.90"))
    assert (september.wins, september.losses, september.break_even) == (1, 1, 0)
    assert september.sessions_by_status == (
        (SessionStatus.TRADED, 1),
        (SessionStatus.NO_TRADE, 1),
        (SessionStatus.SKIPPED, 0),
        (SessionStatus.INCOMPLETE, 0),
    )
    # October: sessions 3 to 7. Five sessions, four traded; five trades (F, C, G won, D lost, E broke even);
    # 0 + 49.35 - 29.20 + 100.00 = 120.15; fees 0.65 + 5.20 = 5.85.
    assert (october.year, october.month, october.sessions, october.sessions_traded) == (2026, 10, 5, 4)
    assert (october.trades, october.net_profit_usd, october.fees_usd) == (5, D("120.15"), D("5.85"))
    assert (october.wins, october.losses, october.break_even) == (3, 1, 1)
    assert october.sessions_by_status == (
        (SessionStatus.TRADED, 3),
        (SessionStatus.NO_TRADE, 0),
        (SessionStatus.SKIPPED, 1),
        (SessionStatus.INCOMPLETE, 1),
    )
    # The months add up to the run.
    assert september.net_profit_usd + october.net_profit_usd == summary.net_profit_usd == D("65.25")


def test_a_month_is_the_new_york_month_of_the_trading_date_and_the_table_follows_the_calendar_across_a_year() -> None:
    last_of_2026, first_of_2027 = date(2026, 12, 31), date(2027, 1, 4)
    sessions = [
        a_session(last_of_2026, "10000.00", status=SessionStatus.NO_TRADE),
        a_session(first_of_2027, "10000.00", status=SessionStatus.NO_TRADE),
    ]

    summary = summarize_run(sessions, starting_cash_usd=STARTING)

    assert [(row.year, row.month, row.sessions) for row in summary.by_month] == [(2026, 12, 1), (2027, 1, 1)]


def test_a_trade_that_gained_on_the_price_and_lost_on_its_fees_is_a_lost_trade() -> None:
    # Bought 1.00 and sold 1.01: +1.00 on the price; fees 0.65 + 0.65 = 1.30 make it -0.30.
    sessions = [
        a_session(
            OCT_7,
            "10000.00",
            status=SessionStatus.TRADED,
            trades=(
                a_trade(
                    "a",
                    entry=("1.00", "1.00"),
                    exit=("1.01", "1.01"),
                    reason=ExitReason.EXIT_TIME,
                    fees=("0.65", "0.65"),
                ),
            ),
        )
    ]

    summary = summarize_run(sessions, starting_cash_usd=STARTING)

    assert (summary.trades_won, summary.trades_lost, summary.trades_break_even) == (0, 1, 0)
    assert (summary.gross_profit_usd, summary.gross_loss_usd, summary.net_profit_usd) == (D("0"), D("0.30"), D("-0.30"))
    # No winning trade over a loss: a factor of exactly nothing, not an absent one.
    assert summary.profit_factor == Ratio(D("0"), D("0.30"))
    assert summary.profit_factor.exact() == D("0")


def test_with_no_losing_trade_there_is_no_profit_factor() -> None:
    sessions = [
        a_session(
            OCT_7,
            "10000.00",
            status=SessionStatus.TRADED,
            trades=(a_trade("a", entry=("2.00", "2.00"), exit=("3.00", "3.00"), reason=ExitReason.EXIT_TIME),),
        )
    ]

    summary = summarize_run(sessions, starting_cash_usd=STARTING)

    assert summary.profit_factor is None
    assert summary.gross_profit_usd == D("100.00") and summary.gross_loss_usd == D("0")
    assert summary.trade_win_rate == Ratio(D("1"), D("1")) and summary.trade_win_rate.exact() == D("1")
    assert summary.drawdown_by_fraction is None and summary.drawdown_by_dollars is None  # equity only rose


def test_a_run_in_which_nothing_traded_has_no_rates_and_no_drawdown_and_its_cash_unchanged() -> None:
    sessions = [
        a_session(SEP_30, "10000.00", status=SessionStatus.NO_TRADE),
        a_session(OCT_1, "10000.00", status=SessionStatus.SKIPPED, skip_reason=SkipReason.WINDOW_DOES_NOT_FIT),
    ]

    summary = summarize_run(sessions, starting_cash_usd=STARTING)

    assert (summary.sessions_tested, summary.sessions_traded, summary.trades) == (2, 0, 0)
    assert summary.daily_win_rate is None and summary.trade_win_rate is None and summary.profit_factor is None
    assert summary.drawdown_by_fraction is None and summary.drawdown_by_dollars is None
    assert (summary.net_profit_usd, summary.ending_cash_usd) == (D("0"), STARTING)
    assert summary.skipped_by_reason == ((SkipReason.WINDOW_DOES_NOT_FIT, 1), (SkipReason.UNDERLYING_DID_NOT_TRADE, 0))


def test_a_summary_of_no_sessions_is_empty_and_still_says_what_cash_it_started_with() -> None:
    summary = summarize_run([], starting_cash_usd=STARTING)

    assert summary.sessions_tested == 0 and summary.by_day == () and summary.by_month == ()
    assert summary.ending_cash_usd == STARTING


@pytest.mark.parametrize(
    ("numerator", "denominator", "exact", "rounded"),
    [
        ("3", "4", "0.75", "0.750000"),
        ("1", "8", "0.125", "0.125000"),
        ("6", "3", "2", "2.000000"),
        ("1", "3", None, "0.333333"),
        ("2", "3", None, "0.666667"),
        # A tie at the seventh place goes to the even sixth digit: 0.0000005 to 0.000000, 0.0000015 to 0.000002,
        # 0.0000025 to 0.000002, 0.0000035 to 0.000004.
        ("0.0000005", "1", "0.0000005", "0.000000"),
        ("0.0000015", "1", "0.0000015", "0.000002"),
        ("0.0000025", "1", "0.0000025", "0.000002"),
        ("0.0000035", "1", "0.0000035", "0.000004"),
    ],
)
def test_a_ratio_is_exact_where_it_ends_and_rounds_half_to_even_where_it_is_shown(
    numerator: str, denominator: str, exact: str | None, rounded: str
) -> None:
    ratio = Ratio(D(numerator), D(denominator))

    assert ratio.exact() == (None if exact is None else D(exact))
    assert ratio.rounded_to_6_places() == D(rounded)
    assert ratio.rounded_to_6_places().as_tuple().exponent == -6


def test_a_ratio_divides_by_something_above_zero() -> None:
    with pytest.raises(ValueError, match="above zero"):
        Ratio(D("1"), D("0"))
    with pytest.raises(ValueError, match="above zero"):
        Ratio(D("1"), D("-2"))


def test_a_ratio_does_not_depend_on_the_decimal_context_it_is_asked_in() -> None:
    with localcontext(Context(prec=3)):
        assert Ratio(D("245.75"), D("180.50")).rounded_to_6_places() == D("1.361496")
        assert Ratio(D("1"), D("8")).exact() == D("0.125")


def test_the_summary_is_exact_dollars_and_never_a_float() -> None:
    summary = summarize_run(_scene(), starting_cash_usd=STARTING)

    for name in (
        "net_profit_usd",
        "gross_profit_usd",
        "gross_loss_usd",
        "total_fees_usd",
        "slippage_paid_usd",
        "ending_cash_usd",
    ):
        assert isinstance(getattr(summary, name), Decimal), name
