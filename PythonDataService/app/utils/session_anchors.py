"""ET session anchors and the admissible instant range, as ``int64 ms UTC``.

A trading date on the wire or at rest is one ms instant anchored at an ET
session boundary — never a string, never a fixed offset (ADR 0022). These conversions used to be
re-derived at every seam that needed them (the recency job body, the engine
router, the sweep service and the routers on top of it, and the tests beside
each); this module is the one place they live.

``MAX_TIMESTAMP_MS`` sits here for the same reason and answers the companion
question: not *where* an instant is anchored but *how large* one may be.
``timestamps.py`` is hashed into deployed bots' qualification seals
(``registry.py``'s ``artifact_paths``), so both live beside it rather than
inside it — a line added there changes every program's ``artifact_digest`` and
invalidates every committed golden qualification receipt.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.utils.timestamps import now_ms_utc, to_ms_utc

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


#: The last date the NYSE calendar can schedule. ``pandas_market_calendars``
#: builds sessions as nanosecond ``pd.Timestamp``s, which end on 2262-04-11,
#: so a window past it is one the service cannot serve.
LAST_SCHEDULABLE_DATE = date(2262, 4, 10)


def require_schedulable_end(end_ms: int) -> None:
    """Refuse a window ending past :data:`LAST_SCHEDULABLE_DATE`, before any schedule is built (#2771).

    A window may end anywhere up to ``MAX_TIMESTAMP_MS``, but asking the
    calendar for one ending in 9999 spent tens of seconds building ~8,000
    years of sessions only to overflow; this refusal is immediate.
    """
    end = et_date_at_ms(end_ms)
    if end > LAST_SCHEDULABLE_DATE:
        raise ValueError(
            f"The NYSE calendar cannot schedule past {LAST_SCHEDULABLE_DATE.isoformat()}; "
            f"the window ends {end.isoformat()}."
        )


def calendar_days_to_expiry(expiration_date: str, now_ms: int | None = None) -> int:
    """Calendar days from today's ET date to ``expiration_date`` (``YYYY-MM-DD``), floored at 0.

    Today is the America/New_York date at ``now_ms`` (the wall clock when
    omitted), never the host's local date. The chain snapshot looks its
    risk-free rate up at this DTE and the strategy analysis prices over it,
    so the two agree (#2789).
    """
    at_ms = now_ms_utc() if now_ms is None else now_ms
    expiry = datetime.strptime(expiration_date, "%Y-%m-%d").date()
    return max((expiry - et_date_at_ms(at_ms)).days, 0)
