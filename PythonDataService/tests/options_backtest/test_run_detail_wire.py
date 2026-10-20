"""What the page is sent of a run's trades, session and comparison: exact text, the ceiling on instants, and words tied
to the enums they come from (#3030).

The trades below are written by hand with the scale of every figure chosen on purpose, so the text is what was saved
and no replay arithmetic decides it.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import get_args, get_type_hints

import pytest
from pydantic import BaseModel, ValidationError

from app.options.contract import OptionContract
from app.options_backtest.comparison import compare
from app.options_backtest.market import PickRefusal
from app.options_backtest.results import (
    CancelReason,
    EntryRefusal,
    Event,
    EventKind,
    ExitReason,
    Fill,
    LegTrade,
    Mark,
    SessionStatus,
)
from app.options_backtest.run import RunResult, run_sessions
from app.options_backtest.run_detail import TradePage
from app.options_backtest.saved_runs import SessionLine, StoredTrade
from app.options_backtest.session_levels import (
    ChartedFill,
    ChartedMark,
    Level,
    LevelKind,
    fills_of,
    levels_of,
    marks_of,
)
from app.options_backtest.session_series import Series, SeriesPoint, SeriesReason, SessionSeries
from app.options_backtest.session_view import SessionView
from app.options_backtest.setup import MS_PER_MINUTE
from app.schemas import options_backtest_run_detail as wire
from app.utils.session_anchors import MAX_TIMESTAMP_MS
from tests.options_backtest._detail_scene import SESSIONS, WED_OPEN_MS, the_scene, the_setup
from tests.options_backtest._headers import THE_REASON, a_header, a_session_record
from tests.options_backtest._run_data import fake_data
from tests.options_backtest._stored_runs import no_date_in

D = Decimal
OPEN_MS = 1_791_379_800_000
CALL = OptionContract.of(root="SPY", expiry_date=date(2026, 10, 7), right="call", strike=D("500.5"))


def _sold() -> LegTrade:
    return LegTrade(
        leg_id="call",
        entry_number=1,
        contract=CALL,
        contracts=2,
        multiplier=100,
        starting_price=D("4.00"),
        momentum_level=D("5.0000"),
        stop_level=D("4.080"),
        entry_decided_at_ms=OPEN_MS + 600_000,
        entry=Fill("call", CALL, "buy", 2, 100, OPEN_MS + 660_000, D("5.0"), D("5.02")),
        exit_reason=ExitReason.STOP,
        exit_group=3,
        exit_decided_at_ms=OPEN_MS + 1_200_000,
        exit=Fill("call", CALL, "sell", 2, 100, OPEN_MS + 1_260_000, D("3.9"), D("3.88")),
        entry_fee_usd=D("1.30"),
        exit_fee_usd=D("1.30"),
        pnl_usd=D("-230.60"),
    )


def _marked() -> LegTrade:
    return LegTrade(
        leg_id="put",
        entry_number=0,
        contract=CALL,
        contracts=1,
        multiplier=100,
        starting_price=None,
        momentum_level=None,
        stop_level=None,
        entry_decided_at_ms=OPEN_MS + 300_000,
        entry=Fill("put", CALL, "buy", 1, 100, OPEN_MS + 360_000, D("2.00"), D("2.00")),
        exit_reason=ExitReason.MARKED_NOT_SOLD,
        exit_group=1,
        exit_decided_at_ms=OPEN_MS + 3_600_000,
        exit=Mark(OPEN_MS + 6_000_000, D("1.75")),
        entry_fee_usd=D("0.65"),
        exit_fee_usd=D("0"),
        pnl_usd=D("-25.65"),
    )


def test_a_sold_trade_is_sent_with_every_figure_as_the_text_it_was_saved_as_and_no_mark() -> None:
    sent = wire.OptionsBacktestTradeResponse.from_stored(StoredTrade(OPEN_MS, 4, _sold()))

    assert sent.model_dump() == {
        "session_open_ms": OPEN_MS,
        "trade_number": 4,
        "leg_id": "call",
        "entry_number": 1,
        "contract": {
            "symbol": "SPY261007C00500500",
            "underlying": "SPY",
            "right": "call",
            "strike_usd": "500.5",
            "expiry_ms": OPEN_MS + 390 * 60_000,
        },
        "contracts": 2,
        "multiplier": 100,
        "starting_price_usd": "4.00",
        "momentum_level_usd": "5.0000",
        "stop_level_usd": "4.080",
        "entry_decided_at_ms": OPEN_MS + 600_000,
        "entry": {"side": "buy", "bar_close_ms": OPEN_MS + 660_000, "seen_usd": "5.0", "filled_usd": "5.02"},
        "exit_reason": "stop",
        "exit_group": 3,
        "exit_decided_at_ms": OPEN_MS + 1_200_000,
        "exit_kind": "sale",
        "sale": {"side": "sell", "bar_close_ms": OPEN_MS + 1_260_000, "seen_usd": "3.9", "filled_usd": "3.88"},
        "mark": None,
        "entry_fee_usd": "1.30",
        "exit_fee_usd": "1.30",
        "pnl_usd": "-230.60",
    }


def _bought_back() -> LegTrade:
    """A sold leg that left by a buy-back: sold for 2.50 at entry, bought back for 1.80, two contracts."""
    return LegTrade(
        leg_id="short_call",
        entry_number=0,
        contract=CALL,
        contracts=2,
        multiplier=100,
        starting_price=None,
        momentum_level=None,
        stop_level=None,
        entry_decided_at_ms=OPEN_MS + 300_000,
        entry=Fill("short_call", CALL, "sell", 2, 100, OPEN_MS + 360_000, D("2.50"), D("2.50")),
        exit_reason=ExitReason.EXIT_TIME,
        exit_group=1,
        exit_decided_at_ms=OPEN_MS + 3_600_000,
        exit=Fill("short_call", CALL, "buy", 2, 100, OPEN_MS + 3_660_000, D("1.80"), D("1.80")),
        entry_fee_usd=D("0"),
        exit_fee_usd=D("0"),
        pnl_usd=D("140.00"),
    )


def _owed() -> LegTrade:
    """A sold leg never bought back: sold for 2.50 and marked at 3.00, which it owes."""
    return LegTrade(
        leg_id="short_call",
        entry_number=0,
        contract=CALL,
        contracts=1,
        multiplier=100,
        starting_price=None,
        momentum_level=None,
        stop_level=None,
        entry_decided_at_ms=OPEN_MS + 300_000,
        entry=Fill("short_call", CALL, "sell", 1, 100, OPEN_MS + 360_000, D("2.50"), D("2.50")),
        exit_reason=ExitReason.MARKED_NOT_BOUGHT_BACK,
        exit_group=1,
        exit_decided_at_ms=OPEN_MS + 3_600_000,
        exit=Mark(OPEN_MS + 6_000_000, D("3.00")),
        entry_fee_usd=D("0"),
        exit_fee_usd=D("0"),
        pnl_usd=D("-50.00"),
    )


def test_a_buy_back_is_sent_as_a_buy_back_with_the_side_of_each_fill() -> None:
    sent = wire.OptionsBacktestTradeResponse.from_stored(StoredTrade(OPEN_MS, 0, _bought_back()))

    assert (sent.exit_kind, sent.mark, sent.exit_reason) == ("buy_back", None, "exit_time")
    assert sent.entry.side == "sell"
    assert sent.sale == wire.OptionsBacktestTradeFillResponse(
        side="buy", bar_close_ms=OPEN_MS + 3_660_000, seen_usd="1.80", filled_usd="1.80"
    )


@pytest.mark.parametrize(
    ("trade_of", "exit_reason", "entry_side", "mark_price", "pnl"),
    [
        # A sold leg marked and not bought back is a mark whose entry says it is owed.
        pytest.param(_owed, "marked_not_bought_back", "sell", "3.00", "-50.00", id="sold-leg-marked-not-bought-back"),
        # A leg marked and not sold is a mark with no sale and no exit fee.
        pytest.param(_marked, "marked_not_sold", "buy", "1.75", "-25.65", id="bought-leg-marked-not-sold"),
    ],
)
def test_a_leg_marked_and_not_closed_is_sent_as_a_mark_with_no_sale_and_no_exit_fee(
    trade_of: Callable[[], LegTrade], exit_reason: str, entry_side: str, mark_price: str, pnl: str
) -> None:
    sent = wire.OptionsBacktestTradeResponse.from_stored(StoredTrade(OPEN_MS, 0, trade_of()))

    assert (sent.exit_kind, sent.sale, sent.exit_reason) == ("mark", None, exit_reason), (
        "a leg marked and not closed is sent as a mark with no sale"
    )
    assert sent.entry.side == entry_side, "the entry says whether the leg is owed (sold) or held (bought)"
    assert sent.mark == wire.OptionsBacktestTradeMarkResponse(bar_close_ms=OPEN_MS + 6_000_000, price_usd=mark_price), (
        "the mark is the last close, as text"
    )
    assert (sent.starting_price_usd, sent.momentum_level_usd, sent.stop_level_usd) == (None, None, None), (
        "a leg with no stop sends no starting price, momentum level or stop level"
    )
    assert (sent.exit_fee_usd, sent.pnl_usd) == ("0", pnl), "a mark has no exit fee, and the profit is at the mark"


def test_a_trade_whose_exit_kind_disagrees_with_the_side_of_its_fill_cannot_be_made() -> None:
    sent = wire.OptionsBacktestTradeResponse.from_stored(StoredTrade(OPEN_MS, 0, _bought_back()))

    with pytest.raises(ValidationError):
        wire.OptionsBacktestTradeResponse.model_validate(sent.model_dump() | {"exit_kind": "sale"})


def test_a_figure_is_written_in_plain_digits_whatever_form_the_decimal_holds_it_in() -> None:
    # 1E+1 is ten and 1E-7 is a tenth of a millionth: neither may reach the page in exponent form or through a float.
    trade = _sold()
    odd = LegTrade(
        **{
            **trade.__dict__,
            "starting_price": D("1E+1"),
            "momentum_level": D("1E-7"),
            "stop_level": D("0E-7"),
            "pnl_usd": D("-1.5E+3"),
        }
    )

    sent = wire.OptionsBacktestTradeResponse.from_stored(StoredTrade(OPEN_MS, 0, odd))

    assert (sent.starting_price_usd, sent.momentum_level_usd, sent.stop_level_usd, sent.pnl_usd) == (
        "10",
        "0.0000001",
        "0.0000000",
        "-1500",
    )


def test_a_trade_that_names_both_a_sale_and_a_mark_or_neither_cannot_be_made() -> None:
    sent = wire.OptionsBacktestTradeResponse.from_stored(StoredTrade(OPEN_MS, 0, _sold()))
    both = sent.model_dump() | {"mark": {"bar_close_ms": OPEN_MS, "price_usd": "1"}}
    neither = sent.model_dump() | {"sale": None}
    wrong_one = sent.model_dump() | {"exit_kind": "mark"}

    for broken in (both, neither, wrong_one):
        with pytest.raises(ValidationError):
            wire.OptionsBacktestTradeResponse.model_validate(broken)


@pytest.mark.parametrize(
    ("words", "members"),
    [
        (wire.ExitReasonWord, [member.value for member in ExitReason]),
        (wire.EventKindWord, [member.value for member in EventKind]),
        (
            wire.EventReasonWord,
            [
                *(member.value for member in PickRefusal),
                *(member.value for member in EntryRefusal),
                *(member.value for member in CancelReason),
            ],
        ),
        (wire.LevelKindWord, [member.value for member in LevelKind]),
        (wire.SeriesReasonWord, [member.value for member in SeriesReason]),
        (wire.SeriesStateWord, ["shown", "not_shown"]),
        (wire.FillKindWord, list(get_args(get_type_hints(ChartedFill)["kind"]))),
        (wire.SessionStatusWord, [member.value for member in SessionStatus]),
    ],
)
def test_each_closed_set_on_the_wire_is_exactly_the_members_of_the_enum_it_comes_from(
    words: object, members: list[str]
) -> None:
    assert list(get_args(words)) == members


def test_an_event_names_its_contract_by_symbol_its_price_as_text_and_its_words_as_the_backend_wrote_them() -> None:
    stored = (
        "the strike rule comes off the end of the 4 call strikes listed for 2026-10-09 with the underlying at 499.07"
    )
    event = Event(
        kind=EventKind.PICK_REFUSED,
        at_ms=OPEN_MS + 300_000,
        leg_id="far",
        entry_number=0,
        reason=PickRefusal.STRIKE_NOT_LISTED.value,
        detail=stored,
    )
    priced = Event(EventKind.ENTRY_FILLED, OPEN_MS + 360_000, "call", 1, CALL, D("5.10"))

    sent, sent_priced = (
        wire.OptionsBacktestEventResponse.from_event(event),
        wire.OptionsBacktestEventResponse.from_event(priced),
    )

    assert (sent.reason, sent.contract, sent.price_usd) == ("strike_not_listed", None, None), (
        "an event with no contract and no price is sent with its reason and neither"
    )
    assert (sent_priced.contract, sent_priced.price_usd, sent_priced.detail) == ("SPY261007C00500500", "5.10", None), (
        "an event names its contract by symbol and its price as text"
    )
    # The words are the backend's own prose for the owner: stored as written and served as written. A refused pick
    # names the day it looked for, so the date stays in it, character for character.
    assert sent.detail == stored, "an event's words are served exactly as the backend wrote them"
    assert sent.detail is not None and "2026-10-09" in sent.detail, "an event's words keep the date they name"


def test_a_series_is_shown_with_no_reason_or_not_shown_with_a_reason_a_message_and_no_points() -> None:
    shown = {"symbol": "SPY", "state": "shown", "points": [{"at_ms": OPEN_MS, "close_usd": "500.2"}]}
    not_shown = {"symbol": "SPY", "state": "not_shown", "reason": "gone", "message": "words", "points": []}

    wire.OptionsBacktestSeriesResponse.model_validate(shown)
    wire.OptionsBacktestSeriesResponse.model_validate(not_shown)
    for broken in (
        shown | {"reason": "gone"},
        shown | {"message": "words"},
        not_shown | {"reason": None},
        not_shown | {"message": None},
        not_shown | {"points": shown["points"]},
    ):
        with pytest.raises(ValidationError):
            wire.OptionsBacktestSeriesResponse.model_validate(broken)


def _served_models() -> list[type[BaseModel]]:
    return [
        member
        for _, member in inspect.getmembers(wire, inspect.isclass)
        if issubclass(member, BaseModel) and member.__module__ == wire.__name__
    ]


def test_every_served_model_is_named_for_the_feature_and_has_bounded_instants_text_prices_and_no_bare_dict() -> None:
    models = _served_models()

    assert len(models) >= 10, "this module serves at least ten models"
    assert [m.__name__ for m in models if not m.__name__.startswith("OptionsBacktest")] == [], (
        "every model this module serves is named for the feature, so none collides with another"
    )
    for model in models:
        schema = model.model_json_schema()
        for name, prop in schema.get("properties", {}).items():
            where = f"{model.__name__}.{name}"
            options = prop.get("anyOf", [prop])
            if name.endswith("_ms"):
                bounded = [o for o in options if o.get("type") == "integer"]
                assert bounded and all(o.get("maximum") == MAX_TIMESTAMP_MS for o in bounded), where
            if name.endswith("_usd"):
                assert {o.get("type") for o in options} <= {"string", "null"}, where
            assert not any(o.get("type") == "object" and "$ref" not in o for o in options), f"{where} is a naked dict"
        assert "9223372036854775807" not in str(schema), f"{model.__name__} bounds a field by 2**63 - 1"


def test_a_price_of_twenty_six_digits_is_sent_as_those_digits_by_every_model_that_sends_one() -> None:
    # A float holds about 17 digits: one that crossed a float anywhere would come out as 1234567.1234567891 or so.
    long = D("1234567.1234567890123456789")
    text = "1234567.1234567890123456789"
    trade = replace(
        _sold(),
        starting_price=long,
        momentum_level=long,
        stop_level=long,
        entry=Fill("call", CALL, "buy", 2, 100, OPEN_MS + 660_000, long, long),
        exit=Fill("call", CALL, "sell", 2, 100, OPEN_MS + 1_260_000, long, long),
        entry_fee_usd=long,
        exit_fee_usd=long,
        pnl_usd=long,
    )
    marked = replace(_marked(), exit=Mark(OPEN_MS + 6_000_000, long))

    sent_trade = wire.OptionsBacktestTradeResponse.from_stored(StoredTrade(OPEN_MS, 0, trade))
    sent_mark = wire.OptionsBacktestTradeResponse.from_stored(StoredTrade(OPEN_MS, 0, marked))
    assert sent_trade.sale is not None and sent_mark.mark is not None
    assert {
        sent_trade.starting_price_usd,
        sent_trade.momentum_level_usd,
        sent_trade.stop_level_usd,
        sent_trade.entry.seen_usd,
        sent_trade.entry.filled_usd,
        sent_trade.sale.seen_usd,
        sent_trade.sale.filled_usd,
        sent_trade.entry_fee_usd,
        sent_trade.exit_fee_usd,
        sent_trade.pnl_usd,
        sent_mark.mark.price_usd,
    } == {text}

    event = wire.OptionsBacktestEventResponse.from_event(Event(EventKind.ENTRY_FILLED, OPEN_MS, "call", 0, CALL, long))
    level = wire.OptionsBacktestLevelResponse.from_level(Level(LevelKind.STOP, "call", 0, CALL, long, OPEN_MS, OPEN_MS))
    fill = wire.OptionsBacktestFillResponse.from_fill(
        ChartedFill("entry", "buy", "call", 0, CALL, 1, OPEN_MS, long, long, None)
    )
    mark = wire.OptionsBacktestMarkResponse.from_mark(ChartedMark("call", 0, CALL, OPEN_MS, long, False))
    series = wire.OptionsBacktestSeriesResponse.from_series(Series("SPY", None, (SeriesPoint(OPEN_MS, long),), None))
    assert {
        event.price_usd,
        level.price_usd,
        fill.seen_usd,
        fill.filled_usd,
        mark.price_usd,
        series.points[0].close_usd,
    } == {text}


# ── What the page is sent of one session, of a page of trades, and of two runs compared ──
#
# Built by hand from the dataclasses the store fills, with no database: the figures are those of ``_detail_scene.py``,
# worked in its docstring (Wednesday made 8.70 - 121.30 - 31.30 = -143.90, so the cash went 10,000.00 -> 9,856.10).


@pytest.fixture(scope="module")
def scene() -> RunResult:
    return run_sessions(the_setup(), SESSIONS, fake_data(the_scene()))


def _view(scene: RunResult, at: int) -> SessionView:
    record = a_session_record(scene, at)
    outcome = record.session.result.outcome
    nothing = SessionSeries(underlying=Series("SPY", None, (), SeriesReason.GONE), contracts=())
    return SessionView(
        record=record,
        series=nothing,
        levels=levels_of(scene.setup, outcome),
        fills=fills_of(outcome.trades),
        marks=marks_of(outcome.trades),
    )


def test_a_session_is_sent_with_its_own_cash_its_own_profit_its_own_close_and_its_events_in_the_order_they_happened(
    scene: RunResult,
) -> None:
    sent = wire.OptionsBacktestSessionResponse.from_view(_view(scene, 0))

    # Wednesday: 10,000.00 before, 9,856.10 after, -143.90 of profit; a full day closes 390 minutes after it opens.
    assert (sent.session_open_ms, sent.session_close_ms) == (WED_OPEN_MS, WED_OPEN_MS + 390 * MS_PER_MINUTE)
    assert (sent.status, sent.skip_reason) == ("traded", None)
    assert (sent.cash_before_usd, sent.cash_after_usd, sent.profit_usd) == ("10000", "9856.10", "-143.90")
    # The first event is the call leg picking its contract at m5; the last is the put's sale filling at m61.
    assert [
        (e.kind, (e.at_ms - WED_OPEN_MS) // MS_PER_MINUTE, e.leg_id) for e in (sent.events[0], sent.events[-1])
    ] == [
        ("picked", 5, "call"),
        ("close_filled", 61, "put"),
    ]
    assert len(sent.events) == 20 and len(sent.trades) == 3 and len(sent.levels) == 8 and len(sent.fills) == 6
    assert sent.setup_cannot_open_reason is None


def test_a_session_skipped_for_want_of_an_underlying_price_is_sent_with_the_reason_it_was_skipped_for(
    scene: RunResult,
) -> None:
    sent = wire.OptionsBacktestSessionResponse.from_view(_view(scene, 1))

    assert (sent.status, sent.skip_reason) == ("skipped", "underlying_did_not_trade")
    assert (sent.cash_before_usd, sent.cash_after_usd, sent.profit_usd) == ("9856.10", "9856.10", "0")


def test_a_session_of_a_run_whose_setup_was_lost_says_why_beside_everything_else_it_serves(scene: RunResult) -> None:
    record = a_session_record(scene, 0, readable=False)
    outcome = record.session.result.outcome
    view = SessionView(
        record=record,
        series=SessionSeries(underlying=Series("SPY", None, (), SeriesReason.GONE), contracts=()),
        levels=levels_of(None, outcome),
        fills=fills_of(outcome.trades),
        marks=marks_of(outcome.trades),
    )

    sent = wire.OptionsBacktestSessionResponse.from_view(view)

    assert sent.setup_cannot_open_reason == THE_REASON
    assert (sent.status, sent.profit_usd, len(sent.trades), len(sent.events)) == ("traded", "-143.90", 3, 20)


def test_a_fill_is_sent_with_the_price_seen_apart_from_the_price_it_filled_at() -> None:
    fill = ChartedFill("entry", "buy", "call", 0, CALL, 1, OPEN_MS, D("2.00"), D("2.02"), None)

    sent = wire.OptionsBacktestFillResponse.from_fill(fill)

    assert (sent.seen_usd, sent.filled_usd) == ("2.00", "2.02")


def test_a_page_of_trades_says_its_total_where_it_began_and_where_the_rest_begins() -> None:
    trade = StoredTrade(OPEN_MS, 0, _sold())
    page = TradePage(run_id=7, total=5, offset=2, limit=2, trades=(trade, StoredTrade(OPEN_MS, 1, _marked())))

    sent = wire.OptionsBacktestTradesResponse.from_page(page)

    # Five trades, this page holds the 3rd and 4th (from offset 2), and the 5th is at offset 4.
    assert (sent.total, sent.offset, sent.next_offset) == (5, 2, 4)
    assert [t.trade_number for t in sent.trades] == [0, 1]


def test_two_runs_compared_are_sent_each_with_its_own_record_and_each_shared_session_with_each_runs_own_status() -> (
    None
):
    a = a_header(
        1,
        10,
        50,
        4,
        created_at_ms=1_791_800_000_000,
        setup_name="the first",
        fee_policy_text="Fees are $0.65 per contract traded.",
    )
    b = a_header(
        2,
        20,
        40,
        2,
        created_at_ms=1_791_900_000_000,
        setup_name=None,
        fee_policy_text="Fees are not charged, so results are before fees.",
        setup_changes={"slippage_percent": "0.5"},
    )
    T, K, S, N = SessionStatus.TRADED, SessionStatus.INCOMPLETE, SessionStatus.SKIPPED, SessionStatus.NO_TRADE
    # Sessions 20 and 30 are in both runs; 10 is only in a and 40 only in b. The two runs disagree about each shared one.
    a_lines = [SessionLine(10, T, D("1.00"), 1), SessionLine(20, T, D("-5.50"), 2), SessionLine(30, S, D("0"), 0)]
    b_lines = [SessionLine(20, S, D("0"), 0), SessionLine(30, K, D("2.25"), 3), SessionLine(40, N, D("0"), 0)]

    sent = wire.OptionsBacktestComparisonResponse.from_comparison(a, b, compare(a, a_lines, b, b_lines))

    # Each run's own record: never the other's, never blank.
    assert (sent.a.id, sent.a.setup_name, sent.a.created_at_ms) == (1, "the first", 1_791_800_000_000)
    assert (sent.b.id, sent.b.setup_name, sent.b.created_at_ms) == (2, None, 1_791_900_000_000)
    # Run a's range is 10 to 50 and b's is 20 to 40, in that order, each with its first before its last.
    assert sent.range_difference is not None
    assert (sent.range_difference.a.first_session_open_ms, sent.range_difference.a.last_session_open_ms) == (10, 50)
    assert (sent.range_difference.b.first_session_open_ms, sent.range_difference.b.last_session_open_ms) == (20, 40)
    # Shared: 20 (a traded -5.50, b skipped 0) and 30 (a skipped 0, b incomplete 2.25).
    assert [
        (s.session_open_ms, s.a_status, s.b_status, s.a_profit_usd, s.b_profit_usd) for s in sent.overlap.sessions
    ] == [(20, "traded", "skipped", "-5.50", "0"), (30, "skipped", "incomplete", "0", "2.25")]
    assert (sent.overlap.only_in_a, sent.overlap.only_in_b) == (1, 1)


def test_the_guard_that_nothing_sent_names_a_date_lets_through_only_the_words_of_an_event() -> None:
    names_the_day = "no strike is listed for 2026-10-09"
    refused = {"events": [{"kind": "pick_refused", "reason": "strike_not_listed", "detail": names_the_day}]}

    no_date_in(refused)  # an event's words are served as written
    for body in (
        {"events": [{"kind": "x", "reason": names_the_day}]},  # any other string of an event is held
        {"events": [{"detail": "fine", "leg_id": names_the_day}]},
        {"detail": names_the_day},  # a detail that is not an event's is held
        {"message": names_the_day},
        {"trades": [{"detail": names_the_day}]},
        {"events": names_the_day},
        {names_the_day: 1},  # and so is a key
    ):
        with pytest.raises(AssertionError, match="names a date"):
            no_date_in(body)
