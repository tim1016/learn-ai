"""Read-only Account desk roster and Account service projections."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.account_reconciliation import AccountTriageVerdictState
from app.schemas.live_runs import GateResult

AccountEffectivePosture = Literal["PAPER_EXECUTION", "UNSAFE", "UNKNOWN"]
AccountServiceAttachmentState = Literal["ATTACHED", "UNATTACHED", "FENCED"]
AccountServicePhase = Literal["accepting", "reconnecting", "draining", "frozen"]
AccountServiceOperatingState = Literal["READY", "STANDBY", "ATTENTION"]
AccountBindingLedgerReadAuthority = Literal["legacy_registry", "clerk_ledger"]
AccountBindingLedgerParityState = Literal["clean", "dirty"]
AccountGatePromotionState = Literal[
    "SAFE_DEFAULT",
    "WAITING_FOR_SHADOW_PARITY",
    "WAITING_FOR_CLERK_RESTART_SMOKE",
    "CLERK_PROOF_ACTIVE",
]


class AccountServiceSummary(BaseModel):
    """Small Account service state for roster rows."""

    model_config = ConfigDict(frozen=True)

    attachment: AccountServiceAttachmentState
    phase: AccountServicePhase | None = None
    generation: int | None = Field(default=None, ge=1)
    operating_state: AccountServiceOperatingState
    headline: str = Field(min_length=1, max_length=160)


class AccountRosterVerdictSummary(BaseModel):
    """The exact dominant account-triage state, authored by the backend."""

    model_config = ConfigDict(frozen=True)

    state: AccountTriageVerdictState
    headline: str = Field(min_length=1, max_length=160)
    generated_at_ms: int = Field(ge=0, le=9_223_372_036_854_775_807)


class AccountRosterRow(BaseModel):
    """One configured or durably-known account available to the desk."""

    model_config = ConfigDict(frozen=True)

    account_id: str = Field(min_length=1, max_length=64)
    broker: Literal["IBKR"] = "IBKR"
    effective_posture: AccountEffectivePosture
    service: AccountServiceSummary
    latest_verdict_summary: AccountRosterVerdictSummary
    last_verified_at_ms: int | None = Field(default=None, ge=0, le=9_223_372_036_854_775_807)


class AccountsRosterResponse(BaseModel):
    """Versioned roster; an empty list means no account is configured or known."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[2] = 2
    rows: list[AccountRosterRow] = Field(default_factory=list)


class AccountServiceBinding(BaseModel):
    """Backend-owned attachment decision; the client never derives it from lease fields."""

    model_config = ConfigDict(frozen=True)

    state: AccountServiceAttachmentState
    generation: int | None = Field(default=None, ge=1)
    lease_generation: int | None = Field(default=None, ge=1)
    pending_retirement_proposals: int = Field(default=0, ge=0)
    ledger_read_authority: AccountBindingLedgerReadAuthority
    ledger_parity: AccountBindingLedgerParityState
    ledger_parity_issue_count: int = Field(ge=0)


class AccountServiceLease(BaseModel):
    """Durable Account Clerk lease projected under Account-service vocabulary."""

    model_config = ConfigDict(frozen=True)

    status: Literal["RUNNING", "DRAINING"]
    generation: int = Field(ge=1)
    started_at_ms: int = Field(ge=0, le=9_223_372_036_854_775_807)
    renewed_at_ms: int = Field(ge=0, le=9_223_372_036_854_775_807)
    valid_until_ms: int = Field(ge=0, le=9_223_372_036_854_775_807)


class AccountServiceJournalWatermark(BaseModel):
    """Newest durable Account Clerk journal entry, if any."""

    model_config = ConfigDict(frozen=True)

    last_seq: int | None = Field(default=None, ge=1)
    last_write_ms: int | None = Field(default=None, ge=0, le=9_223_372_036_854_775_807)


class AccountServiceGateAuthority(BaseModel):
    """Exact backend-authoritative account-gate promotion state."""

    model_config = ConfigDict(frozen=True)

    requested_authority: Literal["account_truth", "observation_lease"]
    effective_authority: Literal["account_truth", "observation_lease"]
    promotion_state: AccountGatePromotionState
    reason_code: str = Field(min_length=1, max_length=128)
    disposition: str | None = Field(default=None, max_length=128)
    action_authority: Literal["account_truth", "observation_lease"]
    action_gate: GateResult
    observed_session_dates: list[str] = Field(default_factory=list)
    lease_weaker_comparison_count: int = Field(ge=0)
    restart_smoke_recorded_at_ms: int | None = Field(default=None, ge=0)


class AccountServiceSessionPolicy(BaseModel):
    """The account-wide live-session enforcement verdict and exception flag."""

    model_config = ConfigDict(frozen=True)

    allow_outside_live_session: bool
    gate_result: GateResult


class AccountServiceStatusResponse(BaseModel):
    """Full read-only Account service status for one known account."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[3] = 3
    account_id: str = Field(min_length=1, max_length=64)
    attachment: AccountServiceAttachmentState
    phase: AccountServicePhase | None = None
    generation: int | None = Field(default=None, ge=1)
    generation_recorded_at_ms: int | None = Field(default=None, ge=0, le=9_223_372_036_854_775_807)
    source: str | None = Field(default=None, max_length=256)
    binding: AccountServiceBinding
    gate_authority: AccountServiceGateAuthority
    session_policy: AccountServiceSessionPolicy
    lease: AccountServiceLease | None = None
    journal: AccountServiceJournalWatermark
    operating_state: AccountServiceOperatingState
    headline: str = Field(min_length=1, max_length=160)
    detail: str = Field(min_length=1, max_length=512)
