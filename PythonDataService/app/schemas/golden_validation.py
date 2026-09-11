"""HTTP contracts for Validation Golden Run designation and review."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.research.golden_validation.service import GoldenValidationDossier


class DesignateGoldenRunRequest(BaseModel):
    source_run_id: int = Field(gt=0)
    command_id: str = Field(min_length=1, max_length=200)
    label: str | None = Field(default=None, max_length=120)
    rationale: str = Field(min_length=3, max_length=4000)

    @field_validator("command_id", "rationale")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator("label")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        stripped = value.strip() if value is not None else None
        return stripped or None


class ReviewGoldenRunRequest(BaseModel):
    command_id: str = Field(min_length=1, max_length=200)
    expected_evidence_revision: str = Field(min_length=64, max_length=64)
    decision: Literal["accept", "reject"]
    reason: str = Field(min_length=3, max_length=4000)
    quantconnect_backtest_id: str | None = Field(default=None, max_length=200)
    authorized_program_version: str | None = Field(default=None, max_length=200)

    @field_validator("command_id", "expected_evidence_revision", "reason")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator("quantconnect_backtest_id", "authorized_program_version")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        stripped = value.strip() if value is not None else None
        return stripped or None


class GoldenValidationApplicabilityRequest(BaseModel):
    strategy_name: str = Field(min_length=1, max_length=120)
    program_version: str | None = Field(default=None, max_length=200)
    symbol: str = Field(min_length=1, max_length=32)
    parameters: dict[str, Any]
    window: dict[str, Any]
    data_policy: dict[str, Any] | None
    execution: dict[str, Any]

    @field_validator("strategy_name", "symbol")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator("program_version")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        stripped = value.strip() if value is not None else None
        return stripped or None

    def as_configuration(self) -> dict[str, Any]:
        payload = self.model_dump()
        payload["symbol"] = self.symbol.strip().upper()
        return payload


class GoldenValidationApplicabilityResponse(BaseModel):
    golden_validation_id: int
    applicable: bool
    state: str
    classification: str | None
    mismatched_fields: list[str]
    explanation: str


class GoldenReviewResponse(BaseModel):
    id: int
    decision: Literal["accept", "reject"]
    classification: Literal["engine_agreement", "reviewed_deviations", "manual_override"] | None
    evidence_state: str
    reason: str
    quantconnect_backtest_id: str | None
    authorized_program_version: str | None
    reviewed_by: str
    reviewed_at_ms: int
    expected_evidence_revision: str


class GoldenValidationResponse(BaseModel):
    id: int
    source_run_id: int
    label: str | None
    strategy_name: str
    symbol: str
    rationale: str
    designated_by: str
    designated_at_ms: int
    state: str
    validation_case: dict[str, Any]
    evidence_state: str
    evidence_revision: str
    parity_evidence: dict[str, Any]
    latest_review: GoldenReviewResponse | None
    review_is_current: bool | None
    reviews: list[GoldenReviewResponse]

    @classmethod
    def from_dossier(cls, dossier: GoldenValidationDossier) -> GoldenValidationResponse:
        golden = dossier.golden_run
        reviews = [
            GoldenReviewResponse(
                id=row.id,
                decision=row.decision,
                classification=row.classification,
                evidence_state=row.evidence_state,
                reason=row.reason,
                quantconnect_backtest_id=row.quantconnect_backtest_id,
                authorized_program_version=row.authorized_program_version,
                reviewed_by=row.reviewed_by,
                reviewed_at_ms=row.reviewed_at_ms,
                expected_evidence_revision=row.expected_evidence_revision,
            )
            for row in dossier.reviews
        ]
        return cls(
            id=golden.id,
            source_run_id=golden.source_run_id,
            label=golden.label,
            strategy_name=golden.strategy_name,
            symbol=golden.symbol,
            rationale=golden.rationale,
            designated_by=golden.designated_by,
            designated_at_ms=golden.designated_at_ms,
            state=dossier.state,
            validation_case=dossier.validation_case,
            evidence_state=dossier.evidence.state,
            evidence_revision=dossier.evidence.revision,
            parity_evidence=dossier.evidence.payload,
            latest_review=reviews[0] if reviews else None,
            review_is_current=dossier.review_is_current,
            reviews=reviews,
        )
