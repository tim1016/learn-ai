"""Wire shapes every lake-sourced research study shares (#3523; PRD #3490 R13).

A study that computes from the data lake says where its bars came from (the ``data_source`` block of
:func:`app.research.lake_study.data_source_block`) and, when the lake could not supply them, why not
(:meth:`app.research.study_inputs.StudyInputRefusal.as_dict`). These models are that wire as a typed HTTP response
declares it; the browser's side is ``Frontend/src/app/shared/lake-study/lake-study.ts``. A study with a typed route
extends :class:`LakeDataSource` with its own fields. A study that answers as a job returns the dicts and needs none of
this.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.research.study_inputs import AdjustmentMode, RefusalCode, SessionPolicy


class StudyCoverageGroup(BaseModel):
    """Distinct-session counts from lake admission for the window or its warmup lead-in."""

    expected_sessions: int
    reused: int = Field(..., description="Sessions ready before this run began")
    added: int = Field(..., description="Sessions this run made ready")
    missing: int = Field(..., description="Sessions not held and not fetched")
    failed: int = Field(..., description="building + corrupt + unavailable_from_provider")
    building: int
    corrupt: int
    unavailable_from_provider: int


class StudyArtifactCounts(BaseModel):
    """Artifact counts, kept apart from session counts (a session is one minute artifact plus metadata)."""

    fetched: int
    reused: int
    refreshed: int


class StudyCoverage(BaseModel):
    window: StudyCoverageGroup
    warmup: StudyCoverageGroup
    artifacts: StudyArtifactCounts


class LakeEffectiveWindow(BaseModel):
    """The first and last session admitted (ET midnight, int64 ms UTC), unlike the requested bounds in the identity."""

    start_ms: int
    end_ms: int


class LakeCacheState(BaseModel):
    hit: bool
    cached_at_ms: int | None = Field(None, description="When the cache stored the result; null when not recorded")


class LakeDataSource(BaseModel):
    """Where a study's bars came from: the lake input identity, the admitted bytes, and what admission did."""

    kind: Literal["lake"] = "lake"
    provider: str
    symbol: str
    price_adjustment_mode: AdjustmentMode
    session_policy: SessionPolicy
    window_start_ms: int = Field(..., description="Requested first date, ET midnight, int64 ms UTC")
    window_end_ms: int = Field(..., description="Requested last date, ET midnight, int64 ms UTC")
    effective_window: LakeEffectiveWindow | None = Field(None, description="Null until admission succeeds")
    warmup_sessions: int = Field(..., description="Lead-in sessions the run read (after any unsupplied prefix was dropped)")
    bar_minutes: int
    aggregation: str
    identity_digest: str = Field(..., description="Hash of every input that decides the numbers")
    calculation_version: str
    data_root_id: str | None = None
    snapshot_digest: str | None = Field(None, description="sha256 of every byte admitted; null when nothing was admitted")
    corporate_action_versions: dict[str, str] = Field(default_factory=dict)
    coverage: StudyCoverage | None = None
    minute_bars_read: int | None = Field(None, description="One-minute bars read, warmup lead-in included")
    bars_used: int | None = Field(None, description="Bars the statistics ran on, after the folding and the warmup trim")
    cache: LakeCacheState


class StudySessionIssue(BaseModel):
    """One required session admission could not make ready, and why."""

    group: Literal["window", "warmup"]
    trading_date_ms: int = Field(..., description="The session's canonical open, int64 ms UTC")
    outcome: Literal["missing", "building", "corrupt", "unavailable_from_provider"]
    reason: str
    failure_reason: str | None = Field(None, description="The materializer's own reason code for the session, when it gave one")


class StudyRefusal(BaseModel):
    code: RefusalCode
    reason: str
    sessions: list[StudySessionIssue] = Field(..., description="The sessions that could not be made ready; the first of sessions_total")
    sessions_total: int
    coverage: StudyCoverage | None = Field(None, description="Null when admission never started")


class LakeRefusedResult(BaseModel):
    """A study that did not run because the lake could not supply its inputs: the same body a refused job parks."""

    success: Literal[False] = False
    status: Literal["refused"] = "refused"
    error: str
    refusal: StudyRefusal
    data_source: LakeDataSource | None = Field(None, description="Null when the refusal came before anything was read")
