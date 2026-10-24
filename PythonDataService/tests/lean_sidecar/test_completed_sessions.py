from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.lean_sidecar.completed_sessions import latest_completed_sessions
from app.lean_sidecar.trading_calendar import NoSessionError, session_close_ms_utc

_ET = ZoneInfo("America/New_York")


def _et_ms(day: date, hour: int, minute: int = 0) -> int:
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=_ET).timestamp() * 1000)


def test_a_session_in_progress_is_not_completed() -> None:
    # Tuesday 2026-01-06, 11:00 ET: Monday the 5th is the latest completed session.
    sessions = latest_completed_sessions(_et_ms(date(2026, 1, 6), 11), 2)

    assert [window.session_date for window in sessions] == [date(2026, 1, 2), date(2026, 1, 5)]


def test_a_session_completes_at_its_scheduled_close_not_before() -> None:
    close_ms = session_close_ms_utc(date(2026, 1, 6))

    assert latest_completed_sessions(close_ms - 1, 1)[0].session_date == date(2026, 1, 5)
    assert latest_completed_sessions(close_ms, 1)[0].session_date == date(2026, 1, 6)


def test_a_weekend_and_a_holiday_are_skipped() -> None:
    # Tuesday 2026-01-20 morning follows MLK Day (Monday the 19th) and a weekend: Friday the 16th, then the 15th.
    sessions = latest_completed_sessions(_et_ms(date(2026, 1, 20), 8), 2)

    assert [window.session_date for window in sessions] == [date(2026, 1, 15), date(2026, 1, 16)]


def test_a_half_day_is_completed_at_its_early_close() -> None:
    # Black Friday 2026-11-27 closes at 13:00 ET: by 14:00 it is completed.
    sessions = latest_completed_sessions(_et_ms(date(2026, 11, 27), 14), 1)

    assert sessions[0].session_date == date(2026, 11, 27)
    assert sessions[0].close_ms_utc == _et_ms(date(2026, 11, 27), 13)


def test_a_request_the_lookback_cannot_serve_is_refused() -> None:
    with pytest.raises(NoSessionError):
        latest_completed_sessions(_et_ms(date(2026, 1, 6), 11), 50)


def test_a_nonsense_instant_or_count_is_refused() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        latest_completed_sessions(-1, 1)
    with pytest.raises(ValueError, match="at least 1"):
        latest_completed_sessions(_et_ms(date(2026, 1, 6), 11), 0)
