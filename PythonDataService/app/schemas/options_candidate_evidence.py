"""Bounded retained candidate reads; opening these views cannot expose final history (#3397)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.options_backtest.setup import OptionsBacktestSetup
from app.schemas.options_golden_search import Candidate
from app.utils.session_anchors import MAX_TIMESTAMP_MS


class OptionsCandidateListItem(BaseModel):
    candidate_hash: str
    label: str
    is_original: bool
    state: Literal["retained", "uncomputed"]
    verdict: str | None
    net_profit_usd: str | None


class OptionsCandidatePage(BaseModel):
    items: list[OptionsCandidateListItem]
    total: int
    offset: int
    limit: int


class OptionsCandidateSampling(BaseModel):
    state: Literal["available", "unavailable"]
    raw_observations: int
    displayed_observations: int
    maximum_observations: int = 2000
    explanation: str


class OptionsCandidateCoverage(BaseModel):
    scheduled_sessions: int
    replayed_sessions: int
    traded_sessions: int
    required_held_marks: int
    missing_held_marks: int
    complete: bool


class OptionsCandidateEvidence(BaseModel):
    study_id: str
    candidate_hash: str
    evidence_identity: str
    evidence_blob_sha256: str
    scope: Literal["development", "final"]
    start_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    end_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    setup: OptionsBacktestSetup
    settings_description: list[str]
    candidate: Candidate
    coverage: OptionsCandidateCoverage
    sampling: OptionsCandidateSampling
    session_offset: int
    session_limit: int
    session_total: int
    unobserved_sessions: int = Field(ge=0)
