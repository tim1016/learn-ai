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
    observed_at_ms: int = Field(ge=0)
    # Direction 2 (run-scoped replay proof): the canonical per-bucket trace
    # digest (`trace_root([trace])`) and the decision bucket's close, captured
    # at live time so a replay can compare decision CONTENT, not just intent
    # direction. Optional: legacy callers and traceless compatibility
    # strategies omit them; the replay receipt discloses digest coverage.
    trace_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    decision_bar_close_ms: int | None = Field(default=None, ge=0)

    @property
    def reason_code(self) -> str:
        """Derived from ``outcome`` alone -- never an independent input.

        ADR 0043 Decision 5: a decision and its custody outcome cannot
        diverge. ``reason_code`` used to be a free-form constructor field
        that every caller happened to set as ``f"STRATEGY_{intent.kind
        .value}"`` -- sound only because both current call sites derived it
        from the same intent kind that produced ``outcome``, not because the
        type forbade drift. A future caller setting it independently could
        silently defeat the invariant the stable-key decision-receipt
        comparison in ``decision_receipts._same_decision`` relies on. Making
        it a computed property closes that gap at the source: divergence is
        now unrepresentable rather than merely unexercised.
        """
        return "STRATEGY_ENTER" if self.outcome == "enter_intent" else "STRATEGY_EXIT"


__all__ = ["EffectDecisionEvidence"]
