"""A full session's open and close as New York wall-clock minutes (#3030).

A setup's times are distances from a session's open or close. To say them to
the owner as a clock time ("09:35 on a full day") the page needs the open and
close of a full session, and the calendar is the only place that knows them.
They are read from the latest session that is a trading day and not an early
close. Wall-clock minutes in New York do not move with a clock change, so the
answer is the same all year.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from app.lean_sidecar.trading_calendar import (
    NoSessionError,
    is_early_close,
    is_trading_day,
    session_window_for_date,
)
from app.utils.session_anchors import et_date_at_ms
from app.utils.timestamps import ny_datetime

# Early closes and holidays come in ones and twos; two weeks always holds a full session.
_REACH_DAYS = 14


@dataclass(frozen=True)
class SessionClock:
    """A full session's open and close, as minutes past New York midnight."""

    open_minute_et: int
    close_minute_et: int


def _minute_of_day(instant_ms: int) -> int:
    local = ny_datetime(instant_ms)
    return local.hour * 60 + local.minute


def full_session_clock(*, as_of_ms: int, lookback_days: int = _REACH_DAYS) -> SessionClock:
    """The clock of the latest full session on or before the New York date of ``as_of_ms``.

    Looks back over ``lookback_days`` calendar days, that date included, and
    refuses with ``NoSessionError`` when none of them is a full session.
    """
    latest = et_date_at_ms(as_of_ms)
    for days_back in range(lookback_days):
        day = latest - timedelta(days=days_back)
        if is_trading_day(day) and not is_early_close(day):
            window = session_window_for_date(day)
            return SessionClock(
                open_minute_et=_minute_of_day(window.open_ms_utc),
                close_minute_et=_minute_of_day(window.close_ms_utc),
            )
    raise NoSessionError(f"no full NYSE session in the {lookback_days} days up to {latest.isoformat()}")
