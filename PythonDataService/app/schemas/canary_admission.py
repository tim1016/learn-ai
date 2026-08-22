"""Backend-authored verdict for stopping a canary at a Clerk-proved boundary.

Issue #1729 AC10. See ``app/services/canary_admission.py`` for the pure
function that builds this verdict and how it composes with the existing Stop
custody proof (``app.services.bot_carryover.prove_stop_outcome``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CanaryRollbackDecision(BaseModel):
    """Whether stopping one canary run is admitted at a Clerk-proved boundary.

    A rollback is refused outright, not merely recorded, when the Clerk
    cannot prove the resulting position is flat or an explicitly approved
    carried exposure. This differs from an ordinary Stop, which always
    persists an honest checkpoint even when custody is unprovable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_instance_id: str
    allowed: bool
    reason_code: str
    explanation: str
    next_step: str | None
    stop_outcome: Literal[
        "STOPPED_FLAT",
        "STOPPED_WITH_APPROVED_ATTRIBUTED_EXPOSURE",
        "STOP_REQUIRES_FLATTEN",
        "STOPPED_CUSTODY_UNPROVABLE",
    ]
    evaluated_at_ms: int = Field(ge=0)


__all__ = ["CanaryRollbackDecision"]
