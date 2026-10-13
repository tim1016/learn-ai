"""Many sessions: the cash carried from one to the next, what a leg marked and not sold does to it, and what a run keeps.

Minutes are counted after the open. The scenes use a setup that enters at minute 5 and leaves at minute 60 (so a
bought leg fills at the open of minute 6 and a sale at the open of minute 61), one call leg at the money for the day
itself, no slippage and no fee unless a scene says so. Every expectation is worked by hand and written as a literal.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import date

from app.options.contract import OptionContract
from app.options_backtest.data import Captured
from app.options_backtest.market import MinuteBar
from app.options_backtest.picking import Listing
from app.options_backtest.results import ExitReason, SessionStatus, SkipReason
from app.options_backtest.run import run_sessions
from app.options_backtest.session_result import PinnedDay, SessionPins
from app.options_backtest.setup import FeePolicy, OptionsBacktestSetup
from tests.options_backtest._market import after_open, before_close, leg, setup
from tests.options_backtest._run_data import (
    FRI,
    HALF_DAY,
    OPEN_MS,
    THU,
    WED,
    D,
    Day,
    FakeRunData,
    changed,
    contract_on,
    fake_data,
    flat,
    sha_of,
    steady,
)

CALL_500 = ("call", 500)


def _trade_day(buy: str, sell: str) -> Day:
    """The underlying steady at 500, so a same-day at-the-money call is the 500 strike; that call is bought at the
    open of minute 6 for ``buy`` and sold at the open of minute 61 for ``sell``."""
    return Day(underlying=steady("500.00"), options={CALL_500: {**flat(buy, 5, 60), 61: (sell, sell)}})


def _day_with_no_underlying_trade_at_the_entry_time() -> Day:
    return Day(underlying={**steady("500.00", 1, 4), **steady("500.00", 6, 390)})


def test_cash_is_carried_over_a_win_a_loss_and_a_skipped_session() -> None:
    # Wednesday: bought 2.00, sold 3.00: (3.00 - 2.00) x 100 = +100.00, so cash 10,000.00 -> 10,100.00.
    # Thursday: bought 4.00, sold 3.50: (3.50 - 4.00) x 100 = -50.00, so 10,100.00 -> 10,050.00.
    # Friday: the underlying did not trade at minute 5, the entry time, so the session is skipped and cash stays.
    days = {
        WED: _trade_day("2.00", "3.00"),
        THU: _trade_day("4.00", "3.50"),
        FRI: _day_with_no_underlying_trade_at_the_entry_time(),
    }

    result = run_sessions(setup(leg()), [WED, THU, FRI], fake_data(days))

    assert [session.cash_before_usd for session in result.sessions] == [D("10000"), D("10100.00"), D("10050.00")]
    assert [session.cash_after_usd for session in result.sessions] == [D("10100.00"), D("10050.00"), D("10050.00")]
    assert [session.pnl_usd for session in result.sessions] == [D("100.00"), D("-50.00"), D("0")]
    assert [session.status for session in result.sessions] == [
        SessionStatus.TRADED,
        SessionStatus.TRADED,
        SessionStatus.SKIPPED,
    ]
    assert [session.outcome.skip_reason for session in result.sessions] == [
        None,
        None,
        SkipReason.UNDERLYING_DID_NOT_TRADE,
    ]
    bought_and_sold = [(trade.entry.filled, trade.exit_price) for s in result.sessions for trade in s.outcome.trades]
    assert bought_and_sold == [(D("2.00"), D("3.00")), (D("4.00"), D("3.50"))]


def test_a_session_keeps_the_calendars_open_and_close_in_ms_and_derives_its_date_from_the_open() -> None:
    days = {WED: _trade_day("2.00", "3.00"), FRI: _trade_day("2.00", "3.00")}

    result = run_sessions(setup(leg()), [WED, FRI], fake_data(days))

    # 2026-10-07 09:30 New York (UTC-4) is 13:30 UTC; 2026-10-09 is two days (172,800,000 ms) later; 390 minutes is
    # 23,400,000 ms. The Friday's open is 1,791,552,600,000 ms.
    assert [session.session_open_ms for session in result.sessions] == [OPEN_MS[WED], OPEN_MS[FRI]]
    assert [session.trading_date for session in result.sessions] == [WED, FRI]  # derived, and not a field
    assert result.sessions[1].session_open_ms == 1_791_552_600_000
    assert result.sessions[1].session_close_ms == 1_791_576_000_000
    assert result.sessions[0].session_open_ms == 1_791_552_600_000 - 172_800_000


def test_a_leg_marked_and_not_sold_pays_back_its_mark_into_the_cash_the_next_session_starts_with() -> None:
    # Wednesday: bought at 2.00 (minute 6's open), and the contract's last bar is minute 59, which closes at 1.80, so
    # the exit time at minute 60 finds nothing to sell into and the leg is marked at 1.80.
    #   cash that moved: -(2.00 x 100) = -200.00;  marked value: 1.80 x 100 = 180.00;
    #   profit at the mark: (1.80 - 2.00) x 100 = -20.00 = -200.00 + 180.00.
    #   the next session starts with 10,000.00 - 200.00 + 180.00 = 9,980.00.
    # Thursday: bought 2.00, sold 2.50: +50.00, so 9,980.00 -> 10,030.00.
    marked = Day(underlying=steady("500.00"), options={CALL_500: {**flat("2.00", 5, 58), 59: ("1.90", "1.80")}})
    days = {WED: marked, THU: _trade_day("2.00", "2.50")}

    result = run_sessions(setup(leg()), [WED, THU], fake_data(days))

    first, second = result.sessions
    assert first.status is SessionStatus.INCOMPLETE
    assert [trade.exit_reason for trade in first.outcome.trades] == [ExitReason.MARKED_NOT_SOLD]
    assert first.outcome.cash_change_usd == D("-200.00")
    assert first.outcome.marked_value_usd == D("180.00")
    assert first.pnl_usd == D("-20.00")
    assert first.cash_after_usd == D("9980.00")
    assert second.cash_before_usd == D("9980.00")
    assert second.cash_after_usd == D("10030.00")


def test_cash_after_is_cash_before_plus_the_profit_with_the_marks_in_it_on_every_session() -> None:
    marked = Day(underlying=steady("500.00"), options={CALL_500: {**flat("2.00", 5, 58), 59: ("1.90", "1.80")}})
    days = {WED: marked, THU: _trade_day("2.00", "2.50"), FRI: _day_with_no_underlying_trade_at_the_entry_time()}

    result = run_sessions(setup(leg()), [WED, THU, FRI], fake_data(days))

    for session in result.sessions:
        assert session.pnl_usd == session.outcome.cash_change_usd + session.outcome.marked_value_usd
        assert session.cash_after_usd == session.cash_before_usd + session.pnl_usd
    for before, after in zip(result.sessions, result.sessions[1:], strict=False):
        assert after.cash_before_usd == before.cash_after_usd


def test_a_fee_per_contract_comes_out_of_each_sessions_profit_and_cash() -> None:
    # 0.65 a contract, one contract: 0.65 on the buy and 0.65 on the sale.
    # Wednesday: (3.00 - 2.00) x 100 - 0.65 - 0.65 = 98.70. Cash that moved: -200.00 - 0.65 + 300.00 - 0.65 = 98.70.
    paying = changed(setup(leg()), fees=FeePolicy(kind="per_contract", per_contract_usd=D("0.65")))

    result = run_sessions(paying, [WED], fake_data({WED: _trade_day("2.00", "3.00")}))

    session = result.sessions[0]
    assert session.fees_usd == D("1.30")
    assert session.pnl_usd == D("98.70")
    assert session.cash_after_usd == D("10098.70")


def test_estimated_fees_are_priced_by_the_fee_model_on_the_date_of_each_fill() -> None:
    # On 2026-10-07: a buy of 1 owes CAT 0.0003 up to 0.01, ORF 0.015 up to 0.02, OCC 0.025 up to 0.03: 0.06.
    # The sale of 1 at 3.00 owes SEC 1 x 3.00 x 100 x 0.0000206 = 0.00618 up to 0.01, TAF 0 (paused), CAT 0.01,
    # ORF 0.02, OCC 0.03: 0.07. Profit (3.00 - 2.00) x 100 - 0.06 - 0.07 = 99.87.
    estimated = changed(setup(leg()), fees=FeePolicy(kind="estimated"))

    result = run_sessions(estimated, [WED], fake_data({WED: _trade_day("2.00", "3.00")}))

    [trade] = result.sessions[0].outcome.trades
    assert (trade.entry_fee_usd, trade.exit_fee_usd) == (D("0.06"), D("0.07"))
    assert result.sessions[0].pnl_usd == D("99.87")


def test_a_session_keeps_the_sha256_of_everything_it_read() -> None:
    days = {WED: _trade_day("2.00", "3.00"), FRI: _day_with_no_underlying_trade_at_the_entry_time()}

    result = run_sessions(setup(leg()), [WED, FRI], fake_data(days))

    wednesday_call = contract_on(WED, "call", 500)
    assert result.sessions[0].pins == SessionPins(
        underlying_sha256=sha_of("underlying", WED),
        listing_sha256=sha_of("listing", WED),
        option_days=(PinnedDay(wednesday_call, sha_of("option", WED, wednesday_call)),),
    )
    # Friday read its underlying and listing and, being skipped, needed no contract.
    assert result.sessions[1].pins == SessionPins(sha_of("underlying", FRI), sha_of("listing", FRI), ())


class _ReadOnce:
    """A ``RunData`` whose bars and listing come back as iterators, which can be read once and are then empty."""

    def __init__(self, inner: FakeRunData) -> None:
        self._inner = inner

    def underlying_day(self, trading_date: date) -> Captured[Sequence[MinuteBar]] | None:
        found = self._inner.underlying_day(trading_date)
        return found and Captured(iter(found.value), found.sha256)  # type: ignore[arg-type]

    def listing(self, trading_date: date) -> Captured[Listing] | None:
        found = self._inner.listing(trading_date)
        return found and Captured(replace(found.value, contracts=iter(found.value.contracts)), found.sha256)  # type: ignore[arg-type]

    def option_day(self, contract: OptionContract, trading_date: date) -> Captured[Sequence[MinuteBar]] | None:
        found = self._inner.option_day(contract, trading_date)
        return found and Captured(iter(found.value), found.sha256)  # type: ignore[arg-type]


def test_bars_and_a_listing_that_can_be_read_only_once_are_held_before_they_are_used() -> None:
    # Bought at 2.00 and sold at 3.00: +100.00, as for a list. Read once and then looked at again, the underlying would
    # seem not to have traded and the day would be skipped quietly, so a run must hold what it is handed.
    data = _ReadOnce(fake_data({WED: _trade_day("2.00", "3.00")}))

    result = run_sessions(setup(leg()), [WED], data)

    [session] = result.sessions
    assert (session.status, session.pnl_usd) == (SessionStatus.TRADED, D("100.00"))


def test_only_the_contracts_a_session_needs_are_read_and_each_is_read_once() -> None:
    # Two legs, a call and a put at the money, both of which the day has; the listing holds more that nothing picks.
    both = Day(
        underlying=steady("500.00"),
        options={
            CALL_500: {**flat("2.00", 5, 60), 61: ("3.00", "3.00")},
            ("put", 500): {**flat("1.00", 5, 60), 61: ("1.50", "1.50")},
        },
    )
    data = fake_data({WED: both})

    run_sessions(setup(leg("a", "call"), leg("b", "put")), [WED], data)

    assert data.asked == [
        ("underlying", WED),
        ("listing", WED),
        ("option", WED, contract_on(WED, "call", 500).occ_symbol),
        ("option", WED, contract_on(WED, "put", 500).occ_symbol),
    ]


def test_the_same_inputs_give_an_equal_result_twice() -> None:
    days = {
        WED: _trade_day("2.00", "3.00"),
        THU: _trade_day("4.00", "3.50"),
        FRI: _day_with_no_underlying_trade_at_the_entry_time(),
    }

    first = run_sessions(setup(leg()), [WED, THU, FRI], fake_data(days))
    second = run_sessions(setup(leg()), [WED, THU, FRI], fake_data(days))

    assert first == second


def test_a_run_holds_one_sessions_data_at_a_time_and_none_of_it_when_it_ends() -> None:
    days = {WED: _trade_day("2.00", "3.00"), THU: _trade_day("4.00", "3.50"), FRI: _trade_day("1.00", "1.10")}
    data = fake_data(days)

    result = run_sessions(setup(leg()), [WED, THU, FRI], data)

    assert len(result.sessions) == 3
    assert data.handed_out_count() > 0  # the fake did hand things out, or "none alive" would prove nothing
    # Each time a session's underlying was asked for, nothing handed out for the sessions before it was still alive.
    assert data.alive_at_each_session == [0, 0, 0]
    assert data.handed_out_and_still_alive() == 0


def test_a_half_day_is_replayed_to_its_own_close_and_a_window_it_cannot_fit_is_skipped_without_reading_anything() -> (
    None
):
    # Enter at minute 190 and leave 30 minutes before the close: minute 190 to 360 on a full day, 190 to 180 on the half
    # day (it closes at minute 210), which cannot fit. The half day needs no data, so none is stored or asked for.
    late = changed(setup(leg()), entry=after_open(190), exit=before_close(30))
    full = Day(underlying=steady("500.00"), options={CALL_500: {**flat("2.00", 190, 360), 361: ("2.50", "2.50")}})
    data = fake_data({THU: full})

    result = run_sessions(late, [THU, HALF_DAY], data)

    thursday, half = result.sessions
    assert thursday.status is SessionStatus.TRADED
    assert half.status is SessionStatus.SKIPPED
    assert half.outcome.skip_reason is SkipReason.WINDOW_DOES_NOT_FIT
    assert half.pins == SessionPins(None, None, ())
    assert [question for question in data.asked if question[1] == HALF_DAY] == []
    assert half.cash_before_usd == half.cash_after_usd == thursday.cash_after_usd
    # The half day's open and close are the calendar's: 09:30 to 13:00 New York time, 210 minutes.
    assert (half.session_close_ms - half.session_open_ms) // 60_000 == 210


def test_a_window_counted_from_the_close_ends_at_each_sessions_own_close() -> None:
    # Leave 10 minutes before the close: minute 380 of a full day and minute 200 of the half day, whose sales fill
    # at the open of minutes 381 and 201.
    leaving_at_the_close = changed(setup(leg()), exit=before_close(10))
    full = Day(underlying=steady("500.00"), options={CALL_500: {**flat("2.00", 5, 380), 381: ("2.50", "2.50")}})
    short = Day(
        underlying=steady("500.00", 1, 210), options={CALL_500: {**flat("2.00", 5, 200), 201: ("2.60", "2.60")}}
    )

    result = run_sessions(leaving_at_the_close, [THU, HALF_DAY], fake_data({THU: full, HALF_DAY: short}))

    thursday, half = result.sessions
    # Thursday: (2.50 - 2.00) x 100 = +50.00. The half day: (2.60 - 2.00) x 100 = +60.00.
    assert (thursday.pnl_usd, half.pnl_usd) == (D("50.00"), D("60.00"))
    [half_trade] = half.outcome.trades
    assert half_trade.exit_decided_at_ms == half.session_close_ms - 10 * 60_000
    assert half_trade.exit.bar_close_ms == half.session_close_ms - 9 * 60_000


# ── A setup that sells ───────────────────────────────────────────────────


def _put_spread_setup() -> OptionsBacktestSetup:
    """A put 495 bought and a put 500 sold, one contract each, for the day itself: a credit put spread 5 wide."""
    return setup(
        {**leg("long", "put"), "strike": {"kind": "atm_offset", "steps": -1}},
        leg("short", "put", side="sell"),
    )


def _spread_day(*, sold_last_bar: int | None = None) -> Day:
    """The 495 put bought at 1.00 and the 500 put sold at 2.20 at the open of minute 6, and closed at the open of
    minute 61: the 500 put bought back at 1.50 and the 495 put sold at 0.60. ``sold_last_bar`` is the last minute the
    500 put traded in, at 2.20 and closing at 2.40, with nothing after it: it cannot be bought back."""
    if sold_last_bar is None:
        return Day(
            underlying=steady("500.00"),
            options={
                ("put", 495): {**flat("1.00", 5, 60), 61: ("0.60", "0.60")},
                ("put", 500): {**flat("2.20", 5, 60), 61: ("1.50", "1.50")},
            },
        )
    return Day(
        underlying=steady("500.00"),
        options={
            ("put", 495): flat("1.00", 5, 61),
            ("put", 500): {**flat("2.20", 5, sold_last_bar - 1), sold_last_bar: ("2.20", "2.40")},
        },
    )


def test_a_sold_leg_marked_and_owed_takes_its_mark_out_of_the_cash_the_next_session_starts_with() -> None:
    # Wednesday: the 495 put is bought at 1.00 (-100.00) and the 500 put sold at 2.20 (+220.00), so +120.00 moves. The
    # 500 put's last bar is minute 60, closing at 2.40, so it cannot be bought back at the exit time and is owed at 2.40;
    # the 495 put, which may not leave it bare, is marked at its last close, 1.00.
    #   marked value: +1.00 x 100 for the bought leg and -2.40 x 100 for the owed one = 100.00 - 240.00 = -140.00;
    #   profit at the marks: (1.00 - 1.00) x 100 + (2.20 - 2.40) x 100 = -20.00 = +120.00 - 140.00.
    #   the next session starts with 10,000.00 + 120.00 - 140.00 = 9,980.00.
    # Thursday: a spread that closes cleanly: (0.60 - 1.00) x 100 + (2.20 - 1.50) x 100 = +30.00, so 9,980.00 -> 10,010.00.
    days = {WED: _spread_day(sold_last_bar=60), THU: _spread_day()}

    result = run_sessions(_put_spread_setup(), [WED, THU], fake_data(days))

    first, second = result.sessions
    assert first.status is SessionStatus.INCOMPLETE
    assert [trade.exit_reason for trade in first.outcome.trades] == [
        ExitReason.MARKED_NOT_SOLD,
        ExitReason.MARKED_NOT_BOUGHT_BACK,
    ]
    assert first.outcome.cash_change_usd == D("120.00")
    assert first.outcome.marked_value_usd == D("-140.00")
    assert first.pnl_usd == D("-20.00")
    assert first.cash_after_usd == D("9980.00")
    assert second.cash_before_usd == D("9980.00")
    assert second.status is SessionStatus.TRADED
    assert second.pnl_usd == D("30.00")
    assert second.cash_after_usd == D("10010.00")
    assert result.summary.ending_cash_usd == D("10010.00")
    assert result.summary.exits_by_reason[-2:] == (
        (ExitReason.MARKED_NOT_SOLD, 1),
        (ExitReason.MARKED_NOT_BOUGHT_BACK, 1),
    )


def test_the_slippage_ladder_replays_a_setup_that_sells_at_each_value_with_the_fills_moved_against_each_side() -> None:
    # No slippage: (0.60 - 1.00) x 100 + (2.20 - 1.50) x 100 = +30.00.
    # One percent: the 495 put is bought at 1.00 x 1.01 = 1.01 and sold at 0.60 x 0.99 = 0.594 down to 0.59, the 500 put
    # is sold at 2.20 x 0.99 = 2.178 down to 2.17 and bought back at 1.50 x 1.01 = 1.515 up to 1.52:
    # (0.59 - 1.01) x 100 + (2.17 - 1.52) x 100 = -42.00 + 65.00 = +23.00.
    result = run_sessions(
        _put_spread_setup(),
        [THU],
        fake_data({THU: _spread_day()}),
        slippage_ladder=(D("0"), D("1")),
    )

    assert [(point.slippage_percent, point.net_profit_usd) for point in result.slippage] == [
        (D("0"), D("30.00")),
        (D("1"), D("23.00")),
    ]
