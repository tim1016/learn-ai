"""NYSE previous-completed-session lookup, in int64 ms UTC.

Consumed only by indicator-state hydrate-validation (see
indicator_state.py check #3 in the ladder). Pure function; no IO; uses
pandas_market_calendars (already in requirements-light.txt) for the
authoritative NYSE schedule incl. early-close days and holidays.

Why ms UTC: per .claude/rules/numerical-rigor.md the canonical timestamp
format for any boundary is int64 ms UTC. Local timezone strings never
escape this function — input is UTC ms, output is UTC ms; the only NY
arithmetic happens inside pandas_market_calendars' tz-aware Timestamps.
"""

from __future__ import annotations

import pandas as pd
import pandas_market_calendars as mcal


class NoSessionError(LookupError):
    """No completed NYSE session exists in the lookback window."""


_LOOKBACK_DAYS = 14
_CALENDAR_NAME = "NYSE"


def previous_completed_nyse_session_close_ms(session_start_ms: int) -> int:
    """Return int64 ms UTC of the most recent NYSE session close strictly before session_start_ms.

    Honors early-close days (13:00 ET) and holidays. The previous
    session may be 1, 2, or 3+ calendar days back (weekend, holiday,
    holiday-after-weekend).

    Raises NoSessionError if no completed session exists in the
    14-day lookback window. (Pathological inputs only — the operator
    shouldn't be starting a runner with a session_start_ms with no
    trading history.)
    """
    # Guard: epoch 0 or negative ms is not a valid session start.
    # pandas_market_calendars covers NYSE back to 1900, so a 14-day
    # lookback from epoch 0 finds sessions in Dec 1969 — but a
    # session_start_ms of 0 is pathological and callers must not pass it.
    if session_start_ms <= 0:
        raise NoSessionError(f"session_start_ms={session_start_ms} is not a valid trading timestamp")
    cal = mcal.get_calendar(_CALENDAR_NAME)
    session_start_ts = pd.Timestamp(session_start_ms, unit="ms", tz="UTC")
    start = (session_start_ts - pd.Timedelta(days=_LOOKBACK_DAYS)).normalize()
    end = session_start_ts.normalize()
    schedule = cal.schedule(start_date=start, end_date=end)
    if schedule.empty:
        raise NoSessionError(f"no NYSE sessions in {_LOOKBACK_DAYS}-day lookback ending at {session_start_ts}")
    # schedule['market_close'] is tz-aware UTC; filter strictly < start.
    earlier = schedule[schedule["market_close"] < session_start_ts]
    if earlier.empty:
        raise NoSessionError(f"no completed NYSE session strictly before {session_start_ts}")
    last_close: pd.Timestamp = earlier["market_close"].iloc[-1]
    # pandas Timestamp -> int64 ms. .value is ns.
    return int(last_close.value // 1_000_000)
