"""Canonical NYSE calendar source of truth.

Per docs/handoffs/2026-05-18-design-p2-5-date-semantics-v2.md, both the
``TrustedRunRequestModel`` validator and the staging iteration consult
this module so they cannot drift on which calendar dates are trading
days, holidays, or half-days. The ``/calendar/blocked-dates`` endpoint
also reads from here.

Backed by ``pandas_market_calendars`` (already in
``requirements-light.txt``).  All boundary types are ``date`` /
``int64 ms UTC`` per the repo's timestamp-rigor rule. Internal use of
tz-aware ``pd.Timestamp`` is fine; it must not escape.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

import pandas as pd
import pandas_market_calendars as mcal

_ET = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")
_CALENDAR_NAME = "NYSE"
_CALENDAR = mcal.get_calendar(_CALENDAR_NAME)
_LOOKBACK_DAYS = 14
_REGULAR_CLOSE_MINUTE_ET = 16 * 60

NyseSessionState = Literal["RTH_OPEN", "CLOSED"]


class NoSessionError(LookupError):
    """No completed NYSE session exists in the requested lookback window."""


@dataclass(frozen=True)
class SessionWindow:
    """One scheduled NYSE session window expressed in canonical ms UTC."""

    session_date: date
    open_ms_utc: int
    close_ms_utc: int


def _timestamp_to_ms_utc(ts: pd.Timestamp) -> int:
    return int(ts.value // 1_000_000)


def _schedule(start: date | str | pd.Timestamp, end: date | str | pd.Timestamp) -> pd.DataFrame:
    return _CALENDAR.schedule(start_date=start, end_date=end)


def session_windows_ms_utc(start: date | str, end: date | str) -> list[SessionWindow]:
    """Return scheduled NYSE session windows in ``[start, end]``.

    The returned values are canonical ``int64 ms UTC`` boundaries. Internal
    ``pandas_market_calendars`` timestamps never escape this module.
    """
    schedule = _schedule(start, end)
    windows: list[SessionWindow] = []
    for idx, row in schedule.iterrows():
        session_date = pd.Timestamp(idx).date()
        market_open: pd.Timestamp = row["market_open"]
        market_close: pd.Timestamp = row["market_close"]
        windows.append(
            SessionWindow(
                session_date=session_date,
                open_ms_utc=_timestamp_to_ms_utc(market_open),
                close_ms_utc=_timestamp_to_ms_utc(market_close),
            )
        )
    return windows


def session_window_for_date(d: date) -> SessionWindow:
    """Return the scheduled NYSE session window for ``d``.

    Raises ``LookupError`` when ``d`` is not a session.
    """
    windows = session_windows_ms_utc(d, d)
    if not windows:
        raise LookupError(f"{d.isoformat()} is not a NYSE session")
    return windows[0]


def is_trading_day(d: date) -> bool:
    """True iff ``d`` is a regular or half-day NYSE session.

    Weekends and federal holidays return False. Early-close half-days
    return True — they ARE sessions; whether the validator accepts them
    is a separate policy (see ``is_early_close``).
    """
    schedule = _schedule(d, d)
    return not schedule.empty


def is_early_close(d: date) -> bool:
    """True iff ``d`` is a NYSE half-day (typically 13:00 ET close).

    Convention: a regular session closes at 16:00 ET; any session
    whose ``market_close`` is strictly earlier than the day's
    16:00-ET wall-clock counts as an early close. Non-session days
    return False categorically.
    """
    try:
        window = session_window_for_date(d)
    except LookupError:
        return False
    close_et = datetime.fromtimestamp(window.close_ms_utc / 1000, tz=_UTC).astimezone(_ET)
    close_minute = close_et.hour * 60 + close_et.minute
    return close_minute < _REGULAR_CLOSE_MINUTE_ET


def next_trading_day(d: date) -> date:
    """Return the next NYSE session strictly after ``d``.

    Skips weekends and holidays. Half-days count as sessions and ARE
    returned (the validator separately rejects windows touching them;
    this primitive is calendar-only).

    Uses a generous forward window so multi-day holiday clusters
    (Christmas → New Year's, MLK weekend, etc.) resolve in one
    schedule call.
    """
    schedule = _schedule(
        d + pd.Timedelta(days=1),
        d + pd.Timedelta(days=14),
    )
    if schedule.empty:
        # 14 calendar days with zero sessions only happens in
        # pathological time travel; let the caller see the surprise.
        raise LookupError(f"no NYSE session within 14 days after {d.isoformat()}")
    first_close: pd.Timestamp = schedule["market_close"].iloc[0]
    return first_close.tz_convert(_ET).date()


def session_open_ms_utc(d: date) -> int:
    """09:30 ET of ``d`` as int64 ms since Unix epoch UTC.

    Conversion goes through the NY zone (``ZoneInfo``) — never a
    fixed offset. DST days (EDT vs EST) produce different UTC ms for
    the same wall-clock 09:30. A fixed-offset implementation would
    be a silent 1-hour bug on either side of 2026-03-08 / 2026-11-01.

    ``d`` does NOT need to be a trading day — callers may use this to
    compute the half-open window's exclusive end as
    ``session_open_ms_utc(next_trading_day(end_date))``.
    """
    try:
        return session_window_for_date(d).open_ms_utc
    except LookupError:
        open_et = datetime(d.year, d.month, d.day, 9, 30, tzinfo=_ET)
        # ``.timestamp()`` already gives epoch seconds in UTC; multiply
        # for ms. Cast through float→int is exact for any date the NYSE
        # calendar supports (≪ 2^53 ms).
        return int(open_et.timestamp() * 1000)


def session_close_ms_utc(d: date) -> int:
    """Return the scheduled NYSE session close for ``d`` as int64 ms UTC."""
    return session_window_for_date(d).close_ms_utc


def is_regular_session_ms_utc(ts_ms: int) -> bool:
    """True iff ``ts_ms`` falls inside the scheduled NYSE regular session."""
    return session_state_at_ms(ts_ms) == "RTH_OPEN"


def expected_sessions(start: date, end: date) -> list[date]:
    """Return every NYSE session date in ``[start, end]`` (inclusive).

    Inverse of :func:`blocked_dates_in_range`. Half-days remain
    sessions; weekends and holidays are omitted. Used by the canonical
    fetch's opt-in completeness check to assert no expected session is
    missing from a Polygon response.
    """
    schedule = _schedule(start, end)
    return sorted(ts.date() for ts in schedule.index)


def session_close_minute_et(d: date) -> int:
    """Return the session close as minutes past midnight ET.

    Regular session closes at 16:00 ET → 960. Half-days close at
    13:00 ET → 780 (occasionally other times historically; the
    ``pandas_market_calendars`` schedule is the source of truth and
    is consulted directly). Raises :class:`LookupError` when ``d`` is
    not a session.
    """
    close_et = datetime.fromtimestamp(session_close_ms_utc(d) / 1000, tz=_UTC).astimezone(_ET)
    return close_et.hour * 60 + close_et.minute


def trading_session_count(start: date, end: date) -> int:
    """Return the count of scheduled NYSE sessions in ``[start, end]``."""
    return len(expected_sessions(start, end))


def valid_session_minutes_ms_utc(
    start: date | str,
    end: date | str,
    frequency: str = "1min",
) -> set[int]:
    """Return valid scheduled session instants as canonical ms UTC."""
    schedule = _schedule(start, end)
    if schedule.empty:
        return set()
    minutes = mcal.date_range(schedule, frequency=frequency)
    return {_timestamp_to_ms_utc(pd.Timestamp(ts)) for ts in minutes}


def holiday_names_in_range(start: date, end: date) -> dict[date, str]:
    """Return ``{date: holiday_name}`` for NYSE holidays in ``[start, end]``."""
    series = _CALENDAR.regular_holidays.holidays(
        pd.Timestamp(start),
        pd.Timestamp(end),
        return_name=True,
    )
    return {ts.date(): str(name) for ts, name in series.items()}


def session_state_at_ms(now_ms: int) -> NyseSessionState:
    """Return scheduled NYSE session state for ``now_ms``.

    This deliberately does not synthesize ``HALTED``; unscheduled liveness
    belongs to the live broker/vendor feed.
    """
    if now_ms < 0:
        raise ValueError("now_ms must be non-negative int64 ms UTC")
    now_utc = pd.Timestamp(now_ms, unit="ms", tz="UTC")
    ny_day = now_utc.tz_convert("America/New_York").date()
    try:
        window = session_window_for_date(ny_day)
    except LookupError:
        return "CLOSED"
    if window.open_ms_utc <= now_ms < window.close_ms_utc:
        return "RTH_OPEN"
    return "CLOSED"


def previous_completed_session_close_ms(session_start_ms: int) -> int:
    """Return the latest scheduled NYSE close strictly before ``session_start_ms``."""
    if session_start_ms <= 0:
        raise NoSessionError(f"session_start_ms={session_start_ms} is not a valid trading timestamp")
    session_start_ts = pd.Timestamp(session_start_ms, unit="ms", tz="UTC")
    start = (session_start_ts - pd.Timedelta(days=_LOOKBACK_DAYS)).normalize()
    end = session_start_ts.normalize()
    windows = session_windows_ms_utc(start, end)
    earlier = [window for window in windows if window.close_ms_utc < session_start_ms]
    if not windows:
        raise NoSessionError(f"no NYSE sessions in {_LOOKBACK_DAYS}-day lookback ending at {session_start_ts}")
    if not earlier:
        raise NoSessionError(f"no completed NYSE session strictly before {session_start_ts}")
    return earlier[-1].close_ms_utc


def blocked_dates_in_range(
    start: date,
    end: date,
) -> Mapping[date, str]:
    """Return a mapping of blocked dates in ``[start, end]`` (inclusive).

    Each value is a short reason tag — ``"weekend"`` or ``"holiday"``.
    The endpoint exposes this to the UI so the picker can disable +
    label each non-tradeable date in a single payload. Early-close
    half-days are sessions, so they are intentionally not blocked.
    """
    if end < start:
        raise ValueError(f"blocked_dates_in_range: end {end.isoformat()} is before start {start.isoformat()}")

    # ``pandas_market_calendars`` schedules ONLY sessions — so weekends
    # and holidays show up as absent rows, and we need to walk the
    # range to find them. Build a set of session dates first to make
    # the per-day lookup O(1). Early closes remain sessions here.
    schedule = _schedule(start, end)
    session_dates: set[date] = {ts.date() for ts in schedule.index}

    out: dict[date, str] = {}
    current = start
    while current <= end:
        if current.weekday() >= 5:
            out[current] = "weekend"
        elif current not in session_dates:
            out[current] = "holiday"
        current = current + pd.Timedelta(days=1).to_pytimedelta()
    return out
