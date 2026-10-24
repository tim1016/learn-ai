"""HTTP contract of a stored study run: ``GET /api/research/study-records/{study}/{record_id}``.

One run of Feature, Signal, Cross-sectional, Indicator reliability or Realized vs IV, as it was recorded when it ended
(``app.research.study_records``). ``result`` is the study's own result exactly as the study delivered it, so each
study's page maps it as it maps a live one; it is JSON and therefore free-form here. Everything around it is typed.

Instants are ``int64 ms UTC``. A window is the requested one, date-anchored the way the History shows it (ET midnight).
A field the record did not capture is ``null`` and reads "not recorded"; nothing is inferred to fill it.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.research.study_records.models import InstantMs, RecordStatus, StudyType
from app.schemas.research_history import ResearchHistoryRef, ResearchHistoryWindow


class StoredStudyRecord(BaseModel):
    """A finished run, read back without running anything."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    study: StudyType
    record_id: str
    schema_version: int = Field(description="The version of the record's files, for a reader that must know it.")
    status: RecordStatus
    status_detail: str | None = Field(description="Why it failed or stopped, or what part of it failed; ``null`` for a clean finish.")
    question: str = Field(description="What the run asked, in a sentence built from its inputs.")
    symbols: list[str] = Field(description="The instrument (one symbol) or the universe (several) it covered.")
    window: ResearchHistoryWindow | None = Field(description="The requested window; ``null`` when not recorded.")
    created_at_ms: InstantMs = Field(description="When the run started.")
    completed_at_ms: InstantMs = Field(description="When it ended, whichever way it ended.")
    inputs: dict[str, Any] = Field(
        description="What the run was asked, as its request validated it. The study's page prefills a rerun from this."
    )
    data_source: dict[str, Any] | None = Field(
        description="The result's own provenance block, when it carried one; ``null`` reads \"not recorded\"."
    )
    data_identity: str | None = Field(
        description="A digest of the data the request carried (Realized vs IV); ``null`` for a study that reads its own data."
    )
    job_id: str | None = Field(description="The job that produced it; ``null`` for a synchronous study.")
    rerun_of: ResearchHistoryRef | None = Field(description="The record this run was prefilled from, when it was a rerun.")
    result: dict[str, Any] | None = Field(
        description="The study's result as delivered; ``null`` when the run produced none (a failed or cancelled run)."
    )


class StudyRecordRefusal(BaseModel):
    code: str
    message: str


class StudyRecordRefusalBody(BaseModel):
    """The 404 and 500 body: FastAPI wraps an ``HTTPException`` detail as ``{"detail": ...}``."""

    detail: StudyRecordRefusal
