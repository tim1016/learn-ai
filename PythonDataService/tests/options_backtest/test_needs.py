"""Which contracts a session could read, worked out from the underlying and the listing alone.

Minutes are counted after the open. Every strike a scene expects is worked out by hand from the underlying's price at
the minute the pick is made, against a ladder of strikes 5 apart: the money is the nearest strike.
"""

from __future__ import annotations

from datetime import date

from app.lean_sidecar.trading_calendar import session_window_for_date
from app.options_backtest.needs import SessionNeeds, session_needs
from app.options_backtest.results import SkipReason
from app.options_backtest.setup import OptionsBacktestSetup
from tests.options_backtest._market import after_open, before_close, leg, setup
from tests.options_backtest._run_data import FRI, HALF_DAY, listing_on, steady, underlying_bars

LADDER = (490, 495, 500, 505, 510, 515, 520)
STOP_25 = ("percent", "25")


def _needs(
    written: OptionsBacktestSetup, prices: dict[int, str], *, day: date = FRI, strikes: tuple[int, ...] = LADDER
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


def test_a_leg_that_cannot_come_back_needs_only_the_contract_picked_at_the_entry_time() -> None:
    # Entry at minute 5 with the underlying at 500.40: the nearest strike is 500 (0.40 away; 495 is 5.40 away).
    # The underlying then goes to 506 (the 505 strike) and to 495.2 (the 495 strike), and none of that matters.
    prices = {**steady("506.0", 6, 59), 5: "500.40", 30: "495.2"}

    needs = _needs(setup(leg(stop=STOP_25)), prices)

    assert needs.skip_reason is None
    assert _read(needs) == [("call", 500)]


def test_a_leg_that_can_come_back_needs_the_contract_picked_at_every_minute_between_entry_and_exit() -> None:
    # Entry at minute 5, exit at minute 60. A re-entry starts at some minute 6 to 59 on which the underlying closed.
    # Minute 3 is before the entry and minutes 60 and 61 are at and after the exit: none of them can start one.
    prices = {
        3: "515.4",  # would pick 515, but it is before the entry
        5: "500.40",  # 500: 0.40 away
        6: "503.2",  # 505: 1.8 away against 3.2 to 500
        7: "497.3",  # 495: 2.3 away against 2.7 to 500
        30: "500.0",  # 500
        59: "509.0",  # 510: 1 away against 4 to 505
        60: "520.2",  # would pick 520, but nothing enters at the exit time
        61: "489.8",  # would pick 490, but it is after the exit
    }
    written = setup(leg(stop=STOP_25, reentry=("at_once", 1)))

    needs = _needs(written, prices)

    assert _read(needs) == [("call", 495), ("call", 500), ("call", 505), ("call", 510)]


def test_the_exit_time_counted_from_the_close_ends_the_minutes_a_re_entry_could_start_in() -> None:
    # Exit 10 minutes before the close: minute 380 on a full day, minute 200 on a half day (it closes at minute 210).
    written = setup(leg(stop=STOP_25, reentry=("at_once", 1)), exit=before_close(10))
    full_day = {5: "500.4", 379: "515.2", 380: "520.4"}  # 515 is picked at 379; 520 at 380 is the exit time
    half_day = {5: "500.4", 199: "515.2", 200: "520.4"}

    assert _read(_needs(written, full_day)) == [("call", 500), ("call", 515)]
    assert _read(_needs(written, half_day, day=HALF_DAY)) == [("call", 500), ("call", 515)]


def test_a_re_entry_rule_on_a_leg_with_no_stop_adds_nothing_when_a_stop_elsewhere_closes_only_that_leg() -> None:
    # Leg A could re-enter if it were closed, and under partial square-off only its own stop closes it, and it has none.
    prices = {5: "500.40", 6: "506.0", 7: "494.0"}
    written = setup(leg("a", reentry=("at_once", 1)), leg("b", "put", stop=STOP_25), square_off="partial")

    assert _read(_needs(written, prices)) == [("call", 500), ("put", 500)]


def test_a_complete_square_off_lets_a_leg_with_no_stop_come_back_when_another_leg_can_be_stopped() -> None:
    # Under complete, B's stop closes A too, and A has a re-entry rule: A picks at 506 (505) and 494 (495) as well.
    prices = {5: "500.40", 6: "506.0", 7: "494.0"}
    written = setup(leg("a", reentry=("at_once", 1)), leg("b", "put", stop=STOP_25), square_off="complete")

    assert _read(_needs(written, prices)) == [("call", 495), ("call", 500), ("call", 505), ("put", 500)]


def test_a_complete_square_off_with_no_stop_on_any_leg_closes_nothing_so_nothing_comes_back() -> None:
    prices = {5: "500.40", 6: "506.0", 7: "494.0"}
    written = setup(leg("a", reentry=("at_once", 1)), leg("b", "put"), square_off="complete")

    assert _read(_needs(written, prices)) == [("call", 500), ("put", 500)]


def test_each_leg_is_picked_by_its_own_right_and_strike_rule() -> None:
    # At 500.40 the money is 500; one strike below it is 495.
    written = setup(leg("a", "call"), {**leg("b", "put"), "strike": {"kind": "atm_offset", "steps": -1}})

    assert _read(_needs(written, {5: "500.40"})) == [("call", 500), ("put", 495)]


def test_a_leg_whose_rules_come_to_no_contract_needs_none_and_the_others_still_do() -> None:
    nothing_listed_that_far_out = {**leg("far"), "expiry": {"kind": "min_days", "days": 30}}

    needs = _needs(setup(nothing_listed_that_far_out, leg("near")), {5: "500.40"})

    assert needs.skip_reason is None
    assert _read(needs) == [("call", 500)]


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


def test_the_entry_pick_uses_the_underlying_at_the_entry_time_and_no_other_minute() -> None:
    # Minute 4 is 480.0 (the 490 strike) and minute 6 is 520.0 (520); only minute 5, at 507.6, picks: 505 is 2.6 away
    # and 510 is 2.4 away, so 510.
    assert _read(_needs(setup(leg()), {4: "480.0", 5: "507.6", 6: "520.0"})) == [("call", 510)]
