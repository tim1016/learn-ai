"""The two files of a study record: a small config that indexes it, and the study's own result.

``config.json`` is the index card: how the run ended, the inputs it was asked, where its data came from and what it was
rerun from. The History lists from it alone, so listing never reads a result. ``result.json`` is whatever the study
returned, stored exactly as the job or the route delivered it; the study's own page maps it as it maps a live result.

A record is written once, when the run ends, and never changed: a rerun is a new record that names the one it was
prefilled from. ``schema_version`` is the version of these two files. A reader refuses a version it does not know (the
record then counts as unreadable, never as a different record), and ignores fields it does not know, so a build that
adds a field leaves the records of the build before it readable.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

from app.utils.session_anchors import MAX_TIMESTAMP_MS

StudyType = Literal[
    "feature-study",
    "signal-study",
    "cross-sectional-study",
    "indicator-reliability",
    "realized-vs-iv",
]
"""One member per study that keeps records. The same words are the History's ``type`` and the store's directory."""

STUDY_TYPES: tuple[StudyType, ...] = get_args(StudyType)

RecordStatus = Literal["completed", "partial", "failed", "cancelled"]
"""How a run ended. ``partial``: it finished and part of its work did not (members of a universe that errored)."""

SCHEMA_VERSION = 1

RECORD_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
"""What ``uuid.uuid4().hex`` emits; the same shape every other research store uses for its ids."""

InstantMs = Annotated[int, Field(ge=0, le=MAX_TIMESTAMP_MS)]
RecordId = Annotated[str, Field(pattern=RECORD_ID_PATTERN.pattern)]


class StudyRecordConfig(BaseModel):
    """The index card of one run."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    schema_version: Literal[1] = SCHEMA_VERSION
    record_id: RecordId
    study: StudyType
    created_at_ms: InstantMs
    """When the run started."""
    completed_at_ms: InstantMs
    """When it ended, whichever way it ended."""
    status: RecordStatus
    status_detail: str | None = None
    """Why it failed or was stopped, or what part of it failed. ``None`` for a clean finish."""
    headline: str | None = None
    """One line of the result, for a list row. ``None`` when the run left no result."""
    inputs: dict[str, Any]
    """What the run was asked, as the study's request validated it, without the job id and the cache switch."""
    data_source: dict[str, Any] | None = None
    """The result's own typed provenance block, when it carried one. ``None`` reads "not recorded"."""
    data_identity: str | None = None
    """A digest of the data the study was handed, for a study whose request carries its data. Otherwise ``None``."""
    job_id: str | None = None
    """The job that produced it (the job store forgets it after 24 hours); ``None`` for a synchronous study."""
    rerun_of: RecordId | None = None
    """The record this run was prefilled from, when the user reran one."""


class StudyRecordResult(BaseModel):
    """What the study returned."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    schema_version: Literal[1] = SCHEMA_VERSION
    record_id: RecordId
    result: dict[str, Any] | None
    """The study's result in JSON, ``None`` when the run produced none. A failed run may still carry a payload (a refusal)."""
