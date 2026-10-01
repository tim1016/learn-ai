"""The broker's declared window resolves PRE/RTH/POST around the calendar's regular session."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.broker.contract.capabilities import ExtendedHoursWindow
from app.lean_sidecar.trading_calendar import session_close_ms_utc, session_open_ms_utc
from app.services.session_authority import (
    declared_extended_phase_at_ms,
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


@pytest.mark.parametrize(
    ("day", "hour", "minute", "proven"),
    [
        (_REGULAR, 4, 0, True),  # PRE opens at the declared open
        (date(2026, 3, 9), 4, 0, True),  # PRE on the first EDT trading day (DST)
        (_REGULAR, 9, 29, True),
        (_REGULAR, 16, 0, True),  # POST
        (_REGULAR, 19, 59, True),
        (_EARLY, 13, 0, True),  # POST starts at a half-day's calendar close
        (_REGULAR, 3, 59, False),  # before the declared open
        (_REGULAR, 12, 0, False),  # RTH is not an extended phase
        (_REGULAR, 20, 0, False),  # at the declared close
        (_SUNDAY, 12, 0, False),  # not a trading day
    ],
)
def test_only_a_declared_pre_or_post_phase_proves_extended_hours(
    day: date, hour: int, minute: int, proven: bool
) -> None:
    """The ENTER gate's and the market pulse's one predicate (#1671): the
    declared window is the only proof, and a resolved RTH or CLOSED never
    counts as extended — so a use_rth=False bot cannot override a fresh
    broker CLOSED outside the declared window."""
    assert declared_extended_phase_at_ms(now_ms=_et(day, hour, minute), extended_window=_WINDOW) is proven


def test_without_a_declared_window_no_instant_proves_extended_hours() -> None:
    for hour in (6, 12, 18):
        assert declared_extended_phase_at_ms(now_ms=_et(_REGULAR, hour, 0), extended_window=None) is False
