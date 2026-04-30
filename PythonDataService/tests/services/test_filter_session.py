"""
Tests for filter_session at non-minute resolutions.

Regression: a 2-year SPY hourly RTH dataset returned 6 bars/day instead
of 7, and a daily RTH dataset returned zero rows. Root cause was that
filter_session only checked the bar's start-of-window timestamp against
[09:30, 16:00) — it didn't account for the bar's *duration*. The 09:00
hourly bar (window 09:00–10:00) contains the 09:30 RTH open and should
be kept; the 00:00 daily bar (window covers the whole trading day)
should also be kept; both were silently dropped.

The fix: include any bar whose trade window overlaps RTH on a weekday.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from app.services.dataset_service import filter_session

_ET = ZoneInfo("US/Eastern")


def _ms(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=_ET).timestamp() * 1000)


def test_hourly_rth_includes_09_00_bar_for_session_open():
    """The 09:00 ET hourly bar contains the 09:30 RTH open. RTH session
    must keep it — bars at 10:00 through 15:00 alone miss the open."""
    bars = [
        _ms(2025, 2, 3, 8, 0),   # 08:00 — pre-market only, drop
        _ms(2025, 2, 3, 9, 0),   # 09:00 — overlaps RTH (09:30 open), keep
        _ms(2025, 2, 3, 10, 0),  # keep
        _ms(2025, 2, 3, 15, 0),  # 15:00 — last fully-RTH hour, keep
        _ms(2025, 2, 3, 16, 0),  # 16:00 — entirely post-RTH, drop
    ]
    df = pd.DataFrame({"timestamp": bars, "close": [100.0] * len(bars)})

    out = filter_session(df, session="rth", timespan="hour", multiplier=1)

    kept_hours = pd.to_datetime(out["timestamp"], unit="ms", utc=True).dt.tz_convert(_ET).dt.hour.tolist()
    assert kept_hours == [9, 10, 15]


def test_hourly_rth_keeps_seven_bars_per_normal_day():
    """A typical RTH trading day produces 7 hourly bars at multiplier=1
    (09:00, 10:00, 11:00, 12:00, 13:00, 14:00, 15:00 ET)."""
    # All seven RTH-overlapping hours plus one pre-market and one post-market.
    bars = [
        _ms(2025, 2, 3, 8, 0),   # pre, drop
        _ms(2025, 2, 3, 9, 0),   # 09:00 — overlaps RTH at 09:30
        _ms(2025, 2, 3, 10, 0),
        _ms(2025, 2, 3, 11, 0),
        _ms(2025, 2, 3, 12, 0),
        _ms(2025, 2, 3, 13, 0),
        _ms(2025, 2, 3, 14, 0),
        _ms(2025, 2, 3, 15, 0),  # 15:00 — last hour fully inside RTH
        _ms(2025, 2, 3, 16, 0),  # post, drop
    ]
    df = pd.DataFrame({"timestamp": bars, "close": [100.0] * len(bars)})

    out = filter_session(df, session="rth", timespan="hour", multiplier=1)

    assert len(out) == 7


def test_hourly_rth_drops_weekend_overlap():
    """A Saturday 09:00 bar (would be 03:00 UTC if any) must still be
    excluded by the weekday mask — overlap with RTH would otherwise
    keep it."""
    sat = _ms(2025, 2, 8, 9, 0)  # 2025-02-08 is a Saturday
    df = pd.DataFrame({"timestamp": [sat], "close": [100.0]})

    out = filter_session(df, session="rth", timespan="hour", multiplier=1)

    assert len(out) == 0


def test_daily_rth_keeps_one_bar_per_weekday():
    """Daily bars are stamped at 00:00 ET — well before 09:30 — but
    cover the whole trading day. RTH must keep them on weekdays."""
    bars = [
        _ms(2025, 2, 3, 0, 0),   # Monday
        _ms(2025, 2, 4, 0, 0),   # Tuesday
        _ms(2025, 2, 5, 0, 0),   # Wednesday
        _ms(2025, 2, 8, 0, 0),   # Saturday — drop
        _ms(2025, 2, 9, 0, 0),   # Sunday — drop
    ]
    df = pd.DataFrame({"timestamp": bars, "close": [100.0] * len(bars)})

    out = filter_session(df, session="rth", timespan="day", multiplier=1)

    assert len(out) == 3


def test_minute_rth_unchanged_behavior():
    """Default (minute, 1) — must behave exactly like before the fix:
    [09:30, 16:00) on weekdays."""
    bars = [
        _ms(2025, 2, 3, 9, 29),  # one min before RTH — drop
        _ms(2025, 2, 3, 9, 30),  # 09:30 RTH open — keep
        _ms(2025, 2, 3, 15, 59),  # last RTH minute — keep
        _ms(2025, 2, 3, 16, 0),  # 16:00 — drop (RTH end is exclusive)
    ]
    df = pd.DataFrame({"timestamp": bars, "close": [100.0] * len(bars)})

    out = filter_session(df, session="rth", timespan="minute", multiplier=1)

    assert len(out) == 2
    kept = pd.to_datetime(out["timestamp"], unit="ms", utc=True).dt.tz_convert(_ET)
    kept_times = [(t.hour, t.minute) for t in kept]
    assert kept_times == [(9, 30), (15, 59)]


def test_extended_session_returns_unchanged():
    bars = [
        _ms(2025, 2, 3, 4, 0),
        _ms(2025, 2, 3, 12, 0),
        _ms(2025, 2, 3, 19, 59),
    ]
    df = pd.DataFrame({"timestamp": bars, "close": [100.0] * len(bars)})

    out = filter_session(df, session="extended", timespan="minute", multiplier=1)

    assert len(out) == 3
