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
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.utils.timestamps import to_ms_utc

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


def et_wall_clock_ms(day: date, wall_clock: time) -> int:
    """Anchor an ET wall-clock value to ``day`` as an ``int64 ms UTC`` instant.

    A recurring session setting is executed as a wall-clock rule on every
    trading day; it is not itself a one-off instant.  Persistence therefore
    anchors it to the run's evaluation-start date solely to make the frozen
    receipt unambiguous and canonical.  Execution continues to apply the
    original local wall-clock value to each session.
    """
    return to_ms_utc(datetime.combine(day, wall_clock, tzinfo=_NY))


def persisted_execution_configuration(
    *,
    evaluation_start: date,
    compatibility_profile: str | None,
    warmup_from_date: str | None,
    slippage_per_share: float,
    session_entry_cutoff: time | None,
    force_flat_at: time | None,
    limit_penetration: float,
) -> dict[str, Any]:
    """Freeze execution settings with one canonical session-time receipt shape.

    The session controls remain recurring America/New_York wall-clock rules at
    runtime. Their persisted ``*_ms`` fields are reference instants on the
    evaluation-start date, paired with the zone, so Python and LEAN evidence
    can compare the same unambiguous representation without serializing a
    time-of-day string.
    """
    return {
        "compatibility_profile": compatibility_profile,
        "warmup_from_date": warmup_from_date,
        "slippage_per_share": slippage_per_share,
        "session_time_reference_ms": et_midnight_ms(evaluation_start),
        "session_time_zone": "America/New_York",
        "session_entry_cutoff_ms": (
            et_wall_clock_ms(evaluation_start, session_entry_cutoff)
            if session_entry_cutoff is not None
            else None
        ),
        "force_flat_at_ms": et_wall_clock_ms(evaluation_start, force_flat_at) if force_flat_at is not None else None,
        "limit_penetration": limit_penetration,
    }


def et_day_end_ms(day: date) -> int:
    """The half-open end of ``day``: ET midnight beginning the next calendar day."""
    return et_midnight_ms(day + timedelta(days=1))


def et_date_at_ms(ms: int) -> date:
    """The America/New_York calendar date containing instant ``ms``."""
    return datetime.fromtimestamp(ms / 1000, tz=UTC).astimezone(_NY).date()
