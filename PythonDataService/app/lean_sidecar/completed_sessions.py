"""The most recent completed NYSE sessions before an instant, as ``int64 ms UTC`` windows.

This lives beside ``trading_calendar`` rather than inside it: ``trading_calendar.py``
is hashed into deployed signal programs' qualification seals, so a line added there
invalidates every committed golden qualification receipt. It composes only the
calendar's public functions and constructs no calendar of its own.
"""

from __future__ import annotations

import pandas as pd

from app.lean_sidecar.trading_calendar import NoSessionError, SessionWindow, session_windows_ms_utc

# Two weeks of calendar days always holds a handful of NYSE sessions, even across
# the longest holiday runs; callers ask for the last one or two.
_LOOKBACK_DAYS = 14


def latest_completed_sessions(now_ms: int, count: int) -> list[SessionWindow]:
    """The ``count`` most recent sessions whose scheduled close is at or before ``now_ms``, oldest first.

    A session is completed when the calendar's own close for that date has
    passed, so an early-close half-day completes at its real close and the
    session in progress is never one of them.
    """
    if now_ms < 0:
        raise ValueError("now_ms must be non-negative int64 ms UTC")
    if count < 1:
        raise ValueError("count must be at least 1")
    now_ts = pd.Timestamp(now_ms, unit="ms", tz="UTC")
    start = (now_ts - pd.Timedelta(days=_LOOKBACK_DAYS)).normalize()
    completed = [window for window in session_windows_ms_utc(start, now_ts.normalize()) if window.close_ms_utc <= now_ms]
    if len(completed) < count:
        raise NoSessionError(f"fewer than {count} completed NYSE sessions in the {_LOOKBACK_DAYS}-day lookback ending at {now_ts}")
    return completed[-count:]
