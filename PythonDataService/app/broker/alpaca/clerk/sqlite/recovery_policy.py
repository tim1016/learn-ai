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
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal, NamedTuple, TypeGuard

from app.broker.alpaca.clerk.account_authority import (
    is_shadow_account_id,
    is_synthetic_account_id,
)
from app.broker.alpaca.clerk.recovery_reduction import market_leg_sendable
from app.broker.alpaca.clerk.sqlite.contract_terms import is_option_contract_symbol
from app.broker.alpaca.clerk.sqlite.execution_coverage_policy import (
    ExecutionCoverageRecoveryDecision,
    coverage_resolution_decision,
    historical_exact_recovery_decision,
)
from app.broker.alpaca.clerk.sqlite.exit_resolution import (
    RESEND_WAIT_CEILING_MS,
    expired_option_close,
    option_close_unpriced,
    option_flatten_waits_for_open,
)
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.alpaca.clerk.sqlite.option_owner_ending import (
    END_EXPIRED_OPTION_LABEL,
    expired_contract_hold_ref,
    owner_ending_offered_from_ms,
)
from app.broker.alpaca.clerk.sqlite.projection_models import (
    AccountCheckKind,
    AuthorityHealth,
    ClerkScope,
    ProjectedExecutionCoverageConflict,
    ProjectedOrder,
    ProjectedPosition,
    ProjectedReconciliation,
    ProjectedRun,
    ProjectedUncertainty,
    ProjectedUnknownExitOrder,
    ProjectionGuidance,
    RecoveryCapability,
    RecoveryConfirmation,
    RecoveryEvidence,
    SafeFlattenPlan,
    SafeFlattenPlanLeg,
    SettlementChoice,
    UnknownOrderSettlement,
)
from app.broker.alpaca.clerk.sqlite.reads import WORKING_BROKER_STATES
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    OPTION_EXPIRED_NOT_SETTLED_REASON_CODE,
    ORDER_OUTCOME_UNKNOWN_REASON_CODE,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_policies import (
    VoidAfter,
    reason_age_policy,
    reason_policy,
    reasons_that_strand_a_residue,
    residue_discharge_role,
)
from app.options.contract import OptionContract
from app.options.deadlines import past_last_session
from app.utils.et_words import et_when_words

RecoveryActionId = Literal[
    "reconcile_now",
    "recover_exact_execution_evidence",
    "resolve_execution_coverage",
    "cancel_verified_working_orders",
    "prepare_safe_flatten",
    "execute_safe_flatten",
    "discharge_attributed_residue",
    "end_expired_option",
    "settle_unknown_exit_order",
    "stop_bot_decisions",
    "open_custody_timeline",
]

FRESH_EVIDENCE_MAX_AGE_MS = 30_000
#: The sweep publishes a pass about every 15 s, so one older than this means
#: it has stopped: its verdict proves nothing about the account now.
SWEEP_LIVENESS_BOUND_MS = 60_000
# Imported rather than restated: the catalog's inert-terminal test asks the
# same question of the same column (`reads.WORKING_BROKER_STATES`), and two
# copies would let "working order" drift between the two answers.
_WORKING_BROKER_STATES = WORKING_BROKER_STATES


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
class PublishedPass:
    """The Clerk's latest published check of the account, as one bot's Flatten gate reads it (#2839).

    ``vouches`` is whether that pass proves what the bot holds: it is recent,
    it saw the bot's every custody transition, it found the account clean,
    and no order of the bot's is working or unresolved
    (``SqliteAlpacaClerkFacade.published_pass``).

    ``observed_at_ms`` is the pass's own time: when its verdict was reached
    (``AccountReconciliationResult.verdict_at_ms``), read with
    ``through_sequence`` under one intake hold. It is never when the pass was
    published -- the Clerk can record more between the two, which the
    comparison never saw -- and never when this value, or a proof read from
    the pass, was built: a pass is read many times after it ran.

    ``through_sequence`` is the newest custody transition the pass's final
    broker comparison saw (``AccountReconciliationResult.through_sequence``).
    """

    vouches: bool
    observed_at_ms: int
    through_sequence: int


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
    execution_coverage_conflicts: tuple[ProjectedExecutionCoverageConflict, ...] = ()
    recovery_proof: AuthorityRecoveryProof = AuthorityRecoveryProof()
    # The Clerk's latest published pass, read before this context's snapshot
    # was taken, and the bot's newest custody transition read inside it
    # (#2839). ``None`` for an account-scoped context and wherever no pass was
    # read: the safe-flatten gate then accepts a Reconcile now receipt only.
    published_pass: PublishedPass | None = None
    subject_custody_sequence: int | None = None
    # This bot's closing orders whose outcome is unknown and that an operator
    # may settle (#2854); empty for an account-scoped context.
    unknown_exit_orders: tuple[ProjectedUnknownExitOrder, ...] = ()
    # A Dry Run's positions in option contracts that nothing kept can price a
    # close of now, each as its contract and the side that closes it
    # (ADR 0076). Empty on every other account, which keeps no option quotes.
    unpriced_option_closes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class AccountCheckEvidence:
    """The check of the account against Alpaca that a safe-flatten plan rests on (#2839).

    A Reconcile now receipt (``reference`` is its reconciliation id) or the
    Clerk's own published pass. ``observed_at_ms`` is when that check was
    made, and the plan is good until ``max_age_ms`` after it: each kind of
    check is aged by its own bound.
    """

    kind: AccountCheckKind
    reference: str
    observed_at_ms: int
    max_age_ms: int


@dataclass(frozen=True)
class _AccountCheckRefused:
    """Why no check of the account stands behind a safe flatten."""

    reason_code: str
    reason: str


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
    # False when there is nothing for the action to do (see ``RecoveryCapability.needed``).
    needed: bool = True
    # The account check an available safe flatten rests on; its plan is built from it.
    account_check: AccountCheckEvidence | None = None
    # The order an available settle action asks the operator about (#2854).
    settlement: UnknownOrderSettlement | None = None


@dataclass(frozen=True)
class _Descriptor:
    action_id: RecoveryActionId
    label: str
    explanation: str
    mutation: bool
    # Authored per subject: a confirmation may name the bot it acts on.
    confirmation: Callable[[RecoveryPolicyContext], RecoveryConfirmation] | None


def _confirmation(
    title: str, explanation: str, confirm_label: str, *, required_token: str = ""
) -> Callable[[RecoveryPolicyContext], RecoveryConfirmation]:
    copy = RecoveryConfirmation(
        title=title,
        explanation=explanation,
        confirm_label=confirm_label,
        required_token=required_token,
    )
    return lambda _ctx: copy


def _stop_confirmation(ctx: RecoveryPolicyContext) -> RecoveryConfirmation:
    """Stop's confirmation, naming the bot the way Home's row does (#2634).

    A Dry Run's cash is simulated, so it leaves out the sentence about cash
    going back to the account: none of this account's money was set aside.
    """
    cash_back = () if is_synthetic_account_id(ctx.account_id) else (
        "Cash it isn't using goes back to the account.",
    )
    return RecoveryConfirmation(
        title="Stop this bot?" if ctx.strategy_instance_id is None else f"Stop {ctx.strategy_instance_id}?",
        explanation=" ".join((
            "The bot stops making new decisions. A sale already sent can still go through.",
            *cash_back,
            "Its scheduled end is cancelled: nothing is sold at the end time.",
        )),
        confirm_label="Stop bot decisions",
    )


#: The settle action's name: its descriptor's label, and what every text that
#: sends the operator to the action quotes (#2854).
SETTLE_UNKNOWN_EXIT_ORDER_LABEL = "Settle the uncertain order"

_DESCRIPTORS: tuple[_Descriptor, ...] = (
    _Descriptor(
        action_id="recover_exact_execution_evidence",
        label="Recover exact execution evidence",
        explanation=(
            "Read one retained Alpaca paper execution, bind it to the Clerk conflict, "
            "and prepare the existing no-delta coverage proof."
        ),
        mutation=False,
        confirmation=None,
    ),
    _Descriptor(
        action_id="resolve_execution_coverage",
        label="Resolve execution coverage",
        explanation=(
            "Replace one fully matching cumulative recovery total with its quarantined exact execution."
        ),
        mutation=True,
        confirmation=_confirmation(
            "Resolve execution coverage?",
            "The Clerk will replace only the matching rebuildable fill fold; both source records remain auditable.",
            "Resolve exact coverage",
        ),
    ),
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
        action_id="execute_safe_flatten",
        label="Execute safe flatten",
        explanation=(
            "Submit the prepared exact reduction as recovery EXIT custody; "
            "quantities are re-derived from durable attributed positions at submit time."
        ),
        mutation=True,
        confirmation=_confirmation(
            "Flatten attributed exposure?",
            "The Clerk will submit reduction-only orders for the exact attributed "
            "quantities in the prepared plan.",
            "Flatten now",
        ),
    ),
    _Descriptor(
        action_id="discharge_attributed_residue",
        label="Discharge stranded residue",
        explanation=(
            "Write off an attributed position the broker does not hold, after the "
            "Clerk re-reads the broker and proves the account flat for it."
        ),
        mutation=True,
        confirmation=_confirmation(
            "Discharge the stranded residue?",
            "Confirm at the broker that this symbol is flat. The Clerk re-reads the "
            "account, writes the attributed quantity to zero under your reason, and "
            "submits no order.",
            "Discharge residue",
        ),
    ),
    _Descriptor(
        action_id="end_expired_option",
        label=END_EXPIRED_OPTION_LABEL,
        explanation=(
            "Count an expired option contract that nothing will settle as worth nothing, at your word. "
            "The Clerk looks once more for a settlement first and sends no order."
        ),
        mutation=True,
        confirmation=_confirmation(
            "End the expired contract at your word?",
            "The Clerk looks once more for the contract's settlement and books that if one has come. If there "
            "is still none, it closes the position at nothing: what the contract cost is booked as a loss. "
            "It sends no order. This cannot be undone.",
            "End the contract",
            required_token="END",
        ),
    ),
    _Descriptor(
        action_id="settle_unknown_exit_order",
        label=SETTLE_UNKNOWN_EXIT_ORDER_LABEL,
        explanation=(
            "Say what Alpaca shows for a closing order the Clerk could not confirm. "
            "The Clerk reads Alpaca itself first and sends no order."
        ),
        mutation=True,
        confirmation=_confirmation(
            "Settle the uncertain order?",
            "The Clerk reads Alpaca again and settles the order only if what it reads "
            "agrees with what you said. It sends no order. If you said Alpaca has no such "
            "order, the position stays with the bot and the Clerk sends no order for it by "
            "itself: you then close it or write it off from this page. This cannot be undone.",
            "Settle the order",
            required_token="SETTLE",
        ),
    ),
    _Descriptor(
        action_id="stop_bot_decisions",
        label="Stop bot decisions",
        explanation="Stop the bot making new decisions. Stopping doesn't sell its shares.",
        mutation=True,
        confirmation=_stop_confirmation,
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
    max_age_ms: int = FRESH_EVIDENCE_MAX_AGE_MS,
) -> RecoveryEvidence:
    age_ms = _age(ctx.now_ms, observed_at_ms)
    if observed_at_ms is None:
        freshness = "unavailable"
    elif required_fresh and age_ms is not None and age_ms > max_age_ms:
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
    return "CUSTODY_SUBJECT" if ctx.strategy_instance_id is not None else "ACCOUNT_CLERK"


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
        and position_quantity_is_nonzero(position.attributed_qty)
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


def _safe_flatten_blocking_uncertainties(
    ctx: RecoveryPolicyContext,
) -> tuple[ProjectedUncertainty, ...]:
    """In-scope uncertainties whose registered policy refuses a safe flatten.

    Read from the policy registry's ``admits_safe_flatten``, never from a
    hand list and never from ``allows_reduction`` (POSITION_DRIFT and the
    loss hold admit a proven REDUCE yet must still refuse a flatten plan).
    An unregistered code has no policy and refuses.
    """
    return tuple(
        uncertainty
        for uncertainty in _relevant_uncertainties(ctx)
        if (policy := reason_policy(uncertainty.reason_code)) is None
        or not policy.admits_safe_flatten
    )


def _admitted_uncertainties_newer_than(
    ctx: RecoveryPolicyContext, checked_at_ms: int
) -> tuple[ProjectedUncertainty, ...]:
    """Admitted broker-side episodes raised or updated after the account check made at ``checked_at_ms``.

    A safe flatten admitted under such an episode must not reuse broker truth
    that predates it (#2363 review); the registry's
    ``safe_flatten_requires_later_reconciliation`` names which causes are
    that kind of evidence. The check is a Reconcile now receipt or the
    Clerk's own pass (#2839), judged alike.
    """
    return tuple(
        uncertainty
        for uncertainty in _relevant_uncertainties(ctx)
        if (policy := reason_policy(uncertainty.reason_code)) is not None
        and policy.safe_flatten_requires_later_reconciliation
        and uncertainty.observed_at_ms > checked_at_ms
    )


def _timeline_evidence(ctx: RecoveryPolicyContext) -> tuple[RecoveryEvidence, ...]:
    """Carry the exact evidence scope when the timeline action is presented."""
    candidates: list[tuple[str, str, int | None]] = [
        (
            f"uncertainty:{uncertainty.uncertainty_id}",
            "Custody uncertainty",
            uncertainty.observed_at_ms,
        )
        for uncertainty in _relevant_uncertainties(ctx)
    ]
    for conflict in ctx.execution_coverage_conflicts:
        if conflict.order_ref and conflict.execution_id:
            candidates.extend(
                (
                    (f"order:{conflict.order_ref}", "Conflicted Clerk order", None),
                    (f"execution:{conflict.execution_id}", "Conflicted Alpaca execution", None),
                )
            )
    seen: set[str] = set()
    evidence: list[RecoveryEvidence] = []
    for reference, label, observed_at_ms in candidates:
        if reference in seen:
            continue
        seen.add(reference)
        evidence.append(
            _evidence(
                ctx,
                reference=reference,
                label=label,
                observed_at_ms=observed_at_ms,
                required_fresh=False,
            )
        )
    return tuple(evidence)


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
        evidence = _timeline_evidence(ctx)
        return _Decision(
            available=available,
            reason_code=None if available else "READABLE_TIMELINE_REQUIRED",
            reason=(
                None
                if available
                else "The failed authority has no verified online timeline reader."
            ),
            freshness="not_required",
            evidence=evidence,
            next_step=(
                "Open the timeline and select an operation to inspect all three clocks."
                if available
                else "Stop the process and verify the preserved database or finalized mirror offline."
            ),
            token_facts={
                "authority_health": ctx.authority_health,
                "evidence_refs": [item.reference for item in evidence],
            },
        )
    if ctx.authority_health != "healthy":
        # ADR 0047: a failed authority is replaced by an offline ceremony, not
        # by a panel action. The failure catalog therefore offers only the
        # timeline reader (handled above); anything else asked for here is
        # refused rather than silently evaluated against healthy-path policy.
        raise AssertionError(
            f"failure catalog cannot evaluate {action_id!r}; "
            "authority recovery is offline-only"
        )
    if action_id == "recover_exact_execution_evidence":
        return _historical_execution_recovery_decision(ctx)
    if action_id == "resolve_execution_coverage":
        return _execution_coverage_resolution_decision(ctx)
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
            needed=bool(active_runs),
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
            # Needed while any order may still fill, verified or not: one the
            # broker has not acknowledged, or one held or pending replace.
            needed=any(order.may_fill for order in ctx.current_orders),
        )
    if action_id == "prepare_safe_flatten":
        return _safe_flatten_decision(ctx)
    if action_id == "execute_safe_flatten":
        return _execute_safe_flatten_decision(ctx)
    if action_id == "discharge_attributed_residue":
        return _residue_discharge_decision(ctx)
    if action_id == "end_expired_option":
        return _end_expired_option_decision(ctx)
    if action_id == "settle_unknown_exit_order":
        return _settle_unknown_exit_order_decision(ctx)
    raise AssertionError(f"healthy catalog cannot evaluate {action_id!r}")


def _receipt_evidence(
    ctx: RecoveryPolicyContext,
    positions: tuple[ProjectedPosition, ...],
    reconciliation_evidence: RecoveryEvidence,
) -> AccountCheckEvidence | _AccountCheckRefused:
    """A fresh Reconcile now receipt for the whole account, or why the latest one does not stand."""
    reconciliation = ctx.latest_account_reconciliation
    if not _is_successful_account_reconciliation(reconciliation):
        return _AccountCheckRefused(
            "CLEAN_RECONCILIATION_REQUIRED",
            "A successful account reconciliation is required before preparing reduction.",
        )
    if _admitted_uncertainties_newer_than(ctx, reconciliation.attempted_at_ms):
        return _AccountCheckRefused(
            "UNCERTAINTY_EVIDENCE_NOT_RECONCILED",
            "Broker evidence the Clerk could not record arrived after the account "
            "reconciliation.",
        )
    if any(position.updated_at_ms > reconciliation.attempted_at_ms for position in positions):
        return _AccountCheckRefused(
            "POSITION_EVIDENCE_NOT_RECONCILED",
            "Attributed position evidence changed after the account reconciliation.",
        )
    if reconciliation_evidence.freshness != "fresh":
        return _AccountCheckRefused(
            "RECONCILIATION_EVIDENCE_STALE",
            "The successful reconciliation is too old for a reduction decision.",
        )
    return AccountCheckEvidence(
        kind="reconcile_now",
        reference=reconciliation.reconciliation_id,
        observed_at_ms=reconciliation.attempted_at_ms,
        max_age_ms=FRESH_EVIDENCE_MAX_AGE_MS,
    )


def _pass_evidence(
    ctx: RecoveryPolicyContext, positions: tuple[ProjectedPosition, ...]
) -> AccountCheckEvidence | None:
    """The Clerk's own latest check of the account, when one stopped bot's flatten may rest on it (#2839).

    Accepted because an operator's Flatten is checked against the account
    again as it is sent (``flatten_cover``). For one bot's flatten only, never
    the account's, and only while every one of these holds:

    - the pass vouches for this bot (``PublishedPass.vouches``);
    - the bot has no ACTIVE run: a running bot's own decisions can move its
      custody at any moment, and its Flatten is refused until it stops;
    - the pass is no older than the sweep's liveness bound, counted from the
      pass's own time;
    - nothing of the bot's custody was written after what the pass's final
      broker comparison saw;
    - no admitted broker-side episode and no attributed position is newer
      than the pass: the two tests a receipt passes.
    """
    published = ctx.published_pass
    if (
        ctx.strategy_instance_id is None
        or published is None
        or ctx.subject_custody_sequence is None
        or not published.vouches
        or _relevant_runs(ctx)
        or not 0 <= ctx.now_ms - published.observed_at_ms <= SWEEP_LIVENESS_BOUND_MS
        or ctx.subject_custody_sequence > published.through_sequence
        # An episode recorded in the verdict's own millisecond may have
        # landed just after it, so the pass counts it as newer (#2839).
        or _admitted_uncertainties_newer_than(ctx, published.observed_at_ms - 1)
        or any(position.updated_at_ms > published.observed_at_ms for position in positions)
    ):
        return None
    return AccountCheckEvidence(
        kind="clerk_pass",
        reference=f"clerk-pass:{published.through_sequence}",
        observed_at_ms=published.observed_at_ms,
        max_age_ms=SWEEP_LIVENESS_BOUND_MS,
    )


def _account_evidence(
    ctx: RecoveryPolicyContext,
    positions: tuple[ProjectedPosition, ...],
    reconciliation_evidence: RecoveryEvidence,
) -> AccountCheckEvidence | _AccountCheckRefused:
    """The check of the account a safe flatten of ``positions`` rests on, or why none stands (#2839).

    A fresh Reconcile now receipt, judged as it was before the Clerk's own
    pass was accepted, and preferred when both stand. Otherwise the Clerk's
    latest published pass (:func:`_pass_evidence`). With neither, the refusal
    is the receipt's own.
    """
    receipt = _receipt_evidence(ctx, positions, reconciliation_evidence)
    if isinstance(receipt, AccountCheckEvidence):
        return receipt
    return _pass_evidence(ctx, positions) or receipt


# A Flatten is refused while a closing order of the bot is unconfirmed, and
# no reconciliation cures that: the operator settles the order (#2854).
_UNCONFIRMED_CLOSING_ORDER_REFUSES_FLATTEN = (
    "The Clerk sent a closing order for this bot and could not confirm it, so a flatten could "
    "close the position a second time."
)
_SETTLE_BEFORE_FLATTEN = (
    "Settle that order first: look at this account's orders at Alpaca, then use "
    f'"{SETTLE_UNKNOWN_EXIT_ORDER_LABEL}".'
)


def settle_unavailable_words(reason: str | None, next_step: str | None) -> str:
    """Why the settle action cannot be used for the order it is needed for, and what to do meanwhile (#2854).

    The settle capability's own unavailable reason and next step, under the
    action's name. Said by whatever would otherwise point the operator at
    settling -- a refused Flatten, a refused Clear, a notice -- while settling
    is itself refused: for a running bot, inside the wait after a send, or
    for an order Alpaca answered with one that names no client order id.
    """
    return " ".join(
        part
        for part in (f'"{SETTLE_UNKNOWN_EXIT_ORDER_LABEL}" cannot be used now:', reason, next_step)
        if part
    )


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
    relevant_uncertainties = _safe_flatten_blocking_uncertainties(ctx)
    reason_code: str | None = None
    reason: str | None = None
    account_check: AccountCheckEvidence | None = None
    # What to do about an unconfirmed closing order, by whether the settle
    # action is available for it (#2854); ``None`` when that is not the refusal.
    settle_first: str | None = None
    # A position in an option contract that has expired (ADR 0076): no order
    # can close it, so no plan to close it is offered.
    expired = next(
        (found for position in positions if (found := expired_option_close(position.symbol, ctx.now_ms)) is not None),
        None,
    )
    # One met outside the regular session: an option position is closed by a
    # market order in regular hours and nowhere else.
    holds_an_option = any(is_option_contract_symbol(position.symbol.upper()) for position in positions)
    after_hours = (
        option_flatten_waits_for_open(ctx.now_ms) if holds_an_option and not market_leg_sendable(ctx.now_ms) else None
    )
    # One the simulation could not price a close of now: a Flatten of it would
    # be refused as it is pressed, so it is not offered until a quote can.
    held_symbols = {position.symbol.upper() for position in positions}
    unpriced = next(
        (
            option_close_unpriced(symbol, side=side)
            for symbol, side in ctx.unpriced_option_closes
            if symbol.upper() in held_symbols
        ),
        None,
    )
    if non_finite_positions:
        reason_code = "EXPOSURE_NOT_PROVEN"
        reason = (
            "Attributed exposure is not finite, so a reduction quantity cannot be proven."
        )
    elif not positions:
        reason_code = "NO_ATTRIBUTED_EXPOSURE"
        reason = "No attributed exposure requires a flatten plan."
    elif expired is not None:
        reason_code, reason = expired.reason_code, expired.explanation
    elif after_hours is not None:
        reason_code, reason = after_hours.reason_code, after_hours.explanation
    elif unpriced is not None:
        reason_code, reason = unpriced.reason_code, unpriced.explanation
    elif working_orders:
        reason_code = "WORKING_ORDERS_REQUIRE_CANCEL_FIRST"
        reason = "Cancel and prove every working order terminal before preparing reduction."
    elif relevant_uncertainties:
        reason_code = "EXPOSURE_NOT_PROVEN"
        settle = _settle_unknown_exit_order_decision(ctx)
        if settle.needed:
            reason = _UNCONFIRMED_CLOSING_ORDER_REFUSES_FLATTEN
            settle_first = (
                _SETTLE_BEFORE_FLATTEN
                if settle.available
                else settle_unavailable_words(settle.reason, settle.next_step)
            )
        else:
            reason = "Active uncertainty prevents the Clerk from proving a safe reduction quantity."
    else:
        checked = _account_evidence(ctx, positions, reconciliation_evidence)
        if isinstance(checked, AccountCheckEvidence):
            account_check = checked
        else:
            reason_code, reason = checked.reason_code, checked.reason
    available = reason_code is None
    token_facts: dict[str, object] = {
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
    }
    # A plan resting on a receipt shows, and is versioned by, exactly what it
    # was before the Clerk's own pass was accepted. One resting on the pass
    # shows the pass and adds the bot's newest custody record to its token.
    check_evidence = reconciliation_evidence
    if account_check is not None and account_check.kind == "clerk_pass":
        check_evidence = _evidence(
            ctx,
            reference=account_check.reference,
            label="The Clerk's latest check of the account",
            observed_at_ms=account_check.observed_at_ms,
            required_fresh=True,
            max_age_ms=account_check.max_age_ms,
        )
        # Never the pass itself: it is published again every 15 s, so a token
        # naming it would go stale under the operator's hands with nothing
        # about the bot changed. The bot's newest custody record is what a
        # Flatten moves. One that fails or is refused writes a new record, so
        # the plan prepared after it has a new token, is a new EXIT, and reads
        # the account again as it is sent; without it the retry would be the
        # failed EXIT, told its stored refusal with no new read (#2839).
        token_facts["subject_custody_sequence"] = ctx.subject_custody_sequence
    return _Decision(
        available=available,
        reason_code=reason_code,
        reason=reason,
        needed=reason_code != "NO_ATTRIBUTED_EXPOSURE",
        freshness=check_evidence.freshness,
        evidence=(check_evidence,),
        account_check=account_check,
        next_step=(
            "Review the exact attributed quantities in the generated plan; "
            "no order was submitted."
            if available
            else (
                expired.next_step
                if expired is not None and reason_code == expired.reason_code
                else after_hours.next_step
                if after_hours is not None and reason_code == after_hours.reason_code
                else unpriced.next_step
                if unpriced is not None and reason_code == unpriced.reason_code
                else "Cancel verified working orders first."
                if working_orders
                else settle_first
                if settle_first is not None
                else "Run Reconcile now and refresh the custody snapshot."
            )
        ),
        token_facts=token_facts,
    )


def _execute_safe_flatten_decision(ctx: RecoveryPolicyContext) -> _Decision:
    """Executor gates = prepare gates, plus two.

    Episodes whose policy declares ``admits_safe_flatten`` (EXIT_NOT_FLAT /
    EXIT_STUCK / FAILED_ENTER_FILLED -- the exact states this executor exists
    to clear -- and an unfoldable foreign order) are already admitted by the shared prepare
    gate; the downstream ``require_capability(REDUCE, …)`` still enforces
    movement toward zero per leg. (1) Execution is single-strategy, one
    leg. (2) No run may be ACTIVE: a running strategy could re-enter
    right after the flatten; the operator stops decisions first
    (``stop_bot_decisions`` is presented alongside). This gate is
    presentation/recheck-time only — the same fact is re-asserted inside the
    capture transaction by ``accept_recovery_exit(forbid_active_run=True)``,
    closing the recheck→capture Resume race (Task 4/Task 8).
    """
    base = _safe_flatten_decision(ctx)
    legs = _relevant_positions(ctx)
    # The executor drives one strategy-owned recovery EXIT to flat. An
    # account-scoped context spans strategies and manual (NULL-strategy)
    # custody the executor cannot reduce, so presenting execute there would be
    # a lie and could leave a partially applied multi-leg mutation. Restrict
    # execution to a single strategy-owned leg; account-wide and manual-custody
    # flatten stay prepare-only (Codex review 2026-08-25 P1). ``prepare`` still
    # renders the full plan for review.
    single_strategy_leg = ctx.strategy_instance_id is not None and len(legs) == 1
    active_run_ids = [run.run_id for run in ctx.runs if run.state == "ACTIVE"]
    token_facts = {"base": base.token_facts, "active_runs": active_run_ids}
    if base.available and not single_strategy_leg:
        return replace(
            base,
            available=False,
            reason_code="RECOVERY_SCOPE_UNSUPPORTED",
            reason=(
                "Execute a safe flatten from a single bot holding one attributed "
                "symbol; account-wide and manual-custody flatten remain prepare-only."
            ),
            next_step="Open the specific bot to execute its flatten, or prepare the plan for review.",
            token_facts=token_facts,
        )
    if base.available and active_run_ids:
        return replace(
            base,
            available=False,
            reason_code="RUN_STILL_ACTIVE",
            reason="Stop the bot's active run before executing a recovery flatten.",
            next_step="Stop bot decisions first, then execute the prepared flatten.",
            token_facts=token_facts,
        )
    return replace(base, token_facts=token_facts)


def _reconciliation_confirms_residue(
    reconciliation: ProjectedReconciliation | None,
    positions: tuple[ProjectedPosition, ...],
    episodes: tuple[ProjectedUncertainty, ...],
) -> bool:
    """Whether a clean account pass observed the broker after the residue formed."""
    if not _is_successful_account_reconciliation(reconciliation):
        return False
    residue_formed_at_ms = max(
        (
            *(position.updated_at_ms for position in positions),
            *(episode.observed_at_ms for episode in episodes),
        ),
        default=0,
    )
    return reconciliation.attempted_at_ms > residue_formed_at_ms


#: Why the write-off is not offered for an option contract past its last session.
EXPIRED_CONTRACT_IS_NOT_WRITTEN_OFF = "EXPIRED_OPTION_CONTRACT_IS_NOT_WRITTEN_OFF"


def _expired_contracts_held(ctx: RecoveryPolicyContext) -> tuple[ProjectedPosition, ...]:
    """This bot's positions in option contracts whose last session is over, by symbol."""
    return tuple(
        position
        for position in _relevant_positions(ctx)
        if is_option_contract_symbol(position.symbol.upper())
        and past_last_session(OptionContract.from_occ_symbol(position.symbol.upper()).expiry_date, ctx.now_ms)
    )


def _why_an_expired_contract_is_not_ended_yet(
    ctx: RecoveryPolicyContext, position: ProjectedPosition
) -> tuple[str, str, str] | None:
    """Why the owner may not end one expired contract at their word yet, as a code, a reason and a step. None when they may.

    The same on every account: a whole read made after the expiry found
    no settlement, which the bot's open ``OPTION_EXPIRED_NOT_SETTLED`` hold
    says by naming the contract. A real account also waits until the close
    of the session after the contract's last, so the broker's record has
    had a full business day to come. A sold contract is never ended this
    way: if it was assigned, shares moved.
    """
    symbol = position.symbol.upper()
    if position.attributed_qty < 0:
        return (
            "SOLD_OPTION_CONTRACT_NOT_ENDED_AT_A_WORD",
            f"{symbol} is held sold. If it was assigned, shares moved, and no word undoes that.",
            "Compare this bot's positions with the broker's.",
        )
    held_for = any(
        uncertainty.reason_code == OPTION_EXPIRED_NOT_SETTLED_REASON_CODE
        and uncertainty.strategy_instance_id == ctx.strategy_instance_id
        and expired_contract_hold_ref(symbol) in uncertainty.evidence_refs
        for uncertainty in ctx.uncertainties
    )
    simulated = is_synthetic_account_id(ctx.account_id)
    if not held_for:
        return (
            "EXPIRED_OPTION_CONTRACT_MAY_STILL_BE_SETTLED",
            (
                f"The simulation has not yet said that it has no price to settle {symbol} from."
                if simulated
                else f"The Clerk has not yet read the broker's records since {symbol} expired and found none for it."
            ),
            "Nothing to do yet: the Clerk looks for the contract's settlement by itself.",
        )
    if simulated:
        return None
    offered_from_ms = owner_ending_offered_from_ms(OptionContract.from_occ_symbol(symbol).expiry_date)
    if ctx.now_ms < offered_from_ms:
        return (
            "EXPIRED_OPTION_CONTRACT_RECORD_MAY_STILL_COME",
            f"The broker's record of how {symbol} ended may still come. It is waited for until "
            f"{et_when_words(offered_from_ms, now_ms=ctx.now_ms)}, the close of the session after the contract's last.",
            "Nothing to do yet: the Clerk books the record by itself when it comes.",
        )
    return None


def _end_expired_option_decision(ctx: RecoveryPolicyContext) -> _Decision:
    """Offer the owner's ending of an expired option contract that nothing will settle (ADR 0076 decision 22).

    Presentation reads only durable custody. The proof is the executor's
    own look, made at the press (``option_events.end_option_at_the_owners_word``):
    a settlement that has come by then is booked in the ending's place. It
    asks for no stopped run and no failed close: a running bot whose feed
    was down for the contract's last session has neither, and holds a
    contract nothing will settle all the same.
    """
    expired = _expired_contracts_held(ctx)
    waits = {position.symbol.upper(): _why_an_expired_contract_is_not_ended_yet(ctx, position) for position in expired}
    ready = next((symbol for symbol, why in waits.items() if why is None), None)
    reason_code: str | None = None
    reason: str | None = None
    next_step = "End the contract at your word. The Clerk looks once more for its settlement first."
    if ctx.strategy_instance_id is None:
        reason_code = "RECOVERY_SCOPE_UNSUPPORTED"
        reason = "An expired contract is ended for the one bot that holds it."
        next_step = "Open the bot that holds the contract."
    elif not expired:
        reason_code = "NO_EXPIRED_OPTION_CONTRACT"
        reason = "This bot holds no option contract past its last session."
        next_step = "Nothing to end."
    elif ready is None:
        reason_code, reason, next_step = next(why for why in waits.values() if why is not None)
    available = reason_code is None
    return _Decision(
        available=available,
        reason_code=reason_code,
        reason=reason,
        # Nothing to end with no expired contract held, for one bot.
        needed=ctx.strategy_instance_id is not None and bool(expired),
        freshness="not_required",
        evidence=(),
        next_step=next_step,
        token_facts={
            "expired": [
                (position.strategy_instance_id, position.symbol, position.attributed_qty, position.updated_at_ms)
                for position in expired
            ],
            "waits": {symbol: None if why is None else why[0] for symbol, why in waits.items()},
        },
        execution_ref=ready if available else None,
    )


def _residue_discharge_decision(ctx: RecoveryPolicyContext) -> _Decision:
    """Offer the discharge of one bot's residue that an open EXIT episode strands (#2381).

    Presentation reads only durable custody; the broker proof is the
    executor's own fresh read (``residue_discharge.discharge_attributed_residue``),
    which refuses unless the discharge restores broker agreement. A last
    successful reconciliation that postdates both the residue and its
    stranding episode means the broker agreed with it, so the position is real
    and the cure is a flatten, not a discharge. An older one says nothing about
    the residue and does not hide the action.

    An expired option contract is never offered it, on any account (ADR
    0076 decision 22). A write-off takes the position off the books and
    leaves its lot open: the premium would never be a loss, and the budget
    would carry it as a position for good. The contract's end is booked
    from its settlement, and where none will come the owner ends it at
    their word (``_end_expired_option_decision``), which closes the lot.
    """
    positions = _relevant_positions(ctx)
    relevant = _relevant_uncertainties(ctx)
    episodes = tuple(
        uncertainty
        for uncertainty in relevant
        if residue_discharge_role(uncertainty.reason_code) == "strands"
    )
    refusing = tuple(
        uncertainty
        for uncertainty in relevant
        if residue_discharge_role(uncertainty.reason_code) == "refuses"
    )
    active_runs = _relevant_runs(ctx)
    working_orders = _working_orders(ctx)
    reason_code: str | None = None
    reason: str | None = None
    next_step = "Confirm at the broker that the symbol is flat, then discharge the residue."
    # An expired option contract, on any account (ADR 0076 decision 22). Asked
    # before everything else, so the write-off is never offered and then
    # refused. Its end is booked from its settlement or at the owner's word.
    expired_contract = (
        expired_option_close(positions[0].symbol, ctx.now_ms) if len(positions) == 1 else None
    )
    if ctx.strategy_instance_id is None or len(positions) != 1:
        reason_code = "RECOVERY_SCOPE_UNSUPPORTED"
        reason = "Discharge a residue from the single bot that holds one attributed symbol."
        next_step = "Open the bot that holds the residue."
    elif expired_contract is not None:
        reason_code = EXPIRED_CONTRACT_IS_NOT_WRITTEN_OFF
        reason = (
            f"{positions[0].symbol.upper()} has expired. An expired contract is not written off: a write-off "
            "would leave what it cost on the books as a position for good."
        )
        next_step = expired_contract.next_step
    elif not episodes:
        reason_code = "NO_STRANDED_EXIT_EPISODE"
        reason = f"No open {reasons_that_strand_a_residue()} episode names this bot's exposure."
        next_step = "Flatten real exposure instead of discharging it."
    elif refusing:
        reason_code = "EXPOSURE_NOT_PROVEN"
        reason = (
            "Another open uncertainty may mean the Clerk has not yet recorded a fill "
            "that moves this residue."
        )
        next_step = "Resolve that uncertainty first; it may settle the residue itself."
    elif active_runs:
        reason_code = "RUN_STILL_ACTIVE"
        reason = "Stop the bot's active run before discharging its residue."
        next_step = "Stop bot decisions first."
    elif working_orders:
        reason_code = "WORKING_ORDERS_REQUIRE_CANCEL_FIRST"
        reason = "Cancel and prove every working order terminal before discharging."
        next_step = "Cancel verified working orders first."
    elif _reconciliation_confirms_residue(ctx.latest_account_reconciliation, positions, episodes):
        reason_code = "BROKER_AGREES_WITH_CUSTODY"
        reason = "The latest reconciliation found the broker holding this exposure."
        expired = expired_option_close(positions[0].symbol, ctx.now_ms)
        # An expired option contract cannot be flattened either (ADR 0076): the step says what is true of it.
        next_step = "Flatten the exposure instead of discharging it." if expired is None else expired.next_step
    available = reason_code is None
    evidence = tuple(
        _evidence(
            ctx,
            reference=f"uncertainty:{episode.uncertainty_id}",
            label="Stranding EXIT episode",
            observed_at_ms=episode.observed_at_ms,
            required_fresh=False,
        )
        for episode in episodes
    )
    return _Decision(
        available=available,
        reason_code=reason_code,
        reason=reason,
        # Nothing to write off with nothing held, no stranded exit naming the
        # bot (whatever its scope), a broker that holds the shares too, or an
        # expired contract, which is never written off.
        needed=(
            bool(positions)
            and bool(episodes)
            and reason_code not in ("BROKER_AGREES_WITH_CUSTODY", EXPIRED_CONTRACT_IS_NOT_WRITTEN_OFF)
        ),
        freshness="not_required",
        evidence=evidence,
        next_step=next_step,
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
            "episodes": [(episode.uncertainty_id, episode.reason_code) for episode in episodes],
            "refusing": [(episode.uncertainty_id, episode.reason_code) for episode in refusing],
            "active_runs": [run.run_id for run in active_runs],
            "working_orders": [order.order_ref for order in working_orders],
            "reconciliation": (
                None
                if ctx.latest_account_reconciliation is None
                else ctx.latest_account_reconciliation.reconciliation_id
            ),
        },
        execution_ref=positions[0].symbol if available else None,
    )


def unknown_order_lookup_settles_at_ms(order: ProjectedUnknownExitOrder) -> int:
    """When Alpaca's reads can be trusted to show an order sent at ``order``'s latest send (#2854).

    The submit-absence wait after that send: an order whose answer was lost
    may be at Alpaca and not yet show in a read, so nothing read inside the
    wait is proof it is absent. The one declared wait
    (``uncertainty_policies``, ADR 0048 Decision 1), counted from where the
    Clerk's own re-send counts it. Not the longer waits between the Clerk's
    own re-sends: those space out sends, and say nothing about what a read
    shows.
    """
    grace_ms = reason_age_policy(ORDER_OUTCOME_UNKNOWN_REASON_CODE, VoidAfter).grace_ms
    return order.lookup_wait_from_ms + grace_ms


def _settlement_choices(side: Literal["buy", "sell"], symbol: str) -> tuple[SettlementChoice, ...]:
    """The two attestations an operator chooses between (#2854).

    "No such order" says what the Clerk checks, and what stays with the bot
    by the order's side: the shares a closing sale would have sold, or the
    short a cover would have bought back. It says too that the Clerk sends
    nothing for them afterwards, and what the operator then does.
    """
    stays = (
        "The shares stay with the bot, and the Clerk sends no order for them by itself: you "
        "then sell them or write them off from this page."
        if side == "sell"
        else "The short position stays with the bot, and the Clerk sends no order for it by "
        "itself: you then buy it back or write it off from this page."
    )
    return (
        SettlementChoice(
            attestation="absent",
            label="Alpaca has no such order",
            explanation=(
                "The Clerk checks once more that this order is not at Alpaca and that nothing is "
                f"working on {symbol}. If so, it ends this exit as not sent. {stays} This cannot "
                "be undone."
            ),
            asks_broker_order_id=False,
        ),
        SettlementChoice(
            attestation="exists",
            label="Alpaca shows it",
            explanation=(
                "The Clerk reads that order and takes it only if it is the same order. Nothing is sent."
            ),
            asks_broker_order_id=True,
        ),
    )


def _settlement(ctx: RecoveryPolicyContext, order: ProjectedUnknownExitOrder) -> UnknownOrderSettlement:
    """What the operator is asked about ``order``, in the Clerk's own words (#2854).

    The ticket says what the Clerk does meanwhile, which is not nothing. It
    starts no other closing order for the bot, but while the session the
    order was sent in is open it sends this same order again by itself, after
    waits that grow to a ceiling and then stay there
    (``exit_resolution.resend_wait_ms``), and each send makes what the
    operator saw at Alpaca, and the action they were presented, out of date.
    Once that session has ended it never sends the order again, on that day
    or a later one, and the ticket says so: an operator reading it the next
    morning must not wait for a send that will not come. An order Alpaca
    answered under another client order id is never sent again.
    """
    side: Literal["buy", "sell"] = "sell" if order.side.upper() == "SELL" else "buy"
    sent = (
        f"The Clerk sent an order to {side} {order.quantity:g} {order.symbol} on "
        f"{et_when_words(order.first_sent_at_ms, now_ms=ctx.now_ms)}"
    )
    holds = "holds the position and starts no other closing order for this bot"
    if order.answered_as_client_order_id is None:
        ticket = (
            f"{sent} and could not confirm it. It {holds}. While the session the order was "
            "sent in is open, the Clerk sends this same order again by itself, after a wait "
            f"that grows to {RESEND_WAIT_CEILING_MS // 60_000} minutes and then stays there. If "
            "it does, look at Alpaca again and open this action again. Once that session has "
            "ended, the Clerk never sends the order again."
        )
    else:
        ticket = (
            f"{sent}, and Alpaca answered under another order id. The Clerk {holds}, and does "
            "not send this order again."
        )
    return UnknownOrderSettlement(
        order_ref=order.order_ref,
        symbol=order.symbol,
        side=side,
        quantity=order.quantity,
        first_sent_at_ms=order.first_sent_at_ms,
        ticket=ticket,
        choices=_settlement_choices(side, order.symbol),
    )


class SettleTokenOrder(NamedTuple):
    """What the settle action's token names of the order it is offered for (#2854): the order and its latest send."""

    effect_operation_id: str
    order_ref: str
    latest_send_sequence: int
    answered_as_client_order_id: str | None
    uncertainty_id: str


def _settle_token_facts(order: SettleTokenOrder | None, active_run_ids: list[str]) -> dict[str, object]:
    return {"order": order, "active_runs": active_run_ids}


def settle_unknown_exit_order_token(ctx: RecoveryPolicyContext, order: SettleTokenOrder) -> str:
    """The token the settle action carried while it was available for ``order`` (#2854).

    Available, so with no ACTIVE run of the bot. A settled order is no
    longer presented, and the token of the request that settled it is spent:
    this is how that same request, asked again, is told from any other.
    """
    return _token(ctx, "settle_unknown_exit_order", _settle_token_facts(order, []))


def _settle_unknown_exit_order_decision(ctx: RecoveryPolicyContext) -> _Decision:
    """Offer the settling of one bot's closing order whose outcome is unknown (#2854).

    For an EXIT only, and only while the bot has no ACTIVE run: a running
    bot's own EXIT is sent with no check of the account, and would sell again
    the moment an attestation freed its position. Not in a simulation, whose
    orders fill inside the send and reach no broker an operator could look
    at. Presentation reads only durable custody; the proof is the executor's
    own fresh reads of the broker (``unknown_order_attestation``).

    Until the wait after the order's latest send has passed, the Clerk's own
    exact lookup may still find it, and no read is yet proof it is absent. An
    order whose send the broker answered under another client order id is
    offered at once: the Clerk will never send it again or find it by itself.

    An order whose send the broker answered with an order that names no
    client order id (recorded as the empty string) is not offered: neither
    attestation can settle it. "No such order" rests on the Clerk's own read
    of the order the broker answered with, and there is no id to read it by;
    "it is this order" takes up only an order under the client order id that
    was sent, which that answer did not carry. The Clerk keeps the position,
    and the reason says so, whether or not the bot is still running.

    The token names the order and its latest send, never a pass's own record
    that the order is still absent: every pass writes one, and a token naming
    them would go stale under the operator's hands every 15 s with nothing
    about the order changed. A send made again changes the token.
    """
    simulated = is_synthetic_account_id(ctx.account_id) or is_shadow_account_id(ctx.account_id)
    bot_scoped = ctx.strategy_instance_id is not None
    order = (
        ctx.unknown_exit_orders[0]
        if bot_scoped and not simulated and ctx.unknown_exit_orders
        else None
    )
    active_runs = _relevant_runs(ctx)
    reason_code: str | None = None
    reason: str | None = None
    next_step = "Look at this account's orders at Alpaca, then say what it shows for this order."
    if order is None:
        reason_code = "NO_UNKNOWN_EXIT_ORDER"
        if bot_scoped:
            reason = "No closing order of this bot has an outcome the Clerk could not confirm."
            next_step = "Nothing to settle."
        else:
            reason = "An uncertain closing order is settled from its bot's page."
            next_step = "Open the bot whose closing order the Clerk could not confirm."
    elif order.answered_as_client_order_id == "":
        reason_code = "ANSWERED_ORDER_NAMES_NO_ID"
        reason = (
            "Alpaca answered this order's send with an order that names no client order id, so "
            "the Clerk has no id to read that order by and cannot tell what it did. The Clerk "
            "keeps the position held. This action cannot settle the order."
        )
        next_step = (
            "Look at this account's orders at Alpaca for the order it answered with. No action "
            "of the Clerk settles this order yet."
        )
    elif active_runs:
        reason_code = "RUN_STILL_ACTIVE"
        reason = "Stop the bot before settling its uncertain order."
        next_step = "Stop bot decisions first."
    elif (
        order.answered_as_client_order_id is None
        and ctx.now_ms < unknown_order_lookup_settles_at_ms(order)
    ):
        reason_code = "ORDER_LOOKUP_STILL_SETTLING"
        reason = "The Clerk sent this order moments ago and may still find it at Alpaca by itself."
        next_step = "Wait for the Clerk's next check of the account."
    available = reason_code is None
    return _Decision(
        available=available,
        reason_code=reason_code,
        reason=reason,
        needed=order is not None,
        freshness="not_required",
        evidence=(
            ()
            if order is None
            else (
                _evidence(
                    ctx,
                    reference=f"order:{order.order_ref}",
                    label="Closing order with an unknown outcome",
                    observed_at_ms=order.lookup_wait_from_ms,
                    required_fresh=False,
                ),
            )
        ),
        next_step=next_step,
        token_facts=_settle_token_facts(
            None
            if order is None
            else SettleTokenOrder(
                effect_operation_id=order.effect_operation_id,
                order_ref=order.order_ref,
                latest_send_sequence=order.latest_send_sequence,
                answered_as_client_order_id=order.answered_as_client_order_id,
                uncertainty_id=order.uncertainty_id,
            ),
            [run.run_id for run in active_runs],
        ),
        execution_ref=order.order_ref if available and order is not None else None,
        settlement=_settlement(ctx, order) if available and order is not None else None,
    )


def _execution_coverage_resolution_decision(ctx: RecoveryPolicyContext) -> _Decision:
    return _coverage_decision(
        coverage_resolution_decision(
            control_revision=ctx.control_revision,
            conflicts=ctx.execution_coverage_conflicts,
        )
    )


def _historical_execution_recovery_decision(ctx: RecoveryPolicyContext) -> _Decision:
    return _coverage_decision(
        historical_exact_recovery_decision(
            strategy_instance_id=ctx.strategy_instance_id,
            control_revision=ctx.control_revision,
            conflicts=ctx.execution_coverage_conflicts,
        )
    )


# Coverage refusals that mean the action has nothing to do: no conflict, or a
# missing fill whose exact evidence the Clerk already holds (resolve it instead).
_COVERAGE_NOTHING_TO_DO = frozenset({"NO_EXECUTION_COVERAGE_CONFLICT", "EXACT_EVIDENCE_ALREADY_AVAILABLE"})


def _coverage_decision(decision: ExecutionCoverageRecoveryDecision) -> _Decision:
    return _Decision(
        available=decision.available,
        needed=decision.reason_code not in _COVERAGE_NOTHING_TO_DO,
        reason_code=decision.reason_code,
        reason=decision.reason,
        freshness=decision.freshness,
        evidence=decision.evidence,
        next_step=decision.next_step,
        token_facts=decision.token_facts,
        execution_ref=decision.execution_ref,
    )


def _build_safe_flatten_plan(
    ctx: RecoveryPolicyContext,
    *,
    version_token: str,
    account_check: AccountCheckEvidence | None,
) -> SafeFlattenPlan:
    """Build the read-only exact-close plan for proven attributed positions.

    Formula: signed close quantity = -attributed_qty; side is SELL for a
      positive attributed quantity and BUY for a negative attributed quantity;
      displayed quantity = abs(attributed_qty).
    Reference: ADR 0035.
    Canonical implementation: this file.
    Validated against:
      tests/broker/alpaca/clerk/sqlite/test_recovery_policy.py::
      test_safe_flatten_prepares_versioned_exact_close_plan and the adjacent
      account-reconciliation, position-clock, uncertainty, and order gates.

    ``account_check`` is the check of the account the gate accepted
    (:func:`_account_evidence`). The plan names it and expires with it, each
    kind of check by its own bound (#2839).
    """
    if account_check is None:
        raise AssertionError("available safe-flatten plan requires an accepted account check")
    return SafeFlattenPlan(
        version_token=version_token,
        account_id=ctx.account_id,
        authority_generation=ctx.authority_generation,
        db_identity_token=ctx.db_identity_token,
        control_revision=ctx.control_revision,
        scope=_scope(ctx),
        strategy_instance_id=ctx.strategy_instance_id,
        reconciliation_id=(
            account_check.reference if account_check.kind == "reconcile_now" else None
        ),
        account_check_kind=account_check.kind,
        account_check_observed_at_ms=account_check.observed_at_ms,
        prepared_at_ms=ctx.now_ms,
        expires_at_ms=account_check.observed_at_ms + account_check.max_age_ms,
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
                        account_check=decision.account_check,
                    )
                    if descriptor.action_id in ("prepare_safe_flatten", "execute_safe_flatten")
                    and decision.available
                    else None
                ),
                confirmation=None if descriptor.confirmation is None else descriptor.confirmation(ctx),
                next_step=decision.next_step,
                concurrency_token=concurrency_token,
                execution_ref=decision.execution_ref,
                mutation=descriptor.mutation,
                primary=False,
                needed=decision.needed,
                settlement=decision.settlement,
            )
        )
    primary_id = _primary_action_id(capabilities)
    return tuple(
        capability
        if capability.action_id != primary_id
        else replace(capability, primary=True)
        for capability in capabilities
    )


# The capabilities ``_decide`` marks available without reading any custody
# state. ``reconcile_now`` is the whole set today: its branch returns
# ``available=True`` unconditionally, consulting no hold, uncertainty or
# exposure, because a custody refresh is always a legal operator move.
#
# That makes it undiagnostic. It outranks every action below it in
# ``_primary_action_id``'s priority, so it becomes ``primary`` for any subject
# whose genuinely-gated cures are all unavailable -- including one with nothing
# wrong at all. A surface asking "what is this subject's cure?" (the roster
# rail) must skip these; a surface offering "what may I run?" (the bot panel)
# must keep them. Exported so that question is answered from the policy that
# creates the property, not re-derived by each consumer against a literal.
UNCONDITIONAL_RECOVERY_ACTION_IDS: frozenset[RecoveryActionId] = frozenset({"reconcile_now"})


def _primary_action_id(capabilities: list[RecoveryCapability]) -> str | None:
    priority = (
        "recover_exact_execution_evidence",
        "resolve_execution_coverage",
        "cancel_verified_working_orders",
        # Above Reconcile now, which cannot settle an order no read shows (#2854).
        "settle_unknown_exit_order",
        "reconcile_now",
        "stop_bot_decisions",
        "execute_safe_flatten",
        "prepare_safe_flatten",
        "discharge_attributed_residue",
        "end_expired_option",
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


def build_projection_guidance(
    ctx: RecoveryPolicyContext,
    actions: tuple[RecoveryCapability, ...],
) -> ProjectionGuidance:
    """Author Trader/Operator impact copy from the same capability set.

    ``actions`` is supplied rather than rebuilt here. Every caller already
    holds ``build_recovery_catalog(ctx)`` for the same ``ctx``, and rebuilding
    it made each projection compute that catalog twice — the dominant per-row
    cost of a catalog read, measured at ~43 % of its per-row work at 144 rows
    (#1801 §11 lever 2, predicted by #1907 §5(a)).
    """
    relevant = _relevant_uncertainties(ctx)
    available = tuple(action.label for action in actions if action.available)
    if ctx.authority_health != "healthy":
        return ProjectionGuidance(
            headline="Alpaca execution is on hold",
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
            recovery_status=primary.recovery_status,
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
