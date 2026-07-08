"""Schemas for account recovery override mutations."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.live_runs import MutationRungReceipt


class CrashRecoveryOverrideRequest(BaseModel):
    """Operator attestation for a crash-retired host runner restart."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    confirm_account_flat: Literal[True]
    approved_by: str = Field(default="operator", min_length=1, max_length=128)
    reason: str | None = Field(default=None, max_length=500)


class CrashRecoveryOverrideResponse(BaseModel):
    """Audit handle returned after recording restart recovery evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted: Literal[True] = True
    account_id: str
    strategy_instance_id: str
    run_id: str
    bot_order_namespace: str
    override_id: str
    recorded_at_ms: int
    blocking_recorded_at_ms: int
    event_type: Literal["account_audited_override_recorded"] = "account_audited_override_recorded"
    rung_receipt: MutationRungReceipt | None = None
    rung_receipt_warnings: list[MutationRungReceipt] = Field(default_factory=list)
