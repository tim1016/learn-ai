"""Daily deployment eligibility follows the exchange calendar and exact entry instant."""

from datetime import date

from app.lean_sidecar.trading_calendar import session_window_for_date
from app.options_bots.schedule import first_eligible_session
from tests.options_backtest._market import after_open, leg, setup


def test_entry_instant_is_eligible_but_one_millisecond_late_waits_until_monday() -> None:
    friday = session_window_for_date(date(2026, 10, 9))
    written = setup(leg())
    entry = friday.open_ms_utc + 300_000
    assert first_eligible_session(written, at_ms=entry).entry_ms == entry
    monday = session_window_for_date(date(2026, 10, 12))
    following = first_eligible_session(written, at_ms=entry + 1)
    assert following.open_ms == monday.open_ms_utc


def test_calendar_holiday_and_setup_that_does_not_fit_an_early_close_are_skipped() -> None:
    wednesday = session_window_for_date(date(2026, 11, 25))
    monday = session_window_for_date(date(2026, 11, 30))
    written = setup(leg(), exit=after_open(300))
    next_session = first_eligible_session(written, at_ms=wednesday.close_ms_utc)
    assert next_session.open_ms == monday.open_ms_utc
    assert next_session.exit_ms == monday.open_ms_utc + 300 * 60_000


def test_next_session_utc_time_follows_the_daylight_saving_calendar() -> None:
    friday = session_window_for_date(date(2026, 10, 30))
    monday = session_window_for_date(date(2026, 11, 2))
    following = first_eligible_session(setup(leg()), at_ms=friday.close_ms_utc)
    assert following.open_ms == monday.open_ms_utc
    assert following.open_ms - friday.open_ms_utc == (3 * 24 + 1) * 60 * 60_000
