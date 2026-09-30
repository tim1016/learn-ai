"""The guarded loss-hold clear's outcome (ADR 0059 D4, ADR 0011 §6 shape)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.clerk.models import EpochMs


class LossHoldClearOutcome(BaseModel):
    """The guarded loss-hold clear's outcome.

    ``day_pnl_usd`` and ``loss_limit_usd`` are the loss rule's floats — the
    bytes the rule compared and the seal recorded. They are machine figures
    that no client renders as money: the dollars the owner reads live in
    ``detail``, authored in Python through the money boundary (#2612), so a
    cent never depends on a browser's float formatting.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Literal["cleared", "no_hold", "refused"]
    reason_code: str | None
    # Loss-rule floats; never displayed as money (see the class docstring).
    day_pnl_usd: float | None
    loss_limit_usd: float | None
    observed_at_ms: EpochMs
    detail: str = Field(min_length=1)
