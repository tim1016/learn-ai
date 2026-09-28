"""UI contracts for one reviewed deployment and its custody-derived money."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.broker.alpaca.clerk.money import consent_cents
from app.schemas.account_authority import AuthorityKind


class DeploymentBudgetInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    amount_usd: str = Field(min_length=1, max_length=40)
    risk_revision: int = Field(strict=True, ge=0)
    review_token: str | None = Field(default=None, min_length=1, max_length=128)
    live_confirmation: str | None = Field(default=None, max_length=200)

    @field_validator("amount_usd")
    @classmethod
    def validate_amount(cls, value: str) -> str:
        cents = consent_cents(value)
        return f"{cents // 100}.{cents % 100:02d}"


class DeployBudgetConsent(BaseModel):
    """Server-resolved transient input, excluded from historical strategy seals."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    committed_cents: int = Field(strict=True, gt=0, le=2**63 - 1)
    risk_revision: int = Field(strict=True, ge=0)
    actor: str = Field(min_length=1)
    request_fingerprint: str = Field(min_length=1)
    world: AuthorityKind


class DeploymentBudgetShortcut(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: Literal["quarter", "half", "all", "position_headroom"]
    label: str
    amount_usd: str
    explanation: str


class DeploymentBudgetPreview(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["ready", "unavailable"]
    detail: str
    world: AuthorityKind
    custody_account_id: str
    observed_at_ms: int | None = Field(default=None, ge=0)
    minimum_budget_usd: str | None = None
    unreserved_usd: str | None = None
    estimated_price_usd: str | None = None
    risk_revision: int = Field(default=0, ge=0)
    risk_limits_summary: str = "Account risk evidence is unavailable."
    shortcuts: tuple[DeploymentBudgetShortcut, ...] = ()
    review_token: str | None = None
    confirmation_text: str | None = None


class DeploymentBudgetView(BaseModel):
    """All dollars are authored by Python, including display rounding."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["ready", "unavailable", "legacy"]
    detail: str
    strategy_instance_id: str
    world: AuthorityKind
    committed_usd: str | None = None
    realized_gross_usd: str | None = None
    fees_usd: str | None = None
    position_cost_usd: str | None = None
    pending_orders_usd: str | None = None
    outstanding_cash_usd: str | None = None
    free_usd: str | None = None
    released_usd: str | None = None
    shortfall_usd: str | None = None
    entry_eligible: bool = False
    observed_at_ms: int | None = Field(default=None, ge=0)


class BudgetDeployCommandReceipt(BaseModel):
    """Recoverable durable result; process absence never fabricates launch."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["pending", "deployed", "failed"]
    outcome: Literal["pending", "success", "failure"]
    receipt_id: str
    recorded_at_ms: int = Field(ge=0)
    command_id: str
    strategy_instance_id: str
    run_id: str
    account_id: str
    world: AuthorityKind
    committed_usd: str
    message: str
    explanation: str
    next_action: str
    panel_path: str


class BudgetAuthorityState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["legacy", "budget"]
    account_id: str
    authorization_version: int
    active_run_count: int
    review_token: str
    detail: str


class BudgetAuthorityApplyRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    review_token: str = Field(min_length=1, max_length=128)
