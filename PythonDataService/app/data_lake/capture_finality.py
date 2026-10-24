"""When a minute capture of a session stops being early (#3521).

A lake day is the vendor's whole day, published once and then immutable. A
capture taken while the day was still forming holds only the minutes the
vendor had delivered by then, and nothing later refreshes it: a regular-hours
chart opened at 16:30 ET writes that day with the after-hours tape only as far
as 16:15, and the day reads as complete from then on. A consumer that needs the
whole tape must be able to tell, and refuse the day if it is not.

Formula: a session's data is final once ``close + tail + delay`` has passed.

* ``close`` is the canonical calendar's scheduled close, so an early close ends
  its tape early.
* ``tail`` is how long the vendor's tape runs past the close for the session
  policy a consumer reads: nothing for ``regular``, four hours for
  ``extended`` (16:00 to 20:00 ET; an early close at 13:00 ends at 17:00).
* ``delay`` is how late the vendor delivers a minute: the plan is 15-minute
  delayed, so a capture at the close misses the day's last quarter hour.

Capture time. The catalog stamps ``CompletedAtMs`` when an artifact is published,
which is after the vendor was asked, so judging by it alone would call final a
fetch that began before the final instant and finished after it. A writer holds
its claim for at most ``CLAIM_LEASE_MS`` before another may take it over, so the
vendor was asked no earlier than the completion minus that lease; a capture is
early when that earliest possible request precedes the final instant.
``FetchedAtMs`` is not used: it records the row's first claim and is never
renewed by a retry or a re-capture, so it could not clear once a day is captured
again, and it dates a retried fetch from its first, failed attempt.

A consumer that captures for itself must therefore start a lease after the final
instant (``capture_final_ms + CLAIM_LEASE_MS``): every claim still live then was taken
after it, and every capture the consumer makes is final under this rule.

Canonical implementation: this file. The calendar owns ``close``; the tail, the
delay and the lease are the vendor's and the lake's, named here once, and so is
the pre-market head that bounds the extended tape from the other side (finality
does not need it; a reader sizing the tape does). The test helper
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

#: How long before the scheduled open the vendor's pre-market tape begins (04:00 ET for a 09:30 open).
PRE_MARKET_HEAD_MS = 330 * 60_000

#: The longest a writer holds a minute-bar claim before another may take it over (``ensure_data._LEASE_TTL_MS``; a
#: test pins the two together). The vendor was asked no earlier than this before the catalog says the artifact completed.
CLAIM_LEASE_MS = 300_000


def capture_final_ms(day: date, session: CaptureSession) -> int:
    """The instant (``int64 ms UTC``) from which a capture of ``day`` holds all the data ``session`` reads."""
    tail = AFTER_HOURS_TAIL_MS if session == "extended" else 0
    return session_close_ms_utc(day) + tail + VENDOR_DELAY_MS


def captured_early(completed_at_ms: int | None, day: date, session: CaptureSession) -> bool:
    """Might ``day`` have been captured (the vendor asked) before ``session`` data stopped forming?

    ``completed_at_ms`` is the catalog's ``CompletedAtMs``. The vendor was asked no earlier than
    ``completed_at_ms - CLAIM_LEASE_MS``, so a capture whose earliest possible request precedes the final instant is
    early. An artifact with no recorded completion time cannot be told apart from a final one, so it is not early:
    the catalog records the time on every completion, and a row that lacks it predates that or was imported.
    """
    return completed_at_ms is not None and completed_at_ms - CLAIM_LEASE_MS < capture_final_ms(day, session)
