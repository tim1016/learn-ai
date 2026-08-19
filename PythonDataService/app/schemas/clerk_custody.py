"""Read-only SQLite Alpaca custody diagnosis contract."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.broker.alpaca.clerk.models import EpochMs

CustodyDivergenceKind = Literal[
    "exposure_attribution_mismatch",
    "exposure_hold",
    "stale_reconciliation",
    "needs_review",
    "foreign_working_order",
]
CustodyDivergenceState = Literal[
    "resolvable_now", "blocked_on_prerequisite", "needs_review"
]
CustodyActionId = Literal["reconcile_now"]


class CustodyPositionDelta(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    clerk_attributed_qty: float
    broker_observed_qty: float


class CustodyDivergence(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: CustodyDivergenceKind
    state: CustodyDivergenceState
    explanation: str
    possible_causes: tuple[str, ...]
    position_deltas: tuple[CustodyPositionDelta, ...] = ()
    resolution_step: CustodyActionId | None = None
    prerequisite_step: CustodyActionId | None = None
    prerequisite_detail: str | None = None
    evidence_refs: tuple[str, ...] = ()


class CustodyResolutionStep(BaseModel):
    model_config = ConfigDict(frozen=True)

    action_id: CustodyActionId
    scope: Literal["account", "bot", "broker"]
    mutates: bool


class CustodyDiagnosis(BaseModel):
    model_config = ConfigDict(frozen=True)

    broker: Literal["alpaca"] = "alpaca"
    account_id: str
    in_sync: bool
    observed_at_ms: EpochMs
    snapshot_version: str
    resolution_posture: Literal["paper", "live"] = "paper"
    resolvable: bool = False
    blocked_reason: str | None = None
    authority_kind: Literal["sqlite"] = "sqlite"
    divergences: tuple[CustodyDivergence, ...] = ()
    resolution_plan: tuple[CustodyResolutionStep, ...] = ()


__all__ = ["CustodyDiagnosis"]
