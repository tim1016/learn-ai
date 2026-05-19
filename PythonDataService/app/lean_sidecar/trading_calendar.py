"""Single calendar source of truth for the LEAN sidecar.

Per docs/handoffs/2026-05-18-design-p2-5-date-semantics-v2.md, both the
``TrustedRunRequestModel`` validator and the staging iteration consult
this module so they cannot drift on which calendar dates are trading
days, holidays, or half-days. The cross-engine reconciler and the
``/calendar/blocked-dates`` endpoint also read from here.

This is intentionally separate from
``app/engine/live/nyse_calendar.py``:

- ``nyse_calendar`` answers a single math-rigor question (previous
  completed session close, used by indicator-state hydrate-validation).
- ``trading_calendar`` answers operator-facing window questions
  (validator, staging, picker advisories). Different consumers,
  different test surfaces, different rate of change.

Backed by ``pandas_market_calendars`` (already in
``requirements-light.txt``).  All boundary types are ``date`` /
``int64 ms UTC`` per the repo's timestamp-rigor rule. Internal use of
tz-aware ``pd.Timestamp`` is fine; it must not escape.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pandas as pd
import pandas_market_calendars as mcal

_ET = ZoneInfo("America/New_York")
_CALENDAR_NAME = "NYSE"
_CALENDAR = mcal.get_calendar(_CALENDAR_NAME)


def is_trading_day(d: date) -> bool:
    """True iff ``d`` is a regular or half-day NYSE session.

    Weekends and federal holidays return False. Early-close half-days
    return True — they ARE sessions; whether the validator accepts them
    is a separate policy (see ``is_early_close``).
    """
    schedule = _CALENDAR.schedule(start_date=d, end_date=d)
    return not schedule.empty


def is_early_close(d: date) -> bool:
    """True iff ``d`` is a NYSE half-day (typically 13:00 ET close).

    Convention: a regular session closes at 16:00 ET; any session
    whose ``market_close`` is strictly earlier than the day's
    16:00-ET wall-clock counts as an early close. Non-session days
    return False categorically.
    """
    schedule = _CALENDAR.schedule(start_date=d, end_date=d)
    if schedule.empty:
        return False
    close_ts: pd.Timestamp = schedule["market_close"].iloc[0]
    # Compare in ET. A regular session closes at 16:00 ET; anything
    # earlier is the half-day signal.
    close_et = close_ts.tz_convert(_ET)
    return close_et.time() < time(16, 0)


def next_trading_day(d: date) -> date:
    """Return the next NYSE session strictly after ``d``.

    Skips weekends and holidays. Half-days count as sessions and ARE
    returned (the validator separately rejects windows touching them;
    this primitive is calendar-only).

    Uses a generous forward window so multi-day holiday clusters
    (Christmas → New Year's, MLK weekend, etc.) resolve in one
    schedule call.
    """
    schedule = _CALENDAR.schedule(
        start_date=d + pd.Timedelta(days=1),
        end_date=d + pd.Timedelta(days=14),
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
    open_et = datetime(d.year, d.month, d.day, 9, 30, tzinfo=_ET)
    # ``.timestamp()`` already gives epoch seconds in UTC; multiply
    # for ms. Cast through float→int is exact for any date the NYSE
    # calendar supports (≪ 2^53 ms).
    return int(open_et.timestamp() * 1000)


def blocked_dates_in_range(
    start: date,
    end: date,
) -> Mapping[date, str]:
    """Return a mapping of blocked dates in ``[start, end]`` (inclusive).

    Each value is a short reason tag — ``"weekend"``, ``"holiday"``,
    or ``"early_close"``. The endpoint exposes this to the UI so the
    picker can disable + label each blocked date in a single payload.

    Half-days are included because the validator rejects them; the UI
    surfaces them with a half-day tooltip rather than a generic
    "blocked" marker. The reason tag is the discriminator.
    """
    if end < start:
        raise ValueError(f"blocked_dates_in_range: end {end.isoformat()} is before start {start.isoformat()}")

    # ``pandas_market_calendars`` schedules ONLY sessions — so weekends
    # and holidays show up as absent rows, and we need to walk the
    # range to find them. Build a dict of session-date → market_close
    # first to make the per-day lookup O(1).
    schedule = _CALENDAR.schedule(start_date=start, end_date=end)
    session_closes: dict[date, pd.Timestamp] = {ts.date(): close for ts, close in schedule["market_close"].items()}

    out: dict[date, str] = {}
    current = start
    while current <= end:
        if current.weekday() >= 5:
            out[current] = "weekend"
        elif current not in session_closes:
            out[current] = "holiday"
        else:
            close_et = session_closes[current].tz_convert(_ET)
            if close_et.time() < time(16, 0):
                out[current] = "early_close"
        current = current + pd.Timedelta(days=1).to_pytimedelta()
    return out
