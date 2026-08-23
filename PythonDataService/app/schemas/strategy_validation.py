from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ValidationState = Literal["validated", "needs_validation"]
StrategyValidationFlag = Literal["validated", "invalidated"]
BehavioralEquivalenceVerdict = Literal["accepted_for_deploy", "evidence_only", "rejected"]
StrategyCategory = Literal["production_candidate", "operational_validation_harness"]
StrategyProofState = Literal["current", "stale", "missing", "blocked", "rejected", "unreadable"]
StrategyProofStageState = Literal["complete", "stale", "missing", "blocked", "not_applicable"]
StrategyProofActionKind = Literal["external_link"]
StrategyArtifactState = Literal["current", "stale", "missing", "unreadable"]


class StrategyValidationDiagnostics(BaseModel):
    verdict: str
    trades_matched: int = Field(ge=0)
    trades_validated: int = Field(ge=0)
    pnl_max_abs_diff: str
    divergence_counts: dict[str, int] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class StrategyArtifactCheck(BaseModel):
    label: str
    ref: str | None = None
    state: StrategyArtifactState
    recorded_sha256: str | None = None
    current_sha256: str | None = None


class StrategyProofAction(BaseModel):
    kind: StrategyProofActionKind = "external_link"
    label: str
    href: str


class StrategyProofStage(BaseModel):
    stage_id: str
    title: str
    state: StrategyProofStageState
    authority: str
    summary: str
    next_step: str | None = None
    actions: list[StrategyProofAction] = Field(default_factory=list)
    evidence: list[StrategyArtifactCheck] = Field(default_factory=list)


class StrategyProofDossier(BaseModel):
    state: StrategyProofState = "missing"
    completed_stages: int = Field(default=0, ge=0)
    total_stages: int = Field(default=0, ge=0)
    blocking_stage_id: str | None = None
    blocking_summary: str | None = None
    stages: list[StrategyProofStage] = Field(default_factory=list)


class StrategyEvidenceSnapshot(BaseModel):
    validator_code_ref: str | None = None
    validator_code_sha256: str | None = None
    settings_file_ref: str | None = None
    settings_file_sha256: str | None = None
    qc_cloud_backtest_id: str | None = None
    audit_copy_ref: str | None = None
    audit_copy_sha256: str | None = None
    reconciliation_ref: str | None = None
    validation_case_symbol: str | None = None
    reconciliation_status: str | None = None
    diagnostics: StrategyValidationDiagnostics | None = None


class StrategyBehavioralEquivalence(BaseModel):
    verdict: BehavioralEquivalenceVerdict
    detail: str
    tolerance: str | None = None
    tolerance_reason: str | None = None
    gating_divergence_counts: dict[str, int] = Field(default_factory=dict)


class StrategyValidationFlagEvent(BaseModel):
    event_id: str
    event_version: Literal["1.0"] = "1.0"
    strategy_key: str
    flag: StrategyValidationFlag
    flagged_by: str
    flagged_at_ms: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=4000)
    behavioral_equivalence: StrategyBehavioralEquivalence
    evidence_snapshot: StrategyEvidenceSnapshot
    evidence_snapshot_sha256: str
    superseded_by_event_id: str | None = None


class StrategyValidationFlagRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flag: StrategyValidationFlag
    reason: str = Field(min_length=1, max_length=4000)
    qc_cloud_backtest_id: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("reason is required")
        return stripped

    @field_validator("qc_cloud_backtest_id")
    @classmethod
    def backtest_id_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("qc_cloud_backtest_id cannot be blank")
        return stripped


class StrategyValidationEntry(BaseModel):
    strategy_key: str
    display_name: str
    description: str
    strategy_category: StrategyCategory = "production_candidate"
    validation_state: ValidationState
    deployable: bool
    proof: StrategyProofDossier = Field(default_factory=StrategyProofDossier)
    validator_code_ref: str | None = None
    validator_code_sha256: str | None = None
    settings_file_ref: str | None = None
    settings_file_sha256: str | None = None
    qc_cloud_backtest_id: str | None = None
    audit_copy_ref: str | None = None
    audit_copy_sha256: str | None = None
    reconciliation_ref: str | None = None
    validation_case_symbol: str | None = None
    reconciliation_status: str | None = None
    diagnostics: StrategyValidationDiagnostics | None = None
    behavioral_equivalence: StrategyBehavioralEquivalence | None = None
    current_flag_event: StrategyValidationFlagEvent | None = None
    flag_events: list[StrategyValidationFlagEvent] = Field(default_factory=list)


class StrategyReferenceCode(BaseModel):
    path: str
    sha256: str
    recorded_sha256: str | None = None
    state: StrategyArtifactState = "current"
    language: str = "python"
    source: str


class StrategyValidationDetail(StrategyValidationEntry):
    reference_code: StrategyReferenceCode | None = None


class StrategyValidationCatalog(BaseModel):
    strategies: list[StrategyValidationEntry]


class StrategyValidationRefreshResult(BaseModel):
    refresh_id: str
    refreshed_at_ms: int = Field(ge=0)
    detail: StrategyValidationDetail
