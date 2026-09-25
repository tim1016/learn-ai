"""Response models for the SQLite Alpaca Clerk command endpoints (#1376).

Backend-authored resource shapes only — the frontend derives no verb or
availability from these (R11); it renders ``state`` and ``disabled_tooltip``
as given.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.broker.alpaca.clerk.program_leg import LegRefusal
from app.broker.alpaca.clerk.recovery_reduction import (
    RECOVERY_QUOTE_MAX_AGE_MS,
    RECOVERY_SPREAD_WARNING_BPS,
    ConfirmedRecoveryLimit,
    ExtendedLimitProposal,
    RecoveryReductionPricing,
    evaluate_proposed_limit,
    quote_spread_bps,
)
from app.broker.alpaca.clerk.sqlite.models import CommandResource
from app.broker.alpaca.clerk.sqlite.recovery_policy import RecoveryActionId
from app.utils.session_anchors import MAX_TIMESTAMP_MS

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
    indeterminate_symbols: tuple[str, ...]

    @classmethod
    def from_result(cls, result: AccountReconciliationResult) -> ReconciliationResponse:
        return cls(
            verdict=result.verdict,
            resolved_count=result.resolved_count,
            foreign_order_count=result.foreign_order_count,
            drifted_symbols=result.drifted_symbols,
            indeterminate_symbols=result.indeterminate_symbols,
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
    # When the Clerk will next try to resolve this on its own (the stuck-EXIT
    # watchdog's next re-drive, #2440): int64 ms UTC for the shared timestamp
    # display, never prose. ``None`` when nothing is scheduled — including
    # once the watchdog has escalated to EXIT_STUCK and stopped re-driving.
    next_attempt_at_ms: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)
    # ``next_attempt_at_ms`` has passed without the try resolving the episode
    # (a deferred try records nothing): render it as overdue, never as a
    # future promise (#2440 review).
    next_attempt_overdue: bool = False
    # The episode's recorded facts could not be read: its next attempt is
    # unknown, not unscheduled (#2440 review). The row itself still projects.
    facts_unreadable: bool = False


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
    # The primary episode's next automatic attempt, as its uncertainty
    # projects it (#2440).
    next_attempt_at_ms: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)
    next_attempt_overdue: bool = False


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


class HistoricalExecutionRecoveryPlanResponse(BaseModel):
    """One signed, read-only historical exact-execution recovery plan."""

    model_config = ConfigDict(frozen=True, from_attributes=True, extra="forbid")

    account_id: str
    strategy_instance_id: str
    uncertainty_id: str
    order_ref: str
    broker_order_id: str
    execution_id: str
    exact_symbol: str
    exact_quantity: float
    exact_price: float
    exact_side: Literal["BUY", "SELL"]
    source_event_at_ms: int
    cumulative_fill_id: str
    cumulative_quantity: float
    cumulative_price: float
    cumulative_side: Literal["BUY", "SELL"]
    authority_generation: int
    db_identity_token: str
    control_revision: int
    prepared_at_ms: int
    expires_at_ms: int
    confirmation_token: str


class HistoricalExecutionRecoveryPrepareRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    concurrency_token: str = Field(min_length=1, max_length=128)


class HistoricalExecutionRecoveryConfirmRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    plan: HistoricalExecutionRecoveryPlanResponse
    confirmation_token: str = Field(min_length=1, max_length=128)


class HistoricalExecutionRecoveryReceiptResponse(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True, extra="forbid")

    uncertainty_id: str
    order_ref: str
    execution_id: str
    receipt_id: str
    recorded_at_ms: int
    applied: bool


class RecoveryActionCheckRequest(BaseModel):
    """Action-specific token checked against a fresh policy evaluation.

    ``proposed_limit_price`` asks what a specific extended-hours price would
    do against the quote the Clerk holds (#2007) — the operator's review step.
    It confirms nothing and sends nothing; only the execute route does that.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_id: RecoveryActionId
    concurrency_token: str = Field(min_length=1, max_length=128)
    proposed_limit_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)


class RegularSessionFlattenPricing(BaseModel):
    """Inside the regular session the flatten is a market DAY order; no quote is needed."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["regular_session"]


class ProposedLimitEvaluationResponse(BaseModel):
    """What the price the operator proposed does against the Clerk's quote (#2007).

    Every number an operator reads before confirming is computed by the Clerk
    and rendered as-is; the browser never derives one (AGENTS.md § "Python
    owns all math").
    """

    model_config = ConfigDict(frozen=True)

    limit_price: float
    # Positive reaches through the touch; negative rests behind it.
    through_book_bps: float
    # Every share filling at the limit, measured against the touch, in dollars.
    worst_case_cost: float
    outside_band: bool
    thin_book: bool
    resting: bool


class ExtendedLimitFlattenPricing(BaseModel):
    """The live IBKR quote and suggested limit an operator confirms in PRE/POST (#2007).

    ``suggested_limit_price`` is the bid less the sealed exit allowance for a
    sell (the ask plus it to cover). It is a suggestion: the operator may send
    another price, which the Clerk checks against Alpaca's precision rule.
    ``proposal`` is present only when the operator asked what their own price
    would do.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["extended_limit"]
    phase: Literal["PRE", "POST"]
    symbol: str
    side: Literal["buy", "sell"]
    bid: float
    ask: float
    bid_size: int | None
    ask_size: int | None
    quote_observed_at_ms: int = Field(strict=True, ge=0, le=MAX_TIMESTAMP_MS)
    quote_max_age_ms: int
    exit_allowance_bps: float
    suggested_limit_price: float
    # The furthest-through-the-book price the Clerk accepts: twice the exit
    # allowance past the bid (sell) or ask (cover), owner decision 2026-09-19.
    # None when the configured band is effectively unbounded (a sell band
    # past 100 % floors to zero, which is not a price); any positive limit
    # is then inside the band (PR #2230 review).
    band_limit_price: float | None = None
    # The live spread in dollars and as bps of the mid, and whether that is
    # wide enough to flag.
    spread: float
    spread_bps: float
    wide_spread: bool
    # A bid-ask spread wider than this, in bps of the mid, is flagged.
    spread_warning_bps: float
    proposal: ProposedLimitEvaluationResponse | None = None


class RefusedFlattenPricing(BaseModel):
    """No flatten can be sent now; ``available_at_ms`` is when one can, if the clock is why."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["refused"]
    reason_code: str
    explanation: str
    next_step: str
    available_at_ms: int | None = Field(default=None, strict=True, ge=0, le=MAX_TIMESTAMP_MS)


SafeFlattenPricingResponse = Annotated[
    RegularSessionFlattenPricing | ExtendedLimitFlattenPricing | RefusedFlattenPricing,
    Field(discriminator="kind"),
]


def safe_flatten_pricing_response(
    pricing: RecoveryReductionPricing | LegRefusal,
    *,
    proposed_limit_price: float | None = None,
    quantity: float = 0.0,
) -> SafeFlattenPricingResponse:
    """The wire shape of the facade's ``price_safe_flatten`` answer.

    ``proposed_limit_price`` is the operator's own price, evaluated by the
    Clerk against the same quote so the browser only renders the result.
    """
    if isinstance(pricing, LegRefusal):
        return RefusedFlattenPricing(
            kind="refused",
            reason_code=pricing.reason_code,
            explanation=pricing.explanation,
            next_step=pricing.next_step,
            available_at_ms=pricing.available_at_ms,
        )
    if isinstance(pricing, ExtendedLimitProposal):
        quote = pricing.quote
        spread_bps = quote_spread_bps(quote)
        proposal = (
            None
            if proposed_limit_price is None
            else evaluate_proposed_limit(
                proposal=pricing,
                limit_price=Decimal(str(proposed_limit_price)),
                quantity=quantity,
            )
        )
        return ExtendedLimitFlattenPricing(
            kind="extended_limit",
            phase=pricing.phase,
            symbol=quote.symbol,
            side=pricing.side.value,
            bid=quote.bid,
            ask=quote.ask,
            bid_size=quote.bid_size,
            ask_size=quote.ask_size,
            quote_observed_at_ms=quote.observed_at_ms,
            quote_max_age_ms=RECOVERY_QUOTE_MAX_AGE_MS,
            exit_allowance_bps=float(pricing.exit_allowance_bps),
            suggested_limit_price=float(pricing.suggested_limit_price),
            band_limit_price=None
            if pricing.band_limit_price is None
            else float(pricing.band_limit_price),
            spread=quote.ask - quote.bid,
            spread_bps=spread_bps,
            wide_spread=spread_bps > RECOVERY_SPREAD_WARNING_BPS,
            spread_warning_bps=float(RECOVERY_SPREAD_WARNING_BPS),
            proposal=(
                None
                if proposal is None
                else ProposedLimitEvaluationResponse(
                    limit_price=float(proposal.limit_price),
                    through_book_bps=proposal.through_book_bps,
                    worst_case_cost=proposal.worst_case_cost,
                    outside_band=proposal.outside_band,
                    thin_book=proposal.thin_book,
                    resting=proposal.resting,
                )
            ),
        )
    return RegularSessionFlattenPricing(kind="regular_session")


class RecoveryActionCheckResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    capability: RecoveryCapabilityResponse
    # How a single-leg safe flatten would go out now (#2007); ``None`` for every
    # other capability and for a prepare-only multi-leg plan.
    reduction_pricing: SafeFlattenPricingResponse | None = None


class ExtendedLimitConfirmationRequest(BaseModel):
    """The limit an operator confirmed for an extended-hours safe flatten (#2007).

    ``quote_observed_at_ms`` names the IBKR bid/ask the operator confirmed
    against; the Clerk refuses a confirmation whose quote is more than ten
    seconds old when it arrives. Alpaca's precision rule is checked against
    the leg the Clerk would submit, not restated here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    limit_price: float = Field(gt=0, allow_inf_nan=False)
    quote_observed_at_ms: int = Field(strict=True, ge=0, le=MAX_TIMESTAMP_MS)

    def to_confirmed(self) -> ConfirmedRecoveryLimit:
        return ConfirmedRecoveryLimit(
            limit_price=Decimal(str(self.limit_price)),
            quote_observed_at_ms=self.quote_observed_at_ms,
        )


class RecoveryActionExecuteRequest(BaseModel):
    """Execute one capability exactly as presented by the current snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_id: RecoveryActionId
    concurrency_token: str = Field(min_length=1, max_length=128)
    execution_ref: str | None = Field(default=None, max_length=256)
    reason: str | None = Field(default=None, max_length=512)
    extended_limit: ExtendedLimitConfirmationRequest | None = None

    @model_validator(mode="after")
    def _extended_limit_prices_only_a_flatten(self) -> RecoveryActionExecuteRequest:
        if self.extended_limit is not None and self.action_id != "execute_safe_flatten":
            raise ValueError("extended_limit prices only an execute_safe_flatten action")
        return self


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
