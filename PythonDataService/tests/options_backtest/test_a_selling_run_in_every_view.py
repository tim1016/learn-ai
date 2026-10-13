"""A run that sells a covered spread is read with the side of every leg in every view of it (#3109, PRD #3095).

The run is ``_a_selling_run.py``'s: a put 495 bought and a put 500 sold, one contract each, over two sessions. On
Wednesday the spread opens at minute 5 and closes at minute 60: the 500 is bought back first, then the 495 is sold.
On Thursday the 500 stops trading, so the 495, which may not leave it bare, is marked as well: the 500 is owed.

Each view is asked of the same stored trades: the trades route's rows, one session's fills and marks (what the page
draws and lists), and the export. None of them is allowed to read a sold leg as a bought one.
"""

from __future__ import annotations

import csv
import io

import pytest

from app.options_backtest.run import RunResult
from app.options_backtest.saved_runs import StoredTrade
from app.options_backtest.session_levels import fills_of, levels_of, marks_of
from app.options_backtest.session_series import Series, SeriesReason, SessionSeries
from app.options_backtest.session_view import SessionView
from app.options_backtest.trade_export import CSV_HEADER, trades_csv
from app.schemas import options_backtest_run_detail as wire
from tests.options_backtest._a_selling_run import a_selling_run, what_the_selling_run_reaches
from tests.options_backtest._headers import a_session_record


@pytest.fixture(scope="module")
def selling() -> RunResult:
    return a_selling_run()


def _session(selling: RunResult, at: int) -> wire.OptionsBacktestSessionResponse:
    record = a_session_record(selling, at)
    outcome = record.session.result.outcome
    nothing = SessionSeries(underlying=Series("SPY", None, (), SeriesReason.GONE), contracts=())
    return wire.OptionsBacktestSessionResponse.from_view(
        SessionView(
            record=record,
            series=nothing,
            levels=levels_of(selling.setup, outcome),
            fills=fills_of(outcome.trades),
            marks=marks_of(outcome.trades),
        )
    )


def _stored(selling: RunResult) -> list[StoredTrade]:
    return [
        StoredTrade(session.session_open_ms, number, trade)
        for session in selling.sessions
        for number, trade in enumerate(session.outcome.trades)
    ]


def test_the_run_reaches_every_way_a_spread_can_end_so_that_each_view_is_asked_of_all_of_them(
    selling: RunResult,
) -> None:
    assert all(what_the_selling_run_reaches(selling).values()), what_the_selling_run_reaches(selling)


def test_the_trades_route_says_which_way_each_leg_opened_and_how_it_left(selling: RunResult) -> None:
    sent = [wire.OptionsBacktestTradeResponse.from_stored(stored) for stored in _stored(selling)]

    assert [
        (trade.leg_id, trade.entry.side, trade.exit_kind, None if trade.sale is None else trade.sale.side)
        for trade in sent
    ] == [
        # Wednesday: the bought leg is sold and the sold leg is bought back.
        ("long", "buy", "sale", "sell"),
        ("short", "sell", "buy_back", "buy"),
        # Thursday: both are marked, and the entry side is what says that the sold leg's mark is owed.
        ("long", "buy", "mark", None),
        ("short", "sell", "mark", None),
    ]
    assert [trade.exit_reason for trade in sent[2:]] == ["marked_not_sold", "marked_not_bought_back"]


def test_a_session_says_which_way_each_fill_traded_and_whether_each_mark_is_owed(selling: RunResult) -> None:
    wednesday, thursday = _session(selling, 0), _session(selling, 1)

    # The purchase to open and the sale to open are entries on opposite sides; the sold leg's close is a purchase and the
    # bought leg's a sale. The two closes are in one bar and are listed in the order of their trades.
    assert [(fill.kind, fill.side, fill.leg_id) for fill in wednesday.fills] == [
        ("entry", "buy", "long"),
        ("entry", "sell", "short"),
        ("exit", "sell", "long"),
        ("exit", "buy", "short"),
    ]
    assert wednesday.marks == []
    assert thursday.fills[0].side == "buy" and thursday.fills[1].side == "sell"
    # Marked, not sold: a held value. Marked and owed: a sold leg never bought back.
    assert [(mark.leg_id, mark.owed) for mark in thursday.marks] == [("long", False), ("short", True)]


def test_the_session_s_trades_and_the_trades_route_agree_on_the_side_of_every_leg(selling: RunResult) -> None:
    wednesday = _session(selling, 0)

    assert [(trade.leg_id, trade.entry.side, trade.exit_kind) for trade in wednesday.trades] == [
        ("long", "buy", "sale"),
        ("short", "sell", "buy_back"),
    ]
    # An entry's fill and the trade it belongs to are on the same side, and so are its buy-back and its close.
    entries = {fill.leg_id: fill.side for fill in wednesday.fills if fill.kind == "entry"}
    exits = {fill.leg_id: fill.side for fill in wednesday.fills if fill.kind == "exit"}
    for trade in wednesday.trades:
        assert trade.entry.side == entries[trade.leg_id]
        assert trade.sale is not None and trade.sale.side == exits[trade.leg_id]


def test_the_export_gives_each_row_its_side_and_calls_a_buy_back_a_buy_back(selling: RunResult) -> None:
    rows = list(csv.DictReader(io.StringIO(trades_csv(_stored(selling)).decode("utf-8"))))

    assert CSV_HEADER[-1] == "side"
    assert [(row["leg_id"], row["side"], row["exit_kind"]) for row in rows] == [
        ("long", "buy", "sale"),
        ("short", "sell", "buy_back"),
        ("long", "buy", "mark"),
        ("short", "sell", "mark"),
    ]
    # A mark has no seen price, and the sold leg's is the amount it owes at the close of its last bar.
    assert [(row["exit_seen_usd"], row["exit_price_usd"]) for row in rows[2:]] == [("", "1.00"), ("", "2.40")]
