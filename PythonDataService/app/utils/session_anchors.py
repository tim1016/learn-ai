"""ET session anchors and the admissible instant range, as ``int64 ms UTC``.

A trading date on the wire or at rest is one ms instant anchored at an ET
session boundary — never a string, never a fixed offset (temporal-rigor.md,
"Date-anchored and wall-clock values"; ADR 0022). These conversions used to be
re-derived at every seam that needed them (the recency job body, the engine
router, the sweep service and the routers on top of it, and the tests beside
each); this module is the one place they live.

``MAX_TIMESTAMP_MS`` sits here for the same reason and answers the companion
question: not *where* an instant is anchored but *how large* one may be.
``timestamps.py`` is hashed into deployed bots' qualification seals
(``registry.py``'s ``artifact_paths``), so both live beside it rather than
inside it — a line added there changes every program's ``artifact_digest`` and
invalidates every committed golden qualification receipt.

The owner's words for an instant in ET (``et_when_words``, #2665) live here
for that reason too: they are the one author of backend copy that names a day
and minute in ET, and they read the ET date from ``et_date_at_ms``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.utils.timestamps import ny_datetime, to_ms_utc

_NY = ZoneInfo("America/New_York")


# The largest instant this domain admits: 9999-12-31T23:59:59.999Z, the end of
# the range ``datetime`` itself can represent. It is the ceiling every ``*_ms``
# schema field declares, in place of the int64 maximum they used to declare —
# see the module docstring for why it lives here.
#
# Two reasons for the change, one of them a bug:
#
# * ``2**63 - 1`` is not representable in a float64 — the nearest double is
#   ``2**63``. FastAPI's ``openapi.models.Schema`` types ``maximum`` as
#   ``float``, so a bound of ``9223372036854775807`` was published in the
#   OpenAPI contract as ``9.223372036854776e+18``: a ceiling *one higher* than
#   the one being validated, in a document whose job is to state the contract
#   exactly (#1936).
# * ``2**63 - 1`` ms is the year 292 million. It was never the real bound; it
#   was the widest number the storage type could hold. This one is true, and
#   being under ``2**53`` it survives the float round-trip exactly.
#
# The ``2**63`` literals that remain in ``app/`` are representability guards
# inside functions — "does this value fit an int64 column/wire field?" — which
# is a different question and keeps its own answer.
MAX_TIMESTAMP_MS = 253_402_300_799_999





def et_midnight_ms(day: date) -> int:
    """ET midnight beginning ``day``, resolved through the NY zone (DST-safe)."""
    return to_ms_utc(datetime(day.year, day.month, day.day, tzinfo=_NY))


def et_day_end_ms(day: date) -> int:
    """The half-open end of ``day``: ET midnight beginning the next calendar day."""
    return et_midnight_ms(day + timedelta(days=1))


def et_date_at_ms(ms: int) -> date:
    """The America/New_York calendar date containing instant ``ms``."""
    return datetime.fromtimestamp(ms / 1000, tz=UTC).astimezone(_NY).date()


def et_when_words(instant_ms: int, *, now_ms: int) -> str:
    """An instant as the owner reads it: ``Wed Sep 30, 15:59 ET``, with the year when it is not this one.

    Always the weekday and date, never "today": the owner may not be in the
    market's time zone, so "today" in ET can be tomorrow where they are. The
    words are display-only: never stored, parsed back, or compared.
    """
    return f"{et_day_words(instant_ms, now_ms=now_ms)}, {et_clock_words(instant_ms)} ET"


def et_day_words(instant_ms: int, *, now_ms: int) -> str:
    """An instant's ET date as the owner reads it: ``Wed Sep 30``, with the year when it is not now's (in ET)."""
    day = et_date_at_ms(instant_ms)
    year = "" if day.year == et_date_at_ms(now_ms).year else f" {day.year}"
    return f"{day:%a %b} {day.day}{year}"


def et_clock_words(instant_ms: int) -> str:
    """An instant's ET wall clock, to the minute: ``15:59``."""
    return f"{ny_datetime(instant_ms):%H:%M}"
