"""Single policy authority for SQLite Clerk recovery capabilities.

The same catalog is rendered by read surfaces and re-evaluated immediately
before execution.  Tokens intentionally include only action-relevant durable
facts, so chart or unrelated-bot activity cannot make a confirmed action stale.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from dataclasses import dataclass, replace
from typing import Literal, TypeGuard

from app.broker.alpaca.clerk.sqlite.projection_models import (
    AuthorityHealth,
    ClerkScope,
    ProjectedOrder,
    ProjectedPosition,
    ProjectedReconciliation,
    ProjectedRun,
    ProjectedUncertainty,
    ProjectionGuidance,
    RecoveryCapability,
    RecoveryConfirmation,
    RecoveryEvidence,
    SafeFlattenPlan,
    SafeFlattenPlanLeg,
)

RecoveryActionId = Literal[
    "reconcile_now",
    "cancel_verified_working_orders",
    "prepare_safe_flatten",
    "stop_bot_decisions",
    "open_custody_timeline",
    "rebuild_from_mirror",
    "reset_authority",
]

FRESH_EVIDENCE_MAX_AGE_MS = 30_000
_WORKING_BROKER_STATES = frozenset(
    {"new", "accepted", "pending_new", "partially_filled", "pending_cancel"}
)


class RecoveryPolicyError(Exception):
    """Base error for a recovery request rejected before any effect."""


class StaleRecoveryTokenError(RecoveryPolicyError):
    """The action-relevant facts no longer match the presented capability."""


class RecoveryActionUnavailableError(RecoveryPolicyError):
    """The policy does not currently authorize the requested action."""

    def __init__(self, capability: RecoveryCapability) -> None:
        self.capability = capability
        super().__init__(
            capability.unavailable_reason
            or f"{capability.action_id} is not available in the current state"
        )


@dataclass(frozen=True)
class AuthorityRecoveryProof:
    mirror_verified: bool = False
    mirror_reference: str | None = None
    broker_observed_at_ms: int | None = None
    broker_account_flat: bool | None = None
    broker_order_free: bool | None = None
    broker_proof_reference: str | None = None


@dataclass(frozen=True)
class RecoveryPolicyContext:
    account_id: str
    strategy_instance_id: str | None
    authority_generation: int
    db_identity_token: str
    authority_health: AuthorityHealth
    authority_health_reason: str | None
    control_revision: int
    now_ms: int
    runs: tuple[ProjectedRun, ...]
    current_orders: tuple[ProjectedOrder, ...]
    positions: tuple[ProjectedPosition, ...]
    uncertainties: tuple[ProjectedUncertainty, ...]
    latest_account_reconciliation: ProjectedReconciliation | None
    recovery_proof: AuthorityRecoveryProof = AuthorityRecoveryProof()


@dataclass(frozen=True)
class _Decision:
    available: bool
    reason_code: str | None
    reason: str | None
    freshness: Literal["fresh", "stale", "not_required", "unavailable"]
    evidence: tuple[RecoveryEvidence, ...]
    next_step: str
    token_facts: object
    execution_ref: str | None = None


@dataclass(frozen=True)
class _Descriptor:
    action_id: RecoveryActionId
    label: str
    explanation: str
    mutation: bool
    confirmation: RecoveryConfirmation | None


def _confirmation(title: str, explanation: str, confirm_label: str) -> RecoveryConfirmation:
    return RecoveryConfirmation(
        title=title,
        explanation=explanation,
        confirm_label=confirm_label,
    )


_DESCRIPTORS: tuple[_Descriptor, ...] = (
    _Descriptor(
        action_id="reconcile_now",
        label="Reconcile now",
        explanation="Compare durable Clerk custody with a fresh Alpaca account observation.",
        mutation=True,
        confirmation=None,
    ),
    _Descriptor(
        action_id="cancel_verified_working_orders",
        label="Cancel verified working orders",
        explanation=(
            "Cancel only working orders whose exact Clerk and Alpaca identities are proven."
        ),
        mutation=True,
        confirmation=_confirmation(
            "Cancel verified orders?",
            "Only the listed, freshly verified working orders will be canceled.",
            "Cancel verified orders",
        ),
    ),
    _Descriptor(
        action_id="prepare_safe_flatten",
        label="Prepare safe flatten",
        explanation=(
            "Prepare a fresh, versioned reduction plan; this step does not submit an order."
        ),
        mutation=False,
        confirmation=None,
    ),
    _Descriptor(
        action_id="stop_bot_decisions",
        label="Stop bot decisions",
        explanation="Stop new strategy decisions while leaving exposure under Clerk custody.",
        mutation=True,
        confirmation=_confirmation(
            "Stop this bot?",
            "The bot will stop making decisions. Existing exposure is not flattened blindly.",
            "Stop bot decisions",
        ),
    ),
    _Descriptor(
        action_id="open_custody_timeline",
        label="Open custody timeline",
        explanation="Inspect the immutable operation-first evidence timeline.",
        mutation=False,
        confirmation=None,
    ),
)

_FAILURE_DESCRIPTORS: tuple[_Descriptor, ...] = (
    _Descriptor(
        action_id="open_custody_timeline",
        label="Open custody timeline",
        explanation="Inspect the last readable immutable custody evidence.",
        mutation=False,
        confirmation=None,
    ),
    _Descriptor(
        action_id="rebuild_from_mirror",
        label="Rebuild from mirror",
        explanation="Rebuild only from a contiguous, finalized, hash-verified mirror.",
        mutation=True,
        confirmation=_confirmation(
            "Rebuild the authority?",
            "The failed database is preserved and a verified mirror rebuild is installed.",
            "Rebuild from mirror",
        ),
    ),
    _Descriptor(
        action_id="reset_authority",
        label="Reset authority",
        explanation=(
            "Create a new authority generation only after fresh flat and order-free broker proof."
        ),
        mutation=True,
        confirmation=_confirmation(
            "Reset to a new generation?",
            "Previous control identities become invalid. The failed database remains preserved.",
            "Reset authority",
        ),
    ),
)


def _token(ctx: RecoveryPolicyContext, action_id: str, facts: object) -> str:
    payload = json.dumps(
        {
            "account_id": ctx.account_id,
            "authority_generation": ctx.authority_generation,
            "db_identity_token": ctx.db_identity_token,
            "strategy_instance_id": ctx.strategy_instance_id,
            "action_id": action_id,
            "facts": facts,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _age(now_ms: int, observed_at_ms: int | None) -> int | None:
    return None if observed_at_ms is None else max(0, now_ms - observed_at_ms)


def _evidence(
    ctx: RecoveryPolicyContext,
    *,
    reference: str,
    label: str,
    observed_at_ms: int | None,
    required_fresh: bool,
) -> RecoveryEvidence:
    age_ms = _age(ctx.now_ms, observed_at_ms)
    if observed_at_ms is None:
        freshness = "unavailable"
    elif required_fresh and age_ms is not None and age_ms > FRESH_EVIDENCE_MAX_AGE_MS:
        freshness = "stale"
    else:
        freshness = "fresh"
    return RecoveryEvidence(
        reference=reference,
        label=label,
        observed_at_ms=observed_at_ms,
        age_ms=age_ms,
        freshness=freshness,
    )


def _scope(ctx: RecoveryPolicyContext) -> ClerkScope:
    return "BOT" if ctx.strategy_instance_id is not None else "ACCOUNT_CLERK"


def _relevant_runs(ctx: RecoveryPolicyContext) -> tuple[ProjectedRun, ...]:
    return tuple(
        run
        for run in ctx.runs
        if run.state == "ACTIVE"
        and (
            ctx.strategy_instance_id is None
            or run.strategy_instance_id == ctx.strategy_instance_id
        )
    )


def _scoped_positions(ctx: RecoveryPolicyContext) -> tuple[ProjectedPosition, ...]:
    return tuple(
        sorted(
            (
                position
                for position in ctx.positions
                if (
                    ctx.strategy_instance_id is None
                    or position.strategy_instance_id == ctx.strategy_instance_id
                )
            ),
            key=lambda position: (position.strategy_instance_id, position.symbol),
        )
    )


def _relevant_positions(ctx: RecoveryPolicyContext) -> tuple[ProjectedPosition, ...]:
    return tuple(
        position
        for position in _scoped_positions(ctx)
        if math.isfinite(position.attributed_qty)
        and abs(position.attributed_qty) > 0
    )


def _non_finite_positions(ctx: RecoveryPolicyContext) -> tuple[ProjectedPosition, ...]:
    return tuple(
        position
        for position in _scoped_positions(ctx)
        if not math.isfinite(position.attributed_qty)
    )


def _working_orders(ctx: RecoveryPolicyContext) -> tuple[ProjectedOrder, ...]:
    working = (
        order
        for order in ctx.current_orders
        if order.broker_order_id is not None
        and (order.broker_state or "").lower() in _WORKING_BROKER_STATES
    )
    unique = {order.order_ref: order for order in working}
    return tuple(sorted(unique.values(), key=lambda order: order.order_ref))


def _relevant_uncertainties(
    ctx: RecoveryPolicyContext,
) -> tuple[ProjectedUncertainty, ...]:
    """Uncertainties in scope for the current authority view.

    Account-wide views (``strategy_instance_id is None``) own every
    uncertainty; a bot view owns account-scoped ones plus its own. This is
    the single source of truth for uncertainty scoping — both the
    safe-flatten gate and the projection guidance read it so the copy the
    operator sees can never disagree with the action that is blocked.
    """
    return tuple(
        uncertainty
        for uncertainty in ctx.uncertainties
        if ctx.strategy_instance_id is None
        or uncertainty.scope == "ACCOUNT_CLERK"
        or uncertainty.strategy_instance_id == ctx.strategy_instance_id
    )


def _is_successful_account_reconciliation(
    reconciliation: ProjectedReconciliation | None,
) -> TypeGuard[ProjectedReconciliation]:
    return (
        reconciliation is not None
        and reconciliation.effect_operation_id is None
        and reconciliation.order_ref is None
        and reconciliation.outcome == "RESOLVED_SUCCESS"
    )


def _decision(ctx: RecoveryPolicyContext, action_id: RecoveryActionId) -> _Decision:
    if action_id == "open_custody_timeline":
        available = ctx.authority_health == "healthy"
        return _Decision(
            available=available,
            reason_code=None if available else "READABLE_TIMELINE_REQUIRED",
            reason=(
                None
                if available
                else "The failed authority has no verified online timeline reader."
            ),
            freshness="not_required",
            evidence=(),
            next_step=(
                "Open the timeline and select an operation to inspect all three clocks."
                if available
                else "Stop the process and verify the preserved database or finalized mirror offline."
            ),
            token_facts={"authority_health": ctx.authority_health},
        )
    if ctx.authority_health != "healthy":
        return _failure_decision(ctx, action_id)
    if action_id == "reconcile_now":
        return _Decision(
            available=True,
            reason_code=None,
            reason=None,
            freshness="not_required",
            evidence=(),
            next_step="Run the account comparison now.",
            token_facts={"authority_health": ctx.authority_health},
        )
    if action_id == "stop_bot_decisions":
        active_runs = _relevant_runs(ctx)
        available = ctx.strategy_instance_id is not None and bool(active_runs)
        return _Decision(
            available=available,
            reason_code=None if available else "NO_ACTIVE_BOT_RUN",
            reason=(
                None
                if available
                else "Select a bot with an active run; no decision process is currently stoppable."
            ),
            freshness="not_required",
            evidence=(),
            next_step=(
                "Stop the active run."
                if available
                else "Choose an active bot or keep the current stopped state."
            ),
            token_facts=[(run.run_id, run.lifecycle_run_id, run.state) for run in active_runs],
            execution_ref=(active_runs[0].lifecycle_run_id if available else None),
        )
    if action_id == "cancel_verified_working_orders":
        orders = _working_orders(ctx)
        evidence = tuple(
            _evidence(
                ctx,
                reference=f"order:{order.order_ref}",
                label="Verified working order",
                observed_at_ms=order.updated_at_ms,
                required_fresh=True,
            )
            for order in orders
        )
        available = bool(orders) and all(item.freshness == "fresh" for item in evidence)
        reason_code = None
        reason = None
        if not orders:
            reason_code = "NO_VERIFIED_WORKING_ORDERS"
            reason = "No working order has both a durable Clerk reference and broker identity."
        elif not available:
            reason_code = "WORKING_ORDER_EVIDENCE_STALE"
            reason = "Refresh broker order evidence before canceling anything."
        return _Decision(
            available=available,
            reason_code=reason_code,
            reason=reason,
            freshness="fresh" if available else ("stale" if orders else "unavailable"),
            evidence=evidence,
            next_step=(
                "Cancel only the listed orders."
                if available
                else "Run Reconcile now to refresh exact working-order identity."
            ),
            token_facts=[
                (order.order_ref, order.broker_order_id, order.broker_state, order.updated_at_ms)
                for order in orders
            ],
        )
    if action_id == "prepare_safe_flatten":
        return _safe_flatten_decision(ctx)
    raise AssertionError(f"healthy catalog cannot evaluate {action_id!r}")


def _safe_flatten_decision(ctx: RecoveryPolicyContext) -> _Decision:
    positions = _relevant_positions(ctx)
    non_finite_positions = _non_finite_positions(ctx)
    working_orders = _working_orders(ctx)
    reconciliation = ctx.latest_account_reconciliation
    reconciliation_evidence = _evidence(
        ctx,
        reference=(
            f"reconciliation:{reconciliation.reconciliation_id}"
            if reconciliation is not None
            else "reconciliation:missing"
        ),
        label="Latest account reconciliation",
        observed_at_ms=(reconciliation.attempted_at_ms if reconciliation is not None else None),
        required_fresh=True,
    )
    relevant_uncertainties = _relevant_uncertainties(ctx)
    reason_code: str | None = None
    reason: str | None = None
    if non_finite_positions:
        reason_code = "EXPOSURE_NOT_PROVEN"
        reason = (
            "Attributed exposure is not finite, so a reduction quantity cannot be proven."
        )
    elif not positions:
        reason_code = "NO_ATTRIBUTED_EXPOSURE"
        reason = "No attributed exposure requires a flatten plan."
    elif working_orders:
        reason_code = "WORKING_ORDERS_REQUIRE_CANCEL_FIRST"
        reason = "Cancel and prove every working order terminal before preparing reduction."
    elif relevant_uncertainties:
        reason_code = "EXPOSURE_NOT_PROVEN"
        reason = "Active uncertainty prevents the Clerk from proving a safe reduction quantity."
    elif not _is_successful_account_reconciliation(reconciliation):
        reason_code = "CLEAN_RECONCILIATION_REQUIRED"
        reason = "A successful account reconciliation is required before preparing reduction."
    elif any(
        position.updated_at_ms > reconciliation.attempted_at_ms
        for position in positions
    ):
        reason_code = "POSITION_EVIDENCE_NOT_RECONCILED"
        reason = (
            "Attributed position evidence changed after the account reconciliation."
        )
    elif reconciliation_evidence.freshness != "fresh":
        reason_code = "RECONCILIATION_EVIDENCE_STALE"
        reason = "The successful reconciliation is too old for a reduction decision."
    available = reason_code is None
    return _Decision(
        available=available,
        reason_code=reason_code,
        reason=reason,
        freshness=reconciliation_evidence.freshness,
        evidence=(reconciliation_evidence,),
        next_step=(
            "Review the exact attributed quantities in the generated plan; "
            "no order was submitted."
            if available
            else (
                "Cancel verified working orders first."
                if working_orders
                else "Run Reconcile now and refresh the custody snapshot."
            )
        ),
        token_facts={
            "positions": [
                (
                    position.strategy_instance_id,
                    position.symbol,
                    position.attributed_qty,
                    position.updated_at_ms,
                )
                for position in positions
            ],
            "non_finite_positions": [
                (
                    position.strategy_instance_id,
                    position.symbol,
                    repr(position.attributed_qty),
                    position.updated_at_ms,
                )
                for position in non_finite_positions
            ],
            "working_orders": [
                (order.order_ref, order.broker_state, order.updated_at_ms)
                for order in working_orders
            ],
            "uncertainties": [
                (uncertainty.uncertainty_id, uncertainty.reason_code)
                for uncertainty in relevant_uncertainties
            ],
            "reconciliation": (
                None
                if reconciliation is None
                else (
                    reconciliation.reconciliation_id,
                    reconciliation.outcome,
                    reconciliation.attempted_at_ms,
                )
            ),
        },
    )


def _build_safe_flatten_plan(
    ctx: RecoveryPolicyContext,
    *,
    version_token: str,
) -> SafeFlattenPlan:
    """Build the read-only exact-close plan for proven attributed positions.

    Formula: signed close quantity = -attributed_qty; side is SELL for a
      positive attributed quantity and BUY for a negative attributed quantity;
      displayed quantity = abs(attributed_qty).
    Reference: docs/prds/alpaca-account-clerk-sqlite-control-plane.md R6-R7;
      docs/references/alpaca-sqlite-clerk-recovery-language.md.
    Canonical implementation: this file.
    Validated against:
      tests/broker/alpaca/clerk/sqlite/test_recovery_policy.py::
      test_safe_flatten_prepares_versioned_exact_close_plan and the adjacent
      account-reconciliation, position-clock, uncertainty, and order gates.
    """
    reconciliation = ctx.latest_account_reconciliation
    if not _is_successful_account_reconciliation(reconciliation):
        raise AssertionError(
            "available safe-flatten plan requires successful account reconciliation"
        )
    return SafeFlattenPlan(
        version_token=version_token,
        account_id=ctx.account_id,
        authority_generation=ctx.authority_generation,
        db_identity_token=ctx.db_identity_token,
        control_revision=ctx.control_revision,
        scope=_scope(ctx),
        strategy_instance_id=ctx.strategy_instance_id,
        reconciliation_id=reconciliation.reconciliation_id,
        prepared_at_ms=ctx.now_ms,
        expires_at_ms=(
            reconciliation.attempted_at_ms + FRESH_EVIDENCE_MAX_AGE_MS
        ),
        legs=tuple(
            SafeFlattenPlanLeg(
                strategy_instance_id=position.strategy_instance_id,
                symbol=position.symbol,
                side="sell" if position.attributed_qty > 0 else "buy",
                quantity=abs(position.attributed_qty),
                position_updated_at_ms=position.updated_at_ms,
            )
            for position in _relevant_positions(ctx)
        ),
    )


def _failure_decision(ctx: RecoveryPolicyContext, action_id: RecoveryActionId) -> _Decision:
    proof = ctx.recovery_proof
    if action_id == "rebuild_from_mirror":
        evidence = (
            RecoveryEvidence(
                reference=proof.mirror_reference or "mirror:unverified",
                label="Finalized mirror verification",
                observed_at_ms=None,
                age_ms=None,
                freshness="fresh" if proof.mirror_verified else "unavailable",
            ),
        )
        return _Decision(
            available=proof.mirror_verified,
            reason_code=None if proof.mirror_verified else "VERIFIED_MIRROR_REQUIRED",
            reason=(
                None
                if proof.mirror_verified
                else "No contiguous finalized hash-verified mirror is available."
            ),
            freshness="fresh" if proof.mirror_verified else "unavailable",
            evidence=evidence,
            next_step=(
                "Preserve the failed database and rebuild from the verified mirror."
                if proof.mirror_verified
                else "Verify the finalized mirror or use the reset prerequisites."
            ),
            token_facts=(proof.mirror_verified, proof.mirror_reference),
        )
    if action_id == "reset_authority":
        evidence = _evidence(
            ctx,
            reference=proof.broker_proof_reference or "broker-account-proof:missing",
            label="Flat and order-free Alpaca account proof",
            observed_at_ms=proof.broker_observed_at_ms,
            required_fresh=True,
        )
        available = (
            evidence.freshness == "fresh"
            and proof.broker_account_flat is True
            and proof.broker_order_free is True
        )
        return _Decision(
            available=available,
            reason_code=None if available else "FRESH_FLAT_ORDER_FREE_PROOF_REQUIRED",
            reason=(
                None
                if available
                else "Reset requires fresh proof that this account is flat with no open orders."
            ),
            freshness=evidence.freshness,
            evidence=(evidence,),
            next_step=(
                "Confirm the new authority generation."
                if available
                else "Refresh Alpaca positions and open orders before reset."
            ),
            token_facts=(
                proof.broker_observed_at_ms,
                proof.broker_account_flat,
                proof.broker_order_free,
                proof.broker_proof_reference,
            ),
        )
    raise AssertionError(f"failure catalog cannot evaluate {action_id!r}")


def build_recovery_catalog(ctx: RecoveryPolicyContext) -> tuple[RecoveryCapability, ...]:
    """Author the complete capability set for the current authority posture."""
    descriptors = _DESCRIPTORS if ctx.authority_health == "healthy" else _FAILURE_DESCRIPTORS
    capabilities: list[RecoveryCapability] = []
    for descriptor in descriptors:
        decision = _decision(ctx, descriptor.action_id)
        concurrency_token = _token(
            ctx,
            descriptor.action_id,
            decision.token_facts,
        )
        capabilities.append(
            RecoveryCapability(
                action_id=descriptor.action_id,
                label=descriptor.label,
                explanation=descriptor.explanation,
                available=decision.available,
                unavailable_reason_code=decision.reason_code,
                unavailable_reason=decision.reason,
                scope=_scope(ctx),
                freshness=decision.freshness,
                evidence=decision.evidence,
                reduction_plan=(
                    _build_safe_flatten_plan(
                        ctx,
                        version_token=concurrency_token,
                    )
                    if descriptor.action_id == "prepare_safe_flatten"
                    and decision.available
                    else None
                ),
                confirmation=descriptor.confirmation,
                next_step=decision.next_step,
                concurrency_token=concurrency_token,
                execution_ref=decision.execution_ref,
                mutation=descriptor.mutation,
                primary=False,
            )
        )
    primary_id = _primary_action_id(capabilities)
    return tuple(
        capability
        if capability.action_id != primary_id
        else replace(capability, primary=True)
        for capability in capabilities
    )


def _primary_action_id(capabilities: list[RecoveryCapability]) -> str | None:
    priority = (
        "rebuild_from_mirror",
        "reset_authority",
        "cancel_verified_working_orders",
        "reconcile_now",
        "stop_bot_decisions",
        "prepare_safe_flatten",
        "open_custody_timeline",
    )
    available = {capability.action_id for capability in capabilities if capability.available}
    return next((action_id for action_id in priority if action_id in available), None)


def recheck_recovery_action(
    ctx: RecoveryPolicyContext,
    *,
    action_id: str,
    concurrency_token: str,
) -> RecoveryCapability:
    """Rebuild the same policy and reject stale/unavailable requests effect-free."""
    capability = next(
        (item for item in build_recovery_catalog(ctx) if item.action_id == action_id),
        None,
    )
    if capability is None:
        raise RecoveryActionUnavailableError(
            RecoveryCapability(
                action_id=action_id,
                label="Unavailable action",
                explanation="This action is not part of the current authority posture.",
                available=False,
                unavailable_reason_code="ACTION_NOT_PRESENTED",
                unavailable_reason="Refresh the Clerk surface before trying this action.",
                scope=_scope(ctx),
                freshness="unavailable",
                evidence=(),
                reduction_plan=None,
                confirmation=None,
                next_step="Refresh the Clerk surface.",
                concurrency_token="",
                execution_ref=None,
                mutation=True,
                primary=False,
            )
        )
    if not hmac.compare_digest(capability.concurrency_token, concurrency_token):
        raise StaleRecoveryTokenError(
            "The recovery evidence changed. Refresh before confirming the action."
        )
    if not capability.available:
        raise RecoveryActionUnavailableError(capability)
    return capability


def build_projection_guidance(ctx: RecoveryPolicyContext) -> ProjectionGuidance:
    """Author Trader/Operator impact copy from the same state as capabilities."""
    relevant = _relevant_uncertainties(ctx)
    actions = build_recovery_catalog(ctx)
    available = tuple(action.label for action in actions if action.available)
    if ctx.authority_health != "healthy":
        return ProjectionGuidance(
            headline="Alpaca execution is paused",
            explanation=(
                ctx.authority_health_reason
                or "The Account Clerk authority is unavailable and cannot prove safe execution."
            ),
            scope="ACCOUNT_CLERK",
            impact="All new broker-mutating Clerk work is blocked.",
            custody_owner="ACCOUNT_CLERK",
            may_create_exposure=False,
            available_safety_actions=available,
            action_required=True,
            next_step=(
                next((action.next_step for action in actions if action.primary), "Inspect recovery evidence.")
            ),
        )
    if relevant:
        primary = relevant[0]
        return ProjectionGuidance(
            headline=primary.headline,
            explanation=primary.explanation,
            scope=primary.scope,
            impact=primary.operator_impact,
            custody_owner=primary.custody_owner,
            may_create_exposure=not any(item.blocks_new_exposure for item in relevant),
            available_safety_actions=available,
            action_required=False,
            next_step=primary.next_step,
        )
    return ProjectionGuidance(
        headline="Account Clerk custody is healthy",
        explanation="Durable Clerk state has no active hold or unresolved uncertainty in this scope.",
        scope=_scope(ctx),
        impact="Normal Clerk-governed controls remain available.",
        custody_owner="ACCOUNT_CLERK",
        may_create_exposure=True,
        available_safety_actions=available,
        action_required=False,
        next_step="No recovery action is required.",
    )
