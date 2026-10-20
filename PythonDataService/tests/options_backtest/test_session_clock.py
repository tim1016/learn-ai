"""The clock facts a description is read against: a full session's open and close, from the calendar (#3030)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.lean_sidecar.trading_calendar import NoSessionError, session_window_for_date
from app.options_backtest.session_clock import SessionClock, full_session_clock
from app.utils.timestamps import ny_datetime


def _noon_utc(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, 12, tzinfo=UTC).timestamp() * 1000)


def _clock_of(session_day: date) -> SessionClock:
    """What the calendar says one session's open and close are, as minutes past New York midnight."""
    window = session_window_for_date(session_day)
    opened, closed = ny_datetime(window.open_ms_utc), ny_datetime(window.close_ms_utc)
    return SessionClock(
        open_minute_et=opened.hour * 60 + opened.minute, close_minute_et=closed.hour * 60 + closed.minute
    )


@pytest.mark.parametrize(
    ("as_of", "session_day"),
    [
        # 2026-10-12 is a Monday and a full session; the clock is read from it.
        pytest.param(
            date(2026, 10, 12),
            date(2026, 10, 12),
            id="the_clock_is_the_open_and_close_of_a_full_session_the_calendar_names",
        ),
        pytest.param(date(2026, 10, 11), date(2026, 10, 9), id="a_weekend_is_walked_back_to_the_last_session"),
    ],
)
def test_the_clock_is_read_from_the_full_session_the_calendar_names(as_of: date, session_day: date) -> None:
    assert full_session_clock(as_of_ms=_noon_utc(as_of)) == _clock_of(session_day)


def test_an_early_close_is_not_a_full_session() -> None:
    # The Friday after Thanksgiving 2026 closes early, and Thanksgiving itself is shut.
    # Walking back from the Saturday passes both and lands on the Wednesday.
    early = _clock_of(date(2026, 11, 27))
    clock = full_session_clock(as_of_ms=_noon_utc(date(2026, 11, 28)))

    assert early.close_minute_et < clock.close_minute_et
    assert clock == _clock_of(date(2026, 11, 25))


def test_the_clock_is_the_same_either_side_of_a_clock_change() -> None:
    # Wall-clock minutes in New York do not move when the clocks do; a UTC-based read would.
    before_spring = full_session_clock(as_of_ms=_noon_utc(date(2026, 3, 7)))
    after_spring = full_session_clock(as_of_ms=_noon_utc(date(2026, 3, 9)))
    before_autumn = full_session_clock(as_of_ms=_noon_utc(date(2026, 10, 31)))
    after_autumn = full_session_clock(as_of_ms=_noon_utc(date(2026, 11, 2)))

    assert before_spring == after_spring == before_autumn == after_autumn


def test_no_full_session_within_reach_is_refused_not_guessed() -> None:
    # Sunday, with one day of reach: nothing but the weekend.
    with pytest.raises(NoSessionError):
        full_session_clock(as_of_ms=_noon_utc(date(2026, 10, 11)), lookback_days=1)
