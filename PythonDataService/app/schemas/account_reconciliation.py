"""Wire models for account-scoped reconciliation receipts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.account_truth import (
    AccountTruthFinalVerdict,
    AccountTruthResponse,
    AccountTruthSeverity,
)
from app.schemas.live_runs import GateResult

AccountReconciliationState = Literal["CLEAN", "NOT_PROVEN"]


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
    account_truth: AccountTruthResponse
    evidence_refs: list[AccountReconciliationEvidenceRef] = Field(default_factory=list)
    generated_at_ms: int = Field(ge=0)
    account_truth_generated_at_ms: int = Field(ge=0)
    expires_at_ms: int = Field(ge=0)
    ttl_ms: int = Field(ge=1)


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
    gate_rows: list[AccountTriageGateRow] = Field(default_factory=list)
    affected_bots: list[AccountTriageBotRef] = Field(default_factory=list)
