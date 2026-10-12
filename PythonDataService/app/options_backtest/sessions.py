"""What the exchange calendar says about a date and its Monday-to-Sunday week, for the picker and the tags (#3030).

Formula: with ``S`` the sessions the canonical calendar has between a Monday and the Sunday after it:
    a date is a session      when the calendar has one on it
    a date ends its week     when it is the last of ``S`` for the week it falls in: a Friday, or a Thursday
                             when the Friday is closed
Reference: ``app/lean_sidecar/trading_calendar.py``, the calendar; and ``app/options/resolver.py``, whose
  ``_ends_its_week`` is the same fact for bots.
Canonical implementation: this file for the backtest; ``app/options/resolver.py`` for bots.
Validated against: tests/options_backtest/test_sessions.py (every date of four years against the calendar's
  own list of sessions), tests/options_backtest/test_picking_parity.py (the resolver and this file on every
  session of two years), and tests/options_backtest/test_picking.py and test_classification.py (holiday weeks).

Both ``picking.py`` and ``classification.py`` read these facts and neither reads the other. A week is read from
the calendar once and kept, so asking about every date of a long listing is not a calendar read for each.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

from app.lean_sidecar.trading_calendar import expected_sessions

_DAYS_IN_A_WEEK = 7


@lru_cache(maxsize=512)
def _sessions_of_week_from(monday: date) -> tuple[date, ...]:
    return tuple(expected_sessions(monday, monday + timedelta(days=_DAYS_IN_A_WEEK - 1)))


def sessions_of_week(day: date) -> tuple[date, ...]:
    """The sessions, oldest first, of the Monday-to-Sunday week ``day`` falls in."""
    return _sessions_of_week_from(day - timedelta(days=day.weekday()))


def is_session(day: date) -> bool:
    """Whether the calendar has a session on ``day``.

    The calendar's own ``is_trading_day`` gives the same answer and derives its schedule afresh on every
    call; a pick asks this of one date at every minute of a session, so it is answered from the kept week.
    """
    return day in sessions_of_week(day)


def ends_its_week(day: date) -> bool:
    """Whether ``day`` is the last session of its Monday-to-Sunday week: a Friday, or a Thursday when that is closed."""
    sessions = sessions_of_week(day)
    return bool(sessions) and sessions[-1] == day
