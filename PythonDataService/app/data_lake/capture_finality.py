"""When a minute capture of a session stops being early (#3521 review M3).

A lake day is the vendor's whole day, published once and then immutable. A
capture taken while the day was still forming holds only the minutes the
vendor had delivered by then, and nothing later refreshes it: a regular-hours
chart opened at 16:30 ET writes that day with the after-hours tape only as far
as 16:15, and the day reads as complete from then on. A consumer that needs the
whole tape must be able to tell.

Formula: a session's data is final once ``close + tail + delay`` has passed.

* ``close`` is the canonical calendar's scheduled close, so an early close ends
  its tape early.
* ``tail`` is how long the vendor's tape runs past the close for the session
  policy a consumer reads: nothing for ``regular``, four hours for
  ``extended`` (16:00 to 20:00 ET; an early close at 13:00 ends at 17:00).
* ``delay`` is how late the vendor delivers a minute: the plan is 15-minute
  delayed, so a capture at the close misses the day's last quarter hour.

Canonical implementation: this file. The calendar owns ``close``; the tail and
the delay are the vendor's, named here once. The test helper
``tests/_helpers/pinned_vendor.py`` serves an after-hours tape of the same
length.
Validated against: tests/unit/data_lake/test_capture_finality.py
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from app.lean_sidecar.trading_calendar import session_close_ms_utc

#: The session a consumer reads: ``regular`` is the scheduled session, ``extended`` every bar the vendor served.
CaptureSession = Literal["regular", "extended"]

#: The vendor delivers a minute this long after it ends (the plan here is 15-minute delayed).
VENDOR_DELAY_MS = 15 * 60_000

#: How long the vendor's after-hours tape runs past the scheduled close.
AFTER_HOURS_TAIL_MS = 4 * 3_600_000


def capture_final_ms(day: date, session: CaptureSession) -> int:
    """The instant (``int64 ms UTC``) from which a capture of ``day`` holds all the data ``session`` reads."""
    tail = AFTER_HOURS_TAIL_MS if session == "extended" else 0
    return session_close_ms_utc(day) + tail + VENDOR_DELAY_MS


def captured_early(completed_at_ms: int | None, day: date, session: CaptureSession) -> bool:
    """Was ``day`` captured (the artifact completed) before ``session`` data stopped forming?

    An artifact with no recorded completion time cannot be told apart from a
    final one, so it is not early: the catalog records the time on every
    completion, and a row that lacks it predates that or was imported.
    """
    return completed_at_ms is not None and completed_at_ms < capture_final_ms(day, session)
