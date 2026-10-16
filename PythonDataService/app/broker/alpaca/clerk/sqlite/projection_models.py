"""Typed read-model values for the SQLite Alpaca Account Clerk.

These immutable values are deliberately transport-neutral.  The projection
reader builds them exclusively from the materialized fold tables and bounded
transition windows; Pydantic schemas adapt them at the HTTP boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ClerkScope = Literal["CUSTODY_SUBJECT", "ACCOUNT_CLERK"]
AuthorityHealth = Literal["healthy", "failed"]
EvidenceFreshness = Literal["fresh", "stale", "not_required", "unavailable"]
# The check of the account against Alpaca a safe-flatten plan rests on: an
# operator's Reconcile now receipt, or the Clerk's own published pass (#2839).
AccountCheckKind = Literal["reconcile_now", "clerk_pass"]
# What an operator attests Alpaca shows for a closing order whose outcome is
# unknown (#2854): no such order, or the order under a broker order id.
SettlementAttestation = Literal["absent", "exists"]


@dataclass(frozen=True)
class RecoveryStatus:
    kind: Literal["working", "on_hold", "allowed_now", "allowed_from", "broker_unreachable", "stuck", "unknown"]
    reason_code: str
    explanation: str
    last_checked_at_ms: int | None
    stuck_since_ms: int | None
    allowed_from_ms: int | None = None


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
    updated_at_ms: int
    # Immutable order-leg facts and effective execution totals are read from
    # the SQLite authority alongside this working-order projection.  The
    # optional defaults preserve compatibility for historical projections
    # that predate those presentation fields.
    symbol: str | None = None
    side: str | None = None
    quantity: float | None = None
    order_type: str | None = None
    limit_price: float | None = None
    time_in_force: str | None = None
    filled_quantity: float | None = None
    # Whether the order may still fill: ``order_projection.ORDER_OPEN_SQL``, the
    # one definition, read with the order.
    may_fill: bool = True


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
    evidence_refs: tuple[str, ...]
    # The symbol the episode's cause names, when it names one (an
    # EXIT_NOT_FLAT does); the lane's attention bell shows it.
    symbol: str | None = None
    # The one observed recovery status shared across operator surfaces.
    recovery_status: RecoveryStatus | None = None


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
class ProjectedUnknownExitOrder:
    """An EXIT's closing order whose outcome is unknown and that an operator may settle (#2854).

    The order was sent at least once, carries no broker order id, no
    acknowledgement and no fill, and is an identity of the bot's open
    ``ORDER_OUTCOME_UNKNOWN`` episode. ``side`` is as its creation recorded
    it (``SELL`` or ``BUY``). ``latest_send_sequence`` is its latest
    ``ORDER_SUBMIT_REQUESTED``; ``lookup_wait_from_ms`` is where the wait
    after that send is counted from: the send, or the first record of its
    answer when that was written later. ``answered_as_client_order_id`` is
    the other client order id that send was answered under, when it was.
    """

    effect_operation_id: str
    order_ref: str
    symbol: str
    side: str
    quantity: float
    created_at_ms: int
    first_sent_at_ms: int
    latest_send_sequence: int
    lookup_wait_from_ms: int
    answered_as_client_order_id: str | None
    uncertainty_id: str


@dataclass(frozen=True)
class ProjectedReconciliation:
    reconciliation_id: str
    effect_operation_id: str | None
    order_ref: str | None
    attempted_at_ms: int
    outcome: str
    evidence_refs: tuple[str, ...]


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
    # The word the operator types to confirm; empty asks for a plain yes.
    required_token: str = ""


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
    # The Reconcile now receipt the plan rests on; ``None`` when it rests on
    # the Clerk's own pass, which writes no receipt (#2839).
    reconciliation_id: str | None
    account_check_kind: AccountCheckKind
    # When that check was made. The plan expires a bound after it: 30 s for a
    # receipt, the sweep's liveness bound for the Clerk's pass.
    account_check_observed_at_ms: int
    prepared_at_ms: int
    expires_at_ms: int
    legs: tuple[SafeFlattenPlanLeg, ...]


@dataclass(frozen=True)
class SettlementChoice:
    """One thing the operator can say Alpaca shows for an uncertain order (#2854)."""

    attestation: SettlementAttestation
    label: str
    explanation: str
    # Whether the operator must give the order's Alpaca order id with it.
    asks_broker_order_id: bool


@dataclass(frozen=True)
class UnknownOrderSettlement:
    """What the operator is asked about one closing order whose outcome is unknown (#2854).

    ``ticket`` is the Clerk's own sentence about the order, its time in ET.
    """

    order_ref: str
    symbol: str
    side: Literal["buy", "sell"]
    quantity: float
    first_sent_at_ms: int
    ticket: str
    choices: tuple[SettlementChoice, ...]


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
    # False when the action has nothing to do now -- no run to stop, nothing
    # held to sell or write off, no order that can still fill, no fill to
    # settle -- rather than being needed and gated. Decided where the policy
    # writes the reason (#2794).
    needed: bool = True
    # The order an available ``settle_unknown_exit_order`` asks about (#2854).
    settlement: UnknownOrderSettlement | None = None


@dataclass(frozen=True)
class ProjectionGuidance:
    headline: str
    explanation: str
    scope: ClerkScope
    impact: str
    custody_owner: str
    next_step: str
    # The primary episode's next automatic attempt, projected as
    # ``ProjectedUncertainty`` projects it (#2440): the bot page shows it
    # beside ``next_step``.
    recovery_status: RecoveryStatus | None = None


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
    operations: tuple[ProjectedOperation, ...]
    # Every still-working order of this bot, as a STOP proof and Clear's
    # commit-time check count them (``order_evidence.is_working_order``): all
    # its orders, not only those under the bounded ``operations`` window.
    # Empty for an account snapshot.
    working_order_refs: tuple[str, ...]
    positions: tuple[ProjectedPosition, ...]
    holds: tuple[ProjectedHold, ...]
    uncertainties: tuple[ProjectedUncertainty, ...]
    latest_reconciliation: ProjectedReconciliation | None
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

    high_water_sequence: int
    operations: tuple[ProjectedOperation, ...]
    next_cursor: str | None
