"""The canonical NYSE calendar as the window a date picker needs.

A projection only: every session, close and holiday comes from
``app.lean_sidecar.trading_calendar`` (ADR 0022).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from functools import lru_cache

from app.lean_sidecar.trading_calendar import holiday_names_in_range, session_open_ms_utc, session_windows_ms_utc
from app.schemas.trading_calendar import CalendarHoliday, TradingCalendarWindow
from app.utils.session_anchors import LAST_SCHEDULABLE_DATE

#: One request covers every year a picker can reach; a wider window is a caller's mistake.
MAX_CALENDAR_WINDOW_DAYS = 40 * 366


@lru_cache(maxsize=8)
def calendar_window(start: date, end: date) -> TradingCalendarWindow:
    """Sessions and holidays in ``[start, end]``; raises ``ValueError`` for a window the calendar cannot serve."""
    if end < start:
        raise ValueError("The window ends before it starts.")
    if end > LAST_SCHEDULABLE_DATE:
        raise ValueError(f"The NYSE calendar cannot schedule past {LAST_SCHEDULABLE_DATE.isoformat()}.")
    span_days = (end - start).days + 1
    if span_days > MAX_CALENDAR_WINDOW_DAYS:
        raise ValueError(f"The window is {span_days} days; at most {MAX_CALENDAR_WINDOW_DAYS} are served at once.")
    sessions = session_windows_ms_utc(start, end)
    holidays = holiday_names_in_range(start, end)
    return TradingCalendarWindow(
        session_open_ms=[session.open_ms_utc for session in sessions],
        session_close_ms=[session.close_ms_utc for session in sessions],
        holidays=[CalendarHoliday(date_ms=session_open_ms_utc(day), name=name) for day, name in sorted(holidays.items())],
    )


CalendarWindowFn = Callable[[date, date], TradingCalendarWindow]


def get_calendar_window() -> CalendarWindowFn:
    """The calendar projection as a FastAPI dependency, so a transport test can replace it."""
    return calendar_window
