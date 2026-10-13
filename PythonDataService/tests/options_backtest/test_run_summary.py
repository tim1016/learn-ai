"""A run read end to end: five sessions replayed from stored bars, and the summary the owner reads, worked by hand.

One call leg at the money for the day, no slippage, a fee of 0.50 a contract, 10,000.00 to start. A bought leg fills at
the open of minute 6 and a sale at the open of minute 61.

    Wed 10-07  bought 2.00, sold 3.00             (3.00 - 2.00) x 100 - 0.50 - 0.50 = 99.00
    Thu 10-08  bought 2.00, sold 2.01             (2.01 - 2.00) x 100 - 0.50 - 0.50 =  0.00  a break-even trade
    Fri 10-09  the contract was captured, and never traded: the entry is dropped, no trade
    Mon 10-12  bought 2.00; the contract's last bar closes at 1.80, so the leg is marked
               (1.80 - 2.00) x 100 - 0.50 = -20.50 (no sale, no exit fee)
    Tue 10-13  the underlying did not trade at the entry time: skipped
"""

from __future__ import annotations

from datetime import date

from app.options_backtest.market import PickRefusal
from app.options_backtest.results import ExitReason, SessionStatus, SkipReason
from app.options_backtest.run import RunResult, run_sessions
from app.options_backtest.setup import FeePolicy
from app.options_backtest.statistics import Ratio
from tests.options_backtest._market import leg, setup
from tests.options_backtest._run_data import FRI, OPEN_MS, THU, WED, D, Day, changed, fake_data, flat, steady

MON, TUE = date(2026, 10, 12), date(2026, 10, 13)
CALL_500 = ("call", 500)


def _bought_at_two_sold_at(price: str) -> dict[int, tuple[str, ...]]:
    return {**flat("2.00", 5, 60), 61: (price, price)}


def _run() -> RunResult:
    days = {
        WED: Day(underlying=steady("500.00"), options={CALL_500: _bought_at_two_sold_at("3.00")}),
        THU: Day(underlying=steady("500.00"), options={CALL_500: _bought_at_two_sold_at("2.01")}),
        FRI: Day(underlying=steady("500.00"), options={CALL_500: {}}),
        MON: Day(underlying=steady("500.00"), options={CALL_500: {**flat("2.00", 5, 58), 59: ("1.90", "1.80")}}),
        TUE: Day(underlying={**steady("500.00", 1, 4), **steady("500.00", 6, 390)}),
    }
    written = changed(setup(leg()), fees=FeePolicy(kind="per_contract", per_contract_usd=D("0.50")))
    return run_sessions(written, [WED, THU, FRI, MON, TUE], fake_data(days))


def test_each_session_is_what_the_hand_worked_scene_says() -> None:
    result = _run()

    assert [(s.status, s.outcome.skip_reason, s.pnl_usd) for s in result.sessions] == [
        (SessionStatus.TRADED, None, D("99.00")),
        (SessionStatus.TRADED, None, D("0.00")),
        (SessionStatus.NO_TRADE, None, D("0")),
        (SessionStatus.INCOMPLETE, None, D("-20.50")),
        (SessionStatus.SKIPPED, SkipReason.UNDERLYING_DID_NOT_TRADE, D("0")),
    ]
    # 10,000.00 + 99.00 + 0.00, nothing on Friday; Monday's mark is paid back: 10,099.00 - 200.50 + 180.00 = 10,078.50.
    assert [s.cash_after_usd for s in result.sessions] == [
        D("10099.00"),
        D("10099.00"),
        D("10099.00"),
        D("10078.50"),
        D("10078.50"),
    ]


def test_the_summary_counts_the_sessions_and_trades_of_the_run() -> None:
    summary = _run().summary

    # Tested 5 = traded 3 (Wed, Thu, Mon) + no trade 1 (Fri) + skipped 1 (Tue). Mon is the incomplete one.
    assert (summary.sessions_tested, summary.sessions_traded, summary.sessions_no_trade) == (5, 3, 1)
    assert (summary.sessions_skipped, summary.sessions_incomplete) == (1, 1)
    assert (summary.sessions_profitable, summary.sessions_losing, summary.sessions_break_even) == (1, 1, 1)
    assert summary.daily_win_rate == Ratio(D("1"), D("3"))
    # Three trades: Wed won (99.00), Mon lost (-20.50), Thu broke even (0.00).
    assert (summary.trades, summary.trades_won, summary.trades_lost, summary.trades_break_even) == (3, 1, 1, 1)
    assert summary.trade_win_rate == Ratio(D("1"), D("3"))
    assert summary.exits_by_reason == (
        (ExitReason.STOP, 0),
        (ExitReason.SQUARE_OFF, 0),
        (ExitReason.EXIT_TIME, 2),
        (ExitReason.MARKED_NOT_SOLD, 1),
        (ExitReason.MARKED_NOT_BOUGHT_BACK, 0),
    )


def test_the_summary_adds_up_the_money_with_the_marked_leg_at_its_mark() -> None:
    summary = _run().summary

    # Net 99.00 + 0.00 - 20.50 = 78.50, from gross profit 99.00 and gross loss 20.50.
    assert (summary.net_profit_usd, summary.gross_profit_usd, summary.gross_loss_usd) == (
        D("78.50"),
        D("99.00"),
        D("20.50"),
    )
    # 99.00 / 20.50 = 198 / 41 = 4.8292682926..., the digits 82926 repeating after the 4.
    assert summary.profit_factor == Ratio(D("99.00"), D("20.50"))
    assert summary.profit_factor.rounded_to_6_places() == D("4.829268")
    # Fees: Wed 1.00, Thu 1.00, Mon 0.50 (a buy, and no sale). No slippage was set.
    assert (summary.total_fees_usd, summary.slippage_paid_usd) == (D("2.50"), D("0"))
    assert summary.ending_cash_usd == D("10078.50") == summary.starting_cash_usd + summary.net_profit_usd


def test_the_summary_draws_the_drawdown_from_the_peak_to_the_marked_day_and_counts_what_did_not_trade() -> None:
    summary = _run().summary

    # Equity peaks at 10,099.00 on Wednesday (Thursday and Friday leave it there) and ends Monday at 10,078.50.
    # Wednesday is 2026-10-07 and Monday 2026-10-12: their 09:30 New York opens are 1,791,379,800,000 and
    # 1,791,811,800,000 ms UTC.
    drawdown = summary.drawdown_by_fraction
    assert drawdown is not None
    assert (drawdown.peak_equity_usd, drawdown.trough_equity_usd, drawdown.fall_usd) == (
        D("10099.00"),
        D("10078.50"),
        D("20.50"),
    )
    assert (OPEN_MS[WED], OPEN_MS[MON]) == (1_791_379_800_000, 1_791_811_800_000)
    assert (drawdown.peak_session_open_ms, drawdown.trough_session_open_ms) == (OPEN_MS[WED], OPEN_MS[MON])
    assert drawdown.fall_ratio == Ratio(D("20.50"), D("10099.00"))
    assert summary.drawdown_by_dollars == drawdown
    # Friday's contract never traded, so its entry was dropped.
    assert summary.entries_dropped == 1
    assert (
        summary.entries_refused_for_cash,
        summary.re_entries_blocked_by_cutoff,
        summary.sessions_without_momentum,
    ) == (0, 0, 0)
    assert summary.picks_refused_by_reason == tuple((reason, 0) for reason in PickRefusal)


def test_the_tables_cover_the_five_sessions_and_the_one_month() -> None:
    summary = _run().summary

    # The rows are the five sessions, Wed 10-07 to Tue 10-13, each named by its 09:30 New York open in ms UTC (the
    # table in _run_data.py works each out from its date).
    assert [(row.session_open_ms, row.status, row.net_profit_usd) for row in summary.by_day] == [
        (OPEN_MS[WED], SessionStatus.TRADED, D("99.00")),
        (OPEN_MS[THU], SessionStatus.TRADED, D("0.00")),
        (OPEN_MS[FRI], SessionStatus.NO_TRADE, D("0")),
        (OPEN_MS[MON], SessionStatus.INCOMPLETE, D("-20.50")),
        (OPEN_MS[TUE], SessionStatus.SKIPPED, D("0")),
    ]
    [october] = summary.by_month
    assert (october.year, october.month, october.sessions, october.sessions_traded, october.trades) == (
        2026,
        10,
        5,
        3,
        3,
    )
    assert (october.net_profit_usd, october.fees_usd) == (D("78.50"), D("2.50"))


def test_the_run_keeps_the_summary_of_its_own_sessions_and_the_slippage_point_of_its_own_setup() -> None:
    result = _run()

    assert result.summary.net_profit_usd == sum((s.pnl_usd for s in result.sessions), start=D("0"))
    [own] = [point for point in result.slippage if point.is_setups_own]
    assert own.net_profit_usd == result.summary.net_profit_usd
    assert (own.trades, own.traded_sessions, own.winning_sessions) == (3, 3, 1)
