from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ValidationState = Literal["validated", "needs_validation"]
StrategyValidationFlag = Literal["validated", "invalidated"]
BehavioralEquivalenceVerdict = Literal["accepted_for_deploy", "evidence_only", "rejected"]


class StrategyValidationDiagnostics(BaseModel):
    verdict: str
    trades_matched: int = Field(ge=0)
    trades_validated: int = Field(ge=0)
    pnl_max_abs_diff: str
    divergence_counts: dict[str, int] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class StrategyEvidenceSnapshot(BaseModel):
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

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("reason is required")
        return stripped


class StrategyValidationEntry(BaseModel):
    strategy_key: str
    display_name: str
    description: str
    validation_state: ValidationState
    deployable: bool
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
