"""The guarded loss-hold clear's outcome (ADR 0059 D4, ADR 0011 §6 shape)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.clerk.models import EpochMs


class LossHoldClearOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Literal["cleared", "no_hold", "refused"]
    reason_code: str | None
    day_pnl_usd: float | None
    loss_limit_usd: float | None
    observed_at_ms: EpochMs
    detail: str = Field(min_length=1)
