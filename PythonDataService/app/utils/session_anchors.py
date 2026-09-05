"""ET session anchors for date-anchored values, as ``int64 ms UTC``.

A trading date on the wire or at rest is one ms instant anchored at an ET
session boundary — never a string, never a fixed offset (temporal-rigor.md,
"Date-anchored and wall-clock values"; ADR 0022). These three conversions
used to be re-derived at every seam that needed them (the recency job body,
the engine router, the sweep service and the routers on top of it, and the
tests beside each); this module is the one place they live. ``timestamps.py``
is hashed into deployed bots' qualification seals, so the helpers sit beside
it rather than inside it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.utils.timestamps import to_ms_utc

_NY = ZoneInfo("America/New_York")


def et_midnight_ms(day: date) -> int:
    """ET midnight beginning ``day``, resolved through the NY zone (DST-safe)."""
    return to_ms_utc(datetime(day.year, day.month, day.day, tzinfo=_NY))


def et_day_end_ms(day: date) -> int:
    """The half-open end of ``day``: ET midnight beginning the next calendar day."""
    return et_midnight_ms(day + timedelta(days=1))


def et_date_at_ms(ms: int) -> date:
    """The America/New_York calendar date containing instant ``ms``."""
    return datetime.fromtimestamp(ms / 1000, tz=UTC).astimezone(_NY).date()
