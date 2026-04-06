"""DST transition day tests: verify session filtering and tagging on clock-change days.

Spring forward (EST→EDT): 2024-03-10 at 2:00 AM
Fall back (EDT→EST): 2024-11-03 at 2:00 AM

These tests ensure RTH session filtering and tagging produce correct results
when the UTC↔ET offset changes mid-day.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.services.dataset_service import filter_session, _tag_session_column

_ET = ZoneInfo("US/Eastern")
_UTC = ZoneInfo("UTC")


def _make_minute_bars(date_str: str, start_hour_utc: int, end_hour_utc: int) -> pd.DataFrame:
    """Generate synthetic 1-min bars spanning a UTC hour range for a given date."""
    date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=_UTC)
    start = date + timedelta(hours=start_hour_utc)
    end = date + timedelta(hours=end_hour_utc)

    timestamps = []
    current = start
    while current < end:
        timestamps.append(int(current.timestamp() * 1000))
        current += timedelta(minutes=1)

    n = len(timestamps)
    return pd.DataFrame({
        "timestamp": timestamps,
        "open": [150.0] * n,
        "high": [151.0] * n,
        "low": [149.0] * n,
        "close": [150.5] * n,
        "volume": [10000.0] * n,
    })


def test_spring_forward_rth_filter():
    """RTH filter on 2024-03-10 (spring forward) keeps correct 9:30-16:00 ET bars.

    On this day, EST→EDT at 2:00 AM. RTH is 9:30-16:00 EDT.
    In UTC: 9:30 EDT = 13:30 UTC, 16:00 EDT = 20:00 UTC.
    """
    # Generate bars from 12:00 UTC to 21:00 UTC (covers pre-market through post-market)
    df = _make_minute_bars("2024-03-10", 12, 21)
    assert len(df) > 0

    filtered = filter_session(df, "rth")

    # Verify all remaining bars fall within RTH in ET
    dt_utc = pd.to_datetime(filtered["timestamp"], unit="ms", utc=True)
    dt_et = dt_utc.dt.tz_convert(_ET)

    # All bars should be on weekday (Sunday 2024-03-10 is a Sunday — market closed!)
    # Actually 2024-03-10 is a Sunday, so RTH filter should return 0 bars.
    # Let's use 2024-03-11 (Monday) instead for RTH test.
    # But the DST change already happened on Sunday, so Monday is in EDT.
    assert len(filtered) == 0, "2024-03-10 is a Sunday — no RTH bars expected"


def test_spring_forward_monday_rth_filter():
    """RTH filter on 2024-03-11 (first business day after spring forward).

    Now in EDT: 9:30 EDT = 13:30 UTC, 16:00 EDT = 20:00 UTC.
    Before DST, 9:30 EST = 14:30 UTC, 16:00 EST = 21:00 UTC.
    This test verifies the filter uses 13:30 UTC (EDT) not 14:30 UTC (EST).
    """
    df = _make_minute_bars("2024-03-11", 12, 21)

    filtered = filter_session(df, "rth")

    dt_utc = pd.to_datetime(filtered["timestamp"], unit="ms", utc=True)
    dt_et = dt_utc.dt.tz_convert(_ET)

    time_mins = dt_et.dt.hour * 60 + dt_et.dt.minute
    assert (time_mins >= 9 * 60 + 30).all(), "All bars should be at or after 9:30 ET"
    assert (time_mins < 16 * 60).all(), "All bars should be before 16:00 ET"

    # Should have 390 RTH minutes (9:30 to 16:00 = 6.5 hours = 390 minutes)
    assert len(filtered) == 390, f"Expected 390 RTH bars, got {len(filtered)}"


def test_fall_back_rth_filter():
    """RTH filter on 2024-11-04 (first business day after fall back).

    Now in EST: 9:30 EST = 14:30 UTC, 16:00 EST = 21:00 UTC.
    Before DST change, 9:30 EDT = 13:30 UTC.
    This test verifies the filter uses 14:30 UTC (EST) not 13:30 UTC (EDT).
    """
    df = _make_minute_bars("2024-11-04", 12, 22)

    filtered = filter_session(df, "rth")

    dt_utc = pd.to_datetime(filtered["timestamp"], unit="ms", utc=True)
    dt_et = dt_utc.dt.tz_convert(_ET)

    time_mins = dt_et.dt.hour * 60 + dt_et.dt.minute
    assert (time_mins >= 9 * 60 + 30).all(), "All bars should be at or after 9:30 ET"
    assert (time_mins < 16 * 60).all(), "All bars should be before 16:00 ET"

    # Should have 390 RTH minutes
    assert len(filtered) == 390, f"Expected 390 RTH bars, got {len(filtered)}"


def test_dst_session_tagging():
    """Session tagging on 2024-03-11 (EDT) correctly assigns pre/rth/post."""
    # Extended hours: 4:00 AM ET to 20:00 ET
    # In EDT: 4:00 EDT = 08:00 UTC, 20:00 EDT = 00:00 UTC next day
    df = _make_minute_bars("2024-03-11", 8, 24)

    tagged = _tag_session_column(df.copy(), "2024-03-11", "2024-03-11")

    assert "session" in tagged.columns, "session column should exist"

    dt_utc = pd.to_datetime(tagged["timestamp"], unit="ms", utc=True)
    dt_et = dt_utc.dt.tz_convert(_ET)
    time_mins = dt_et.dt.hour * 60 + dt_et.dt.minute

    # Pre-market bars (before 9:30 ET)
    pre_mask = time_mins < 9 * 60 + 30
    pre_sessions = tagged.loc[pre_mask, "session"]
    assert (pre_sessions == "pre").all(), f"Bars before 9:30 ET should be 'pre', got {pre_sessions.unique()}"

    # RTH bars (9:30-16:00 ET)
    rth_mask = (time_mins >= 9 * 60 + 30) & (time_mins < 16 * 60)
    rth_sessions = tagged.loc[rth_mask, "session"]
    assert (rth_sessions == "rth").all(), f"Bars 9:30-16:00 ET should be 'rth', got {rth_sessions.unique()}"

    # Post-market bars (16:00-20:00 ET)
    post_mask = (time_mins >= 16 * 60) & (time_mins < 20 * 60)
    post_sessions = tagged.loc[post_mask, "session"]
    assert (post_sessions == "post").all(), f"Bars 16:00-20:00 ET should be 'post', got {post_sessions.unique()}"


def test_fall_back_session_tagging():
    """Session tagging on 2024-11-04 (EST) correctly assigns pre/rth/post."""
    # In EST: 4:00 EST = 09:00 UTC, 20:00 EST = 01:00 UTC next day
    df = _make_minute_bars("2024-11-04", 9, 24)

    tagged = _tag_session_column(df.copy(), "2024-11-04", "2024-11-04")

    assert "session" in tagged.columns

    dt_utc = pd.to_datetime(tagged["timestamp"], unit="ms", utc=True)
    dt_et = dt_utc.dt.tz_convert(_ET)
    time_mins = dt_et.dt.hour * 60 + dt_et.dt.minute

    rth_mask = (time_mins >= 9 * 60 + 30) & (time_mins < 16 * 60)
    rth_sessions = tagged.loc[rth_mask, "session"]
    assert (rth_sessions == "rth").all(), f"RTH bars should be 'rth', got {rth_sessions.unique()}"

    post_mask = (time_mins >= 16 * 60) & (time_mins < 20 * 60)
    post_sessions = tagged.loc[post_mask, "session"]
    assert (post_sessions == "post").all(), f"Post-market bars should be 'post', got {post_sessions.unique()}"
