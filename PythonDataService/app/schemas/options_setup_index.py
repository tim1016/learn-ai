"""The Options setups index: every saved setup, and every piece of evidence, each with its exact content identity (#3512).

A saved name addresses a mutable draft, and nothing here says a name owns evidence. Each run, study and approved
version carries the ``setup_sha256`` of the content it froze, and each saved setup carries the hash of its content now,
so a reader joins them by equal content and never by name. ``recorded_name`` is only the label the evidence was
recorded under. A hash is absent, with its reason, where the stored setup can no longer be read.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.options_backtest_runs import RUN_ID_MAX, RunStatusWord
from app.utils.session_anchors import MAX_TIMESTAMP_MS

Ms = Annotated[int, Field(ge=0, le=MAX_TIMESTAMP_MS)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
# How an approved version was made. ``not_recorded``: a search link that is incomplete, so no origin is claimed.
ApprovalOrigin = Literal["backtest", "search", "not_recorded"]


class IndexedSetup(BaseModel):
    """One library entry, by name: a shipped example, a saved setup, or one that can no longer be opened."""

    name: str
    example: bool
    saved_at_ms: Ms | None = None
    underlying: str | None = None
    setup_sha256: Sha256 | None = None
    cannot_open_reason: str | None = None

    @model_validator(mode="after")
    def _a_hash_or_why_not(self) -> IndexedSetup:
        if (self.setup_sha256 is None) == (self.cannot_open_reason is None):
            raise ValueError("an indexed setup carries its content hash, or the reason it cannot be opened, not both")
        return self


class IndexedRun(BaseModel):
    id: Annotated[int, Field(ge=1, le=RUN_ID_MAX)]
    recorded_name: str | None = None
    underlying: str
    setup_sha256: Sha256 | None = None
    unreadable_reason: str | None = None
    created_at_ms: Ms
    status: RunStatusWord
    first_session_open_ms: Ms
    last_session_open_ms: Ms
    sessions_requested: int
    # Exact dollars, only for a complete run.
    net_profit_usd: str | None = None


class IndexedStudy(BaseModel):
    id: Annotated[str, Field(pattern=r"^os_[a-f0-9]{40}$")]
    recorded_name: str
    underlying: str
    # The hash of the search's frozen baseline setup. Candidates a search changes have their own, inside the study.
    setup_sha256: Sha256 | None = None
    unreadable_reason: str | None = None
    created_at_ms: Ms
    updated_at_ms: Ms
    phase: str
    status: str


class IndexedApproval(BaseModel):
    id: Annotated[str, Field(pattern=r"^ogs_[a-f0-9]{32}(?:[a-f0-9]{8})?$")]
    recorded_name: str
    underlying: str | None = None
    setup_sha256: Sha256
    source_run_id: Annotated[int, Field(ge=1, le=RUN_ID_MAX)]
    # The search that produced the version, when the version records one.
    study_id: str | None = None
    origin: ApprovalOrigin
    status: Literal["ready", "stale", "revoked"]
    created_at_ms: Ms


class OptionsSetupIndexResponse(BaseModel):
    """Newest first within each list. A list shorter than its ``*_total`` was cut, and the page says so."""

    read_at_ms: Ms
    setups: list[IndexedSetup]
    runs: list[IndexedRun]
    runs_total: int = Field(ge=0)
    studies: list[IndexedStudy]
    studies_total: int = Field(ge=0)
    approvals: list[IndexedApproval]
    approvals_total: int = Field(ge=0)
