"""Versioned, Python-authored account safety composition contract."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.engine.live.account_epoch import AccountEpoch
from app.schemas.account_directory import AccountEffectivePosture
from app.schemas.account_reconciliation import AccountReconciliationEvidenceRef, AccountReconciliationReceipt
from app.schemas.operator_blocker import OperatorBlocker, OperatorConfirmationCopy
from app.schemas.presented_operator_action import (
    PresentedOperatorActionAvailability,
    PresentedOperatorActionDisposition,
    PresentedOperatorActionId,
    PresentedOperatorActionPrecondition,
    PresentedOperatorActionTarget,
    PresentedOperatorEffectClass,
)

AccountSafetySnapshotVerdict = Literal[
    "CLEAN",
    "RECONCILING",
    "SUSPENDED",
    "CONTAMINATED",
    "STALE",
    "UNAVAILABLE",
]
AccountSafetySnapshotSourceState = Literal["FRESH", "STALE", "UNAVAILABLE"]
AccountSafetySnapshotEpochRelation = Literal[
    "CURRENT",
    "PRE_EPOCH",
    "UNATTESTED",
    "MISMATCH",
]


class PresentedOperatorAction(BaseModel):
    """Closed, snapshot-bound operator action; never an executable URL or command."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_id: PresentedOperatorActionId
    target: PresentedOperatorActionTarget
    snapshot_id: str = Field(min_length=16, max_length=64)
    snapshot_version: str = Field(min_length=16, max_length=64)
    evidence_refs: tuple[AccountReconciliationEvidenceRef, ...] = ()
    effect_class: PresentedOperatorEffectClass
    idempotency_key: str = Field(min_length=16, max_length=160)
    issued_at_ms: int = Field(ge=0)
    expires_at_ms: int = Field(ge=0)
    presentation_token: str | None = Field(default=None, min_length=64, max_length=64)
    preconditions: tuple[PresentedOperatorActionPrecondition, ...] = ()
    confirmation: OperatorConfirmationCopy
    availability: PresentedOperatorActionAvailability
    disposition: PresentedOperatorActionDisposition
    finished_copy: str = Field(min_length=1, max_length=512)


class PresentedOperatorActionResult(BaseModel):
    """Durable dispatch result; acceptance and observed effect stay distinct."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_id: PresentedOperatorActionId
    action_attempt_id: str = Field(min_length=16, max_length=160)
    state: Literal["ACCEPTED", "IN_PROGRESS", "OUTCOME_UNKNOWN"]
    replayed: bool
    finished_copy: str = Field(min_length=1, max_length=512)
    reconciliation_receipt: AccountReconciliationReceipt | None = None
    refreshed_snapshot_id: str | None = Field(default=None, min_length=16, max_length=64)


class PresentedOperatorActionRejection(BaseModel):
    """Typed refusal for a closed presented action; never an inferred UI error."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason_code: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=512)
    snapshot_id: str | None = Field(default=None, min_length=16, max_length=64)
    snapshot_version: str | None = Field(default=None, min_length=16, max_length=64)


class PresentedOperatorActionRejectionResponse(BaseModel):
    """FastAPI HTTP-error envelope for a presented-action refusal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    detail: PresentedOperatorActionRejection


class AccountSafetySnapshotSource(BaseModel):
    """One independently ageing source; assembly never restamps it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(min_length=1, max_length=128)
    state: AccountSafetySnapshotSourceState
    critical: bool = True
    epoch_relation: AccountSafetySnapshotEpochRelation = "UNATTESTED"
    as_of_ms: int | None = Field(default=None, ge=0)
    age_ms: int | None = Field(default=None, ge=0)
    hard_ttl_ms: int | None = Field(default=None, ge=1)
    reason_code: str = Field(min_length=1, max_length=160)
    epoch: AccountEpoch | None = None
    reconciliation_id: str | None = Field(default=None, min_length=1, max_length=160)
    reconciliation_state: Literal["CLEAN", "NOT_PROVEN"] | None = None
    evidence_refs: tuple[AccountReconciliationEvidenceRef, ...] = ()


class AccountSafetySnapshotCustody(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    retired_owner_custody_count: int = Field(ge=0)
    clerk_journal_last_seq: int | None = Field(default=None, ge=1)
    a0_custody_accepted_count: int = Field(ge=0)
    a1_broker_write_started_count: int = Field(ge=0)
    a2_broker_known_count: int = Field(ge=0)
    a3_economic_terminal_count: int = Field(ge=0)


class AccountSafetySnapshotExposure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    open_order_count: int = Field(ge=0)
    execution_count: int = Field(ge=0)
    position_count: int = Field(ge=0)
    unmanaged_or_unknown_count: int = Field(ge=0)


class AccountSafetySnapshot(BaseModel):
    """Read-only safety spine shared by Deploy, Bot Control, and Account Desk."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    snapshot_version: str = Field(min_length=16, max_length=64)
    snapshot_id: str = Field(min_length=16, max_length=64)
    generated_at_ms: int = Field(ge=0, le=9_223_372_036_854_775_807)
    account_id: str = Field(min_length=1, max_length=64)
    posture: AccountEffectivePosture
    verdict: AccountSafetySnapshotVerdict
    reason_code: str = Field(min_length=1, max_length=160)
    verdict_reason: str = Field(min_length=1, max_length=512)
    account_epoch: AccountEpoch | None = None
    reconciliation_id: str | None = Field(default=None, min_length=1, max_length=160)
    reconciliation_state: Literal["CLEAN", "NOT_PROVEN"] | None = None
    sources: tuple[AccountSafetySnapshotSource, ...]
    custody: AccountSafetySnapshotCustody
    exposure: AccountSafetySnapshotExposure
    blockers: tuple[OperatorBlocker, ...] = ()
    outage_diff: dict[str, object] | None = None
    evidence_refs: tuple[AccountReconciliationEvidenceRef, ...] = ()
    actions: tuple[PresentedOperatorAction, ...] = ()
