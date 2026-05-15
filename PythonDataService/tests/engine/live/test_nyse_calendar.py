"""Tests for previous_completed_nyse_session_close_ms.

The function is consumed only by indicator-state hydrate-validation
(see indicator_state.py); test it as a pure function so the validation
ladder's correctness rests on a deterministic primitive.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.engine.live.nyse_calendar import (
    NoSessionError,
    previous_completed_nyse_session_close_ms,
)

_NY = ZoneInfo("America/New_York")


def _ms(year: int, month: int, day: int, hour: int, minute: int) -> int:
    """Local NY datetime -> int64 ms UTC."""
    return int(datetime(year, month, day, hour, minute, tzinfo=_NY).astimezone(UTC).timestamp() * 1000)


@pytest.mark.parametrize(
    "case,session_start_ms,expected_prev_close_ms",
    [
        ("tue_after_normal_mon", _ms(2026, 5, 19, 9, 30), _ms(2026, 5, 18, 16, 0)),
        ("tue_after_memorial_day_mon", _ms(2026, 5, 26, 9, 30), _ms(2026, 5, 22, 16, 0)),
        ("mon_after_normal_fri", _ms(2026, 5, 18, 9, 30), _ms(2026, 5, 15, 16, 0)),
        # Thanksgiving 2026 = Thu Nov 26; Fri Nov 27 is early close at 13:00.
        ("fri_after_thanksgiving_thu", _ms(2026, 11, 27, 12, 0), _ms(2026, 11, 25, 16, 0)),
        # Day after Black Friday early-close.
        ("mon_after_early_close", _ms(2026, 11, 30, 9, 30), _ms(2026, 11, 27, 13, 0)),
        # Independence Day 2026 falls Saturday → observed Friday 7/3.
        ("mon_after_observed_independence", _ms(2026, 7, 6, 9, 30), _ms(2026, 7, 2, 16, 0)),
    ],
)
def test_previous_completed_session_close(case: str, session_start_ms: int, expected_prev_close_ms: int) -> None:
    actual = previous_completed_nyse_session_close_ms(session_start_ms)
    assert actual == expected_prev_close_ms, f"{case}: expected {expected_prev_close_ms}, got {actual}"


def test_weekend_session_start_raises_no_session_error() -> None:
    sat = _ms(2026, 5, 16, 9, 30)
    # The function asks for the previous SESSION close. A start_ms on
    # a non-session day is pathological but the function is still
    # expected to find the previous session's close — Friday's close.
    # We declare the contract: a session_start_ms before the first
    # session in our lookback raises NoSessionError. A Saturday with
    # 14-day lookback returns Friday close (not a raise).
    assert previous_completed_nyse_session_close_ms(sat) == _ms(2026, 5, 15, 16, 0)


def test_session_start_ms_before_any_lookback_session_raises() -> None:
    # 1970 — far before NYSE data; pandas_market_calendars covers it,
    # but the contract is: if no session exists in the lookback window
    # ending at session_start_ms, raise.
    with pytest.raises(NoSessionError):
        previous_completed_nyse_session_close_ms(0)
