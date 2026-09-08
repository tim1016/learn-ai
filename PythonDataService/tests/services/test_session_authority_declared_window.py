"""The broker's declared window resolves PRE/RTH/POST around the calendar's regular session."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.broker.contract.capabilities import ExtendedHoursWindow
from app.lean_sidecar.trading_calendar import session_close_ms_utc, session_open_ms_utc
from app.services.session_authority import (
    et_minute_of_day_ms,
    extended_session_bounds_ms,
    session_state_at_ms,
)
from app.utils.timestamps import to_ms_utc

_ET = ZoneInfo("America/New_York")
_WINDOW = ExtendedHoursWindow(open_minute_et=4 * 60, close_minute_et=20 * 60)
_REGULAR = date(2026, 9, 2)  # Wednesday
_EARLY = date(2026, 11, 27)  # 13:00 ET close
_SUNDAY = date(2026, 9, 6)


def _et(d: date, hour: int, minute: int) -> int:
    return to_ms_utc(datetime(d.year, d.month, d.day, hour, minute, tzinfo=_ET))


@pytest.mark.parametrize(
    ("day", "expected_utc_hour"),
    [(date(2026, 3, 6), 9), (date(2026, 3, 9), 8)],  # EST Friday, EDT Monday around 2026-03-08
)
def test_et_minute_of_day_ms_follows_dst(day: date, expected_utc_hour: int) -> None:
    ms = et_minute_of_day_ms(day, 4 * 60)
    assert datetime.fromtimestamp(ms / 1000, tz=ZoneInfo("UTC")).hour == expected_utc_hour


def test_extended_bounds_enclose_the_regular_session() -> None:
    bounds = extended_session_bounds_ms(_REGULAR, window=_WINDOW)
    assert bounds.open_ms == _et(_REGULAR, 4, 0)
    assert bounds.rth_open_ms == session_open_ms_utc(_REGULAR)
    assert bounds.rth_close_ms == session_close_ms_utc(_REGULAR)
    assert bounds.close_ms == _et(_REGULAR, 20, 0)


def test_extended_bounds_refuse_a_non_trading_day() -> None:
    with pytest.raises(ValueError, match="not a trading day"):
        extended_session_bounds_ms(_SUNDAY, window=_WINDOW)


def test_extended_bounds_refuse_a_window_inside_the_regular_session() -> None:
    with pytest.raises(ValueError, match="enclose"):
        extended_session_bounds_ms(_REGULAR, window=ExtendedHoursWindow(open_minute_et=10 * 60, close_minute_et=15 * 60))


@pytest.mark.parametrize(
    ("hour", "minute", "phase", "next_hour", "next_minute"),
    [
        (3, 59, "CLOSED", 4, 0),
        (4, 0, "PRE", 9, 30),
        (9, 29, "PRE", 9, 30),
        (9, 30, "RTH", 16, 0),
        (15, 59, "RTH", 16, 0),
        (16, 0, "POST", 20, 0),
        (19, 59, "POST", 20, 0),
    ],
)
def test_declared_window_phases_on_a_regular_day(hour: int, minute: int, phase: str, next_hour: int, next_minute: int) -> None:
    state = session_state_at_ms(now_ms=_et(_REGULAR, hour, minute), extended_window=_WINDOW)

    assert state.phase == phase
    assert state.source == "broker_declared_window"
    assert state.extended_phase_proven is True
    assert state.next_transition_ms == _et(_REGULAR, next_hour, next_minute)


def test_after_the_declared_close_the_next_transition_is_the_next_sessions_open() -> None:
    state = session_state_at_ms(now_ms=_et(_REGULAR, 20, 0), extended_window=_WINDOW)
    assert state.phase == "CLOSED"
    assert state.next_transition_ms == _et(date(2026, 9, 3), 4, 0)


def test_a_non_trading_day_is_closed_until_the_next_declared_open() -> None:
    state = session_state_at_ms(now_ms=_et(_SUNDAY, 12, 0), extended_window=_WINDOW)
    assert state.phase == "CLOSED"
    # Labor Day 2026-09-07 → next session Tuesday 2026-09-08.
    assert state.next_transition_ms == _et(date(2026, 9, 8), 4, 0)


def test_an_early_close_starts_post_at_the_calendar_close() -> None:
    state = session_state_at_ms(now_ms=_et(_EARLY, 13, 0), extended_window=_WINDOW)
    assert state.phase == "POST"
    assert state.next_transition_ms == _et(_EARLY, 20, 0)


def test_without_a_window_the_calendar_still_proves_only_rth_or_closed() -> None:
    state = session_state_at_ms(now_ms=_et(_REGULAR, 18, 0))
    assert state.phase == "CLOSED"
    assert state.source == "nyse_calendar"
    assert state.extended_phase_proven is False
