"""Wire models for account-scoped reconciliation receipts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.account_condition_actions import AccountCureAction
from app.schemas.account_truth import (
    AccountTruthFinalVerdict,
    AccountTruthResponse,
    AccountTruthSeverity,
)
from app.schemas.live_runs import GateResult

AccountReconciliationState = Literal["CLEAN", "NOT_PROVEN"]
AccountExposureResolution = Literal["flat", "accepted_override", "unresolved"]
AccountConditionType = Literal[
    "exposure_freeze",
    "account_freeze",
    "evidence_stale",
    "daemon_unreachable",
    "evidence_missing",
    "exit_flatten_failed",
    "exit_lease_stuck",
    "crashed",
    "ended_without_status",
    "liveness_unproven",
    "repeated_unclean_start",
]


class AccountReconciliationEvidenceRef(BaseModel):
    """Concrete evidence pointer stored with an account reconciliation receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(min_length=1, max_length=128)
    ref: str = Field(min_length=1, max_length=512)
    detail: str | None = Field(default=None, max_length=512)


class AccountReconciliationReceipt(BaseModel):
    """Durable account-clean proof over the existing Account Truth verdict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    receipt_id: str = Field(min_length=1, max_length=160)
    account_id: str = Field(min_length=1, max_length=64)
    requested_account_id: str = Field(min_length=1, max_length=64)
    connected_account_id: str | None = Field(default=None, max_length=64)
    state: AccountReconciliationState
    account_truth_verdict: AccountTruthFinalVerdict
    account_truth_severity: AccountTruthSeverity
    final_gate_result: GateResult
    exposure_resolution: AccountExposureResolution = "unresolved"
    account_truth: AccountTruthResponse
    evidence_refs: list[AccountReconciliationEvidenceRef] = Field(default_factory=list)
    generated_at_ms: int = Field(ge=0)
    account_truth_generated_at_ms: int = Field(ge=0)
    expires_at_ms: int = Field(ge=0)
    ttl_ms: int = Field(ge=1)


class AccountReconciliationAutomationPolicy(BaseModel):
    """Durable operator policy for bot-owned trade reconciliation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    account_id: str = Field(min_length=1, max_length=64)
    enabled: bool = False
    updated_at_ms: int = Field(ge=0)
    updated_by: str = Field(min_length=1, max_length=128)


class AccountReconciliationAutomationPolicyUpdate(BaseModel):
    """Operator request to enable or disable automatic reconciliation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool
    updated_by: str = Field(default="account-monitor.operator", min_length=1, max_length=128)


class AccountTriageBotRef(BaseModel):
    """Bot identity affected by an account-scoped triage row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_instance_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    bot_order_namespace: str = Field(min_length=1, max_length=256)
    lifecycle_state: str = Field(min_length=1, max_length=32)


class AccountTriageGateRow(BaseModel):
    """Backend-authored account triage row rendered by recovery pages."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gate_id: str = Field(min_length=1, max_length=128)
    status: Literal["pass", "block", "freeze", "unknown"]
    scope: Literal["account", "reconciliation"]
    severity: Literal["ok", "warning", "critical"]
    title: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    operator_next_step: str | None = None
    source: str = Field(min_length=1)
    evidence_at_ms: int = Field(ge=0)
    affected_strategy_instance_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[AccountReconciliationEvidenceRef] = Field(default_factory=list)
    primary_remediation: str | None = None


class AccountConditionOwner(BaseModel):
    """Owner of a derived sick-bay condition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    owner_type: Literal["account", "bot"]
    owner_id: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=256)
    strategy_instance_id: str | None = Field(default=None, max_length=128)
    run_id: str | None = Field(default=None, max_length=128)
    lifecycle_state: str | None = Field(default=None, max_length=32)


class AccountConditionRow(BaseModel):
    """ADR-0026 sick-bay condition row with exactly one cure action."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    condition_type: AccountConditionType
    scope: Literal["account", "bot"]
    owner: AccountConditionOwner
    severity: Literal["warning", "critical"]
    title: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    operator_next_step: str | None = None
    source: str = Field(min_length=1)
    evidence_at_ms: int = Field(ge=0)
    evidence_refs: list[AccountReconciliationEvidenceRef] = Field(default_factory=list)
    affected_strategy_instance_ids: list[str] = Field(default_factory=list)
    cure_action: AccountCureAction


class AccountFreezeBanner(BaseModel):
    """Backend-authored banner copy for active account freezes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    headline: str = Field(min_length=1, max_length=160)
    detail: str = Field(min_length=1, max_length=512)


class AccountObservationHistoryEvent(BaseModel):
    """One durable account-observation transition, never a heartbeat."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["VERIFIED", "REVOKED"]
    reason_line: str = Field(min_length=1, max_length=512)
    recorded_at_ms: int = Field(ge=0)


class AccountObservationView(BaseModel):
    """Operator-facing durable observation proof and its transition history."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["VERIFIED", "REVOKED", "EXPIRED", "ABSENT"]
    reason_line: str = Field(min_length=1, max_length=512)
    observed_at_ms: int | None = Field(default=None, ge=0)
    valid_until_ms: int | None = Field(default=None, ge=0)
    history: list[AccountObservationHistoryEvent] = Field(default_factory=list)


class AccountTriageResponse(BaseModel):
    """Thin account-scoped recovery projection over existing authorities."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    generated_at_ms: int = Field(ge=0)
    account_id: str = Field(min_length=1, max_length=64)
    strategy_instance_id: str | None = Field(default=None, max_length=128)
    summary_headline: str = Field(min_length=1)
    summary_detail: str = Field(min_length=1)
    overall_gate_result: GateResult
    account_reconciliation_receipt: AccountReconciliationReceipt | None = None
    account_reconciliation_valid_until_ms: int | None = Field(default=None, ge=0)
    reconciliation_automation_policy: AccountReconciliationAutomationPolicy
    account_observation: AccountObservationView
    gate_rows: list[AccountTriageGateRow] = Field(default_factory=list)
    conditions: list[AccountConditionRow] = Field(default_factory=list)
    freeze_banner: AccountFreezeBanner | None = None
    clear_freeze_actionable: bool = False
    affected_bots: list[AccountTriageBotRef] = Field(default_factory=list)


class AccountClearFreezeRequest(BaseModel):
    """Operator request to clear an active freeze using the latest clean receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    requested_by: str = Field(default="account-monitor.operator", min_length=1, max_length=128)
    receipt_id: str | None = Field(default=None, min_length=1, max_length=160)
    reason: str | None = Field(default=None, max_length=512)


class AccountClearFreezeResponse(BaseModel):
    """Receipt for a guarded account-freeze clear."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    account_id: str = Field(min_length=1, max_length=64)
    cleared: bool = True
    cleared_source: Literal["account_recovery_proof"] = "account_recovery_proof"
    recovery_id: str = Field(min_length=1, max_length=128)
    receipt_id: str = Field(min_length=1, max_length=160)
    gate_result: GateResult
    triage: AccountTriageResponse


class AccountAcceptExposureOverrideRequest(BaseModel):
    """Operator request to accept exposure and clear an exposure freeze."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    requested_by: str = Field(default="account-monitor.operator", min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=512)
    strategy_instance_id: str | None = Field(default=None, max_length=128)
    run_id: str | None = Field(default=None, max_length=128)
    bot_order_namespace: str | None = Field(default=None, max_length=256)


class AccountAcceptExposureOverrideResponse(BaseModel):
    """Receipt for an audited exposure override that clears a freeze."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    account_id: str = Field(min_length=1, max_length=64)
    cleared: bool = True
    cleared_source: Literal["account_audited_override"] = "account_audited_override"
    override_id: str = Field(min_length=1, max_length=128)
    triage: AccountTriageResponse


class AccountFalseCrashBackfillResponse(BaseModel):
    """Summary of the append-only false-crash registry repair."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    accounts_scanned: int = Field(ge=0)
    candidate_rows: int = Field(ge=0)
    rows_repaired: int = Field(ge=0)
    rows_skipped_no_disproof: int = Field(ge=0)
    invalid_account_dirs: int = Field(ge=0)
    repaired_run_ids: list[str] = Field(default_factory=list)
