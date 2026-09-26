"""Browser contracts for the existing supervised live arming ceremony."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.broker_configuration import LiveEnvelopePayload
from app.schemas.exit_terms import ExitTerms
from app.utils.session_anchors import MAX_TIMESTAMP_MS


class ArmingPlanView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_id: str
    confirmation_token: str
    account_id: str
    strategy_instance_id: str
    created_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    expires_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    envelope: LiveEnvelopePayload
    exit_terms: ExitTerms
    changes: tuple[str, ...]
    shadow_receipt_sha256: str | None


class ArmingApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation_token: str = Field(min_length=1, max_length=64)


class ArmingStatusView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str
    strategy_instance_id: str
    state: Literal["unarmed", "armed", "disarmed", "lapsed"]
    reason_code: str | None
    sessions_remaining: int
    armed_instance_count: int | None = Field(ge=0)
    observed_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
