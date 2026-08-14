"""Typed read-model values for the SQLite Alpaca Account Clerk.

These immutable values are deliberately transport-neutral.  The projection
reader builds them exclusively from the materialized fold tables and bounded
transition windows; Pydantic schemas adapt them at the HTTP boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ClerkScope = Literal["CUSTODY_SUBJECT", "ACCOUNT_CLERK"]
AuthorityHealth = Literal["healthy", "degraded_to_mirror", "failed"]
EvidenceFreshness = Literal["fresh", "stale", "not_required", "unavailable"]


@dataclass(frozen=True)
class ProjectedCommand:
    command_id: str
    kind: str
    action: str
    state: str
    run_id: str | None
    receipt_id: str | None
    created_at_ms: int
    updated_at_ms: int


@dataclass(frozen=True)
class ProjectedRun:
    run_id: str
    strategy_instance_id: str
    lifecycle_run_id: str
    state: str
    started_at_ms: int
    stopped_at_ms: int | None


@dataclass(frozen=True)
class ProjectedOrder:
    order_ref: str
    client_order_id: str
    broker_order_id: str | None
    role: str
    broker_state: str | None
    submitted_at_ms: int | None
    updated_at_ms: int
    # Immutable order-leg facts and effective execution totals are read from
    # the SQLite authority alongside this working-order projection.  The
    # optional defaults preserve compatibility for historical projections
    # that predate those presentation fields.
    symbol: str | None = None
    side: str | None = None
    quantity: float | None = None
    filled_quantity: float | None = None


@dataclass(frozen=True)
class ProjectedOperation:
    effect_operation_id: str
    kind: str
    state: str
    custody_owner: str
    strategy_instance_id: str | None
    run_id: str | None
    created_at_ms: int
    updated_at_ms: int
    latest_transition_sequence: int
    transition_count: int
    terminal_receipt_id: str | None
    command: ProjectedCommand
    orders: tuple[ProjectedOrder, ...]
    subject_id: str | None = None
    custody_subject_kind: str | None = None


@dataclass(frozen=True)
class ProjectedPosition:
    strategy_instance_id: str
    symbol: str
    attributed_qty: float
    updated_at_ms: int


@dataclass(frozen=True)
class ProjectedHold:
    hold_id: str
    scope: ClerkScope
    strategy_instance_id: str | None
    reason_code: str
    opened_at_ms: int
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class ProjectedUncertainty:
    uncertainty_id: str
    scope: ClerkScope
    severity: str
    blocks_new_exposure: bool
    allows_reduction: bool
    custody_owner: str
    strategy_instance_id: str | None
    reason_code: str
    headline: str
    explanation: str
    operator_impact: str
    next_step: str
    observed_at_ms: int
    evidence_age_ms: int
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class ProjectedExecutionCoverageConflict:
    """Read-only proof material for one quarantined exact execution."""

    uncertainty_id: str
    order_ref: str
    execution_id: str
    cumulative_fill_id: str | None
    exact_qty: float | None
    exact_price: float | None
    exact_side: str | None
    cumulative_qty: float | None
    cumulative_price: float | None
    cumulative_side: str | None
    proof_available: bool
    unavailable_reason: str | None
    execution_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProjectedReconciliation:
    reconciliation_id: str
    effect_operation_id: str | None
    order_ref: str | None
    trigger: str
    attempted_at_ms: int
    outcome: str
    evidence_age_ms: int
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class ProjectedReceipt:
    receipt_id: str
    command_id: str | None
    effect_operation_id: str | None
    terminal_state: str
    summary_code: str
    proof_reference: str | None
    recorded_at_ms: int


@dataclass(frozen=True)
class RecoveryEvidence:
    reference: str
    label: str
    observed_at_ms: int | None
    age_ms: int | None
    freshness: EvidenceFreshness


@dataclass(frozen=True)
class RecoveryConfirmation:
    title: str
    explanation: str
    confirm_label: str


@dataclass(frozen=True)
class SafeFlattenPlanLeg:
    strategy_instance_id: str
    symbol: str
    side: Literal["buy", "sell"]
    quantity: float
    position_updated_at_ms: int


@dataclass(frozen=True)
class SafeFlattenPlan:
    version_token: str
    account_id: str
    authority_generation: int
    db_identity_token: str
    control_revision: int
    scope: ClerkScope
    strategy_instance_id: str | None
    reconciliation_id: str
    prepared_at_ms: int
    expires_at_ms: int
    legs: tuple[SafeFlattenPlanLeg, ...]


@dataclass(frozen=True)
class RecoveryCapability:
    action_id: str
    label: str
    explanation: str
    available: bool
    unavailable_reason_code: str | None
    unavailable_reason: str | None
    scope: ClerkScope
    freshness: EvidenceFreshness
    evidence: tuple[RecoveryEvidence, ...]
    reduction_plan: SafeFlattenPlan | None
    confirmation: RecoveryConfirmation | None
    next_step: str
    concurrency_token: str
    execution_ref: str | None
    mutation: bool
    primary: bool


@dataclass(frozen=True)
class ProjectionGuidance:
    headline: str
    explanation: str
    scope: ClerkScope
    impact: str
    custody_owner: str
    may_create_exposure: bool
    available_safety_actions: tuple[str, ...]
    action_required: bool
    next_step: str


@dataclass(frozen=True)
class ClerkProjection:
    account_id: str
    strategy_instance_id: str | None
    authority_generation: int
    db_identity_token: str
    authority_health: AuthorityHealth
    authority_health_reason: str | None
    control_revision: int
    custody_owner: str
    runs: tuple[ProjectedRun, ...]
    commands: tuple[ProjectedCommand, ...]
    operations: tuple[ProjectedOperation, ...]
    positions: tuple[ProjectedPosition, ...]
    holds: tuple[ProjectedHold, ...]
    uncertainties: tuple[ProjectedUncertainty, ...]
    latest_reconciliation: ProjectedReconciliation | None
    terminal_receipts: tuple[ProjectedReceipt, ...]
    guidance: ProjectionGuidance
    recovery_actions: tuple[RecoveryCapability, ...]
    generated_at_ms: int


@dataclass(frozen=True)
class TimelineEntry:
    sequence: int
    operation_ref: str
    effect_operation_id: str | None
    command_id: str | None
    order_ref: str | None
    broker_order_id: str | None
    transition_kind: str
    operation_state: str
    broker_state: str | None
    custody_owner: str
    execution_authority: str
    summary_code: str
    proof_reference: str | None
    source_event_at_ms: int | None
    clerk_observed_at_ms: int
    recorded_at_ms: int


@dataclass(frozen=True)
class TimelinePage:
    account_id: str
    strategy_instance_id: str | None
    authority_generation: int
    control_revision: int
    anchor_sequence: int
    total_entries: int
    entries: tuple[TimelineEntry, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class OperationPage:
    """Stable operation-first page for existing control-plane surfaces."""

    account_id: str
    strategy_instance_id: str | None
    authority_generation: int
    control_revision: int
    high_water_sequence: int
    operations: tuple[ProjectedOperation, ...]
    next_cursor: str | None
