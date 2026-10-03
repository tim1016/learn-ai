"""The calendar window the shared date picker reads (#2858)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.services.trading_calendar_service import MAX_CALENDAR_WINDOW_DAYS, calendar_window
from app.utils.session_anchors import LAST_SCHEDULABLE_DATE


def _utc_ms(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp() * 1000)


def test_calendar_window_lists_sessions_their_scheduled_closes_and_named_holidays() -> None:
    # Thanksgiving week 2024: Thursday is closed, Friday closes at 13:00 ET.
    window = calendar_window(date(2024, 11, 25), date(2024, 12, 2))

    assert window.session_open_ms == [_utc_ms(2024, 11, day, 14, 30) for day in (25, 26, 27, 29)] + [_utc_ms(2024, 12, 2, 14, 30)]
    assert window.session_close_ms == [
        _utc_ms(2024, 11, 25, 21),
        _utc_ms(2024, 11, 26, 21),
        _utc_ms(2024, 11, 27, 21),
        _utc_ms(2024, 11, 29, 18),
        _utc_ms(2024, 12, 2, 21),
    ]
    assert [(holiday.date_ms, "Thanksgiving" in holiday.name) for holiday in window.holidays] == [(_utc_ms(2024, 11, 28, 14, 30), True)]


def test_calendar_window_follows_daylight_time() -> None:
    window = calendar_window(date(2024, 7, 1), date(2024, 7, 1))

    assert window.session_open_ms == [_utc_ms(2024, 7, 1, 13, 30)]
    assert window.session_close_ms == [_utc_ms(2024, 7, 1, 20)]


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        (date(2024, 1, 2), date(2024, 1, 1), "ends before it starts"),
        (date(1980, 1, 1), date(2030, 1, 1), f"at most {MAX_CALENDAR_WINDOW_DAYS}"),
        (date(2262, 1, 1), date(2262, 12, 31), LAST_SCHEDULABLE_DATE.isoformat()),
    ],
)
def test_calendar_window_refuses_a_window_it_cannot_serve(start: date, end: date, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        calendar_window(start, end)
