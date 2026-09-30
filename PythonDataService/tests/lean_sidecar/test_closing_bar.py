"""The closing-bar predicate (#2607): a bucket closing at its session's scheduled close.

Every close below is read from the canonical calendar; the test never states
a session time of its own.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.lean_sidecar import trading_calendar
from app.lean_sidecar.closing_bar import is_closing_bar

_MINUTE_MS = 60_000

REGULAR_DAY = date(2026, 2, 9)
HALF_DAY = date(2025, 11, 28)
# 2026-03-09 is the first session after the 2026-03-08 switch to daylight time.
FIRST_EDT_DAY = date(2026, 3, 9)


@pytest.mark.parametrize("day", [REGULAR_DAY, HALF_DAY, FIRST_EDT_DAY])
def test_a_bucket_closing_at_the_scheduled_close_is_the_closing_bar(day: date) -> None:
    assert is_closing_bar(trading_calendar.session_close_ms_utc(day))


def test_on_an_early_close_the_regular_close_instant_is_not_the_closing_bar() -> None:
    assert trading_calendar.is_early_close(HALF_DAY)
    half_day_close_ms = trading_calendar.session_close_ms_utc(HALF_DAY)
    # How much later a regular session closes, in minutes, per the calendar.
    shortfall_minutes = trading_calendar.session_close_minute_et(REGULAR_DAY) - trading_calendar.session_close_minute_et(
        HALF_DAY
    )

    assert shortfall_minutes > 0
    assert is_closing_bar(half_day_close_ms)
    assert not is_closing_bar(half_day_close_ms + shortfall_minutes * _MINUTE_MS)


@pytest.mark.parametrize("offset_ms", [-15 * _MINUTE_MS, -_MINUTE_MS, _MINUTE_MS, 15 * _MINUTE_MS])
def test_a_bucket_closing_any_other_instant_is_not_the_closing_bar(offset_ms: int) -> None:
    assert not is_closing_bar(trading_calendar.session_close_ms_utc(REGULAR_DAY) + offset_ms)


def test_an_instant_on_a_day_without_a_session_is_never_the_closing_bar() -> None:
    saturday_after = trading_calendar.session_close_ms_utc(date(2026, 2, 13)) + 24 * 60 * _MINUTE_MS
    thanksgiving = trading_calendar.session_close_ms_utc(date(2025, 11, 26)) + 24 * 60 * _MINUTE_MS

    assert not trading_calendar.is_trading_day(date(2026, 2, 14))
    assert not trading_calendar.is_trading_day(date(2025, 11, 27))
    assert not is_closing_bar(saturday_after)
    assert not is_closing_bar(thanksgiving)
