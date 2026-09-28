"""The ET calendar day that contains one instant."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.broker.alpaca.clerk.et_day import ActivityPeriod, activity_period_start_ms, et_day_window_ms
from app.services.session_authority import et_minute_of_day_ms
from app.utils.session_anchors import et_midnight_ms


def test_the_window_is_the_et_calendar_day_containing_the_instant() -> None:
    noon_et = et_minute_of_day_ms(date(2026, 9, 8), 12 * 60)
    start, end = et_day_window_ms(noon_et)
    assert start == et_midnight_ms(date(2026, 9, 8))
    assert end == et_midnight_ms(date(2026, 9, 9))
    # 23:30 ET on 2026-09-08 is 03:30 UTC on 2026-09-09 and still the ET 8th.
    late = et_minute_of_day_ms(date(2026, 9, 8), 23 * 60 + 30)
    assert et_day_window_ms(late) == (start, end)


# Hand-counted oracles (NYSE 2026): Labor Day is Mon 2026-09-07, Independence
# Day is observed Fri 2026-07-03 and Juneteenth falls on Fri 2026-06-19.
@pytest.mark.parametrize(
    ("period", "now", "opens_on"),
    [
        # Today is the ET day of the instant, session or not.
        ("today", et_minute_of_day_ms(date(2026, 9, 8), 12 * 60), date(2026, 9, 8)),
        ("today", et_minute_of_day_ms(date(2026, 9, 12), 10), date(2026, 9, 12)),
        # 30 sessions ending Tue 2026-09-08 skip Labor Day: Tue 2026-07-28 is the 30th.
        ("30d", et_minute_of_day_ms(date(2026, 9, 8), 12 * 60), date(2026, 7, 28)),
        # A Monday holiday is not a session: Fri the 4th is the newest, so one
        # session further back than from Tuesday.
        ("30d", et_minute_of_day_ms(date(2026, 9, 7), 12 * 60), date(2026, 7, 27)),
        # 60 sessions also skip Independence Day (observed) and Juneteenth.
        ("60d", et_minute_of_day_ms(date(2026, 9, 8), 12 * 60), date(2026, 6, 12)),
    ],
)
def test_activity_periods_open_at_the_et_midnight_the_calendar_names(
    period: ActivityPeriod, now: int, opens_on: date
) -> None:
    assert activity_period_start_ms(period, now) == et_midnight_ms(opens_on)


def test_a_period_across_the_dst_change_opens_at_eastern_daylight_midnight() -> None:
    # Now is in EST (after 2026-11-01); the 30th session back, Wed 2026-09-30,
    # is in EDT, so its midnight is 04:00 UTC -- never a fixed -05:00 offset.
    start = activity_period_start_ms("30d", et_minute_of_day_ms(date(2026, 11, 10), 12 * 60))
    assert start == et_midnight_ms(date(2026, 9, 30))
    assert datetime.fromtimestamp(start / 1000, tz=UTC) == datetime(2026, 9, 30, 4, tzinfo=UTC)
