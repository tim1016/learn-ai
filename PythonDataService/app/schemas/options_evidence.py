"""Read-only identity and independent verdict facts for the Options workspace (#3395)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.options_backtest.setup import OptionsBacktestSetup
from app.schemas.options_golden_search import Exposure, GateOutcome
from app.utils.session_anchors import MAX_TIMESTAMP_MS


class OptionsEvidenceDimension(BaseModel):
    status: str
    explanation: str


class OptionsEvidencePeriod(BaseModel):
    first_session_open_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    last_session_open_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    scheduled_sessions: int = Field(ge=0)
    observed_sessions: int | None = Field(ge=0)


class OptionsEvidenceApproval(BaseModel):
    golden_id: str
    source_run_id: int
    setup_sha256: str
    source: Literal["backtest", "search", "not_recorded"]
    identity_matches: bool
    proof_status: Literal["ready", "stale", "revoked"]
    proof_explanation: str
    note: str
    study_id: str | None
    study_manifest_sha256: str | None
    research_verdict: Literal["meets_rules", "owner_exception"] | None
    failed_rules: list[GateOutcome]
    waived_gate_ids: list[str]
    exposure: Exposure | None


class OptionsEvidenceRelation(BaseModel):
    kind: Literal["inspected_search", "historical_comparator"]
    subject_id: str
    setup_sha256: str
    settings_match: bool
    explanation: str


class OptionsEvidenceSearchSetting(BaseModel):
    path: str
    mode: Literal["fixed", "search"]
    values: list[str]


class OptionsEvidenceWorkspace(BaseModel):
    subject_kind: Literal["run", "golden", "study"]
    subject_id: str
    evidence_identity: str
    name: str
    setup: OptionsBacktestSetup
    setup_sha256: str
    source_run_id: int | None
    study_id: str | None
    period: OptionsEvidencePeriod
    headline: str
    replay: OptionsEvidenceDimension
    coverage: OptionsEvidenceDimension
    research: OptionsEvidenceDimension
    final: OptionsEvidenceDimension
    approval: OptionsEvidenceDimension
    approvals: list[OptionsEvidenceApproval]
    relations: list[OptionsEvidenceRelation]
    settings_description: list[str]
    frozen_search_settings: list[OptionsEvidenceSearchSetting] = Field(default_factory=list)
