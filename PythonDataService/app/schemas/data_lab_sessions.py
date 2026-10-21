"""HTTP contracts for the saved Data Lab sessions.

These replace the retired GraphQL ``dataLabSessions`` / ``dataLabSession`` queries and the ``saveDataLabSession`` /
``updateDataLabSession`` / ``renameDataLabSession`` / ``deleteDataLabSession`` mutations. The mutations answered
``{success, id, message}`` in-band; here a missing session is a 404 and a bad body is a 422.

Time on the wire is ``int64 ms UTC`` (ADR 0022). The one exception is the session's saved date-picker state,
``from_date`` / ``to_date``: ``YYYY-MM-DD`` strings the screen restores into its pickers (ADR 0022 amendment (f)).
The server derives ``window_start_ms`` / ``window_end_ms`` from them (UTC midnight of that date) and stamps
``created_at_ms`` / ``updated_at_ms`` from its own clock; a client never supplies them.

``entries`` and ``chart_snapshot`` are documents the screen owns. They travel as real JSON values, not as
JSON-encoded strings, and the server stores them without interpreting them.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, date, datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.utils.session_anchors import MAX_TIMESTAMP_MS
from app.utils.timestamps import to_ms_utc

SESSION_NAME_MAX_LENGTH = 300
TICKER_MAX_LENGTH = 20
SESSION_KIND_MAX_LENGTH = 10
_DATE_FORM = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_EPOCH_DAY = date(1970, 1, 1)
_LAST_DAY = date(9999, 12, 31)


def session_date_window_ms(value: str) -> int | None:
    """UTC midnight of a ``YYYY-MM-DD`` date as ``int64 ms``; ``None`` for anything that is not such a date.

    A date is admitted from the epoch day to 9999-12-31, so the result always sits inside
    ``[0, MAX_TIMESTAMP_MS]``. This is the one derivation both the write path and the read of a legacy row use.
    """
    if not _DATE_FORM.fullmatch(value):
        return None
    try:
        day = date.fromisoformat(value)
    except ValueError:
        return None
    if not _EPOCH_DAY <= day <= _LAST_DAY:
        return None
    return to_ms_utc(datetime(day.year, day.month, day.day, tzinfo=UTC))


def _require_session_date(value: str) -> str:
    if session_date_window_ms(value) is None:
        raise ValueError("must be a real calendar date written YYYY-MM-DD, from 1970-01-01 to 9999-12-31")
    return value


def _require_plain_json(value: Any) -> Any:
    """Refuse NaN and Infinity anywhere inside a document: they are not JSON and the database cannot hold them."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("must be plain JSON: NaN and Infinity cannot be stored")
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return value


SessionDate = Annotated[
    str,
    Field(min_length=10, max_length=10, description="A calendar date written YYYY-MM-DD (the date picker's state)."),
    AfterValidator(_require_session_date),
]
InstantMs = Annotated[int, Field(ge=0, le=MAX_TIMESTAMP_MS)]
EntriesDocument = Annotated[list[dict[str, Any]], AfterValidator(_require_plain_json)]
ChartSnapshotDocument = Annotated[dict[str, Any], AfterValidator(_require_plain_json)]


class DataLabSessionRequest(BaseModel):
    """What the screen saves: the whole session, every time (``PUT`` replaces, ``POST`` creates)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=SESSION_NAME_MAX_LENGTH)
    ticker: str = Field(min_length=1, max_length=TICKER_MAX_LENGTH)
    from_date: SessionDate
    to_date: SessionDate
    session: str = Field(
        default="rth", min_length=1, max_length=SESSION_KIND_MAX_LENGTH, description="'rth' or 'extended'."
    )
    forward_fill: bool = True
    adjusted: bool = True
    entries: EntriesDocument = Field(
        default_factory=list, description="The indicator entries, e.g. [{name: 'ema', params: {length: 20}}]."
    )
    chart_snapshot: ChartSnapshotDocument | None = Field(
        default=None,
        description="The chart the session was saved with; null when it was saved before any chart was fetched.",
    )


class DataLabSessionRenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=SESSION_NAME_MAX_LENGTH)


class DataLabSessionResponse(BaseModel):
    """One saved session, whole."""

    id: UUID
    name: str = Field(max_length=SESSION_NAME_MAX_LENGTH)
    ticker: str = Field(max_length=TICKER_MAX_LENGTH)
    from_date: str = Field(max_length=10, description="YYYY-MM-DD, as the picker saved it.")
    to_date: str = Field(max_length=10, description="YYYY-MM-DD, as the picker saved it.")
    session: str = Field(max_length=SESSION_KIND_MAX_LENGTH)
    forward_fill: bool
    adjusted: bool
    entries: list[dict[str, Any]]
    chart_snapshot: dict[str, Any] | None
    window_start_ms: InstantMs | None = Field(
        description="UTC midnight of from_date. Null only for a legacy row whose date is not written YYYY-MM-DD."
    )
    window_end_ms: InstantMs | None = Field(
        description="UTC midnight of to_date. Null only for a legacy row whose date is not written YYYY-MM-DD."
    )
    created_at_ms: InstantMs
    updated_at_ms: InstantMs


class DataLabSessionReceipt(BaseModel):
    """What a save, replace or rename answers: which session, and the instants the server stamped."""

    id: UUID
    created_at_ms: InstantMs
    updated_at_ms: InstantMs


class DataLabSessionRefusal(BaseModel):
    code: str
    message: str


class DataLabSessionRefusalBody(BaseModel):
    """The 404 body: FastAPI wraps an ``HTTPException`` detail as ``{"detail": ...}``."""

    detail: DataLabSessionRefusal
