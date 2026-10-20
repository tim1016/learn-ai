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


@pytest.mark.parametrize(
    ("day", "open_minute_et", "close_minute_et", "match"),
    [
        pytest.param(_SUNDAY, 4 * 60, 20 * 60, "not a trading day", id="non-trading-day"),
        pytest.param(_REGULAR, 10 * 60, 15 * 60, "enclose", id="window-inside-the-regular-session"),
    ],
)
def test_extended_bounds_refuse_an_unusable_day_or_window(
    day: date, open_minute_et: int, close_minute_et: int, match: str
) -> None:
    window = ExtendedHoursWindow(open_minute_et=open_minute_et, close_minute_et=close_minute_et)

    with pytest.raises(ValueError, match=match):
        extended_session_bounds_ms(day, window=window)


@pytest.mark.parametrize(
    ("now", "phase", "next_transition"),
    [
        pytest.param((_REGULAR, 3, 59), "CLOSED", (_REGULAR, 4, 0), id="closed-before-the-declared-open"),
        pytest.param((_REGULAR, 4, 0), "PRE", (_REGULAR, 9, 30), id="pre-opens-at-the-declared-open"),
        pytest.param((_REGULAR, 9, 29), "PRE", (_REGULAR, 9, 30), id="pre-until-the-last-minute-before-rth"),
        pytest.param((_REGULAR, 9, 30), "RTH", (_REGULAR, 16, 0), id="rth-opens-at-the-calendar-open"),
        pytest.param((_REGULAR, 15, 59), "RTH", (_REGULAR, 16, 0), id="rth-until-the-last-minute-before-post"),
        pytest.param((_REGULAR, 16, 0), "POST", (_REGULAR, 20, 0), id="post-opens-at-the-calendar-close"),
        pytest.param((_REGULAR, 19, 59), "POST", (_REGULAR, 20, 0), id="post-until-the-last-minute-before-the-declared-close"),
        pytest.param(
            (_REGULAR, 20, 0), "CLOSED", (date(2026, 9, 3), 4, 0), id="after-the-declared-close-the-next-transition-is-the-next-sessions-open"
        ),
        # Labor Day 2026-09-07 → next session Tuesday 2026-09-08.
        pytest.param(
            (_SUNDAY, 12, 0), "CLOSED", (date(2026, 9, 8), 4, 0), id="a-non-trading-day-is-closed-until-the-next-declared-open"
        ),
        pytest.param((_EARLY, 13, 0), "POST", (_EARLY, 20, 0), id="an-early-close-starts-post-at-the-calendar-close"),
    ],
)
def test_declared_window_phases_and_next_transitions(
    now: tuple[date, int, int], phase: str, next_transition: tuple[date, int, int]
) -> None:
    state = session_state_at_ms(now_ms=_et(*now), extended_window=_WINDOW)

    assert state.phase == phase, "the declared window resolves the phase"
    assert state.source == "broker_declared_window", "the phase is read from the broker's declared window"
    assert state.extended_phase_proven is True, "the declared window proves the extended phase"
    assert state.next_transition_ms == _et(*next_transition), "the next transition"


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
