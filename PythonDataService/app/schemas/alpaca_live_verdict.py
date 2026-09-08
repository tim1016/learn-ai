"""The server-derived Alpaca live verdict (ADR 0059 D8).

Extends ADR 0011's verdict principles to the Alpaca path: computed in the
data plane from settings and the clerk selection outcome, reactive on every
read, never composed by the Frontend, never a guess. Slice 1 renders the
verdict; arming, shadow and the envelope (slices 4-6) fill the fields that
this slice fixes at their empty values. Slice 4 fills ``shadow_state`` from
the durable shadow evidence and widens ``clerk_authority`` to ``"shadow"``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.clerk.models import EpochMs

ConfiguredMode = Literal["paper", "live", "unconfigured"]
ModeAgreement = Literal["agreed", "disagreed", "unobserved"]
ClerkAuthority = Literal["sqlite", "synthetic", "shadow", "unavailable", "not_installed"]
EnvelopeState = Literal["not_applicable", "configured_unsealed", "sealed"]
ShadowState = Literal["not_applicable", "none", "in_progress", "complete"]
FinalVerdict = Literal["paper", "live-unarmed", "live-armed", "unknown"]


class AlpacaLiveVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    configured_mode: ConfiguredMode
    observed_account_id: str | None
    mode_agreement: ModeAgreement
    clerk_authority: ClerkAuthority
    clerk_refusal_reason_code: str | None
    armed_instance_count: int = Field(ge=0)
    envelope_state: EnvelopeState
    shadow_state: ShadowState
    final_verdict: FinalVerdict
    # Operator copy is authored here, not in the client (CLAUDE.md hard rule).
    headline: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    observed_at_ms: EpochMs
