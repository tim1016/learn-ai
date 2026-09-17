"""Transport models for the supervised Shadow-to-Live graduation ceremony."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class LiveGraduationStatus(BaseModel):
    """Backend-authored state for the Configuration page's authority rail."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str
    configured_mode: Literal["live"]
    authority: Literal["shadow", "live", "unavailable"]
    state: Literal["review_available", "graduated", "blocked"]
    headline: str
    detail: str
    next_action: str | None
    restart_managed: bool


class LiveGraduationPlanView(BaseModel):
    """The exact facts an operator reviews before the one-shot activation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    account_id: str
    created_at_ms: int = Field(ge=0)
    expires_at_ms: int = Field(ge=0)
    broker_observed_at_ms: int = Field(ge=0)
    position_count: int = Field(ge=0)
    open_order_count: int = Field(ge=0)
    stopped_bot_ids: tuple[str, ...]
    backup_reference: str
    daily_loss_fraction: float = Field(gt=0, lt=1)
    daily_loss_usd: float = Field(gt=0)
    arming_max_sessions: int = Field(ge=1)
    extended_hours_entry_bps: float = Field(ge=0, lt=10_000)
    extended_hours_exit_bps: float = Field(ge=0, lt=10_000)
    consequence: str


class LiveGraduationApplyRequest(BaseModel):
    """The content-addressed plan confirmation; no force or path fields exist."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation_token: str = Field(pattern=r"^[0-9a-f]{64}$")


class LiveGraduationApplyOutcome(BaseModel):
    """Durable activation receipt returned before the worker restarts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str
    plan_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal["restart_scheduled"]
    receipt_reference: str
    activated_at_ms: int = Field(ge=0)
    message: str

