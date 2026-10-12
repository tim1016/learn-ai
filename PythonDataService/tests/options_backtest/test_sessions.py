"""The kept week agrees with the calendar it is read from."""

from __future__ import annotations

from datetime import date, timedelta

from app.lean_sidecar.trading_calendar import expected_sessions
from app.options_backtest.sessions import is_session

_FIRST = date(2024, 1, 1)
_LAST = date(2027, 12, 31)


def _every_day() -> list[date]:
    return [_FIRST + timedelta(days=n) for n in range((_LAST - _FIRST).days + 1)]


def test_a_date_is_a_session_exactly_when_the_calendar_has_one() -> None:
    on_the_calendar = set(expected_sessions(_FIRST, _LAST))

    assert on_the_calendar
    assert {day for day in _every_day() if is_session(day)} == on_the_calendar

