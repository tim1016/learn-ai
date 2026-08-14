"""Response models for the SQLite Alpaca Clerk command endpoints (#1376).

Backend-authored resource shapes only — the frontend derives no verb or
availability from these (R11); it renders ``state`` and ``disabled_tooltip``
as given.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.clerk.sqlite.models import CommandResource
from app.broker.alpaca.clerk.sqlite.recovery_policy import RecoveryActionId

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.projection_models import ClerkProjection, TimelinePage
    from app.broker.alpaca.clerk.sqlite.reconcile import AccountReconciliationResult
    from app.broker.alpaca.clerk.sqlite.recovery_execution import RecoveryExecutionResult

ReconciliationVerdict = Literal[
    "clean",
    "unexplained_order",
    "position_drift",
    "stale",
]
CustodyScope = Literal["CUSTODY_SUBJECT", "ACCOUNT_CLERK"]


class CommandResponse(BaseModel):
    """The durable command resource (R2). Mirrors ``CommandResource`` 1:1,
    plus a backend-authored ``disabled_tooltip`` for a non-terminal command
    so a future UI slice needs no extra round trip to render "already
    requested"."""

    model_config = ConfigDict(frozen=True)

    command_id: str
    idempotency_key: str
    kind: str
    strategy_instance_id: str | None
    run_id: str | None
    action: str
    intended_end_state: str | None
    state: str
    effect_operation_id: str | None
    receipt_id: str | None
    created_at_ms: int
    updated_at_ms: int
    disabled_tooltip: str | None

    @classmethod
    def from_resource(cls, resource: CommandResource) -> CommandResponse:
        non_terminal = resource.state in ("reserved", "accepted", "in_progress", "unknown")
        tooltip = (
            (
                f"A {resource.action.lower()} has already been requested for this bot."
                if resource.strategy_instance_id is not None
                else f"A {resource.action.lower()} has already been requested for this manual order."
            )
            if non_terminal
            else None
        )
        return cls(
            command_id=resource.command_id,
            idempotency_key=resource.idempotency_key,
            kind=resource.kind,
            strategy_instance_id=resource.strategy_instance_id,
            run_id=resource.run_id,
            action=resource.action,
            intended_end_state=resource.intended_end_state,
            state=resource.state,
            effect_operation_id=resource.effect_operation_id,
            receipt_id=resource.receipt_id,
            created_at_ms=resource.created_at_ms,
            updated_at_ms=resource.updated_at_ms,
            disabled_tooltip=tooltip,
        )


class StartRunRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    # min_length=1: an empty string still passes reject_colon() (which only
    # blocks ':') and would mint a durable command identity no client could
    # reproduce intentionally (open-pr-review-2026-08-05.md, "lifecycle_run_id
    # accepts an empty string at the boundary").
    lifecycle_run_id: str = Field(min_length=1)
    operator_reason: str | None = None


class StopRunRequest(BaseModel):
    """``lifecycle_run_id`` is required (corrective foundation slice): Stop
    is no longer resolved from the currently active run, since that made a
    lost response unrecoverable — see the pinned contract's §3a."""

    model_config = ConfigDict(frozen=True)

    lifecycle_run_id: str = Field(min_length=1)
    operator_reason: str | None = None


class DurableConflictResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    reason: str
    existing_command: CommandResponse


class ReconciliationResponse(BaseModel):
    """Backend-authored result of an operator reconciliation pass."""

    model_config = ConfigDict(frozen=True)

    verdict: ReconciliationVerdict
    resolved_count: int
    foreign_order_count: int
    drifted_symbols: tuple[str, ...]

    @classmethod
    def from_result(cls, result: AccountReconciliationResult) -> ReconciliationResponse:
        return cls(
            verdict=result.verdict,
            resolved_count=result.resolved_count,
            foreign_order_count=result.foreign_order_count,
            drifted_symbols=result.drifted_symbols,
        )


class ProjectedCommandResponse(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    command_id: str
    kind: str
    action: str
    state: str
    run_id: str | None
    receipt_id: str | None
    created_at_ms: int
    updated_at_ms: int


class ProjectedRunResponse(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    run_id: str
    strategy_instance_id: str
    lifecycle_run_id: str
    state: str
    started_at_ms: int
    stopped_at_ms: int | None


class ProjectedOrderResponse(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    order_ref: str
    client_order_id: str
    broker_order_id: str | None
    role: str
    broker_state: str | None
    submitted_at_ms: int | None
    updated_at_ms: int


class ProjectedOperationResponse(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    effect_operation_id: str
    kind: str
    state: str
    custody_owner: str
    strategy_instance_id: str
    run_id: str | None
    created_at_ms: int
    updated_at_ms: int
    latest_transition_sequence: int
    transition_count: int
    terminal_receipt_id: str | None
    command: ProjectedCommandResponse
    orders: tuple[ProjectedOrderResponse, ...]


class ProjectedPositionResponse(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    strategy_instance_id: str
    symbol: str
    attributed_qty: float
    updated_at_ms: int


class ProjectedHoldResponse(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    hold_id: str
    scope: CustodyScope
    strategy_instance_id: str | None
    reason_code: str
    opened_at_ms: int
    evidence_refs: tuple[str, ...]


class ProjectedUncertaintyResponse(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    uncertainty_id: str
    scope: CustodyScope
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


class ProjectedReconciliationResponse(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    reconciliation_id: str
    effect_operation_id: str | None
    order_ref: str | None
    trigger: str
    attempted_at_ms: int
    outcome: str
    evidence_age_ms: int
    evidence_refs: tuple[str, ...]


class ProjectedReceiptResponse(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    receipt_id: str
    command_id: str | None
    effect_operation_id: str | None
    terminal_state: str
    summary_code: str
    proof_reference: str | None
    recorded_at_ms: int


class RecoveryEvidenceResponse(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    reference: str
    label: str
    observed_at_ms: int | None
    age_ms: int | None
    freshness: Literal["fresh", "stale", "not_required", "unavailable"]


class RecoveryConfirmationResponse(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    title: str
    explanation: str
    confirm_label: str


class SafeFlattenPlanLegResponse(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    strategy_instance_id: str
    symbol: str
    side: Literal["buy", "sell"]
    quantity: float
    position_updated_at_ms: int


class SafeFlattenPlanResponse(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    version_token: str
    account_id: str
    authority_generation: int
    db_identity_token: str
    control_revision: int
    scope: CustodyScope
    strategy_instance_id: str | None
    reconciliation_id: str
    prepared_at_ms: int
    expires_at_ms: int
    legs: tuple[SafeFlattenPlanLegResponse, ...]


class RecoveryCapabilityResponse(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    action_id: RecoveryActionId
    label: str
    explanation: str
    available: bool
    unavailable_reason_code: str | None
    unavailable_reason: str | None
    scope: CustodyScope
    freshness: Literal["fresh", "stale", "not_required", "unavailable"]
    evidence: tuple[RecoveryEvidenceResponse, ...]
    reduction_plan: SafeFlattenPlanResponse | None
    confirmation: RecoveryConfirmationResponse | None
    next_step: str
    concurrency_token: str
    execution_ref: str | None
    mutation: bool
    primary: bool


class ProjectionGuidanceResponse(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    headline: str
    explanation: str
    scope: CustodyScope
    impact: str
    custody_owner: str
    may_create_exposure: bool
    available_safety_actions: tuple[str, ...]
    action_required: bool
    next_step: str


class ClerkProjectionResponse(BaseModel):
    """One backend-authored Trader/Operator snapshot over materialized folds."""

    model_config = ConfigDict(frozen=True, from_attributes=True)

    account_id: str
    strategy_instance_id: str | None
    authority_generation: int
    db_identity_token: str
    authority_health: Literal["healthy", "degraded_to_mirror", "failed"]
    authority_health_reason: str | None
    control_revision: int
    custody_owner: str
    runs: tuple[ProjectedRunResponse, ...]
    commands: tuple[ProjectedCommandResponse, ...]
    operations: tuple[ProjectedOperationResponse, ...]
    positions: tuple[ProjectedPositionResponse, ...]
    holds: tuple[ProjectedHoldResponse, ...]
    uncertainties: tuple[ProjectedUncertaintyResponse, ...]
    latest_reconciliation: ProjectedReconciliationResponse | None
    terminal_receipts: tuple[ProjectedReceiptResponse, ...]
    guidance: ProjectionGuidanceResponse
    recovery_actions: tuple[RecoveryCapabilityResponse, ...]
    generated_at_ms: int

    @classmethod
    def from_projection(cls, projection: ClerkProjection) -> ClerkProjectionResponse:
        return cls.model_validate(projection)


class TimelineEntryResponse(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

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


class TimelinePageResponse(BaseModel):
    """Stable keyset page; later appends never enter an established cursor."""

    model_config = ConfigDict(frozen=True, from_attributes=True)

    account_id: str
    strategy_instance_id: str | None
    authority_generation: int
    control_revision: int
    anchor_sequence: int
    total_entries: int
    entries: tuple[TimelineEntryResponse, ...]
    next_cursor: str | None

    @classmethod
    def from_page(cls, page: TimelinePage) -> TimelinePageResponse:
        return cls.model_validate(page)


class RecoveryActionCheckRequest(BaseModel):
    """Action-specific token checked against a fresh policy evaluation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_id: RecoveryActionId
    concurrency_token: str = Field(min_length=1, max_length=128)


class RecoveryActionCheckResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    capability: RecoveryCapabilityResponse


class RecoveryActionExecuteRequest(BaseModel):
    """Execute one capability exactly as presented by the current snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_id: RecoveryActionId
    concurrency_token: str = Field(min_length=1, max_length=128)
    execution_ref: str | None = Field(default=None, max_length=256)
    reason: str | None = Field(default=None, max_length=512)


class RecoveryActionExecuteResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    action_id: RecoveryActionId
    outcome: Literal["success"] = "success"
    applied: bool
    receipt_id: str
    recorded_at_ms: int
    command: CommandResponse | None
    reconciliation: ReconciliationResponse | None
    orders: tuple[ProjectedOrderResponse, ...]

    @classmethod
    def from_result(
        cls,
        result: RecoveryExecutionResult,
    ) -> RecoveryActionExecuteResponse:
        return cls(
            action_id=result.action_id,
            applied=result.applied,
            receipt_id=result.receipt_id,
            recorded_at_ms=result.recorded_at_ms,
            command=(
                CommandResponse.from_resource(result.command)
                if result.command is not None
                else None
            ),
            reconciliation=(
                ReconciliationResponse.from_result(result.reconciliation)
                if result.reconciliation is not None
                else None
            ),
            orders=tuple(
                ProjectedOrderResponse.model_validate(order)
                for order in result.orders
            ),
        )
