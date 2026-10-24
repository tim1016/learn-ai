"""How many bars a scheduled session holds, from the canonical calendar and the vendor's tape (#3525).

Formula: bars(day, policy) = floor(L(day, policy) / B), with L(day, regular) = close(day) - open(day) from the calendar
  and L(day, extended) = L(day, regular) + PRE_MARKET_HEAD_MS + AFTER_HOURS_TAIL_MS; B is the bar length. A daily bar is
  one bar per session. ``full_session_bars`` is the greatest count over a set of sessions: the session a year's worth of
  bars is counted in, so an early close (fewer bars) does not shrink the annualization of a window that holds ordinary days.
Reference: none outside the NYSE calendar and the vendor's published hours; the HF estimator states the same two counts for
  15-minute bars (26 regular, 64 extended; ``features_realtime/hf_realized_vol.py``).
Canonical implementation: this file.
Validated against: tests/edge/test_session_bars.py (a pandas count of the same grid for an ordinary day, an early close and a
  daylight-saving day, and the HF estimator's own ETH session mask for the extended count).

The extended session is every bar the vendor served (``app.data_lake.capture_finality.CaptureSession``): its tape ends
``AFTER_HOURS_TAIL_MS`` after the scheduled close (so an early close ends at 17:00) and begins ``PRE_MARKET_HEAD_MS`` before the
open. The calendar holds the scheduled open and close; the vendor's head and tail are named once, in ``capture_finality``.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

from app.data_lake.capture_finality import AFTER_HOURS_TAIL_MS, PRE_MARKET_HEAD_MS, CaptureSession
from app.lean_sidecar.trading_calendar import session_window_for_date

#: A daily bar is the fold of a whole ET day of minutes (``fold_minute_bars`` floors a day-long period at ET midnight).
DAY_BAR_MINUTES = 24 * 60


def bars_in_session(day: date, bar_minutes: int, session: CaptureSession) -> int:
    """Bars of ``bar_minutes`` the scheduled ``session`` of ``day`` holds; ``LookupError`` when ``day`` is not a session."""
    if bar_minutes < 1 or bar_minutes > DAY_BAR_MINUTES:
        raise ValueError(f"bar_minutes must be from 1 to {DAY_BAR_MINUTES}, got {bar_minutes}")
    window = session_window_for_date(day)
    if bar_minutes == DAY_BAR_MINUTES:
        return 1
    head, tail = (PRE_MARKET_HEAD_MS, AFTER_HOURS_TAIL_MS) if session == "extended" else (0, 0)
    return (window.close_ms_utc + tail - (window.open_ms_utc - head)) // (bar_minutes * 60_000)


def full_session_bars(days: Iterable[date], bar_minutes: int, session: CaptureSession) -> int:
    """The bars a full-length session of ``days`` holds: the greatest count among them."""
    counts = [bars_in_session(day, bar_minutes, session) for day in days]
    if not counts:
        raise ValueError("no sessions to count bars in")
    return max(counts)
