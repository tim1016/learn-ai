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
import pytest

from app.services.dataset_service import filter_session

_ET = ZoneInfo("US/Eastern")


def _ms(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=_ET).timestamp() * 1000)


@pytest.mark.parametrize(
    ("session", "timespan", "bars", "expected_kept"),
    [
        # The 09:00 ET hourly bar contains the 09:30 RTH open. RTH session
        # must keep it — bars at 10:00 through 15:00 alone miss the open.
        pytest.param(
            "rth",
            "hour",
            (
                (2025, 2, 3, 8, 0),   # 08:00 — pre-market only, drop
                (2025, 2, 3, 9, 0),   # 09:00 — overlaps RTH (09:30 open), keep
                (2025, 2, 3, 10, 0),  # keep
                (2025, 2, 3, 15, 0),  # 15:00 — last fully-RTH hour, keep
                (2025, 2, 3, 16, 0),  # 16:00 — entirely post-RTH, drop
            ),
            ((9, 0), (10, 0), (15, 0)),
            id="hourly_rth_includes_09_00_bar_for_session_open",
        ),
        # A typical RTH trading day produces 7 hourly bars at multiplier=1
        # (09:00, 10:00, 11:00, 12:00, 13:00, 14:00, 15:00 ET).
        pytest.param(
            "rth",
            "hour",
            (
                (2025, 2, 3, 8, 0),   # pre, drop
                (2025, 2, 3, 9, 0),   # 09:00 — overlaps RTH at 09:30
                (2025, 2, 3, 10, 0),
                (2025, 2, 3, 11, 0),
                (2025, 2, 3, 12, 0),
                (2025, 2, 3, 13, 0),
                (2025, 2, 3, 14, 0),
                (2025, 2, 3, 15, 0),  # 15:00 — last hour fully inside RTH
                (2025, 2, 3, 16, 0),  # post, drop
            ),
            ((9, 0), (10, 0), (11, 0), (12, 0), (13, 0), (14, 0), (15, 0)),
            id="hourly_rth_keeps_seven_bars_per_normal_day",
        ),
        # A Saturday 09:00 bar (would be 03:00 UTC if any) must still be
        # excluded by the weekday mask — overlap with RTH would otherwise
        # keep it.
        pytest.param(
            "rth",
            "hour",
            ((2025, 2, 8, 9, 0),),  # 2025-02-08 is a Saturday
            (),
            id="hourly_rth_drops_weekend_overlap",
        ),
        # Daily bars are stamped at 00:00 ET — well before 09:30 — but
        # cover the whole trading day. RTH must keep them on weekdays.
        pytest.param(
            "rth",
            "day",
            (
                (2025, 2, 3, 0, 0),   # Monday
                (2025, 2, 4, 0, 0),   # Tuesday
                (2025, 2, 5, 0, 0),   # Wednesday
                (2025, 2, 8, 0, 0),   # Saturday — drop
                (2025, 2, 9, 0, 0),   # Sunday — drop
            ),
            ((0, 0), (0, 0), (0, 0)),
            id="daily_rth_keeps_one_bar_per_weekday",
        ),
        # Default (minute, 1) — must behave exactly like before the fix:
        # [09:30, 16:00) on weekdays.
        pytest.param(
            "rth",
            "minute",
            (
                (2025, 2, 3, 9, 29),   # one min before RTH — drop
                (2025, 2, 3, 9, 30),   # 09:30 RTH open — keep
                (2025, 2, 3, 15, 59),  # last RTH minute — keep
                (2025, 2, 3, 16, 0),   # 16:00 — drop (RTH end is exclusive)
            ),
            ((9, 30), (15, 59)),
            id="minute_rth_unchanged_behavior",
        ),
        pytest.param(
            "extended",
            "minute",
            ((2025, 2, 3, 4, 0), (2025, 2, 3, 12, 0), (2025, 2, 3, 19, 59)),
            ((4, 0), (12, 0), (19, 59)),
            id="extended_session_returns_unchanged",
        ),
    ],
)
def test_filter_session_keeps_the_bars_whose_window_overlaps_the_session(
    session: str,
    timespan: str,
    bars: tuple[tuple[int, int, int, int, int], ...],
    expected_kept: tuple[tuple[int, int], ...],
):
    stamps = [_ms(*bar) for bar in bars]
    df = pd.DataFrame({"timestamp": stamps, "close": [100.0] * len(stamps)})

    out = filter_session(df, session=session, timespan=timespan, multiplier=1)

    assert len(out) == len(expected_kept), "number of bars kept"
    kept = pd.to_datetime(out["timestamp"], unit="ms", utc=True).dt.tz_convert(_ET)
    assert [(t.hour, t.minute) for t in kept] == list(expected_kept), "kept bars, as ET (hour, minute)"
