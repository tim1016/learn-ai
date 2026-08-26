"""Typed uncertainty causes and one backend capability policy.

The stored R5 envelope is descriptive evidence. Authorization is granted only
when this module recognizes the reason, facts schema, and capability. Unknown
causes are account-wide and fail closed; no generic clear primitive exists.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.broker.alpaca.clerk.sqlite.facts import (
    FACTS_SCHEMA_VERSION,
    UncertaintyRaisedFacts,
    UncertaintyResolvedFacts,
)
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    BROKER_SNAPSHOT_STALE_REASON_CODE,
    EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
    EXIT_NOT_FLAT_REASON_CODE,
    EXIT_STUCK_REASON_CODE,
    ORDER_OUTCOME_UNKNOWN_REASON_CODE,
    POSITION_DRIFT_REASON_CODE,
    RECONCILIATION_INCOMPLETE_REASON_CODE,
    ExecutionCoverageConflictCause,
    ExitNotFlatCause,
    ExitStuckCause,
    PositionDriftCause,
    broker_snapshot_stale_cause_is_valid,
    reconciliation_incomplete_cause_is_valid,
)

DRIFT_REDUCTION_EVIDENCE_MAX_AGE_MS = 30_000


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


class Capability(StrEnum):
    NEW_EXPOSURE = "NEW_EXPOSURE"
    CANCEL = "CANCEL"
    REDUCE = "REDUCE"
    RECONCILE = "RECONCILE"


@dataclass(frozen=True)
class CauseCleared:
    """The episode ends when, and only when, its cause is proven gone.

    No clock at all. This is the honest declaration for an episode this ADR
    does not add age behaviour to (ADR 0048 Decision 1).
    """


@dataclass(frozen=True)
class VoidAfter:
    """Auto-close the episode once its cause has stood unresolved for
    ``grace_ms``, with a receipt whose ``summary_code`` names the age rule
    that closed it."""

    grace_ms: int
    summary_code: str


@dataclass(frozen=True)
class RedriveThenEscalate:
    """Retry the resolution every ``after_ms``, at most ``max_count`` times,
    then open the ``escalate_to`` successor episode."""

    after_ms: int
    max_count: int
    escalate_to: str


# A closed sum of exactly three shapes (ADR 0048 Decision 1). Deliberately
# not three optional fields on ReasonPolicy: independent fields admit
# combinations with no meaning (e.g. a grace window racing a redrive clock
# for the same episode). The sum makes those combinations unrepresentable.
AgePolicy = CauseCleared | VoidAfter | RedriveThenEscalate


@dataclass(frozen=True)
class ReasonPolicy:
    scope: str
    blocks_new_exposure: bool
    allows_reduction: bool
    cause_is_valid: Callable[[Any], bool]
    age: AgePolicy
    facts_schema_version: int = FACTS_SCHEMA_VERSION


def _position_drift_cause_is_valid(value: Any) -> bool:
    try:
        PositionDriftCause.from_mapping(value)
    except ValueError:
        return False
    return True


def _order_outcome_unknown_cause_is_valid(value: Any) -> bool:
    # An unknown broker outcome is never reduction-authorizing.  Its strict
    # decoder lives with the atomic fold that opens and closes the episode.
    return isinstance(value, dict)


def _exit_not_flat_cause_is_valid(value: Any) -> bool:
    try:
        ExitNotFlatCause.from_mapping(value)
    except ValueError:
        return False
    return True


def _exit_stuck_cause_is_valid(value: Any) -> bool:
    try:
        ExitStuckCause.from_mapping(value)
    except ValueError:
        return False
    return True


def _execution_coverage_conflict_cause_is_valid(value: Any) -> bool:
    try:
        ExecutionCoverageConflictCause.from_mapping(value)
    except ValueError:
        return False
    return True


_REASON_POLICIES: dict[str, ReasonPolicy] = {
    POSITION_DRIFT_REASON_CODE: ReasonPolicy(
        scope="ACCOUNT_CLERK",
        blocks_new_exposure=True,
        allows_reduction=True,
        cause_is_valid=_position_drift_cause_is_valid,
        age=CauseCleared(),
    ),
    BROKER_SNAPSHOT_STALE_REASON_CODE: ReasonPolicy(
        scope="ACCOUNT_CLERK",
        blocks_new_exposure=True,
        allows_reduction=False,
        cause_is_valid=broker_snapshot_stale_cause_is_valid,
        age=CauseCleared(),
    ),
    RECONCILIATION_INCOMPLETE_REASON_CODE: ReasonPolicy(
        scope="ACCOUNT_CLERK",
        blocks_new_exposure=True,
        allows_reduction=False,
        cause_is_valid=reconciliation_incomplete_cause_is_valid,
        age=CauseCleared(),
    ),
    ORDER_OUTCOME_UNKNOWN_REASON_CODE: ReasonPolicy(
        scope="CUSTODY_SUBJECT",
        blocks_new_exposure=True,
        allows_reduction=False,
        cause_is_valid=_order_outcome_unknown_cause_is_valid,
        # Byte-identical replacement of the former UNCERTAIN_SUBMIT_GRACE_MS
        # = 30_000 module constant in order_evidence.py; summary_code names
        # the receipt order_evidence.SUBMIT_ABSENCE_SUMMARY_CODE already
        # writes.
        age=VoidAfter(grace_ms=30_000, summary_code="ORDER_SUBMIT_FAILED_ABSENT"),
    ),
    EXIT_NOT_FLAT_REASON_CODE: ReasonPolicy(
        scope="CUSTODY_SUBJECT",
        blocks_new_exposure=True,
        allows_reduction=True,
        cause_is_valid=_exit_not_flat_cause_is_valid,
        # Byte-identical replacement of the former
        # EXIT_NOT_FLAT_REDRIVE_AFTER_MS = 120_000 / EXIT_NOT_FLAT_MAX_REDRIVES
        # = 3 module constants in exit_watchdog.py.
        age=RedriveThenEscalate(after_ms=120_000, max_count=3, escalate_to=EXIT_STUCK_REASON_CODE),
    ),
    EXIT_STUCK_REASON_CODE: ReasonPolicy(
        scope="CUSTODY_SUBJECT",
        blocks_new_exposure=True,
        allows_reduction=True,
        cause_is_valid=_exit_stuck_cause_is_valid,
        # A durable escalation must not carry a clock: only an
        # attributed-flat proof or an operator may end it. VoidAfter here
        # would silently discard the episode the escalation exists to
        # preserve (ADR 0048 Decision 1).
        age=CauseCleared(),
    ),
    EXECUTION_COVERAGE_CONFLICT_REASON_CODE: ReasonPolicy(
        scope="CUSTODY_SUBJECT",
        blocks_new_exposure=True,
        allows_reduction=False,
        cause_is_valid=_execution_coverage_conflict_cause_is_valid,
        age=CauseCleared(),
    ),
}


def reason_age_policy(reason_code: str) -> AgePolicy:
    """The declared age policy for one registered reason code.

    The single place an episode's life is specified (ADR 0048 Decision 1).
    Raises ``KeyError`` for an unregistered code: every caller passes a
    known reason-code constant, so a miss here is a programming error, not
    a runtime condition to absorb.
    """
    return _REASON_POLICIES[reason_code].age


def _effective_identity(
    *, reason_code: str, strategy_instance_id: str | None
) -> tuple[ReasonPolicy | None, str, str | None]:
    policy = _REASON_POLICIES.get(reason_code)
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
) -> bool:
    """Raise or refresh one typed episode; unknown causes fail closed account-wide."""
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
    return outcome != "unchanged"


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


def _exit_not_flat_allows_action(
    *,
    facts: UncertaintyRaisedFacts,
    intent: ReductionIntent | None,
) -> bool:
    if intent is None or intent.quantity <= 0 or intent.side.upper() not in {"BUY", "SELL"}:
        return False
    try:
        cause = ExitNotFlatCause.from_mapping(facts.cause_facts)
    except ValueError:
        return False
    return cause.symbol == intent.symbol.upper()


def _exit_stuck_allows_action(
    *,
    facts: UncertaintyRaisedFacts,
    intent: ReductionIntent | None,
) -> bool:
    if intent is None or intent.quantity <= 0 or intent.side.upper() not in {"BUY", "SELL"}:
        return False
    try:
        cause = ExitStuckCause.from_mapping(facts.cause_facts)
    except ValueError:
        return False
    return cause.symbol == intent.symbol.upper()


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

    holds = repo.active_holds_for_admission(
        strategy_instance_id=strategy_instance_id,
        subject_id=subject_id,
    )
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
        policy = _REASON_POLICIES.get(reason_code)
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
                and (
                    (
                        reason_code == POSITION_DRIFT_REASON_CODE
                        and _position_drift_allows_action(
                            repo,
                            uncertainty=uncertainty,
                            facts=facts,
                            intent=reduction_intent,
                        )
                    )
                    or (
                        reason_code == EXIT_NOT_FLAT_REASON_CODE
                        and _exit_not_flat_allows_action(
                            facts=facts,
                            intent=reduction_intent,
                        )
                        and strategy_instance_id is not None
                        and reduction_intent is not None
                        and _moves_toward_zero_without_crossing(
                            repo.position(
                                strategy_instance_id,
                                reduction_intent.symbol.upper(),
                            ),
                            reduction_intent.signed_delta,
                        )
                    )
                    or (
                        reason_code == EXIT_STUCK_REASON_CODE
                        and _exit_stuck_allows_action(
                            facts=facts,
                            intent=reduction_intent,
                        )
                        and strategy_instance_id is not None
                        and reduction_intent is not None
                        and _moves_toward_zero_without_crossing(
                            repo.position(
                                strategy_instance_id,
                                reduction_intent.symbol.upper(),
                            ),
                            reduction_intent.signed_delta,
                        )
                    )
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
# retry-on-next-clock in the runner's error taxonomy").
TRANSIENT_ADMISSION_REASON_CODES: frozenset[str] = frozenset(
    {
        BROKER_SNAPSHOT_STALE_REASON_CODE,
        RECONCILIATION_INCOMPLETE_REASON_CODE,
        "RECONCILIATION_IN_PROGRESS",
    }
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
    "raise_uncertainty",
    "require_admission",
    "require_capability",
    "require_manual_admission",
    "require_manual_reduction",
    "resolve_exit_not_flat_uncertainty",
    "resolve_exit_stuck_uncertainty",
    "resolve_incomplete_reconciliation_uncertainty",
    "resolve_reconciliation_uncertainty",
]
