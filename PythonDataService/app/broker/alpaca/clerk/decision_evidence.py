"""Typed signal-to-custody evidence carried through Clerk intake."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EffectDecisionEvidence(BaseModel):
    """One effect-bearing Signal Program decision before custody acceptance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluation_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    bar_ref: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    outcome: Literal["enter_intent", "exit_intent"]
    reason_code: str = Field(min_length=1)
    observed_at_ms: int = Field(ge=0)


__all__ = ["EffectDecisionEvidence"]
