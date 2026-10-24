"""The New York date of each bar: the day a Signal study groups its bars by (#3522; ADR 0022).

Regime labels, walk-forward months and the per-bar date labels all need "which day is this bar on". The answer is
the exchange's day, not the UTC day. A bar at 19:00-19:59 ET is already the next UTC date in standard time, so under
extended hours (04:00-20:00 ET) a UTC bucket would move a session's evening bars onto the next day, and on a month's
last session onto the next month. Regular-hours bars (09:30-16:00 ET) and extended bars in daylight time sit on one
UTC date, so for them the two answers are the same.

The conversion goes through the zone, never a fixed offset, so it is right on either side of a clock change. The
result is for grouping inside one call; it is not stored or sent.
"""

from __future__ import annotations

import pandas as pd

from app.research.target import SESSION_TIMEZONE


def ny_session_dates(timestamps_ms: pd.Series) -> pd.Series:
    """The New York calendar date of each ``int64 ms UTC`` timestamp, keeping the series' index."""
    return pd.to_datetime(timestamps_ms, unit="ms", utc=True).dt.tz_convert(SESSION_TIMEZONE).dt.date
