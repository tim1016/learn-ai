"""HTTP contract of the Research History: one typed index over every study's own store.

The index does not move anything. Each store keeps its records and its models; an entry here is a small, uniform
description of one record, with the record's type and native id so the caller can open the store's own detail. An id is
only ever meaningful together with its type: two stores may hold the same number or the same text.

Instants are ``int64 ms UTC``. A window is date-anchored the way its store anchors it (a run's dates sit at
``America/New_York`` midnight, ADR 0072), so the page renders it as dates in ET.

A field the store did not record is ``None`` and reads "not recorded" on the page. Nothing here is inferred to fill it.
"""

from __future__ import annotations

from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

from app.utils.session_anchors import MAX_TIMESTAMP_MS

HistoryType = Literal[
    "spec-run",
    "walk-forward",
    "monte-carlo",
    "baseline",
    "signal-report",
    "feature-experiment",
    "feature-study",
    "signal-study",
    "cross-sectional-study",
    "indicator-reliability",
    "realized-vs-iv",
]
"""One member per store the index reads. A new store adds a member and registers one source (``app.research.history``).

``signal-report`` and ``feature-experiment`` are the rows the retired .NET backend wrote. The last five are the runs the
studies record themselves now (#3520). They are different stores, so they are never one type."""

HISTORY_TYPES: tuple[HistoryType, ...] = get_args(HistoryType)

HistoryStatus = Literal["running", "completed", "partial", "failed", "cancelled", "unreadable"]
"""Where a record ended.

``partial``: finished, with part of its work failed (a walk-forward with failed folds). ``cancelled``: stopped on
request. ``unreadable``: the store holds the record but its outcome cannot be read, so no other status is claimed.
"""

SourceState = Literal["read", "unavailable", "not_queried"]

InstantMs = Annotated[int, Field(ge=0, le=MAX_TIMESTAMP_MS)]

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 25
MAX_PAGE = 1000


class _Wire(BaseModel):
    """Every field is present on the way out, a missing value as ``null``."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class ResearchHistoryRef(_Wire):
    """Another record, named the only way a record can be named: its type and its native id."""

    type: HistoryType
    native_id: str


class ResearchHistoryWindow(_Wire):
    """The window the study was asked about: the store's own start and end instants."""

    start_ms: InstantMs
    end_ms: InstantMs


class ResearchHistoryEntry(_Wire):
    """One stored study or run, as its own store describes it."""

    type: HistoryType
    native_id: str
    question: str = Field(description="What this record asked, in a sentence built from what the store holds.")
    symbols: list[str] = Field(
        description="The instrument (one symbol) or the universe (several) it covered; empty when not recorded."
    )
    window: ResearchHistoryWindow | None = Field(description="The requested window; ``null`` when not recorded.")
    status: HistoryStatus
    status_detail: str | None = Field(description="Why it failed, or what part failed; ``null`` when none was recorded.")
    created_at_ms: InstantMs
    completed_at_ms: InstantMs | None = Field(description="``null`` when the store records no completion time.")
    provenance: str | None = Field(description="The inputs and data it ran on, as the store recorded them; ``null`` when none.")
    headline: str | None = Field(description="The stored headline result, for a store whose list row holds one.")
    parent: ResearchHistoryRef | None = Field(description="The record this one was derived from, when the store links one.")


class ResearchHistorySource(_Wire):
    """What one registered store contributed to this page, so a gap is never mistaken for an empty store."""

    type: HistoryType
    label: str
    state: SourceState = Field(
        description="``read``: counted in ``total``. ``unavailable``: failed, so absent from ``total``. "
        "``not_queried``: left out by the type filter."
    )
    total: int | None = Field(description="Records in this store matching the filters; ``null`` unless ``read``.")
    unreadable: int = Field(
        description="Stored records that could not be read and so are in neither ``total`` nor the entries."
    )
    error: str | None = Field(description="Why the store could not be read; ``null`` unless ``unavailable``.")


class ResearchHistoryPage(_Wire):
    """One page of the merged history, with the count it is a page of."""

    entries: list[ResearchHistoryEntry]
    total: int = Field(description="Matching records across every ``read`` store; the pages add up to exactly this.")
    page: int
    page_size: int
    partial: bool = Field(
        description="A queried store was unavailable or held unreadable records: ``total`` and the entries leave them out."
    )
    sources: list[ResearchHistorySource]
    symbols: list[str] = Field(
        description="Every symbol in the stores read, within the time filter, for the symbol filter to offer."
    )


class ResearchHistoryRefusal(_Wire):
    code: str
    message: str
    sources: list[ResearchHistorySource]


class ResearchHistoryRefusalBody(BaseModel):
    """The 503 body: FastAPI wraps an ``HTTPException`` detail as ``{"detail": ...}``."""

    detail: ResearchHistoryRefusal
