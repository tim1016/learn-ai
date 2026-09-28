"""Server-authored account mode and budget deployment readiness."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.clerk.models import EpochMs

ConfiguredMode = Literal["paper", "live", "unconfigured"]
ModeAgreement = Literal["agreed", "disagreed", "unobserved"]
ClerkAuthority = Literal["sqlite", "synthetic", "shadow", "unavailable", "not_installed"]
EnvelopeAgreement = Literal["not_applicable", "unsealed", "agreed", "disagreed"]
LossHoldState = Literal["not_applicable", "clear", "held"]
FinalVerdict = Literal["paper", "live", "shadow", "unknown"]
DeploymentReadiness = Literal["not_applicable", "upgrade_required", "risk_not_observed", "loss_hold", "ready"]


class AlpacaLiveVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    configured_mode: ConfiguredMode
    observed_account_id: str | None
    """The custody account id the verdict observed.

    Under a shadow authority this carries the ``shadow:`` prefix while the
    headline names the live account (controller ruling FR1-C1): the field is
    the identity the verdict was computed against, and that identity is the
    shadow custody one, not the account the broker read answered.
    """
    mode_agreement: ModeAgreement
    clerk_authority: ClerkAuthority
    clerk_refusal_reason_code: str | None
    budget_authority_version: int = Field(ge=0)
    deployment_readiness: DeploymentReadiness
    envelope_agreement: EnvelopeAgreement
    loss_hold: LossHoldState
    final_verdict: FinalVerdict
    # Operator copy is authored here, not in the client (CLAUDE.md hard rule).
    headline: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    observed_at_ms: EpochMs
