"""Durable canary activation evidence and Clerk-proved rollback verdicts.

See ``app/services/canary_admission.py`` for the operator-controlled
activation ledger and how rollback composes with the existing Stop custody
proof (``app.services.bot_carryover.prove_stop_outcome``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.signal_program_seal import semantic_payload_hash

_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class CanaryActivationEvidence(BaseModel):
    """Fresh proof that one program is eligible for canary activation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    validation_event_id: str = Field(min_length=1)
    validation_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    program_version: str = Field(min_length=1)
    golden_trace_root: str = Field(pattern=_SHA256_PATTERN)
    running_artifact_digest: str = Field(pattern=_SHA256_PATTERN)
    qualification_receipt_hash: str = Field(pattern=_SHA256_PATTERN)
    qualification_suite: str = Field(min_length=1)
    qualified_at_ms: int = Field(ge=0)


class CanaryActivationPlan(BaseModel):
    """Short-lived, content-addressed intent awaiting explicit confirmation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    plan_id: str = Field(pattern=_SHA256_PATTERN)
    confirmation_token: str = Field(pattern=_SHA256_PATTERN)
    program_key: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    created_at_ms: int = Field(ge=0)
    expires_at_ms: int = Field(ge=0)
    ledger_path: str = Field(min_length=1)
    expected_ledger_head_hash: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    evidence: CanaryActivationEvidence

    @model_validator(mode="after")
    def validate_content_identity(self) -> CanaryActivationPlan:
        if self.expires_at_ms <= self.created_at_ms:
            raise ValueError("activation plan expiry must be after creation")
        expected = semantic_payload_hash(
            self.model_dump(mode="json", exclude={"plan_id", "confirmation_token"})
        )
        if self.plan_id != expected or self.confirmation_token != expected:
            raise ValueError("activation plan identity does not match its payload")
        return self


class CanaryActivationRequest(BaseModel):
    """Operator reason for preparing one exact Paper-access pairing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str = Field(min_length=10, max_length=500)


class CanaryActivationConfirmation(BaseModel):
    """The exact reviewed plan and its content-addressed confirmation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan: CanaryActivationPlan
    confirmation_token: str = Field(pattern=_SHA256_PATTERN)


class CanaryAdmissionEvent(BaseModel):
    """One append-only activation or revocation decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    sequence: int = Field(ge=1)
    action: Literal["activated", "revoked"]
    program_key: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    recorded_at_ms: int = Field(ge=0)
    evidence: CanaryActivationEvidence | None = None
    previous_event_hash: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    event_hash: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_event(self) -> CanaryAdmissionEvent:
        if self.action == "activated" and self.evidence is None:
            raise ValueError("an activation event requires its proof evidence")
        if self.action == "revoked" and self.evidence is not None:
            raise ValueError("a revocation event cannot replace activation evidence")
        expected = semantic_payload_hash(self.model_dump(mode="json", exclude={"event_hash"}))
        if self.event_hash != expected:
            raise ValueError("canary admission event hash does not match its payload")
        return self


class CanaryAdmissionLedger(BaseModel):
    """Closed, append-only history from which active exact pairings derive."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    events: tuple[CanaryAdmissionEvent, ...] = ()


class CanaryRollbackDecision(BaseModel):
    """Whether stopping one canary run is admitted at a Clerk-proved boundary.

    A rollback is refused outright, not merely recorded, when the Clerk
    cannot prove the resulting position is flat or an explicitly approved
    carried exposure. This differs from an ordinary Stop, which always
    persists an honest checkpoint even when custody is unprovable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_instance_id: str
    allowed: bool
    reason_code: str
    explanation: str
    next_step: str | None
    stop_outcome: Literal[
        "STOPPED_FLAT",
        "STOPPED_WITH_APPROVED_ATTRIBUTED_EXPOSURE",
        "STOP_REQUIRES_FLATTEN",
        "STOPPED_CUSTODY_UNPROVABLE",
    ]
    evaluated_at_ms: int = Field(ge=0)


__all__ = [
    "CanaryActivationConfirmation",
    "CanaryActivationEvidence",
    "CanaryActivationPlan",
    "CanaryActivationRequest",
    "CanaryAdmissionEvent",
    "CanaryAdmissionLedger",
    "CanaryRollbackDecision",
]
