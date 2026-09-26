"""Recording uncertainty episodes, and deciding what one still authorizes.

Two halves over the same registry: the write path raises, refreshes, and
resolves episodes, and the capability path answers what an operator or
strategy may still do while they stand. The declarative table both consult
lives in ``uncertainty_policies``; the cause payload types live in
``uncertainty_causes``.

The stored R5 envelope is descriptive evidence. Authorization is granted only
when the registry recognizes the reason, facts schema, and capability. Unknown
causes are account-wide and fail closed; no generic clear primitive exists.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.broker.alpaca.clerk.live_arming import ARMING_ADMISSION_REASON_CODES
from app.broker.alpaca.clerk.live_envelope import ENVELOPE_ADMISSION_REASON_CODES
from app.broker.alpaca.clerk.sqlite.facts import (
    FACTS_SCHEMA_VERSION,
    UncertaintyRaisedFacts,
    UncertaintyResolvedFacts,
)
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.alpaca.clerk.sqlite.models import EffectOperationResource, TransitionInput
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    BROKER_SNAPSHOT_STALE_REASON_CODE,
    EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
    EXECUTION_PRICE_CONFLICT_REASON_CODE,
    EXIT_NOT_FLAT_REASON_CODE,
    EXIT_STUCK_REASON_CODE,
    FAILED_ENTER_FILLED_REASON_CODE,
    HOLD_REASON_CODES,
    LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
    ORDER_OUTCOME_UNKNOWN_REASON_CODE,
    POSITION_DRIFT_REASON_CODE,
    RECONCILIATION_INCOMPLETE_REASON_CODE,
    UNFOLDABLE_BROKER_ORDER_REASON_CODE,
    ExecutionPriceConflictCause,
    ExecutionPriceConflictOrder,
    ExitNotFlatCause,
    ExitStuckCause,
    FailedEnterFilledCause,
    FailedEnterFilledOrder,
    PositionDriftCause,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_folds import account_hold_envelope
from app.broker.alpaca.clerk.sqlite.uncertainty_policies import (
    Capability,
    ReasonPolicy,
    reason_policy,
)

DRIFT_REDUCTION_EVIDENCE_MAX_AGE_MS = 30_000


@dataclass(frozen=True)
class TransitionProvenance:
    """Optional broker provenance for the transition an episode appends.

    The trade-update evidence ingress is the one raiser that knows which
    broker order and which stream event it was reacting to. Carrying those
    onto the appended transition is what lets an auditor join the episode
    back to the frame that caused it; the sweep and the stream-health sync
    have no such single event and leave all three unset.

    ``proof_reference`` survives on a *raise* only. On a refresh,
    ``ClerkSqliteRepository.observe_uncertainty`` substitutes the active
    episode's ``uncertainty_id``, because that column is a refresh's only
    join back to its own episode — ``timeline_query`` matches an episode's
    transitions by sequence for the raise, by facts for the resolution, and
    by ``proof_reference`` for the refresh. Nothing is lost by the
    substitution: the causing event is in the same append's
    ``facts_json.evidence_refs``, so a refresh row carries strictly more than
    the pre-v12 ``observe_account_hold`` path wrote (ADR 0048 Decision 2).
    """

    broker_order_id: str | None = None
    proof_reference: str | None = None
    source_event_at_ms: int | None = None


def _has_attributed_exposure(repo: ClerkSqliteRepository, *, strategy_instance_id: str) -> bool:
    """Whether one strategy still has Clerk-attributed economic exposure.

    The Clerk is intentionally conservative here.  An ENTER remains in
    custody until an EXIT proves that the attributed position is flat, but a
    legacy/repaired projection can contain attributed exposure without a
    currently live ENTER effect.  Both cases forbid a fresh ENTER.
    """
    return any(
        position_quantity_is_nonzero(quantity)
        for quantity in repo.attributed_positions_for_strategy(strategy_instance_id).values()
    )


def _effective_identity(
    *, reason_code: str, strategy_instance_id: str | None
) -> tuple[ReasonPolicy | None, str, str | None]:
    policy = reason_policy(reason_code)
    if policy is None:
        return None, "ACCOUNT_CLERK", None
    if policy.scope == "CUSTODY_SUBJECT" and strategy_instance_id is not None:
        return policy, "CUSTODY_SUBJECT", strategy_instance_id
    return policy, "ACCOUNT_CLERK", None


def raise_uncertainty(
    repo: ClerkSqliteRepository,
    *,
    strategy_instance_id: str | None,
    reason_code: str,
    headline: str,
    explanation: str,
    operator_impact: str,
    next_step: str,
    evidence_refs: tuple[str, ...] = (),
    cause_facts: dict[str, Any] | None = None,
    severity: str = "warning",
    refresh_unchanged: bool = False,
    provenance: TransitionProvenance = TransitionProvenance(),
) -> str:
    """Raise or refresh one typed episode; unknown causes fail closed account-wide.

    Returns which of ``"raised"`` / ``"refreshed"`` / ``"unchanged"`` happened,
    rather than a bool. The stream-health sync logs raised and refreshed under
    different structured action codes, and collapsing them would erase an
    operator's only signal for whether an outage is new or ongoing. Callers
    that only care whether anything was recorded compare to ``"unchanged"``.
    """
    policy, scope, effective_strategy_instance_id = _effective_identity(
        reason_code=reason_code, strategy_instance_id=strategy_instance_id
    )
    blocks_new_exposure = True if policy is None else policy.blocks_new_exposure
    allows_reduction = False if policy is None else policy.allows_reduction
    facts = UncertaintyRaisedFacts(
        severity=severity,
        blocks_new_exposure=blocks_new_exposure,
        allows_reduction=allows_reduction,
        reason_code=reason_code,
        headline=headline,
        explanation=explanation,
        operator_impact=operator_impact,
        next_step=next_step,
        evidence_refs=list(evidence_refs),
        cause_facts=cause_facts or {},
    )
    facts_json = facts.to_facts_json()

    def build_transition(transition_kind: str) -> TransitionInput:
        return TransitionInput(
            strategy_instance_id=effective_strategy_instance_id,
            transition_kind=transition_kind,
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=repo.clock(),
            summary_code=transition_kind,
            facts_schema_version=FACTS_SCHEMA_VERSION,
            facts_json=facts_json,
            broker_order_id=provenance.broker_order_id,
            proof_reference=provenance.proof_reference,
            source_event_at_ms=provenance.source_event_at_ms,
        )

    outcome = repo.observe_uncertainty(
        scope=scope,
        reason_code=reason_code,
        strategy_instance_id=effective_strategy_instance_id,
        facts_json=facts_json,
        build_raise=lambda: build_transition("UNCERTAINTY_RAISED"),
        build_refresh=lambda: build_transition("UNCERTAINTY_REFRESHED"),
        refresh_unchanged=refresh_unchanged,
    )
    return outcome


def raise_account_hold(
    repo: ClerkSqliteRepository,
    *,
    reason_code: str,
    evidence_refs: list[str],
    provenance: TransitionProvenance = TransitionProvenance(),
    cause_facts: Mapping[str, Any] | None = None,
) -> str:
    """Raise or refresh one account-hold episode (ADR 0048 Decision 2).

    The former ``holds`` causes now travel the ordinary uncertainty path:
    same table, same append-on-change-only gate, same atomic check-then-append
    under one write lock. What this adds over calling :func:`raise_uncertainty`
    directly is that the caller supplies only its cause and evidence — the
    operator envelope comes from :func:`account_hold_envelope`, so the two
    producers cannot drift into describing the same hold differently.

    ``cause_facts`` is for a hold whose cause is not derivable from its
    evidence lines — the ADR 0059 D4 loss hold, whose cause is the breached
    day-P&L numbers. The envelope refuses to describe that hold without them.

    ``blocks_new_exposure`` and ``allows_reduction`` are deliberately *not*
    taken from the envelope: :func:`raise_uncertainty` reads them from the
    registered policy, which is the authority on what an episode permits.
    """
    envelope = account_hold_envelope(
        reason_code=reason_code, evidence_refs=evidence_refs, cause_facts=cause_facts
    )
    return raise_uncertainty(
        repo,
        strategy_instance_id=None,
        reason_code=envelope.reason_code,
        headline=envelope.headline,
        explanation=envelope.explanation,
        operator_impact=envelope.operator_impact,
        next_step=envelope.next_step,
        evidence_refs=tuple(envelope.evidence_refs),
        cause_facts=envelope.cause_facts,
        severity=envelope.severity,
        provenance=provenance,
    )


def resolve_account_hold(
    repo: ClerkSqliteRepository, *, reason_code: str, summary_code: str
) -> bool:
    """Close one account-hold episode once its cause is proven gone.

    ``summary_code`` stays the caller's, because it names *which* proof ended
    the episode — a completed reconciliation and a recovered stream are
    different receipts, and both were visible to operators before v12.
    """
    if reason_code not in HOLD_REASON_CODES:
        raise ValueError(f"{reason_code!r} is not an account-hold cause")
    return _resolve_account_uncertainty(
        repo,
        reason_code=reason_code,
        resolution_kind="CAUSE_CLEARED",
        summary_code=summary_code,
        evidence_refs=(),
    )


_CLEAN_BROKER_RESOLVABLE_REASONS = frozenset(
    {POSITION_DRIFT_REASON_CODE, BROKER_SNAPSHOT_STALE_REASON_CODE}
)


def _resolve_account_uncertainty(
    repo: ClerkSqliteRepository,
    *,
    reason_code: str,
    resolution_kind: str,
    summary_code: str,
    evidence_refs: tuple[str, ...],
) -> bool:
    def build_transition(uncertainty_id: str) -> TransitionInput:
        facts = UncertaintyResolvedFacts(
            uncertainty_id=uncertainty_id,
            resolution_kind=resolution_kind,
            evidence_refs=list(evidence_refs),
        )
        return TransitionInput(
            transition_kind="UNCERTAINTY_RESOLVED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=repo.clock(),
            summary_code=summary_code,
            facts_json=facts.to_facts_json(),
        )

    return repo.resolve_uncertainty_if_active(
        scope="ACCOUNT_CLERK",
        reason_code=reason_code,
        strategy_instance_id=None,
        build_transition=build_transition,
    )


# Causes an operator review ends. Each is a condition no broker proof can
# clear (the order itself can never fold), so the review is its only exit.
_OPERATOR_ACKNOWLEDGEABLE_REASONS = frozenset({UNFOLDABLE_BROKER_ORDER_REASON_CODE})


def resolve_operator_acknowledged_uncertainty(
    repo: ClerkSqliteRepository,
    *,
    reason_code: str,
    summary_code: str,
    evidence_refs: tuple[str, ...],
) -> bool:
    """Resolve an account episode whose registered exit is an operator review."""
    if reason_code not in _OPERATOR_ACKNOWLEDGEABLE_REASONS:
        raise ValueError(f"{reason_code!r} is not resolved by operator acknowledgement")
    return _resolve_account_uncertainty(
        repo,
        reason_code=reason_code,
        resolution_kind="OPERATOR_ACKNOWLEDGED",
        summary_code=summary_code,
        evidence_refs=evidence_refs,
    )


def resolve_reconciliation_uncertainty(
    repo: ClerkSqliteRepository,
    *,
    reason_code: str,
    evidence_refs: tuple[str, ...],
) -> bool:
    """Resolve only a cause whose clean broker prerequisite was just proven."""
    if reason_code not in _CLEAN_BROKER_RESOLVABLE_REASONS:
        raise ValueError(f"{reason_code!r} has no reconciliation-backed recovery policy")
    return _resolve_account_uncertainty(
        repo,
        reason_code=reason_code,
        resolution_kind="CLEAN_BROKER_RECONCILIATION",
        summary_code="UNCERTAINTY_RESOLVED_BY_RECONCILIATION",
        evidence_refs=evidence_refs,
    )


def resolve_incomplete_reconciliation_uncertainty(
    repo: ClerkSqliteRepository,
    *,
    evidence_refs: tuple[str, ...],
) -> bool:
    """Resolve an aborted pass only after a later pass reached finalization."""
    return _resolve_account_uncertainty(
        repo,
        reason_code=RECONCILIATION_INCOMPLETE_REASON_CODE,
        resolution_kind="COMPLETE_ACCOUNT_RECONCILIATION",
        summary_code="INCOMPLETE_RECONCILIATION_RESOLVED",
        evidence_refs=evidence_refs,
    )


def resolve_exit_not_flat_uncertainty(
    repo: ClerkSqliteRepository,
    *,
    strategy_instance_id: str,
    evidence_refs: tuple[str, ...],
) -> bool:
    """Close only the bot-scoped non-flat fence after attributed-flat proof."""

    def build_transition(uncertainty_id: str) -> TransitionInput:
        facts = UncertaintyResolvedFacts(
            uncertainty_id=uncertainty_id,
            resolution_kind="ATTRIBUTED_FLAT_PROVEN",
            evidence_refs=list(evidence_refs),
        )
        return TransitionInput(
            strategy_instance_id=strategy_instance_id,
            transition_kind="UNCERTAINTY_RESOLVED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=repo.clock(),
            summary_code="EXIT_NOT_FLAT_RESOLVED",
            facts_json=facts.to_facts_json(),
        )

    return repo.resolve_uncertainty_if_active(
        scope="CUSTODY_SUBJECT",
        reason_code=EXIT_NOT_FLAT_REASON_CODE,
        strategy_instance_id=strategy_instance_id,
        build_transition=build_transition,
    )


def resolve_exit_stuck_uncertainty(
    repo: ClerkSqliteRepository,
    *,
    strategy_instance_id: str,
    evidence_refs: tuple[str, ...],
) -> bool:
    """Close the escalated stuck-EXIT fence after attributed-flat proof.

    An ``EXIT_STUCK`` episode outlives its originating ``EXIT_NOT_FLAT`` (the
    watchdog resolves the latter's fence only implicitly, by re-driving). Once
    the operator's safe flatten — or any later reduction — proves the strategy
    attributed-flat, this must clear too, or the now-flat strategy stays
    permanently barred from new exposure.
    """

    def build_transition(uncertainty_id: str) -> TransitionInput:
        facts = UncertaintyResolvedFacts(
            uncertainty_id=uncertainty_id,
            resolution_kind="ATTRIBUTED_FLAT_PROVEN",
            evidence_refs=list(evidence_refs),
        )
        return TransitionInput(
            strategy_instance_id=strategy_instance_id,
            transition_kind="UNCERTAINTY_RESOLVED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=repo.clock(),
            summary_code="EXIT_STUCK_RESOLVED",
            facts_json=facts.to_facts_json(),
        )

    return repo.resolve_uncertainty_if_active(
        scope="CUSTODY_SUBJECT",
        reason_code=EXIT_STUCK_REASON_CODE,
        strategy_instance_id=strategy_instance_id,
        build_transition=build_transition,
    )


def resolve_flat_exit_fences(
    repo: ClerkSqliteRepository,
    *,
    strategy_instance_id: str,
    evidence_refs: tuple[str, ...],
) -> None:
    """Close a strategy's EXIT fences once its attribution is flat in every symbol.

    A stuck-EXIT escalation outlives its EXIT_NOT_FLAT origin, so the same
    attributed-flat proof clears both, or a now-flat strategy stays barred
    from new exposure. A strategy still holding anything keeps both.
    """
    attributed = repo.attributed_positions_for_strategy(strategy_instance_id)
    if any(position_quantity_is_nonzero(quantity) for quantity in attributed.values()):
        return
    resolve_exit_not_flat_uncertainty(
        repo, strategy_instance_id=strategy_instance_id, evidence_refs=evidence_refs
    )
    resolve_exit_stuck_uncertainty(
        repo, strategy_instance_id=strategy_instance_id, evidence_refs=evidence_refs
    )


def _active_failed_enter_filled_cause(
    repo: ClerkSqliteRepository, *, strategy_instance_id: str
) -> FailedEnterFilledCause | None:
    episode = repo.active_uncertainty(
        scope="CUSTODY_SUBJECT",
        reason_code=FAILED_ENTER_FILLED_REASON_CODE,
        strategy_instance_id=strategy_instance_id,
    )
    if episode is None:
        return None
    return FailedEnterFilledCause.from_mapping(
        UncertaintyRaisedFacts.from_facts_json(episode["facts_json"]).cause_facts
    )


def failed_enter_fill_is_answered(
    repo: ClerkSqliteRepository,
    *,
    strategy_instance_id: str,
    order_ref: str,
    filled_qty: float,
) -> bool:
    """Whether the newest episode naming this order recorded this fill quantity (#2348).

    Active *or* resolved: a fence a legitimate flatten cleared must not
    re-raise while the order's fills are unchanged, but any change since that
    newest answer -- a further fill, or a correction back to a quantity an
    *older* episode once recorded -- is a new contradiction and must.
    """
    for episode in repo.uncertainty_history(
        scope="CUSTODY_SUBJECT",
        reason_code=FAILED_ENTER_FILLED_REASON_CODE,
        strategy_instance_id=strategy_instance_id,
    ):
        cause = FailedEnterFilledCause.from_mapping(
            UncertaintyRaisedFacts.from_facts_json(episode["facts_json"]).cause_facts
        )
        for order in cause.orders:
            if order.order_ref == order_ref:
                return not position_quantity_is_nonzero(order.filled_qty - filled_qty)
    return False


def raise_failed_enter_filled_uncertainty(
    repo: ClerkSqliteRepository,
    *,
    strategy_instance_id: str,
    order_ref: str,
    symbol: str,
    filled_qty: float,
) -> str:
    """Flag a fill on an ENTER the Clerk had already folded terminal (#2348).

    The fill itself is real and stays attributed -- the Clerk's belief must
    keep matching the broker. What must not happen is absorbing it silently:
    the strategy believes it is flat, so it would never exit, and every fresh
    ENTER would be refused on the attributed exposure. This episode names the
    order, refuses new exposure for the instance, and admits reduction of the
    contradicted symbols only. A second contradicted order on the same
    instance widens the open episode rather than replacing it.

    Read-merge-write: the active cause is read, widened, and written back as
    a refresh. The caller must hold the Clerk's intake lock, or two
    concurrent raisers could each widen a stale read and drop an order.
    """
    order = FailedEnterFilledOrder(
        order_ref=order_ref, symbol=symbol.upper(), filled_qty=filled_qty
    )
    active = _active_failed_enter_filled_cause(repo, strategy_instance_id=strategy_instance_id)
    cause = (
        FailedEnterFilledCause(orders=(order,)) if active is None else active.with_order(order)
    )
    refs = ", ".join(item.order_ref for item in cause.orders)
    symbols = ", ".join(sorted(cause.symbols))
    return raise_uncertainty(
        repo,
        strategy_instance_id=strategy_instance_id,
        reason_code=FAILED_ENTER_FILLED_REASON_CODE,
        headline="An entry the Clerk recorded as failed has filled",
        explanation=(
            f"Entry order {refs} was recorded as failed, but the broker later reported "
            f"a fill. The Clerk now holds the resulting {symbols} position for this bot; "
            "the strategy still believes it is flat and will not exit it."
        ),
        operator_impact=(
            "New entries for this bot are refused. Reducing the position is allowed."
        ),
        next_step=(
            "Stop the bot and flatten the position, then reconcile. The fence clears "
            "once reconciliation proves the bot flat in the affected symbol."
        ),
        evidence_refs=tuple(item.order_ref for item in cause.orders),
        cause_facts=cause.to_mapping(),
        severity="error",
    )


def resolve_failed_enter_filled_uncertainty_if_flat(
    repo: ClerkSqliteRepository,
    *,
    strategy_instance_id: str,
    evidence_refs: tuple[str, ...],
) -> bool:
    """Close the #2348 fence once every contradicted symbol is attributed-flat.

    The caller supplies the broker half of the proof: this runs only from a
    reconciliation pass whose broker snapshot matched attribution exactly.
    """
    cause = _active_failed_enter_filled_cause(repo, strategy_instance_id=strategy_instance_id)
    if cause is None or any(
        position_quantity_is_nonzero(repo.position(strategy_instance_id, symbol))
        for symbol in cause.symbols
    ):
        return False

    def build_transition(uncertainty_id: str) -> TransitionInput:
        facts = UncertaintyResolvedFacts(
            uncertainty_id=uncertainty_id,
            resolution_kind="ATTRIBUTED_FLAT_PROVEN",
            evidence_refs=list(evidence_refs),
        )
        return TransitionInput(
            strategy_instance_id=strategy_instance_id,
            transition_kind="UNCERTAINTY_RESOLVED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=repo.clock(),
            summary_code="FAILED_ENTER_FILLED_RESOLVED",
            facts_json=facts.to_facts_json(),
        )

    return repo.resolve_uncertainty_if_active(
        scope="CUSTODY_SUBJECT",
        reason_code=FAILED_ENTER_FILLED_REASON_CODE,
        strategy_instance_id=strategy_instance_id,
        build_transition=build_transition,
    )


def _price_conflict_envelope(cause: ExecutionPriceConflictCause) -> UncertaintyRaisedFacts:
    """The operator envelope for one state of the price-conflict cause."""
    refs = ", ".join(item.order_ref for item in cause.orders)
    return UncertaintyRaisedFacts(
        severity="error",
        blocks_new_exposure=True,
        allows_reduction=True,
        reason_code=EXECUTION_PRICE_CONFLICT_REASON_CODE,
        headline="A broker order total disagrees on price with its recorded fills",
        explanation=(
            f"The broker reported an average fill price for order(s) {refs} that "
            "materially disagrees with the average of the exact fills the Clerk "
            "recorded for the same quantity. The recorded fills, positions and "
            "FIFO P&L inputs are unchanged; the disagreement is held open as "
            "economic evidence."
        ),
        operator_impact=(
            "New entries for this bot are refused and its economic coverage reads incomplete. "
            "Reducing or exiting the position stays allowed."
        ),
        next_step=(
            "Check the broker's execution corrections for the named orders. The conflict "
            "clears when the recorded fills and the broker's reported average agree again "
            "within tolerance."
        ),
        evidence_refs=[item.order_ref for item in cause.orders],
        cause_facts=cause.to_mapping(),
    )


def raise_execution_price_conflict_uncertainty(
    repo: ClerkSqliteRepository,
    *,
    effect: EffectOperationResource,
    order: ExecutionPriceConflictOrder,
) -> str:
    """Record one order whose broker total restated the price of its fills (#2460).

    The recorded exact fills are never rewritten; the episode keeps the
    broker's reported average price and the recorded average for the same
    quantity as durable evidence, reports the owner's economic coverage
    incomplete, and never fences reductions. A second conflicted order on the
    same custody subject widens the open episode, and re-folding the same
    conflicting total is a no-op (``"unchanged"``) -- one conflict, not one
    per re-read.

    The episode binds to ``effect``'s durable custody subject, which the
    raise/refresh transitions also carry, so bot and manual-order work
    (whose effects have no strategy instance) scope identically (#2460
    review).

    The cause-accumulating merge is atomic under the repository write lock
    (:meth:`ClerkSqliteRepository.widen_execution_price_conflict`), so callers
    need no intake lock: ``fold_order_evidence`` runs from intake-held paths
    (the trade-updates sink, the reconciliation snapshot fold) and from
    broker-IO-interleaved resolution paths (enter submit resolution, EXIT
    cancel-and-prove, manual cancellation) alike.
    """
    def build(cause: ExecutionPriceConflictCause, kind: str) -> TransitionInput:
        return TransitionInput(
            strategy_instance_id=effect.strategy_instance_id,
            run_id=effect.run_id,
            command_id=effect.command_id,
            effect_operation_id=effect.effect_operation_id,
            transition_kind=kind,
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=repo.clock(),
            summary_code=EXECUTION_PRICE_CONFLICT_REASON_CODE,
            facts_json=_price_conflict_envelope(cause).to_facts_json(),
        )

    return repo.widen_execution_price_conflict(
        effect_operation_id=effect.effect_operation_id,
        order=order,
        build_raise=lambda cause: build(cause, "UNCERTAINTY_RAISED"),
        build_refresh=lambda cause: build(cause, "UNCERTAINTY_REFRESHED"),
    )


def clear_execution_price_conflict_order(
    repo: ClerkSqliteRepository,
    *,
    effect: EffectOperationResource,
    order_ref: str,
    source_event_at_ms: int | None = None,
    expected_episode: tuple[str, ExecutionPriceConflictCause] | None = None,
) -> str:
    """Drop ``order_ref`` from the open price conflict; end it if that was the last (#2460).

    Called when the broker's last reported average for ``order_ref`` and the
    recorded effective fills' average agree within tolerance again -- whether
    a later total restated the original price (``source_event_at_ms`` then
    names that total, and an older one changes nothing), or an identified
    execution correction changed the recorded fills to match (the
    reconciliation sweep's re-derivation,
    :func:`order_evidence.reconcile_execution_price_conflicts`, finds the
    latter from recorded evidence alone, because a terminal order's totals
    are never re-folded, and passes ``expected_episode`` so a concurrently
    refreshed episode is refused). ``"absent"`` when no open episode names
    the order. Atomic like the raise.
    """
    def build(cause: ExecutionPriceConflictCause) -> TransitionInput:
        return TransitionInput(
            strategy_instance_id=effect.strategy_instance_id,
            run_id=effect.run_id,
            command_id=effect.command_id,
            effect_operation_id=effect.effect_operation_id,
            transition_kind="UNCERTAINTY_REFRESHED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=repo.clock(),
            summary_code=EXECUTION_PRICE_CONFLICT_REASON_CODE,
            facts_json=_price_conflict_envelope(cause).to_facts_json(),
        )

    def build_resolved(uncertainty_id: str) -> TransitionInput:
        facts = UncertaintyResolvedFacts(
            uncertainty_id=uncertainty_id,
            resolution_kind="CAUSE_CLEARED",
            evidence_refs=[order_ref],
        )
        return TransitionInput(
            strategy_instance_id=effect.strategy_instance_id,
            run_id=effect.run_id,
            command_id=effect.command_id,
            effect_operation_id=effect.effect_operation_id,
            transition_kind="UNCERTAINTY_RESOLVED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=repo.clock(),
            summary_code="EXECUTION_PRICE_AGREEMENT_PROVEN",
            facts_json=facts.to_facts_json(),
        )

    return repo.clear_execution_price_conflict_order(
        effect_operation_id=effect.effect_operation_id,
        order_ref=order_ref,
        source_event_at_ms=source_event_at_ms,
        expected_episode=expected_episode,
        build_transition=build,
        build_resolved=build_resolved,
    )


@dataclass(frozen=True)
class CapabilityDecision:
    allowed: bool
    capability: Capability
    reason_code: str | None = None
    why: str | None = None


@dataclass(frozen=True)
class ReductionIntent:
    """The exact broker action being authorized, not a generic reduce flag."""

    symbol: str
    side: str
    quantity: float

    @property
    def signed_delta(self) -> float:
        return self.quantity if self.side.upper() == "BUY" else -self.quantity


def _strict_uncertainty_facts(uncertainty: dict[str, Any], policy: ReasonPolicy) -> UncertaintyRaisedFacts | None:
    if uncertainty["facts_schema_version"] != policy.facts_schema_version:
        return None
    try:
        facts = UncertaintyRaisedFacts.from_facts_json(uncertainty["facts_json"])
    except (TypeError, ValueError, KeyError):
        return None
    if (
        facts.reason_code != uncertainty["reason_code"]
        or facts.blocks_new_exposure != bool(uncertainty["blocks_new_exposure"])
        or facts.allows_reduction != bool(uncertainty["allows_reduction"])
        or not policy.cause_is_valid(facts.cause_facts)
    ):
        return None
    return facts


def _moves_toward_zero_without_crossing(quantity: float, delta: float) -> bool:
    if not position_quantity_is_nonzero(quantity) or quantity * delta >= 0:
        return False
    after = quantity + delta
    return not position_quantity_is_nonzero(after) or (
        quantity * after > 0 and abs(after) < abs(quantity)
    )


def _position_drift_allows_action(
    repo: ClerkSqliteRepository,
    *,
    uncertainty: dict[str, Any],
    facts: UncertaintyRaisedFacts,
    intent: ReductionIntent | None,
) -> bool:
    if intent is None or intent.quantity <= 0 or intent.side.upper() not in {"BUY", "SELL"}:
        return False
    age_ms = repo.clock() - uncertainty["observed_at_ms"]
    if age_ms < 0 or age_ms > DRIFT_REDUCTION_EVIDENCE_MAX_AGE_MS:
        return False
    try:
        cause = PositionDriftCause.from_mapping(facts.cause_facts)
    except ValueError:
        return False
    observation = next(
        (position for position in cause.positions if position.symbol == intent.symbol.upper()),
        None,
    )
    if observation is None:
        return False
    current_attributed = repo.attributed_positions_by_symbol().get(intent.symbol.upper(), 0.0)
    if position_quantity_is_nonzero(current_attributed - observation.attributed_qty):
        return False
    return _moves_toward_zero_without_crossing(
        observation.broker_qty, intent.signed_delta
    ) and _moves_toward_zero_without_crossing(current_attributed, intent.signed_delta)


def _hold_defers_reduction_to_its_cause(reason_code: str) -> bool:
    """Whether this hold leaves REDUCE to the per-reason evaluation."""
    policy = reason_policy(reason_code)
    return policy is not None and policy.allows_reduction


def _position_drift_proof(
    repo: ClerkSqliteRepository,
    *,
    uncertainty: dict[str, Any],
    facts: UncertaintyRaisedFacts,
    intent: ReductionIntent | None,
    strategy_instance_id: str | None,
) -> bool:
    return _position_drift_allows_action(
        repo, uncertainty=uncertainty, facts=facts, intent=intent
    )


def _symbol_scoped_reduction_proof(
    repo: ClerkSqliteRepository,
    *,
    symbols: frozenset[str],
    intent: ReductionIntent | None,
    strategy_instance_id: str | None,
) -> bool:
    """A reduction of one of the cause's symbols, toward zero without crossing.

    The shared proof of every custody-subject cause that names the symbols it
    fences (EXIT_NOT_FLAT, EXIT_STUCK, FAILED_ENTER_FILLED): the intent must
    be a well-formed order on a named symbol that moves the instance's
    attributed position toward zero and never through it.
    """
    if (
        intent is None
        or strategy_instance_id is None
        or intent.quantity <= 0
        or intent.side.upper() not in {"BUY", "SELL"}
    ):
        return False
    symbol = intent.symbol.upper()
    return symbol in symbols and _moves_toward_zero_without_crossing(
        repo.position(strategy_instance_id, symbol), intent.signed_delta
    )


def _exit_not_flat_proof(
    repo: ClerkSqliteRepository,
    *,
    uncertainty: dict[str, Any],
    facts: UncertaintyRaisedFacts,
    intent: ReductionIntent | None,
    strategy_instance_id: str | None,
) -> bool:
    try:
        cause = ExitNotFlatCause.from_mapping(facts.cause_facts)
    except ValueError:
        return False
    return _symbol_scoped_reduction_proof(
        repo,
        symbols=frozenset({cause.symbol}),
        intent=intent,
        strategy_instance_id=strategy_instance_id,
    )


def _exit_stuck_proof(
    repo: ClerkSqliteRepository,
    *,
    uncertainty: dict[str, Any],
    facts: UncertaintyRaisedFacts,
    intent: ReductionIntent | None,
    strategy_instance_id: str | None,
) -> bool:
    try:
        cause = ExitStuckCause.from_mapping(facts.cause_facts)
    except ValueError:
        return False
    return _symbol_scoped_reduction_proof(
        repo,
        symbols=frozenset({cause.symbol}),
        intent=intent,
        strategy_instance_id=strategy_instance_id,
    )


def _failed_enter_filled_proof(
    repo: ClerkSqliteRepository,
    *,
    uncertainty: dict[str, Any],
    facts: UncertaintyRaisedFacts,
    intent: ReductionIntent | None,
    strategy_instance_id: str | None,
) -> bool:
    try:
        cause = FailedEnterFilledCause.from_mapping(facts.cause_facts)
    except ValueError:
        return False
    return _symbol_scoped_reduction_proof(
        repo,
        symbols=cause.symbols,
        intent=intent,
        strategy_instance_id=strategy_instance_id,
    )


def _no_per_symbol_proof(
    repo: ClerkSqliteRepository,
    *,
    uncertainty: dict[str, Any],
    facts: UncertaintyRaisedFacts,
    intent: ReductionIntent | None,
    strategy_instance_id: str | None,
) -> bool:
    """The proof an account-scoped reduction-admitting cause needs: none.

    A cause that says nothing about any one position -- ADR 0059 D4's loss
    hold is the first -- cannot be asked for a per-symbol proof. Registering
    this function against a reason code is how that cause declares it has
    nothing per-symbol to prove; a reduction-admitting code with no
    registration is refused, not admitted. Fail-closed is unaffected: the
    caller has already required ``policy.allows_reduction``, which only the
    registry can grant, so an unregistered or reduction-forbidding cause
    never reaches here.
    """
    return True


def _unregistered_proof(
    repo: ClerkSqliteRepository,
    *,
    uncertainty: dict[str, Any],
    facts: UncertaintyRaisedFacts,
    intent: ReductionIntent | None,
    strategy_instance_id: str | None,
) -> bool:
    """The proof dispatch falls back to when a reason code has none registered.

    An ``allows_reduction`` code with no proof here is refused, not admitted.
    """
    return False


type ReductionProof = Callable[..., bool]
# Dispatch on the reason code, once. Registering a proof here is how a cause
# says "authorize this reduction only if my own evidence bears it out";
# registering ``_no_per_symbol_proof`` is how an account-scoped cause
# declares it has nothing per-symbol to prove instead. A reduction-admitting
# code with no registration here is refused, not admitted.
_REDUCTION_PROOFS: dict[str, ReductionProof] = {
    POSITION_DRIFT_REASON_CODE: _position_drift_proof,
    EXIT_NOT_FLAT_REASON_CODE: _exit_not_flat_proof,
    EXIT_STUCK_REASON_CODE: _exit_stuck_proof,
    FAILED_ENTER_FILLED_REASON_CODE: _failed_enter_filled_proof,
    # #2460: the price conflict doubts the cost of executions whose quantity
    # both sides agree on, so it has nothing per-symbol to prove before a
    # reduction -- a position stays reduceable while its basis is disputed.
    EXECUTION_PRICE_CONFLICT_REASON_CODE: _no_per_symbol_proof,
    LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE: _no_per_symbol_proof,
    UNFOLDABLE_BROKER_ORDER_REASON_CODE: _no_per_symbol_proof,
}


def decide_capability(
    repo: ClerkSqliteRepository,
    *,
    capability: Capability,
    strategy_instance_id: str | None = None,
    subject_id: str | None = None,
    reduction_intent: ReductionIntent | None = None,
    continuation_ticket_id: str | None = None,
) -> CapabilityDecision:
    """Author both preview and execution policy from the same typed snapshot."""
    if (strategy_instance_id is None) == (subject_id is None):
        raise ValueError("capability evaluation requires exactly one custody subject")

    if capability in (Capability.CANCEL, Capability.RECONCILE):
        return CapabilityDecision(allowed=True, capability=capability)

    if capability is Capability.NEW_EXPOSURE:
        if repo.reconciliation_in_progress():
            return CapabilityDecision(
                allowed=False,
                capability=capability,
                reason_code="RECONCILIATION_IN_PROGRESS",
                why="Account reconciliation is proving fresh broker truth.",
            )
        active_exit = (
            repo.active_exit_for_strategy(strategy_instance_id)
            if strategy_instance_id is not None
            else None
        )
        if active_exit is not None:
            return CapabilityDecision(
                allowed=False,
                capability=capability,
                reason_code="EXIT_IN_PROGRESS",
                why=(
                    f"EXIT {active_exit.effect_operation_id} is still canceling and "
                    "flattening this strategy's captured exposure."
                ),
            )

    # Before ADR 0059 D4 every hold forbade both capabilities, so this check
    # could short-circuit whatever it was asked about. The loss hold blocks
    # entries account-wide and admits every exit, so a hold whose registered
    # policy allows reduction falls through to the per-reason evaluation below
    # rather than being refused here. An unregistered code has no policy and
    # stays fail-closed.
    holds = [
        hold
        for hold in repo.active_holds_for_admission(
            strategy_instance_id=strategy_instance_id,
            subject_id=subject_id,
        )
        if capability is Capability.NEW_EXPOSURE
        or not _hold_defers_reduction_to_its_cause(hold["reason_code"])
    ]
    if holds:
        hold = holds[0]
        return CapabilityDecision(
            allowed=False,
            capability=capability,
            reason_code=hold["reason_code"],
            why=f"An active {hold['scope'].lower()}-scoped hold blocks {capability.value.lower()}.",
        )

    for uncertainty in repo.active_uncertainties_for_admission(
        strategy_instance_id=strategy_instance_id,
        subject_id=subject_id,
    ):
        reason_code = uncertainty["reason_code"]
        policy = reason_policy(reason_code)
        facts = None if policy is None else _strict_uncertainty_facts(uncertainty, policy)
        understood = facts is not None
        blocked = (
            bool(uncertainty["blocks_new_exposure"])
            if capability is Capability.NEW_EXPOSURE
            else not (
                understood
                and policy is not None
                and policy.allows_reduction
                and uncertainty["allows_reduction"]
                and _REDUCTION_PROOFS.get(reason_code, _unregistered_proof)(
                    repo,
                    uncertainty=uncertainty,
                    facts=facts,
                    intent=reduction_intent,
                    strategy_instance_id=strategy_instance_id,
                )
            )
        )
        if blocked:
            return CapabilityDecision(
                allowed=False,
                capability=capability,
                reason_code=reason_code,
                why=uncertainty["explanation"],
            )
    if capability is Capability.NEW_EXPOSURE and strategy_instance_id is not None:
        active_enter = next(
            (
                effect
                for effect in repo.reconcilable_effect_operations()
                if effect.strategy_instance_id == strategy_instance_id and effect.kind == "ENTER"
            ),
            None,
        )
        if active_enter is not None:
            return CapabilityDecision(
                allowed=False,
                capability=capability,
                reason_code="ENTER_IN_PROGRESS",
                why=(
                    f"ENTER {active_enter.effect_operation_id} still owns a pending or open "
                    "custody lifecycle for this strategy."
                ),
            )
        if _has_attributed_exposure(repo, strategy_instance_id=strategy_instance_id):
            return CapabilityDecision(
                allowed=False,
                capability=capability,
                reason_code="ATTRIBUTED_EXPOSURE_EXISTS",
                why=(
                    "This strategy still has Clerk-attributed exposure; a fresh ENTER waits "
                    "for a proved EXIT to flat."
                ),
            )
    if capability is Capability.NEW_EXPOSURE:
        manual_order_outstanding = (
            repo.has_nonterminal_manual_order_outside_ticket(ticket_id=continuation_ticket_id)
            if continuation_ticket_id is not None
            else repo.has_nonterminal_manual_order()
        )
        if manual_order_outstanding:
            return CapabilityDecision(
                allowed=False,
                capability=capability,
                reason_code="MANUAL_ORDER_OUTSTANDING",
                why=(
                    "A manual order still has a working or unknown broker outcome; "
                    "new account exposure waits for exact resolution."
                ),
            )
    return CapabilityDecision(allowed=True, capability=capability)


class AdmissionBlockedError(Exception):
    def __init__(self, decision: CapabilityDecision) -> None:
        self.decision = decision
        super().__init__(f"{decision.capability.value.lower()} blocked: {decision.reason_code} — {decision.why}")


class RefusalClass(StrEnum):
    """Whether an admission refusal self-heals via the reconciliation sweep."""

    TRANSIENT = "transient"
    TERMINAL = "terminal"


# Codes the automatic reconciliation sweep resolves without operator action:
# a fresh successful broker snapshot clears BROKER_SNAPSHOT_STALE, a complete
# pass clears RECONCILIATION_INCOMPLETE, and RECONCILIATION_IN_PROGRESS ends
# when the in-flight pass ends. Everything else — including every
# CUSTODY_SUBJECT episode and every unknown future code — stays TERMINAL so
# an unclassified refusal keeps today's fail-closed behavior (F19 fix shape:
# ops study §9 "classify snapshot-staleness admission blocks as
# retry-on-next-clock in the runner's error taxonomy"). Every envelope refusal
# joins them: ADR 0059 forbids halting or pausing a bot for an account-scoped
# fact, so an envelope refusal retries on the next decision clock. Every
# arming refusal joins them too (slice 7): a lost arming refuses that
# instance's next ENTER, is warned about once per transition and is named in
# the live verdict — nothing pauses, and nothing halts from admission. The
# unfoldable-broker-order entry pause (#2363) joins them for the same ADR 0059
# reason: it is an account-scoped fact an operator review ends, so a bot's
# refused ENTER is a ``blocked`` receipt retried on the next decision clock,
# never a crash that leaves the bot dead after the review.
TRANSIENT_ADMISSION_REASON_CODES: frozenset[str] = (
    frozenset(
        {
            BROKER_SNAPSHOT_STALE_REASON_CODE,
            RECONCILIATION_INCOMPLETE_REASON_CODE,
            "RECONCILIATION_IN_PROGRESS",
            UNFOLDABLE_BROKER_ORDER_REASON_CODE,
        }
    )
    | ENVELOPE_ADMISSION_REASON_CODES
    | ARMING_ADMISSION_REASON_CODES
)


def classify_admission_refusal(reason_code: str | None) -> RefusalClass:
    if reason_code in TRANSIENT_ADMISSION_REASON_CODES:
        return RefusalClass.TRANSIENT
    return RefusalClass.TERMINAL


def require_capability(
    repo: ClerkSqliteRepository,
    *,
    capability: Capability,
    strategy_instance_id: str | None = None,
    subject_id: str | None = None,
    reduction_intent: ReductionIntent | None = None,
    continuation_ticket_id: str | None = None,
) -> None:
    decision = decide_capability(
        repo,
        capability=capability,
        strategy_instance_id=strategy_instance_id,
        subject_id=subject_id,
        reduction_intent=reduction_intent,
        continuation_ticket_id=continuation_ticket_id,
    )
    if not decision.allowed:
        raise AdmissionBlockedError(decision)


def admit_new_exposure(repo: ClerkSqliteRepository, *, strategy_instance_id: str) -> CapabilityDecision:
    return decide_capability(
        repo,
        capability=Capability.NEW_EXPOSURE,
        strategy_instance_id=strategy_instance_id,
    )


def require_admission(repo: ClerkSqliteRepository, *, strategy_instance_id: str) -> None:
    require_capability(
        repo,
        capability=Capability.NEW_EXPOSURE,
        strategy_instance_id=strategy_instance_id,
    )


def require_manual_admission(
    repo: ClerkSqliteRepository,
    *,
    subject_id: str,
    continuation_ticket_id: str | None = None,
) -> None:
    """Apply the same account/subject admission policy to manual custody."""
    require_capability(
        repo,
        capability=Capability.NEW_EXPOSURE,
        subject_id=subject_id,
        continuation_ticket_id=continuation_ticket_id,
    )


def require_manual_reduction(
    repo: ClerkSqliteRepository,
    *,
    subject_id: str,
    intent: ReductionIntent,
) -> None:
    """Authorize a manual sell only within its independently owned long custody."""
    require_capability(
        repo,
        capability=Capability.REDUCE,
        subject_id=subject_id,
        reduction_intent=intent,
    )
    available_quantity = repo.manual_reduction_available_quantity(
        subject_id=subject_id,
        symbol=intent.symbol,
    )
    if intent.side.upper() != "SELL" or (
        intent.quantity > available_quantity
        and position_quantity_is_nonzero(intent.quantity - available_quantity)
    ):
        decision = CapabilityDecision(
            allowed=False,
            capability=Capability.REDUCE,
            reason_code="MANUAL_LONG_QUANTITY_UNAVAILABLE",
            why=(
                f"Only {available_quantity:g} {intent.symbol.upper()} is available from this manual "
                "custody subject after pending manual reductions."
            ),
        )
        raise AdmissionBlockedError(decision)


__all__ = [
    "BROKER_SNAPSHOT_STALE_REASON_CODE",
    "DRIFT_REDUCTION_EVIDENCE_MAX_AGE_MS",
    "EXECUTION_COVERAGE_CONFLICT_REASON_CODE",
    "EXIT_NOT_FLAT_REASON_CODE",
    "EXIT_STUCK_REASON_CODE",
    "FAILED_ENTER_FILLED_REASON_CODE",
    "ORDER_OUTCOME_UNKNOWN_REASON_CODE",
    "POSITION_DRIFT_REASON_CODE",
    "RECONCILIATION_INCOMPLETE_REASON_CODE",
    "TRANSIENT_ADMISSION_REASON_CODES",
    "AdmissionBlockedError",
    "Capability",
    "CapabilityDecision",
    "ReductionIntent",
    "RefusalClass",
    "admit_new_exposure",
    "classify_admission_refusal",
    "decide_capability",
    "failed_enter_fill_is_answered",
    "raise_failed_enter_filled_uncertainty",
    "raise_uncertainty",
    "require_admission",
    "require_capability",
    "require_manual_admission",
    "require_manual_reduction",
    "resolve_exit_not_flat_uncertainty",
    "resolve_exit_stuck_uncertainty",
    "resolve_failed_enter_filled_uncertainty_if_flat",
    "resolve_incomplete_reconciliation_uncertainty",
    "resolve_reconciliation_uncertainty",
]
