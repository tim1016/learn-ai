"""Which contracts a session could read, worked out from the underlying and the listing alone.

Minutes are counted after the open. Every strike a scene expects is worked out by hand from the underlying's price at
the minute the pick is made, against a ladder of strikes 5 apart: the money is the nearest strike.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.lean_sidecar.trading_calendar import session_window_for_date
from app.options_backtest.needs import SessionNeeds, session_needs
from app.options_backtest.results import ExitReason, SkipReason
from app.options_backtest.setup import OptionsBacktestSetup
from tests.options_backtest._market import after_open, before_close, leg, setup
from tests.options_backtest._run_data import FRI, HALF_DAY, listing_on, steady, underlying_bars

LADDER = (490, 495, 500, 505, 510, 515, 520)
STOP_25 = ("percent", "25")


def _needs(
    written: OptionsBacktestSetup,
    prices: dict[int, str | tuple[str, str, str, str]],
    *,
    day: date = FRI,
    strikes: tuple[int, ...] = LADDER,
) -> SessionNeeds:
    window = session_window_for_date(day)
    return session_needs(
        written,
        trading_date=day,
        session_open_ms=window.open_ms_utc,
        session_close_ms=window.close_ms_utc,
        underlying=underlying_bars(day, prices),
        listing=listing_on(day, strikes=strikes),
    )


def _read(needs: SessionNeeds) -> list[tuple[str, int]]:
    """The contracts as (call or put, strike), lowest first."""
    return sorted((contract.right, int(contract.strike)) for contract in needs.contracts)


@pytest.mark.parametrize(
    ("written", "prices", "expected"),
    [
        pytest.param(
            setup(leg(stop=STOP_25)),
            # Entry at minute 5 with the underlying at 500.40: the nearest strike is 500 (0.40 away; 495 is 5.40 away).
            # The underlying then goes to 506 (the 505 strike) and to 495.2 (the 495 strike), and none of that matters.
            {**steady("506.0", 6, 59), 5: "500.40", 30: "495.2"},
            [("call", 500)],
            id="a leg that cannot come back needs only the contract picked at the entry time",
        ),
        pytest.param(
            setup(leg(stop=STOP_25, reentry=("at_once", 1))),
            # Entry at minute 5, exit at minute 60. A re-entry starts at some minute 6 to 59 on which the underlying
            # closed. Minute 3 is before the entry and minutes 60 and 61 are at and after the exit: none of them can
            # start one.
            {
                3: "515.4",  # would pick 515, but it is before the entry
                5: "500.40",  # 500: 0.40 away
                6: "503.2",  # 505: 1.8 away against 3.2 to 500
                7: "497.3",  # 495: 2.3 away against 2.7 to 500
                30: "500.0",  # 500
                59: "509.0",  # 510: 1 away against 4 to 505
                60: "520.2",  # would pick 520, but nothing enters at the exit time
                61: "489.8",  # would pick 490, but it is after the exit
            },
            [("call", 495), ("call", 500), ("call", 505), ("call", 510)],
            id="a leg that can come back needs the contract picked at every minute between entry and exit",
        ),
        pytest.param(
            # Under complete, B's stop closes A too, and A has a re-entry rule: A picks at 506 (505) and 494 (495) too.
            setup(leg("a", reentry=("at_once", 1)), leg("b", "put", stop=STOP_25), square_off="complete"),
            {5: "500.40", 6: "506.0", 7: "494.0"},
            [("call", 495), ("call", 500), ("call", 505), ("put", 500)],
            id="a complete square-off lets a leg with no stop come back when another leg can be stopped",
        ),
        pytest.param(
            # At 500.40 the money is 500; one strike below it is 495.
            setup(leg("a", "call"), {**leg("b", "put"), "strike": {"kind": "atm_offset", "steps": -1}}),
            {5: "500.40"},
            [("call", 500), ("put", 495)],
            id="each leg is picked by its own right and strike rule",
        ),
        pytest.param(
            setup({**leg("far"), "expiry": {"kind": "min_days", "days": 30}}, leg("near")),
            {5: "500.40"},
            [("call", 500)],
            id="a leg whose rules come to no contract needs none and the others still do",
        ),
        pytest.param(
            setup(leg()),
            # Minute 4 is 480.0 (the 490 strike) and minute 6 is 520.0 (520); only minute 5, at 507.6, picks: 505 is
            # 2.6 away and 510 is 2.4 away, so 510.
            {4: "480.0", 5: "507.6", 6: "520.0"},
            [("call", 510)],
            id="the entry pick uses the underlying at the entry time and no other minute",
        ),
        pytest.param(
            setup(leg(stop=STOP_25)),
            # Strikes are 490, 495, ... 520. The entry bar of minute 5 opened at 495.2 (the 495 strike), reached 512.6
            # (515: 2.4 away, against 2.6 to 510) and 489.0 (490, the lowest), and closed at 503.1 (505: 1.9 away,
            # against 3.1 to 500). Only the close, 505, is the underlying's price. The leg cannot come back, so
            # nothing else is picked.
            {5: ("495.2", "512.6", "489.0", "503.1")},
            [("call", 505)],
            id="the price a pick is made at is the bars close and not its open high or low",
        ),
        pytest.param(
            setup(leg(stop=STOP_25, reentry=("at_once", 1))),
            # The entry at minute 5 is flat at 507.6: 510 (2.4 away, against 2.6 to 505). Minute 6 opened at 497.4
            # (495: 2.4 away, against 2.6 to 500), reached 509.9 (510) and 492.0 (490: 2.0 away, against 3.0 to 495),
            # and closed at 501.0 (500). A re-entry starting then picks 500, so the needs are 500 and 510 and not 495,
            # 490 or a second 510.
            {5: "507.6", 6: ("497.4", "509.9", "492.0", "501.0")},
            [("call", 500), ("call", 510)],
            id="a re-entry minute is picked at its bars close and not its open high or low",
        ),
    ],
)
def test_the_contracts_a_session_needs_are_the_ones_its_rules_pick_at_the_minutes_they_pick(
    written: OptionsBacktestSetup, prices: dict[int, str | tuple[str, str, str, str]], expected: list[tuple[str, int]]
) -> None:
    needs = _needs(written, prices)

    assert needs.skip_reason is None, "the session is not skipped"
    assert _read(needs) == expected, "the contracts the session needs, as (call or put, strike)"


def test_the_exit_time_counted_from_the_close_ends_the_minutes_a_re_entry_could_start_in() -> None:
    # Exit 10 minutes before the close: minute 380 on a full day, minute 200 on a half day (it closes at minute 210).
    written = setup(leg(stop=STOP_25, reentry=("at_once", 1)), exit=before_close(10))
    full_day = {5: "500.4", 379: "515.2", 380: "520.4"}  # 515 is picked at 379; 520 at 380 is the exit time
    half_day = {5: "500.4", 199: "515.2", 200: "520.4"}

    assert _read(_needs(written, full_day)) == [("call", 500), ("call", 515)]
    assert _read(_needs(written, half_day, day=HALF_DAY)) == [("call", 500), ("call", 515)]


def test_an_incomplete_listing_needs_no_contract_and_is_not_a_skip() -> None:
    window = session_window_for_date(FRI)

    needs = session_needs(
        setup(leg()),
        trading_date=FRI,
        session_open_ms=window.open_ms_utc,
        session_close_ms=window.close_ms_utc,
        underlying=underlying_bars(FRI, {5: "500.40"}),
        listing=listing_on(FRI, complete=False),
    )

    assert needs == SessionNeeds(None, frozenset())


def test_a_window_that_does_not_fit_the_session_skips_it_and_needs_nothing_even_with_no_underlying() -> None:
    # Enter 190 minutes after the open, leave 30 before the close: minute 190 to 360 on a full day, which fits,
    # and 190 to 180 on a half day (it closes at minute 210), which does not.
    written = setup(leg(), entry=after_open(190), exit=before_close(30))

    on_a_half_day = _needs(written, {}, day=HALF_DAY)  # not even an underlying: the window is asked first
    on_a_full_day = _needs(written, {190: "500.4"})

    assert on_a_half_day == SessionNeeds(SkipReason.WINDOW_DOES_NOT_FIT, frozenset())
    assert on_a_full_day.skip_reason is None
    assert _read(on_a_full_day) == [("call", 500)]


def test_a_session_whose_underlying_did_not_trade_at_the_entry_time_is_skipped_and_needs_nothing() -> None:
    around_it = {4: "500.4", 6: "500.4", 7: "500.4"}  # no bar closes at minute 5

    assert _needs(setup(leg()), around_it) == SessionNeeds(SkipReason.UNDERLYING_DID_NOT_TRADE, frozenset())
    assert _needs(setup(leg()), {}) == SessionNeeds(SkipReason.UNDERLYING_DID_NOT_TRADE, frozenset())


def test_a_new_way_to_close_a_leg_must_be_weighed_in_what_a_session_needs() -> None:
    assert set(ExitReason) == {
        ExitReason.STOP,
        ExitReason.SQUARE_OFF,
        # A sold leg taken along by the stop on the bought leg that protects it: ``stop_can_close`` says a stop on a
        # bought leg of its kind reaches it, and the sessions the property test generates that sell reach it (#3107).
        ExitReason.CLOSED_WITH_ITS_PROTECTION,
        ExitReason.EXIT_TIME,
        ExitReason.MARKED_NOT_SOLD,
        # A mark is how a session ends for a leg that was not closed. It decides nothing, so it is not a way to close.
        ExitReason.MARKED_NOT_BOUGHT_BACK,
    }, (
        "the replay's ways of closing a leg changed. ``needs._can_come_back`` reasons from them: only a stop on the "
        "leg itself, a stop on a bought leg that takes it along as its protection, or a complete square-off with a "
        "stop on some leg, closes a leg that can start a re-entry, and "
        "the contracts a session needs follow from that. A new way to close a leg (a target, a trailing stop) may "
        "make a leg come back that ``_can_come_back`` says cannot, and then a session's needs are short and the "
        "run fails. Revisit ``_can_come_back``, extend the property test's generator to the new rule, and then "
        "change this list."
    )
