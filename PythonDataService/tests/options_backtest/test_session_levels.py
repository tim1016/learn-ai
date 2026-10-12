"""The lines, fills and marks a one-day timeline draws, from a session's stored trades and events (#3030).

Every expectation is worked by hand in a comment. The busy Wednesday, the quiet Friday and the figures behind them are
in ``_detail_scene.py``; the small scenes below are replays of one leg or two, written out where they are used. A minute
is "m5": the bar closing five minutes after the open.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from app.options_backtest.results import EventKind, ExitReason, SessionOutcome
from app.options_backtest.run import RunResult, run_sessions
from app.options_backtest.session_levels import (
    ChartedFill,
    Level,
    LevelKind,
    SessionRecordInconsistent,
    fills_of,
    levels_of,
    marks_of,
)
from tests.options_backtest._detail_scene import (
    CALL_500,
    FRI_CALL_500,
    FRI_OPEN_MS,
    FRI_PUT_495,
    PUT_495,
    PUT_500,
    SESSIONS,
    WED_OPEN_MS,
    minute,
    the_scene,
    the_setup,
)
from tests.options_backtest._market import (
    TableMarket,
    at,
    contract,
    flat,
    leg,
    minute_of,
    picked,
    replay,
    setup,
    steady,
)
from tests.options_backtest._run_data import fake_data

D = Decimal
SP, MOM, STOP = LevelKind.STARTING_PRICE, LevelKind.MOMENTUM, LevelKind.STOP


@pytest.fixture(scope="module")
def scene() -> RunResult:
    return run_sessions(the_setup(), SESSIONS, fake_data(the_scene()))


def _lines(levels: tuple[Level, ...], open_ms: int) -> list[tuple[LevelKind, str, int, str, Decimal, int, int]]:
    """Each line as (kind, leg, entry number, contract, price, begins at minute, ends at minute)."""
    return [
        (
            each.kind,
            each.leg_id,
            each.entry_number,
            each.contract.occ_symbol,
            each.price,
            (each.from_ms - open_ms) // 60_000,
            (each.to_ms - open_ms) // 60_000,
        )
        for each in levels
    ]


# ── The busy Wednesday: a momentum entry, a stop, a re-entry and the exit time ──


def test_the_busy_day_draws_each_legs_starting_price_momentum_and_stop_between_the_minutes_they_applied(
    scene: RunResult,
) -> None:
    wednesday = scene.sessions[0]

    levels = levels_of(scene.setup, wednesday.outcome)

    # Worked from the scene: call 500 took 4.00 at m5 (level 4.00 x 1.25 = 5.00) and its buy was decided at m10; it filled
    # at m11 at 5.10, so its stop is 5.10 x 0.8 = 4.08 until the stop was hit at m20. Its re-entry took 3.80 at m21
    # (level 3.80 x 1.25 = 4.75), decided at m30, filled at m31 at 4.85: stop 4.85 x 0.8 = 3.88 until the exit time, m60.
    # The lag leg took 1.00 in the 495 put at m5 (level 1.00 x 0.9 = 0.90) and was cancelled at m60 without trading.
    # The put leg has no rule, so no line. Lines begin at m5 together: the call leg's first (leg 0), then the lag leg's.
    assert _lines(levels, WED_OPEN_MS) == [
        (SP, "call", 0, CALL_500, D("4.00"), 5, 10),
        (MOM, "call", 0, CALL_500, D("5.00"), 5, 10),
        (SP, "lag", 0, PUT_495, D("1.00"), 5, 60),
        (MOM, "lag", 0, PUT_495, D("0.90"), 5, 60),
        (STOP, "call", 0, CALL_500, D("4.08"), 11, 20),
        (SP, "call", 1, CALL_500, D("3.80"), 21, 30),
        (MOM, "call", 1, CALL_500, D("4.75"), 21, 30),
        (STOP, "call", 1, CALL_500, D("3.88"), 31, 60),
    ]


def test_a_leg_that_never_filled_has_its_momentum_level_worked_out_from_its_rule_and_its_starting_price(
    scene: RunResult,
) -> None:
    friday = scene.sessions[2]
    assert friday.outcome.trades == (), "Friday traded nothing: there is no trade to carry a level"

    levels = levels_of(scene.setup, friday.outcome)

    # The call took 4.00 at m5 and the momentum rule is 25 percent up: 4.00 x 1.25 = 5.00. The lag leg took 1.00 and
    # waits for a 10 percent fall: 1.00 x 0.9 = 0.90. Both legs waited until the exit time and were cancelled at m60.
    assert _lines(levels, FRI_OPEN_MS) == [
        (SP, "call", 0, FRI_CALL_500, D("4.00"), 5, 60),
        (MOM, "call", 0, FRI_CALL_500, D("5.00"), 5, 60),
        (SP, "lag", 0, FRI_PUT_495, D("1.00"), 5, 60),
        (MOM, "lag", 0, FRI_PUT_495, D("0.90"), 5, 60),
    ]


def test_a_session_skipped_for_want_of_an_underlying_price_has_no_line_no_fill_and_no_mark(scene: RunResult) -> None:
    thursday = scene.sessions[1]

    assert levels_of(scene.setup, thursday.outcome) == ()
    assert fills_of(thursday.outcome.trades) == ()
    assert marks_of(thursday.outcome.trades) == ()


def test_the_busy_day_marks_every_entry_and_sale_on_the_bar_it_filled_in_with_the_price_seen_and_the_price_filled(
    scene: RunResult,
) -> None:
    wednesday = scene.sessions[0]

    fills = fills_of(wednesday.outcome.trades)

    # In order of the bar: the put bought at m6 (2.00); call 0 bought at m11 (5.10) and sold at m21 (3.90, a stop);
    # call 1 bought at m31 (4.85); then at m61 the put (2.10) and call 1 (4.55) sold at the exit time. At m61 the put's
    # trade comes first because the replay recorded it first. No slippage: seen and filled are the same here.
    def row(fill: ChartedFill) -> tuple[object, ...]:
        return (
            fill.kind,
            fill.leg_id,
            fill.entry_number,
            fill.contract.occ_symbol,
            fill.contracts,
            (fill.bar_close_ms - WED_OPEN_MS) // 60_000,
            fill.seen,
            fill.filled,
            fill.exit_reason,
        )

    assert [row(fill) for fill in fills] == [
        ("entry", "put", 0, PUT_500, 1, 6, D("2.00"), D("2.00"), None),
        ("entry", "call", 0, CALL_500, 1, 11, D("5.10"), D("5.10"), None),
        ("exit", "call", 0, CALL_500, 1, 21, D("3.90"), D("3.90"), ExitReason.STOP),
        ("entry", "call", 1, CALL_500, 1, 31, D("4.85"), D("4.85"), None),
        ("exit", "put", 0, PUT_500, 1, 61, D("2.10"), D("2.10"), ExitReason.EXIT_TIME),
        ("exit", "call", 1, CALL_500, 1, 61, D("4.55"), D("4.55"), ExitReason.EXIT_TIME),
    ]
    assert marks_of(wednesday.outcome.trades) == ()
    # Every fill is dated by its bar: the instant of the minute it closed.
    assert fills[0].bar_close_ms == minute(WED_OPEN_MS, 6)


# ── Small scenes, each one thing ─────────────────────────────────────────

CALL = contract("call", "500")
PUT = contract("put", "500")


def _picks(*legs: tuple[str, object]) -> dict[tuple[str, str], object]:
    return picked(*[(leg_id, "500.00", option) for leg_id, option in legs])  # type: ignore[arg-type]


def test_a_stop_hit_on_the_very_bar_the_leg_filled_in_is_a_line_that_begins_and_ends_at_the_same_minute() -> None:
    written = setup(leg("call", stop=("percent", "20")))
    market = TableMarket(
        underlying=steady("500.00"),
        picks=_picks(("call", CALL)),
        # Decided at m5, filled at the open of m6, 5.00. That bar closes at 3.90, under the stop 5.00 x 0.8 = 4.000, so
        # the stop is hit at m6 itself; the sale fills at the open of m7, 3.80.
        options={CALL: {5: ("5.00", "5.00"), 6: ("5.00", "3.90"), 7: ("3.80", "3.80")}},
    )

    outcome = replay(written, market)

    [trade] = outcome.trades
    assert trade.exit_reason is ExitReason.STOP
    assert (trade.entry.bar_close_ms, trade.exit_decided_at_ms) == (at(6), at(6))
    [line] = levels_of(written, outcome)
    assert (line.kind, line.price, line.from_ms, line.to_ms) == (STOP, D("4.000"), at(6), at(6))
    assert [(f.kind, f.bar_close_ms, f.filled, f.exit_reason) for f in fills_of(outcome.trades)] == [
        ("entry", at(6), D("5.00"), None),
        ("exit", at(7), D("3.80"), ExitReason.STOP),
    ]


def test_a_leg_marked_and_not_sold_is_a_mark_and_its_entry_is_the_only_fill() -> None:
    written = setup(leg("call"))
    market = TableMarket(
        underlying=steady("500.00"),
        picks=_picks(("call", CALL)),
        # Bought at the open of m6 (2.00). The contract's last bar is m10, closing 2.10; the sale decided at the exit
        # time (m60) never finds a bar, so the leg is marked at that last close, at m10.
        options={CALL: {5: ("2.00", "2.00"), 6: ("2.00", "2.10"), **flat("2.10", 7, 10)}},
    )

    outcome = replay(written, market)

    [trade] = outcome.trades
    assert trade.exit_reason is ExitReason.MARKED_NOT_SOLD
    assert [(f.kind, f.bar_close_ms, f.filled) for f in fills_of(outcome.trades)] == [("entry", at(6), D("2.00"))]
    [mark] = marks_of(outcome.trades)
    assert (mark.leg_id, mark.entry_number, mark.contract, mark.bar_close_ms, mark.price) == (
        "call",
        0,
        CALL,
        at(10),
        D("2.10"),
    )


def test_a_buy_refused_for_want_of_cash_ends_the_legs_waiting_at_the_minute_the_buy_was_decided() -> None:
    written = setup(leg("call", momentum=("percent_up", "10")))
    market = TableMarket(
        underlying=steady("500.00"),
        picks=_picks(("call", CALL)),
        # The starting price at m5 is 2.00, so the level is 2.00 x 1.1 = 2.200. m8 closes at 2.30: reached, the buy is
        # decided. It would fill at the open of m9, 2.30, for 2.30 x 100 = 230.00, and the cash is 100.00: refused at m9.
        # The leg waited for the level, and the level was reached at m8, so its lines end at m8 as a filled leg's would;
        # the refusal a minute later moves nothing.
        options={CALL: {5: ("2.00", "2.00"), **flat("2.00", 6, 7), 8: ("2.30", "2.30"), 9: ("2.30", "2.30")}},
    )

    outcome = replay(written, market, cash="100")

    assert outcome.trades == () and EventKind.ENTRY_REFUSED_FOR_CASH in {e.kind for e in outcome.events}
    assert [
        (e.kind, minute_of(e.at_ms))
        for e in outcome.events
        if e.kind in (EventKind.MOMENTUM_HIT, EventKind.ENTRY_REFUSED_FOR_CASH)
    ] == [
        (EventKind.MOMENTUM_HIT, 8),
        (EventKind.ENTRY_REFUSED_FOR_CASH, 9),
    ]
    assert _lines(levels_of(written, outcome), at(0)) == [
        (SP, "call", 0, CALL.occ_symbol, D("2.00"), 5, 8),
        (MOM, "call", 0, CALL.occ_symbol, D("2.20"), 5, 8),
    ]


def test_a_buy_that_was_dropped_and_decided_again_before_it_was_refused_for_cash_ends_at_the_last_decision() -> None:
    written = setup(leg("call", momentum=("percent_up", "10")))
    market = TableMarket(
        underlying=steady("500.00"),
        picks=_picks(("call", CALL)),
        # Level 2.00 x 1.1 = 2.200. m8 closes at 2.30: decided. The contract does not trade in m9, so the buy is dropped
        # and the leg waits again. m10 closes at 2.30: decided again. It fills at the open of m11, 2.30, for 230.00
        # against 100.00 of cash: refused at m11. The waiting ended at the last decision, m10.
        options={
            CALL: {
                5: ("2.00", "2.00"),
                **flat("2.00", 6, 7),
                8: ("2.30", "2.30"),
                10: ("2.30", "2.30"),
                11: ("2.30", "2.30"),
            }
        },
    )

    outcome = replay(written, market, cash="100")

    assert [
        e.kind for e in outcome.events if e.kind in (EventKind.ENTRY_DROPPED, EventKind.ENTRY_REFUSED_FOR_CASH)
    ] == [
        EventKind.ENTRY_DROPPED,
        EventKind.ENTRY_REFUSED_FOR_CASH,
    ]
    assert _lines(levels_of(written, outcome), at(0)) == [
        (SP, "call", 0, CALL.occ_symbol, D("2.00"), 5, 10),
        (MOM, "call", 0, CALL.occ_symbol, D("2.20"), 5, 10),
    ]


def test_a_re_entry_refused_for_want_of_cash_ends_its_lines_at_the_minute_its_buy_was_decided_too() -> None:
    written = setup(
        leg("call", stop=("percent", "10"), momentum=("percent_up", "10"), reentry=("on_momentum", 1)),
    )
    market = TableMarket(
        underlying=steady("500.00"),
        picks=_picks(("call", CALL)),
        options={
            CALL: {
                # First entry: starting price 2.00 at m5, level 2.20; m8 closes 2.30, decided; fills at the open of m9,
                # 2.30, for 230.00 of the 300.00. Stop 2.30 x 0.9 = 2.070.
                5: ("2.00", "2.00"),
                **flat("2.00", 6, 7),
                8: ("2.30", "2.30"),
                9: ("2.30", "2.30"),
                # m10 closes at 2.00, under the stop: sold at the open of m11, 2.00, for 200.00. Cash 70.00 + 200.00 = 270.00.
                10: ("2.30", "2.00"),
                # The re-entry begins at m11: its close, 1.50, is the starting price, so the level is 1.50 x 1.1 = 1.650.
                # m14 closes 1.70, decided; m15 opens at 3.00, so the buy costs 300.00 against 270.00: refused at m15.
                11: ("2.00", "1.50"),
                **flat("1.50", 12, 13),
                14: ("1.50", "1.70"),
                15: ("3.00", "3.00"),
            }
        },
    )

    outcome = replay(written, market, cash="300")

    assert [(t.entry_number, minute_of(t.entry.bar_close_ms)) for t in outcome.trades] == [(0, 9)]
    assert [
        (e.kind, e.entry_number, minute_of(e.at_ms))
        for e in outcome.events
        if e.kind is EventKind.ENTRY_REFUSED_FOR_CASH
    ] == [(EventKind.ENTRY_REFUSED_FOR_CASH, 1, 15)]
    assert _lines(levels_of(written, outcome), at(0)) == [
        (SP, "call", 0, CALL.occ_symbol, D("2.00"), 5, 8),
        (MOM, "call", 0, CALL.occ_symbol, D("2.20"), 5, 8),
        (STOP, "call", 0, CALL.occ_symbol, D("2.070"), 9, 10),
        (SP, "call", 1, CALL.occ_symbol, D("1.50"), 11, 14),
        (MOM, "call", 1, CALL.occ_symbol, D("1.650"), 11, 14),
    ]


def test_a_leg_with_no_momentum_rule_whose_buy_is_refused_for_want_of_cash_waited_for_nothing_and_has_no_line() -> None:
    written = setup(leg("call"))
    market = TableMarket(
        underlying=steady("500.00"),
        picks=_picks(("call", CALL)),
        # Decided at the entry time (m5), filled at the open of m6, 2.00, for 200.00 against 100.00 of cash: refused.
        # No starting price was ever taken, so there is nothing that waited and no line, and nothing to be inconsistent.
        options={CALL: {5: ("2.00", "2.00"), 6: ("2.00", "2.00")}},
    )

    outcome = replay(written, market, cash="100")

    assert outcome.trades == () and EventKind.ENTRY_REFUSED_FOR_CASH in {e.kind for e in outcome.events}
    assert levels_of(written, outcome) == ()
    assert fills_of(outcome.trades) == () and marks_of(outcome.trades) == ()


def test_an_attempt_refused_for_cash_whose_momentum_was_never_reached_cannot_be_drawn() -> None:
    written = setup(leg("call", momentum=("percent_up", "10")))
    market = TableMarket(
        underlying=steady("500.00"),
        picks=_picks(("call", CALL)),
        options={CALL: {5: ("2.00", "2.00"), **flat("2.00", 6, 7), 8: ("2.30", "2.30"), 9: ("2.30", "2.30")}},
    )
    outcome = replay(written, market, cash="100")
    without_the_decision = replace(
        outcome, events=tuple(e for e in outcome.events if e.kind is not EventKind.MOMENTUM_HIT)
    )

    with pytest.raises(SessionRecordInconsistent, match="momentum was never reached"):
        levels_of(written, without_the_decision)


def test_a_complete_square_off_ends_the_waiting_of_a_leg_that_had_not_bought_at_the_minute_of_the_stop() -> None:
    written = setup(
        leg("a", stop=("percent", "10")),
        leg("b", "put", momentum=("percent_up", "50")),
        square_off="complete",
    )
    market = TableMarket(
        underlying=steady("500.00"),
        picks=_picks(("a", CALL), ("b", PUT)),
        options={
            # a: bought at the open of m6, 5.00; stop 5.00 x 0.9 = 4.500; m10 closes at 4.40, so the stop is hit at m10.
            CALL: {5: ("5.00", "5.00"), **flat("5.00", 6, 9), 10: ("5.00", "4.40"), 11: ("4.30", "4.30")},
            # b took 2.00 at m5 and wants 2.00 x 1.5 = 3.000; it never gets there and is cancelled by the square-off.
            PUT: flat("2.00", 5, 60),
        },
    )

    outcome = replay(written, market)

    assert [e.reason for e in outcome.events if e.kind is EventKind.LEG_CANCELLED] == ["square_off"]
    assert _lines(levels_of(written, outcome), at(0)) == [
        (SP, "b", 0, PUT.occ_symbol, D("2.00"), 5, 10),
        (MOM, "b", 0, PUT.occ_symbol, D("3.000"), 5, 10),
        (STOP, "a", 0, CALL.occ_symbol, D("4.500"), 6, 10),
    ]


# ── A stored session that does not agree with itself is not drawn from a guess ──


def _without(outcome: SessionOutcome, *, kind: EventKind, leg_id: str, entry_number: int) -> SessionOutcome:
    kept = tuple(
        event
        for event in outcome.events
        if not (event.kind is kind and event.leg_id == leg_id and event.entry_number == entry_number)
    )
    return replace(outcome, events=kept)


def test_a_trade_with_a_starting_price_that_no_event_took_cannot_be_drawn(scene: RunResult) -> None:
    broken = _without(scene.sessions[0].outcome, kind=EventKind.STARTING_PRICE_TAKEN, leg_id="call", entry_number=0)

    with pytest.raises(SessionRecordInconsistent, match="call"):
        levels_of(scene.setup, broken)


def test_an_attempt_that_waited_and_was_neither_cancelled_nor_refused_cannot_be_drawn(scene: RunResult) -> None:
    broken = _without(scene.sessions[2].outcome, kind=EventKind.LEG_CANCELLED, leg_id="lag", entry_number=0)

    with pytest.raises(SessionRecordInconsistent, match="lag"):
        levels_of(scene.setup, broken)


def test_a_starting_price_taken_twice_or_that_differs_from_the_trades_cannot_be_drawn(scene: RunResult) -> None:
    outcome = scene.sessions[0].outcome
    first = next(
        e
        for e in outcome.events
        if e.kind is EventKind.STARTING_PRICE_TAKEN and e.leg_id == "call" and e.entry_number == 0
    )
    twice = replace(outcome, events=(*outcome.events, first))
    other_price = replace(
        outcome,
        events=tuple(replace(e, price=D("4.10")) if e is first else e for e in outcome.events),
    )

    for broken in (twice, other_price):
        with pytest.raises(SessionRecordInconsistent):
            levels_of(scene.setup, broken)


def test_a_starting_price_on_a_leg_whose_setup_has_no_momentum_rule_cannot_be_drawn(scene: RunResult) -> None:
    friday = scene.sessions[2].outcome
    without_momentum = scene.setup.model_copy(
        update={
            "legs": tuple(
                each.model_copy(update={"momentum": None}) if each.leg_id == "lag" else each
                for each in scene.setup.legs
            )
        }
    )

    with pytest.raises(SessionRecordInconsistent, match="momentum rule"):
        levels_of(without_momentum, friday)


def test_a_fill_shows_the_price_seen_and_the_different_price_it_filled_at_after_slippage() -> None:
    written = setup(leg("call"), slippage="1")
    market = TableMarket(
        underlying=steady("500.00"),
        picks=_picks(("call", CALL)),
        # Bought at the open of m6, seen 2.00: 2.00 x 1.01 = 2.0200, which is on the step, so it fills at 2.02. Sold at
        # the open of m61, seen 2.50: 2.50 x 0.99 = 2.475, rounded down to the cent, 2.47.
        options={CALL: {**flat("2.00", 5, 60), 61: ("2.50", "2.50")}},
    )

    outcome = replay(written, market)

    assert [(f.kind, f.bar_close_ms, f.seen, f.filled, f.exit_reason) for f in fills_of(outcome.trades)] == [
        ("entry", at(6), D("2.00"), D("2.02"), None),
        ("exit", at(61), D("2.50"), D("2.47"), ExitReason.EXIT_TIME),
    ]


def test_an_attempt_that_never_filled_and_was_ended_twice_cannot_be_drawn(scene: RunResult) -> None:
    outcome = scene.sessions[2].outcome
    cancelled = next(e for e in outcome.events if e.kind is EventKind.LEG_CANCELLED and e.leg_id == "lag")
    twice = replace(outcome, events=(*outcome.events, replace(cancelled, at_ms=cancelled.at_ms + 60_000)))

    with pytest.raises(SessionRecordInconsistent, match="lag"):
        levels_of(scene.setup, twice)


# ── A run whose stored setup can no longer be read ───────────────────────


def test_with_no_setup_every_line_that_comes_from_a_stored_figure_is_still_drawn_and_only_an_unfilled_momentum_is_not(
    scene: RunResult,
) -> None:
    levels = levels_of(None, scene.sessions[0].outcome)

    # The busy Wednesday as drawn with the setup, less one line: the lag leg took 1.00 and never filled, so its momentum
    # level (1.00 x 0.9 = 0.90) comes from a rule that is lost. Both filled attempts carry their own momentum levels.
    assert _lines(levels, WED_OPEN_MS) == [
        (SP, "call", 0, CALL_500, D("4.00"), 5, 10),
        (MOM, "call", 0, CALL_500, D("5.00"), 5, 10),
        (SP, "lag", 0, PUT_495, D("1.00"), 5, 60),
        (STOP, "call", 0, CALL_500, D("4.08"), 11, 20),
        (SP, "call", 1, CALL_500, D("3.80"), 21, 30),
        (MOM, "call", 1, CALL_500, D("4.75"), 21, 30),
        (STOP, "call", 1, CALL_500, D("3.88"), 31, 60),
    ]
    # Friday filled nothing: only the two starting prices, no momentum line at all.
    assert _lines(levels_of(None, scene.sessions[2].outcome), FRI_OPEN_MS) == [
        (SP, "call", 0, FRI_CALL_500, D("4.00"), 5, 60),
        (SP, "lag", 0, FRI_PUT_495, D("1.00"), 5, 60),
    ]


def test_lines_that_begin_together_follow_the_legs_place_in_the_setup_and_with_no_setup_their_ids() -> None:
    # Two legs that wait for momentum they never get, written b before a. Both take a starting price at m5.
    b_then_a = setup(leg("b", momentum=("percent_up", "50")), leg("a", "put", momentum=("percent_up", "50")))
    market = TableMarket(
        underlying=steady("500.00"),
        picks=_picks(("b", CALL), ("a", PUT)),
        # b: 2.00, level 2.00 x 1.5 = 3.000. a: 3.00, level 3.00 x 1.5 = 4.500. Neither is reached; both are cancelled at m60.
        options={CALL: flat("2.00", 5, 60), PUT: flat("3.00", 5, 60)},
    )

    outcome = replay(b_then_a, market)

    assert [(each[0], each[1]) for each in _lines(levels_of(b_then_a, outcome), at(0))] == [
        (SP, "b"),
        (MOM, "b"),
        (SP, "a"),
        (MOM, "a"),
    ]
    assert [(each[0], each[1], each[4]) for each in _lines(levels_of(None, outcome), at(0))] == [
        (SP, "a", D("3.00")),
        (SP, "b", D("2.00")),
    ]
