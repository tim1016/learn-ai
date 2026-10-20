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

from dataclasses import replace
from datetime import date, datetime
from decimal import Context, Decimal, localcontext
from zoneinfo import ZoneInfo

import pytest

from app.lean_sidecar.trading_calendar import expected_sessions
from app.options.contract import OptionContract
from app.options_backtest.market import PickRefusal, PickRefused
from app.options_backtest.results import (
    EntryRefusal,
    Event,
    EventKind,
    ExitReason,
    SessionOutcome,
    SessionStatus,
    SkipReason,
)
from app.options_backtest.session_result import SessionResult
from app.options_backtest.statistics import Drawdown, Ratio, summarize_run
from tests.options_backtest._market import TableMarket, contract, flat, leg, picked, replay, setup, steady
from tests.options_backtest._results import a_session, a_session_of, a_trade, an_event
from tests.options_backtest._run_data import FRI, OPEN_MS, D

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


def test_the_scene_summary_counts_the_sessions_the_trades_their_exits_and_what_did_not_trade() -> None:
    summary = summarize_run(_scene(), starting_cash_usd=STARTING)

    # Seven tested = five traded (1, 4, 5, 6, 7) + one no trade (2) + one skipped (3).
    assert summary.sessions_tested == 7, "seven sessions are tested"
    assert summary.sessions_traded == 5, "five sessions traded"
    assert summary.sessions_no_trade == 1, "one session had no trade"
    assert summary.sessions_skipped == 1, "one session was skipped"
    assert summary.skipped_by_reason == (
        (SkipReason.WINDOW_DOES_NOT_FIT, 0),
        (SkipReason.UNDERLYING_DID_NOT_TRADE, 1),
    ), "the skipped sessions are counted by reason, with every reason listed"
    assert summary.sessions_incomplete == 1, "session 5, a leg marked and not sold, is the one incomplete session"
    # Of the five traded: 5 (+49.35) and 7 (+100.00) profit, 1 (-54.90) and 6 (-29.20) lose, 4 (0.00) breaks even.
    assert (summary.sessions_profitable, summary.sessions_losing, summary.sessions_break_even) == (2, 2, 1), (
        "of the five traded sessions two profit, two lose and one breaks even"
    )

    assert summary.daily_win_rate == Ratio(D("2"), D("5")), (
        "the daily win rate is profitable sessions over traded sessions"
    )
    assert summary.daily_win_rate.exact() == D("0.4"), "the daily win rate is exactly 0.4"
    assert summary.daily_win_rate.rounded_to_6_places() == D("0.400000"), "the daily win rate is shown as 0.400000"

    # B (55.70), F (49.35), C (40.70), G (100.00) won; A (-110.60) and D (-69.90) lost; E (0.00) broke even.
    assert summary.trades == 7, "seven trades"
    assert (summary.trades_won, summary.trades_lost, summary.trades_break_even) == (4, 2, 1), (
        "four trades won, two lost and one broke even, by their profit after fees"
    )
    # Four sevenths is a decimal that never ends: the two numbers, and a rounded one that says so.
    assert summary.trade_win_rate == Ratio(D("4"), D("7")), "the trade win rate is won trades over trades"
    assert summary.trade_win_rate.exact() is None, "four sevenths never ends, so it has no exact decimal"
    assert summary.trade_win_rate.rounded_to_6_places() == D("0.571429"), (  # 0.571428571...
        "four sevenths is shown rounded to 0.571429"
    )

    # Stop: A. Square off: D. Exit time: B, E, C, G. Marked, not sold: F.
    assert summary.exits_by_reason == (
        (ExitReason.STOP, 1),
        (ExitReason.SQUARE_OFF, 1),
        (ExitReason.CLOSED_WITH_ITS_PROTECTION, 0),
        (ExitReason.EXIT_TIME, 4),
        (ExitReason.MARKED_NOT_SOLD, 1),
        (ExitReason.MARKED_NOT_BOUGHT_BACK, 0),
    ), "the trades are counted by the reason they left, with every reason listed"

    # Session 2 took a starting price (leg b) and no momentum was reached; session 1 took one and reached it.
    assert summary.sessions_without_momentum == 1, "session 2 is the one without momentum"
    assert summary.entries_refused_for_cash == 1, "one entry refused for cash, in session 5"
    assert summary.re_entries_blocked_by_cutoff == 1, "one re-entry blocked by the cutoff, in session 4"
    assert summary.entries_dropped == 1, "one entry dropped, in session 4"
    assert summary.picks_refused_by_reason == (
        (PickRefusal.CHAIN_INCOMPLETE, 0),
        (PickRefusal.EXPIRY_NOT_LISTED, 1),
        (PickRefusal.EXPIRY_NOT_A_SESSION, 0),
        (PickRefusal.STRIKE_NOT_LISTED, 1),
        (PickRefusal.NOT_STANDARD, 0),
    ), "the refused picks are counted by reason, with every reason listed"
    assert summary.legs_without_a_starting_price == 1, "one leg without a starting price, in session 2"
    assert summary.re_entries_lost == 1, "one re-entry lost, in session 6"

    # Recorded and zero is a different thing from "not recorded", which is a run stored before they were counted.
    assert summary.entries_refused_by_reason == (
        (EntryRefusal.OTHER_SIDE, 0),
        (EntryRefusal.SECOND_EXPIRY, 0),
        (EntryRefusal.COVER_DID_NOT_TRADE, 0),
        (EntryRefusal.COVER_NOT_YET_HELD, 0),
        (EntryRefusal.SALE_AT_ZERO, 0),
    ), "a run that only buys counts each refusal of a sale as none and not as not recorded"


def test_the_scene_summary_money_adds_up_with_the_profit_factor_the_costs_and_the_drawdown() -> None:
    summary = summarize_run(_scene(), starting_cash_usd=STARTING)

    # Gross profit 55.70 + 49.35 + 40.70 + 100.00 = 245.75. Gross loss 110.60 + 69.90 = 180.50.
    # Net profit 245.75 - 180.50 = 65.25, which is also -54.90 + 0 + 0 + 0 + 49.35 - 29.20 + 100.00.
    assert summary.gross_profit_usd == D("245.75"), "gross profit is the winning trades' profit after fees"
    assert summary.gross_loss_usd == D("180.50"), "gross loss is the losing trades' loss after fees"
    assert summary.net_profit_usd == D("65.25"), "net profit is gross profit less gross loss"
    assert summary.starting_cash_usd == STARTING, "the starting cash is the one the run began with"
    assert summary.ending_cash_usd == D("10065.25"), "the ending cash is the starting cash plus net profit"

    # 245.75 / 180.50 = 983 / 722 = 1.36149584..., which never ends (722 has a factor of 19). The seventh place is
    # an 8, so six places round up to 1.361496.
    assert summary.profit_factor == Ratio(D("245.75"), D("180.50")), "the profit factor is gross profit over gross loss"
    assert summary.profit_factor.exact() is None, "the profit factor never ends, so it has no exact decimal"
    assert summary.profit_factor.rounded_to_6_places() == D("1.361496"), "the profit factor rounds up to 1.361496"

    # Fees: A 2.60 + B 1.30 + F 0.65 + C 1.30 + D 3.90 = 9.75.
    assert summary.total_fees_usd == D("9.75"), "the fees are those of every fill"
    # Slippage paid, |filled - seen| x 100 x contracts at each fill:
    #   A bought 0.02 x 200 = 4.00, sold 0.02 x 200 = 4.00;  B bought 0.01 x 100 = 1.00, sold 0.02 x 100 = 2.00;
    #   C bought 0.04 x 100 = 4.00, sold 0.04 x 100 = 4.00;  D bought 0.01 x 300 = 3.00, sold 0.01 x 300 = 3.00.
    #   E, G filled at the price seen; F's mark is not a fill. 4 + 4 + 1 + 2 + 4 + 4 + 3 + 3 = 25.00.
    assert summary.slippage_paid_usd == D("25.00"), "the slippage and the price step took 25.00 from every fill"

    # Equity: 10,000.00 at the start, then 9,945.10 after session 1 (and unchanged through 4), 9,994.45, 9,965.25,
    # 10,065.25. The deepest fall is the first: 10,000.00 - 9,945.10 = 54.90, which is 54.90 / 10,000.00 of the peak.
    # The peak is the starting cash, before any session, so it names no session. The trough is session 1, Tue
    # 2026-09-29, whose 09:30 New York open is 1,790,688,600,000 ms UTC.
    expected = Drawdown(
        peak_equity_usd=D("10000.00"),
        trough_equity_usd=D("9945.10"),
        fall_usd=D("54.90"),
        fall_ratio=Ratio(D("54.90"), D("10000.00")),
        peak_session_open_ms=None,
        trough_session_open_ms=OPEN_MS[SEP_29],
    )
    assert OPEN_MS[SEP_29] == 1_790_688_600_000, "session 1's open is 1,790,688,600,000 ms UTC"
    assert summary.drawdown_by_fraction == expected, (
        "the drawdown by fraction is the fall from the starting cash to session 1"
    )
    assert summary.drawdown_by_dollars == expected, "the drawdown by dollars is the same fall"
    assert summary.drawdown_by_fraction.measured_at == "session_end", "the drawdown is measured at session ends"
    assert summary.drawdown_by_fraction.fall_ratio.exact() == D("0.00549"), "the drawdown's fall is 0.00549 of the peak"

    for name in (
        "net_profit_usd",
        "gross_profit_usd",
        "gross_loss_usd",
        "total_fees_usd",
        "slippage_paid_usd",
        "ending_cash_usd",
    ):
        assert isinstance(getattr(summary, name), Decimal), f"{name} is exact dollars, never a float"


OCTOBER = expected_sessions(date(2026, 10, 1), date(2026, 10, 30))


def _many(kind: EventKind, count: int, *, entry_number: int = 0, reason: str | None = None) -> tuple[Event, ...]:
    """``count`` events of one kind, on legs of their own. ``entry_number`` 1 is a re-entry."""
    return tuple(an_event(kind, f"leg{at}", reason, entry_number) for at in range(count))


def _counting_scene() -> list[SessionResult]:
    """Eleven sessions in which every count the owner reads of what did not trade has a value of its own.

    No trade makes or loses anything here (a trade is bought and sold at 1.00, or bought at 1.00 and marked at 1.00),
    so the scene is only about counts. What each session holds, with the counts it adds to:

        1  traded      starting price taken, momentum hit; 2 re-entries blocked by the cutoff
        2  traded      starting price taken, momentum never hit; 1 entry refused for cash; 2 entries dropped;
                       1 re-entry blocked by the cutoff
        3  traded      starting price taken, momentum never hit; 2 re-entries blocked by the cutoff; 3 re-entries
                       dropped, each lost (3 dropped, 3 lost)
        4  traded      1 entry refused for cash; 2 legs without a starting price; 1 pick refused, chain incomplete
        5  incomplete  1 entry refused for cash; 3 entries dropped; 2 re-entries whose pick was refused, strike not
                       listed, each lost (2 refused picks, 2 lost)
        6  incomplete  starting price taken, momentum never hit; 1 entry refused for cash; 2 legs without a starting
                       price; 2 re-entries without a starting price, each lost (2 more without one, 2 lost)
        7  no trade    3 picks refused: 2 expiry not listed, 1 strike not listed
        8  traded      nothing else
        9  skipped     the window does not fit
       10  skipped     the underlying did not trade
       11  skipped     the underlying did not trade

    So: sessions 11; traded 7 (1 to 6 and 8); incomplete 2 (5, 6); no trade 1 (7); skipped 3 (9, 10, 11); without
    momentum 3 (2, 3, 6); refused for cash 1 + 1 + 1 + 1 = 4 (2, 4, 5, 6); blocked by the cutoff 2 + 1 + 2 = 5
    (1, 2, 3); without a starting price 2 + 2 + 2 = 6 (4, 6, 6); lost 3 + 2 + 2 = 7 (3, 5, 6); dropped
    2 + 3 + 3 = 8 (2, 3, 5); picks refused: chain incomplete 1, expiry not listed 2, strike not listed 2 + 1 = 3.
    """
    held = a_trade("a", entry=("1.00", "1.00"), exit=("1.00", "1.00"), reason=ExitReason.EXIT_TIME)
    marked = a_trade("a", entry=("1.00", "1.00"), mark="1.00", reason=ExitReason.MARKED_NOT_SOLD)
    waited, reached = _many(EventKind.STARTING_PRICE_TAKEN, 1), _many(EventKind.MOMENTUM_HIT, 1)
    cutoff, cash = EventKind.RE_ENTRY_BLOCKED_BY_CUTOFF, EventKind.ENTRY_REFUSED_FOR_CASH
    dropped, lost = EventKind.ENTRY_DROPPED, EventKind.RE_ENTRY_LOST
    no_start, refused = EventKind.NO_STARTING_PRICE, EventKind.PICK_REFUSED
    short = EntryRefusal.CASH_SHORT.value
    rows: list[tuple[SessionStatus, tuple, tuple[Event, ...]]] = [
        (SessionStatus.TRADED, (held,), waited + reached + _many(cutoff, 2)),
        (
            SessionStatus.TRADED,
            (held,),
            waited + _many(cash, 1, reason=short) + _many(dropped, 2) + _many(cutoff, 1),
        ),
        (
            SessionStatus.TRADED,
            (held,),
            waited + _many(cutoff, 2) + _many(dropped, 3, entry_number=1) + _many(lost, 3, entry_number=1),
        ),
        (
            SessionStatus.TRADED,
            (held,),
            _many(cash, 1, reason=short)
            + _many(no_start, 2)
            + _many(refused, 1, reason=PickRefusal.CHAIN_INCOMPLETE.value),
        ),
        (
            SessionStatus.INCOMPLETE,
            (marked,),
            _many(cash, 1, reason=short)
            + _many(dropped, 3)
            + _many(refused, 2, reason=PickRefusal.STRIKE_NOT_LISTED.value, entry_number=1)
            + _many(lost, 2, entry_number=1),
        ),
        (
            SessionStatus.INCOMPLETE,
            (marked,),
            waited
            + _many(cash, 1, reason=short)
            + _many(no_start, 2)
            + _many(no_start, 2, entry_number=1)
            + _many(lost, 2, entry_number=1),
        ),
        (
            SessionStatus.NO_TRADE,
            (),
            _many(refused, 2, reason=PickRefusal.EXPIRY_NOT_LISTED.value)
            + _many(refused, 1, reason=PickRefusal.STRIKE_NOT_LISTED.value),
        ),
        (SessionStatus.TRADED, (held,), ()),
    ]
    sessions = [
        a_session(day, "10000.00", status=status, trades=trades, events=events)
        for day, (status, trades, events) in zip(OCTOBER, rows, strict=False)
    ]
    for day, why in zip(
        OCTOBER[len(rows) :],
        (SkipReason.WINDOW_DOES_NOT_FIT, SkipReason.UNDERLYING_DID_NOT_TRADE, SkipReason.UNDERLYING_DID_NOT_TRADE),
        strict=False,
    ):
        sessions.append(a_session(day, "10000.00", status=SessionStatus.SKIPPED, skip_reason=why))
    return sessions


def test_each_status_and_each_count_of_what_did_not_trade_is_its_own_figure_when_no_two_counts_agree() -> None:
    summary = summarize_run(_counting_scene(), starting_cash_usd=STARTING)

    assert summary.sessions_tested == 11, "eleven sessions are tested"
    assert summary.sessions_traded == 7, "seven traded: sessions 1 to 6 and 8, the two incomplete ones included"
    assert summary.sessions_incomplete == 2, "two incomplete: 5 and 6"
    assert summary.sessions_no_trade == 1, "one with no trade: 7"
    assert summary.sessions_skipped == 3, "three skipped: 9, 10 and 11"
    assert summary.skipped_by_reason == (
        (SkipReason.WINDOW_DOES_NOT_FIT, 1),
        (SkipReason.UNDERLYING_DID_NOT_TRADE, 2),
    ), "the skipped sessions are counted by reason"

    assert summary.sessions_without_momentum == 3, (
        "3 sessions without momentum: 2, 3 and 6; session 1 reached its momentum"
    )
    assert summary.entries_refused_for_cash == 4, "4 entries refused for cash: one each in 2, 4, 5 and 6"
    assert summary.re_entries_blocked_by_cutoff == 5, (
        "5 re-entries blocked by the cutoff: 2 in session 1, 1 in 2, 2 in 3"
    )
    assert summary.legs_without_a_starting_price == 6, "6 legs without a starting price: 2 in session 4, 2 + 2 in 6"
    assert summary.re_entries_lost == 7, "7 re-entries lost: 3 in session 3, 2 in 5, 2 in 6"
    assert summary.entries_dropped == 8, "8 entries dropped: 2 in session 2, 3 in 3, 3 in 5"
    assert summary.picks_refused_by_reason == (
        (PickRefusal.CHAIN_INCOMPLETE, 1),  # session 4
        (PickRefusal.EXPIRY_NOT_LISTED, 2),  # session 7
        (PickRefusal.EXPIRY_NOT_A_SESSION, 0),
        (PickRefusal.STRIKE_NOT_LISTED, 3),  # 2 in session 5, 1 in 7
        (PickRefusal.NOT_STANDARD, 0),
    ), "the refused picks are counted by reason"


def test_each_refusal_of_a_sale_or_its_cover_is_its_own_count_and_apart_from_the_count_for_cash() -> None:
    """Hand-written sessions: 1 cover that did not trade, 2 not yet held, 3 second expiry, 4 other side, 5 sale at zero.

    The cash count, which every run has always had, is 6 and is not among them. Zero counts are listed too.
    """
    cover, second = EventKind.ENTRY_REFUSED_FOR_COVER, EventKind.ENTRY_REFUSED_FOR_SECOND_EXPIRY
    other, zero = EventKind.ENTRY_REFUSED_FOR_OTHER_SIDE, EventKind.SALE_NOT_TAKEN_AT_ZERO
    sessions = [
        a_session(
            SEP_29,
            "10000.00",
            status=SessionStatus.NO_TRADE,
            events=_many(cover, 1, reason=EntryRefusal.COVER_DID_NOT_TRADE.value)
            + _many(cover, 2, reason=EntryRefusal.COVER_NOT_YET_HELD.value)
            + _many(second, 1, reason=EntryRefusal.SECOND_EXPIRY.value),
        ),
        a_session(
            SEP_30,
            "10000.00",
            status=SessionStatus.NO_TRADE,
            events=_many(second, 2, reason=EntryRefusal.SECOND_EXPIRY.value, entry_number=1)
            + _many(other, 4, reason=EntryRefusal.OTHER_SIDE.value)
            + _many(zero, 5, reason=EntryRefusal.SALE_AT_ZERO.value)
            + _many(EventKind.ENTRY_REFUSED_FOR_CASH, 6, reason=EntryRefusal.CASH_SHORT.value),
        ),
    ]

    summary = summarize_run(sessions, starting_cash_usd=STARTING)

    assert summary.entries_refused_by_reason == (
        (EntryRefusal.OTHER_SIDE, 4),
        (EntryRefusal.SECOND_EXPIRY, 3),
        (EntryRefusal.COVER_DID_NOT_TRADE, 1),
        (EntryRefusal.COVER_NOT_YET_HELD, 2),
        (EntryRefusal.SALE_AT_ZERO, 5),
    )
    assert summary.entries_refused_for_cash == 6


@pytest.mark.parametrize(
    "kind",
    [EventKind.ENTRY_REFUSED_FOR_COVER, EventKind.PICK_REFUSED],
    ids=[
        "a refusal event with no reason is not counted by a guess",
        "a pick refused event with no reason is not left out of the counts",
    ],
)
def test_a_refusal_event_with_no_reason_is_refused(kind: EventKind) -> None:
    session = a_session(
        SEP_29,
        "10000.00",
        status=SessionStatus.NO_TRADE,
        events=(an_event(kind, "short"),),
    )

    with pytest.raises(ValueError, match="carries no reason"):
        summarize_run([session], starting_cash_usd=STARTING)


PUT_495, PUT_500 = contract("put", "495"), contract("put", "500")
PUT_500_LATER = OptionContract.of(root="SPY", expiry_date=date(2026, 10, 16), right="put", strike=D("500"))


def _a_covered_spread() -> TableMarket:
    return TableMarket(
        underlying=steady("500"),
        picks=picked(("long", "500", PUT_495), ("short", "500", PUT_500)),
        options={PUT_495: flat("1.00", 5, 61), PUT_500: flat("2.20", 5, 61)},
    )


def test_a_sold_leg_closed_with_its_protection_is_counted_by_that_name_and_not_as_a_stop() -> None:
    # The cover's stop (25 percent under 1.00 is 0.75) is hit at minute 10, which closes the sold leg with it.
    cover = {**flat("1.00", 5, 9), 10: ("1.00", "0.70"), **flat("0.70", 11, 61)}
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("long", "500", PUT_495), ("short", "500", PUT_500)),
        options={PUT_495: cover, PUT_500: flat("2.20", 5, 61)},
    )
    outcome = replay(setup(leg("long", "put", stop=("percent", "25")), leg("short", "put", side="sell")), market)

    summary = summarize_run([a_session_of(SEP_29, outcome, "10000.00")], starting_cash_usd=STARTING)

    # One stop, the cover's, and one sold leg that went with it: each under its own name.
    assert dict(summary.exits_by_reason) == {
        ExitReason.STOP: 1,
        ExitReason.SQUARE_OFF: 0,
        ExitReason.CLOSED_WITH_ITS_PROTECTION: 1,
        ExitReason.EXIT_TIME: 0,
        ExitReason.MARKED_NOT_SOLD: 0,
        ExitReason.MARKED_NOT_BOUGHT_BACK: 0,
    }


def test_a_run_whose_sessions_each_refuse_a_sale_for_another_reason_counts_each_by_its_reason() -> None:
    sold = leg("short", "put", side="sell")
    cover_did_not_trade = replay(
        setup(leg("long", "put"), sold),
        TableMarket(
            underlying=steady("500"),
            picks=picked(("long", "500", PUT_495), ("short", "500", PUT_500)),
            # The cover traded at minute 5 and not at minute 6, where the orders fill.
            options={PUT_495: {5: ("1.00", "1.00"), **flat("1.00", 7, 61)}, PUT_500: flat("2.20", 5, 61)},
        ),
    )
    cover_not_yet_held = replay(
        setup(leg("long", "put", momentum=("percent_up", "25")), sold),
        TableMarket(
            underlying=steady("500"),
            picks=picked(("long", "500", PUT_495), ("short", "500", PUT_500)),
            # The cover waits for 1.25 and the sale does not wait: it finds no cover held at minute 6.
            options={
                PUT_495: {**flat("1.00", 5, 7), 8: ("1.00", "1.30"), **flat("1.30", 9, 61)},
                PUT_500: flat("2.20", 5, 61),
            },
        ),
    )
    second_expiry = replay(
        setup(leg("long", "put"), sold),
        TableMarket(
            underlying=steady("500"),
            picks=picked(("long", "500", PUT_495), ("short", "500", PUT_500_LATER)),
            options={PUT_495: flat("1.00", 5, 61), PUT_500_LATER: flat("2.20", 5, 61)},
        ),
    )
    other_side = replay(
        setup(leg("a", "put"), leg("b", "put", side="sell")),
        TableMarket(
            underlying=steady("500"),
            picks=picked(("a", "500", PUT_500), ("b", "500", PUT_500)),
            options={PUT_500: flat("2.00", 5, 61)},
        ),
    )
    sale_at_zero = replay(
        setup(leg("long", "put"), sold, slippage="0.3"),
        TableMarket(
            underlying=steady("500"),
            picks=picked(("long", "500", PUT_495), ("short", "500", PUT_500)),
            # 0.01 x (1 - 0.003) = 0.00997, rounded down to the cent: nothing.
            options={PUT_495: flat("1.00", 5, 61), PUT_500: flat("0.01", 5, 61)},
        ),
    )
    covered = replay(setup(leg("long", "put"), sold), _a_covered_spread())
    days = (SEP_29, SEP_30, OCT_1, OCT_2, OCT_5, OCT_6)
    outcomes = (cover_did_not_trade, cover_not_yet_held, second_expiry, other_side, sale_at_zero, covered)
    sessions = [a_session_of(day, outcome, "10000.00") for day, outcome in zip(days, outcomes, strict=True)]

    summary = summarize_run(sessions, starting_cash_usd=STARTING)

    # One session made each refusal once; the sixth traded the spread and refused nothing.
    assert summary.entries_refused_by_reason == (
        (EntryRefusal.OTHER_SIDE, 1),
        (EntryRefusal.SECOND_EXPIRY, 1),
        (EntryRefusal.COVER_DID_NOT_TRADE, 1),
        (EntryRefusal.COVER_NOT_YET_HELD, 1),
        (EntryRefusal.SALE_AT_ZERO, 1),
    )
    assert summary.entries_refused_for_cash == 0


CALL, CALL_498 = contract("call", "500"), contract("call", "498")
STOP_25 = ("percent", "25")


def _a_re_entry_lost_to_a_refused_pick() -> SessionOutcome:
    # Stopped at minute 10 (2.00 falls to 1.40, under the stop at 1.50), sold at minute 11, where the re-entry's pick,
    # with the underlying at 498, is refused.
    refusal = PickRefused(PickRefusal.CHAIN_INCOMPLETE, "the chain at minute 11 lists too few strikes")
    market = TableMarket(
        underlying={**steady("500", 1, 10), **steady("498", 11)},
        picks={**picked(("call", "500", CALL)), ("call", "498"): refusal},
        options={CALL: {**flat("2.00", 5, 9), 10: ("2.00", "1.40"), 11: ("1.40", "1.40"), **flat("1.50", 12, 61)}},
    )
    return replay(setup(leg(stop=STOP_25, reentry=("at_once", 1))), market)


def _a_re_entry_lost_for_want_of_a_starting_price() -> SessionOutcome:
    # Bought on momentum, stopped at minute 9, sold at minute 10, where the re-entry's fresh contract (the 498) has no
    # bar to take a starting price from.
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
            CALL_498: flat("1.30", 11, 61),
        },
    )
    return replay(setup(leg(momentum=("percent_up", "25"), stop=STOP_25, reentry=("on_momentum", 1))), market)


def _a_re_entry_lost_to_an_entry_dropped_at_once() -> SessionOutcome:
    # Stopped at minute 10, sold at minute 11, and the re-entry bought at once finds no bar in minute 12.
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(("call", "500", CALL)),
        options={CALL: {**flat("2.00", 5, 9), 10: ("2.00", "1.40"), 11: ("1.40", "1.40"), **flat("1.50", 13, 61)}},
    )
    return replay(setup(leg(stop=STOP_25, reentry=("at_once", 2))), market)


@pytest.mark.parametrize(
    ("replayed", "picks_refused", "without_a_starting_price", "dropped"),
    [
        (_a_re_entry_lost_to_a_refused_pick, 1, 0, 0),
        (_a_re_entry_lost_for_want_of_a_starting_price, 0, 1, 0),
        (_a_re_entry_lost_to_an_entry_dropped_at_once, 0, 0, 1),
    ],
    ids=["a refused pick", "no starting price", "an entry dropped at once"],
)
def test_a_re_entry_lost_is_counted_as_lost_and_again_under_the_cause_that_lost_it(
    replayed, picks_refused: int, without_a_starting_price: int, dropped: int
) -> None:
    summary = summarize_run([a_session_of(FRI, replayed(), "10000.00")], starting_cash_usd=STARTING)

    # One re-entry was lost, and the same incident is the one pick refused, or the one leg without a starting price,
    # or the one entry dropped: the headings overlap, they do not add up.
    assert summary.re_entries_lost == 1
    assert sum(count for _, count in summary.picks_refused_by_reason) == picks_refused
    assert summary.legs_without_a_starting_price == without_a_starting_price
    assert summary.entries_dropped == dropped


def test_the_scene_summary_has_a_by_day_row_for_every_session_in_order_and_a_by_month_row_adding_them_up() -> None:
    summary = summarize_run(_scene(), starting_cash_usd=STARTING)

    # (session open in ms UTC, status, trades, net profit, fees, trades won, trades lost, trades break even). A session
    # is named by the 09:30 New York open of its date, from the table in _run_data.py: Tue 09-29, Wed 09-30, Thu 10-01,
    # Fri 10-02, Mon 10-05, Tue 10-06 and Wed 10-07.
    assert [
        (
            row.session_open_ms,
            row.status,
            row.trades,
            row.net_profit_usd,
            row.fees_usd,
            row.trades_won,
            row.trades_lost,
            row.trades_break_even,
        )
        for row in summary.by_day
    ] == [
        (OPEN_MS[SEP_29], SessionStatus.TRADED, 2, D("-54.90"), D("3.90"), 1, 1, 0),
        (OPEN_MS[SEP_30], SessionStatus.NO_TRADE, 0, D("0"), D("0"), 0, 0, 0),
        (OPEN_MS[OCT_1], SessionStatus.SKIPPED, 0, D("0"), D("0"), 0, 0, 0),
        (OPEN_MS[OCT_2], SessionStatus.TRADED, 1, D("0"), D("0"), 0, 0, 1),
        (OPEN_MS[OCT_5], SessionStatus.INCOMPLETE, 1, D("49.35"), D("0.65"), 1, 0, 0),
        (OPEN_MS[OCT_6], SessionStatus.TRADED, 2, D("-29.20"), D("5.20"), 1, 1, 0),
        (OPEN_MS[OCT_7], SessionStatus.TRADED, 1, D("100.00"), D("0"), 1, 0, 0),
    ], "the by-day table has a row for every session in order"
    assert summary.by_day[2].skip_reason is SkipReason.UNDERLYING_DID_NOT_TRADE, "the skipped day says why"
    assert summary.by_day[0].skip_reason is None, "a traded day has no skip reason"

    september, october = summary.by_month
    # September: sessions 1 and 2. Two sessions, one traded; two trades (B won, A lost); -54.90; fees 3.90.
    assert (september.year, september.month, september.sessions, september.sessions_traded) == (2026, 9, 2, 1), (
        "September holds two sessions, one traded"
    )
    assert (september.trades, september.net_profit_usd, september.fees_usd) == (2, D("-54.90"), D("3.90")), (
        "September holds two trades, -54.90 net, 3.90 in fees"
    )
    assert (september.trades_won, september.trades_lost, september.trades_break_even) == (1, 1, 0), (
        "September's trades: one won, one lost"
    )
    assert september.sessions_by_status == (
        (SessionStatus.TRADED, 1),
        (SessionStatus.NO_TRADE, 1),
        (SessionStatus.SKIPPED, 0),
        (SessionStatus.INCOMPLETE, 0),
    ), "September's sessions are counted by status"
    # October: sessions 3 to 7. Five sessions, four traded; five trades (F, C, G won, D lost, E broke even);
    # 0 + 49.35 - 29.20 + 100.00 = 120.15; fees 0.65 + 5.20 = 5.85.
    assert (october.year, october.month, october.sessions, october.sessions_traded) == (2026, 10, 5, 4), (
        "October holds five sessions, four traded"
    )
    assert (october.trades, october.net_profit_usd, october.fees_usd) == (5, D("120.15"), D("5.85")), (
        "October holds five trades, 120.15 net, 5.85 in fees"
    )
    assert (october.trades_won, october.trades_lost, october.trades_break_even) == (3, 1, 1), (
        "October's trades: three won, one lost, one broke even"
    )
    assert october.sessions_by_status == (
        (SessionStatus.TRADED, 3),
        (SessionStatus.NO_TRADE, 0),
        (SessionStatus.SKIPPED, 1),
        (SessionStatus.INCOMPLETE, 1),
    ), "October's sessions are counted by status"
    # The months add up to the run.
    assert september.net_profit_usd + october.net_profit_usd == summary.net_profit_usd == D("65.25"), (
        "the months add up to the run"
    )


def test_the_rows_count_trades_won_lost_and_even_and_not_the_sessions_that_had_them() -> None:
    # Each trade is bought at 1.00 and sold at the price written, one contract: (sold - 1.00) x 100.
    #   Session X: 1.10 (+10.00) and 1.20 (+20.00) won, 0.95 (-5.00) lost, 1.00 and 1.00 (0.00) broke even:
    #              five trades, won 2, lost 1, even 2, net 10.00 + 20.00 - 5.00 = 25.00.
    #   Session Y: 1.01 (+1.00) won, 0.99 (-1.00) and 0.98 (-2.00) lost: three trades, won 1, lost 2, even 0, net -2.00.
    # The month holds both: eight trades, won 2 + 1 = 3, lost 1 + 2 = 3, even 2 + 0 = 2, net 25.00 - 2.00 = 23.00.
    def trade(sold: str):
        return a_trade("a", entry=("1.00", "1.00"), exit=(sold, sold), reason=ExitReason.EXIT_TIME)

    sessions = [
        a_session(
            SEP_29,
            "10000.00",
            status=SessionStatus.TRADED,
            trades=tuple(trade(sold) for sold in ("1.10", "1.20", "0.95", "1.00", "1.00")),
        ),
        a_session(
            SEP_30,
            "10025.00",
            status=SessionStatus.TRADED,
            trades=tuple(trade(sold) for sold in ("1.01", "0.99", "0.98")),
        ),
    ]

    summary = summarize_run(sessions, starting_cash_usd=STARTING)

    first, second = summary.by_day
    assert (first.trades, first.trades_won, first.trades_lost, first.trades_break_even) == (5, 2, 1, 2)
    assert (second.trades, second.trades_won, second.trades_lost, second.trades_break_even) == (3, 1, 2, 0)
    assert (first.net_profit_usd, second.net_profit_usd) == (D("25.00"), D("-2.00"))
    [month] = summary.by_month
    assert (month.sessions, month.sessions_traded, month.trades) == (2, 2, 8)
    assert (month.trades_won, month.trades_lost, month.trades_break_even) == (3, 3, 2)
    assert month.net_profit_usd == D("23.00")
    assert (summary.trades_won, summary.trades_lost, summary.trades_break_even) == (3, 3, 2)


@pytest.mark.parametrize(
    ("dates", "expected"),
    [
        pytest.param(
            (date(2026, 12, 31), date(2027, 1, 4)),
            ((2026, 12, 1), (2027, 1, 1)),
            id="a month is the new york month of the trading date and the table follows the calendar across a year",
        ),
        pytest.param(
            (date(2026, 12, 30), date(2027, 12, 30)),
            ((2026, 12, 1), (2027, 12, 1)),
            id="the same month of two years is two rows",
        ),
    ],
)
def test_the_by_month_table_has_a_row_for_each_year_and_month_of_the_trading_dates(
    dates: tuple[date, ...], expected: tuple[tuple[int, int, int], ...]
) -> None:
    sessions = [a_session(day, "10000.00", status=SessionStatus.NO_TRADE) for day in dates]

    summary = summarize_run(sessions, starting_cash_usd=STARTING)

    assert [(row.year, row.month, row.sessions) for row in summary.by_month] == list(expected)


def test_a_months_year_and_month_are_those_of_the_new_york_date_of_the_open_and_not_of_the_utc_one() -> None:
    # Four sessions with their opens set by hand where a wrong clock names another month. No real open does this (it is
    # 13:30 or 14:30 UTC, the same day in New York and UTC), so a test on real sessions could not tell the clocks apart.
    # Summer time (EDT, UTC-4) until Sun 2026-11-01, standard time (EST, UTC-5) after. UTC midnight of a date is its day
    # number (days since 1970-01-01) times 86,400,000; New York midnight is that plus 4 h on EDT or 5 h on EST.
    #   A  21:30 EDT Wed 2026-09-30 = 01:30 UTC 10-01: day 20,727 = 1,790,812,800,000, plus 1.5 h (5,400,000)
    #                                                                              = 1,790,818,200,000   September
    #   B  00:30 EDT Thu 2026-10-01 = 04:30 UTC 10-01: New York midnight 1,790,827,200,000, plus 30 min (1,800,000)
    #                                                                              = 1,790,829,000,000   October
    #   C  21:30 EST Thu 2026-12-31 = 02:30 UTC 2027-01-01: day 20,819 = 1,798,761,600,000, plus 2.5 h (9,000,000)
    #                                                                              = 1,798,770,600,000   December
    #   D  23:30 EST Thu 2026-12-31 = 04:30 UTC 2027-01-01: the same day plus 4.5 h (16,200,000)
    #                                                                              = 1,798,777,800,000   December
    # UTC says October, October, January 2027, January 2027. A fixed UTC-5 says September for B; a fixed UTC-4 says
    # January 2027 for D.
    opens = (1_790_818_200_000, 1_790_829_000_000, 1_798_770_600_000, 1_798_777_800_000)
    new_york = ZoneInfo("America/New_York")
    assert opens == tuple(
        int(datetime(*when, tzinfo=new_york).timestamp() * 1000)
        for when in ((2026, 9, 30, 21, 30), (2026, 10, 1, 0, 30), (2026, 12, 31, 21, 30), (2026, 12, 31, 23, 30))
    )
    base = a_session(SEP_30, "10000.00", status=SessionStatus.NO_TRADE)
    sessions = [replace(base, session_open_ms=ms) for ms in opens]

    summary = summarize_run(sessions, starting_cash_usd=STARTING)

    assert [(row.year, row.month, row.sessions) for row in summary.by_month] == [
        (2026, 9, 1),
        (2026, 10, 1),
        (2026, 12, 2),
    ]
    # The by-day rows keep the instants as they were set.
    assert [row.session_open_ms for row in summary.by_day] == list(opens)


def test_a_session_is_without_momentum_when_a_leg_took_a_starting_price_and_no_leg_reached_its_momentum() -> None:
    # Session 1: a starting price taken and momentum reached: not counted. Sessions 2 and 3: a starting price and no
    # momentum: counted. Session 4: no leg waited on momentum at all: not counted. Two sessions are without momentum.
    waited = an_event(EventKind.STARTING_PRICE_TAKEN, "a")
    reached = an_event(EventKind.MOMENTUM_HIT, "a")
    days = (SEP_29, SEP_30, OCT_1, OCT_2)
    sessions = [
        a_session(days[0], "10000.00", status=SessionStatus.NO_TRADE, events=(waited, reached)),
        a_session(days[1], "10000.00", status=SessionStatus.NO_TRADE, events=(waited,)),
        a_session(days[2], "10000.00", status=SessionStatus.NO_TRADE, events=(waited, an_event(EventKind.PICKED, "b"))),
        a_session(days[3], "10000.00", status=SessionStatus.NO_TRADE, events=(an_event(EventKind.PICKED, "a"),)),
    ]

    assert summarize_run(sessions, starting_cash_usd=STARTING).sessions_without_momentum == 2


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
        # Ties a float cannot see: 0.0001255 x 1,000,000 is 125.5 exactly, and goes to the even 126, but as a float it
        # is 125.49999999999999 and would round down to 125. 0.0001265 is 126.5, so the even 126, and as a float it is
        # a little over and would round up to 127.
        ("0.0001255", "1", "0.0001255", "0.000126"),
        ("0.0001265", "1", "0.0001265", "0.000126"),
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


def test_a_ratio_display_percent_rounds_the_exact_fraction_whatever_the_decimal_context_it_is_asked_in() -> None:
    assert Ratio(D(217), D(471)).percent_1_place() == D("46.1"), "217 / 471 shows as 46.1 percent"
    assert Ratio(D("960"), D("10837.90")).percent_1_place() == D("8.9"), "960 / 10837.90 shows as 8.9 percent"
    assert Ratio(D(1), D(16)).percent_1_place() == D("6.2"), "1 / 16 shows as 6.2 percent"
    with localcontext(Context(prec=3)):
        assert Ratio(D("245.75"), D("180.50")).rounded_to_6_places() == D("1.361496"), (
            "a ratio rounds the same in a three-digit decimal context"
        )
        assert Ratio(D("1"), D("8")).exact() == D("0.125"), "a ratio is as exact in a three-digit decimal context"


def _big_scene() -> list[SessionResult]:
    """Five sessions of one trade each, large enough that every sum has more digits than a six-digit context holds.

    Cash starts at 1,234,567.89. A trade's profit is (sold - bought) x 100 x contracts less its two fees, bought and
    sold being the prices it filled at. Slippage paid is |filled - seen| x 100 x contracts for each fill.

        A  1,237 contracts  bought 2.03 (seen 2.00437)  sold 2.58 (seen 2.617)   fees 24.74 + 24.74
           profit 0.55 x 123,700 - 49.48 = 68,035.00 - 49.48 = 67,985.52
           slippage 0.02563 x 123,700 + 0.037 x 123,700 = 3,170.431 + 4,576.9 = 7,747.331
        B  2,513 contracts  bought 3.07 (seen 3.00219)  sold 2.37 (seen 2.411)   fees 50.26 + 50.26
           profit -0.70 x 251,300 - 100.52 = -175,910.00 - 100.52 = -176,010.52
           slippage 0.06781 x 251,300 + 0.041 x 251,300 = 17,040.653 + 10,303.3 = 27,343.953
        C  1,111 contracts  bought 4.00  sold 4.57, both at the price seen        fees 22.22 + 22.22
           profit 0.57 x 111,100 - 44.44 = 63,327.00 - 44.44 = 63,282.56
        D  1,111 contracts  bought 4.57  sold 4.0004, both at the price seen      no fees
           profit -0.5696 x 111,100 = -63,282.56, which takes back all of C
        E  1 contract       bought 1.00  sold 0.9999, both at the price seen      no fees
           profit -0.0001 x 100 = -0.01
    """
    trades = (
        a_trade(
            "a",
            entry=("2.00437", "2.03"),
            exit=("2.617", "2.58"),
            reason=ExitReason.EXIT_TIME,
            contracts=1237,
            fees=("24.74", "24.74"),
        ),
        a_trade(
            "a",
            entry=("3.00219", "3.07"),
            exit=("2.411", "2.37"),
            reason=ExitReason.STOP,
            contracts=2513,
            fees=("50.26", "50.26"),
        ),
        a_trade(
            "a",
            entry=("4.00", "4.00"),
            exit=("4.57", "4.57"),
            reason=ExitReason.EXIT_TIME,
            contracts=1111,
            fees=("22.22", "22.22"),
        ),
        a_trade("a", entry=("4.57", "4.57"), exit=("4.0004", "4.0004"), reason=ExitReason.EXIT_TIME, contracts=1111),
        a_trade("a", entry=("1.00", "1.00"), exit=("0.9999", "0.9999"), reason=ExitReason.EXIT_TIME),
    )
    cash = ("1234567.89", "1302553.41", "1126542.89", "1189825.45", "1126542.89")
    return [
        a_session(day, before, status=SessionStatus.TRADED, trades=(trade,))
        for day, before, trade in zip(OCTOBER, cash, trades, strict=False)
    ]


def test_a_summary_is_the_same_in_a_six_digit_decimal_context_as_in_the_default_one() -> None:
    sessions, starting = _big_scene(), D("1234567.89")
    in_the_default_context = summarize_run(sessions, starting_cash_usd=starting)

    with localcontext(Context(prec=6)):
        in_a_small_one = summarize_run(sessions, starting_cash_usd=starting)

    assert in_a_small_one == in_the_default_context
    # And what the summary says is the hand-worked sum, none of it rounded to six digits:
    #   gross profit 67,985.52 + 63,282.56 = 131,268.08;  gross loss 176,010.52 + 63,282.56 + 0.01 = 239,293.09;
    #   net 131,268.08 - 239,293.09 = -108,025.01;  fees 49.48 + 100.52 + 44.44 = 194.44;
    #   slippage 7,747.331 + 27,343.953 = 35,091.284;  ending cash 1,234,567.89 - 108,025.01 = 1,126,542.88.
    assert (in_a_small_one.gross_profit_usd, in_a_small_one.gross_loss_usd) == (D("131268.08"), D("239293.09"))
    assert (in_a_small_one.net_profit_usd, in_a_small_one.total_fees_usd) == (D("-108025.01"), D("194.44"))
    assert in_a_small_one.slippage_paid_usd == D("35091.284")
    assert in_a_small_one.ending_cash_usd == D("1126542.88")
    # Equity: 1,234,567.89 + 67,985.52 = 1,302,553.41 after A (the peak), - 176,010.52 = 1,126,542.89 after B,
    # + 63,282.56 = 1,189,825.45 after C, - 63,282.56 = 1,126,542.89 after D, - 0.01 = 1,126,542.88 after E.
    # B's fall from the peak is 176,010.52 and E's is 176,010.53: a cent more, so E is the worst, though six digits
    # cannot tell the two apart.
    assert in_a_small_one.drawdown_by_fraction == in_a_small_one.drawdown_by_dollars
    drawdown = in_a_small_one.drawdown_by_fraction
    assert drawdown is not None
    assert (drawdown.peak_equity_usd, drawdown.trough_equity_usd, drawdown.fall_usd) == (
        D("1302553.41"),
        D("1126542.88"),
        D("176010.53"),
    )
    # Session E is OCTOBER[4], Wed 2026-10-07, whose 09:30 New York open is 1,791,379,800,000 ms UTC.
    assert OCTOBER[4] == OCT_7
    assert drawdown.trough_session_open_ms == OPEN_MS[OCT_7] == 1_791_379_800_000
    assert drawdown.fall_ratio == Ratio(D("176010.53"), D("1302553.41"))
