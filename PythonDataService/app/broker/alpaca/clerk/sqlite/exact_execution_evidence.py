"""One exact-execution append flow shared by every evidence source (#2178).

Every producer of exact execution slices — the trade-updates websocket sink
(``trade_evidence.py``), the deterministic no-submit adapters'
authoritative aggregates (``order_evidence.py``) and the recovery of a
manual leg's or a bot order's executions from Alpaca's account activity
(``activity_executions.py``, #2686, #2787) — must build the same
``EXECUTION_SLICE_FILLED`` transition, the same typed
``EXECUTION_COVERAGE_CONFLICT`` uncertainty, and route through the same
``append_execution_slice_if_absent`` discipline: identity dedup,
auto-supersession of a matching cumulative row, fail-closed quarantine for
everything else.  This module is that one implementation; each producer
brings its own ``evidence_source``, operator copy, and proof reference.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.broker.alpaca.clerk.sqlite.facts import (
    EXACT_EXECUTION_EVIDENCE_SOURCES,
    ExecutionSliceFilledFacts,
    UncertaintyRaisedFacts,
)
from app.broker.alpaca.clerk.sqlite.models import (
    EffectOperationResource,
    TransitionInput,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
    ExecutionCoverageConflictCause,
)
from app.broker.contract.models import BrokerOrderEvent


@dataclass(frozen=True)
class ExactExecutionConflictCopy:
    """The operator copy one evidence source shows on a coverage conflict."""

    headline: str
    explanation: str
    operator_impact: str
    next_step: str


WEBSOCKET_EXACT_CONFLICT_COPY = ExactExecutionConflictCopy(
    headline="Exact execution conflicts with prior immutable evidence",
    explanation=(
        "The received websocket execution cannot be safely merged with "
        "the order's prior execution evidence."
    ),
    operator_impact=(
        "New exposure is blocked until this order's execution coverage is reconciled."
    ),
    next_step="Reconcile the broker order and its execution slices before resuming.",
)

SIMULATED_EXACT_CONFLICT_COPY = ExactExecutionConflictCopy(
    headline="Simulated execution conflicts with prior immutable evidence",
    explanation=(
        "The no-submit adapter's exact execution cannot be safely merged "
        "with the order's prior execution evidence."
    ),
    operator_impact=(
        "New exposure is blocked until this order's execution coverage is reconciled."
    ),
    next_step="Reconcile the simulated order and its execution evidence before resuming.",
)

ACTIVITY_EXACT_CONFLICT_COPY = ExactExecutionConflictCopy(
    headline="Recovered execution conflicts with prior immutable evidence",
    explanation=(
        "An execution read from Alpaca's account activity cannot be safely merged "
        "with the order's prior execution evidence."
    ),
    operator_impact=(
        "New exposure is blocked until this order's execution coverage is reconciled."
    ),
    next_step="Reconcile the broker order and its execution slices before resuming.",
)


def append_exact_execution_slice(
    repo: ClerkSqliteRepository,
    *,
    event: BrokerOrderEvent,
    symbol: str,
    side: str,
    broker_order_id: str,
    order_ref: str,
    owner: EffectOperationResource,
    evidence_source: str,
    conflict_copy: ExactExecutionConflictCopy,
    proof_reference: str | None = None,
    extra_conflict_evidence_refs: Sequence[str] = (),
) -> str:
    """Append one exact execution slice under the shared custody discipline.

    ``symbol``, ``side`` and ``broker_order_id`` name the broker order that
    executed: the observed order for a stream frame, the activity's own order
    for a recovered execution, which may be an earlier member of a manual
    leg's replacement chain (#2656).

    Returns ``append_execution_slice_if_absent``'s outcome
    (``"appended"``/``"duplicate"``/``"coverage_superseded"``/...), so a
    producer may surface whether its exact evidence replaced legacy coverage.
    The event must carry its execution identity, quantity, and price — an
    exact source that cannot name its slice is a contract violation, not a
    degraded observation, and fails here rather than folding an aggregate.
    """
    if event.execution_id is None:
        raise ValueError("An exact execution event must carry its execution_id.")
    if event.quantity is None or event.price is None:
        raise ValueError(
            "An exact execution event must include its quantity and price "
            f"when execution_id is set ({event.execution_id!r})."
        )
    if evidence_source not in EXACT_EXECUTION_EVIDENCE_SOURCES:
        raise ValueError(f"invalid exact-execution evidence source {evidence_source!r}")
    facts = ExecutionSliceFilledFacts(
        execution_id=event.execution_id,
        symbol=symbol,
        side=side.upper(),
        slice_qty=event.quantity,
        slice_price=event.price,
        fee=None,
        fee_fidelity="not_reported",
        evidence_source=evidence_source,
        source_event_at_ms=event.occurred_at_ms,
    )
    proof = event.execution_id if proof_reference is None else proof_reference

    def _execution_transition() -> TransitionInput:
        return TransitionInput(
            strategy_instance_id=owner.strategy_instance_id,
            run_id=owner.run_id,
            command_id=owner.command_id,
            effect_operation_id=owner.effect_operation_id,
            order_ref=order_ref,
            broker_order_id=broker_order_id,
            transition_kind="EXECUTION_SLICE_FILLED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="in_progress",
            proof_reference=proof,
            source_event_at_ms=event.occurred_at_ms,
            clerk_observed_at_ms=repo.clock(),
            summary_code="EXECUTION_SLICE_FILLED",
            facts_json=facts.to_facts_json(),
        )

    def _coverage_conflict_transition() -> TransitionInput:
        return exact_execution_coverage_conflict(
            repo,
            event=event,
            broker_order_id=broker_order_id,
            order_ref=order_ref,
            owner=owner,
            conflict_copy=conflict_copy,
            proof_reference=proof,
            extra_evidence_refs=extra_conflict_evidence_refs,
        )

    return repo.append_execution_slice_if_absent(
        execution_id=event.execution_id,
        order_ref=order_ref,
        build_transition=_execution_transition,
        build_coverage_conflict=_coverage_conflict_transition,
    )


def exact_execution_coverage_conflict(
    repo: ClerkSqliteRepository,
    *,
    event: BrokerOrderEvent,
    broker_order_id: str,
    order_ref: str,
    owner: EffectOperationResource,
    conflict_copy: ExactExecutionConflictCopy,
    proof_reference: str,
    extra_evidence_refs: Sequence[str] = (),
) -> TransitionInput:
    """The typed ``EXECUTION_COVERAGE_CONFLICT`` an exact execution that cannot be merged raises.

    The one shape every exact producer raises, whether the append flow
    refuses the slice or a producer refuses it first (the account-activity
    recovery's quantity guard, #2686). ``event`` must name its execution.
    """
    if event.execution_id is None:
        raise ValueError("A coverage conflict must name the exact execution it refuses.")
    conflict_facts = UncertaintyRaisedFacts(
        severity="error",
        blocks_new_exposure=True,
        allows_reduction=False,
        reason_code=EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
        headline=conflict_copy.headline,
        explanation=conflict_copy.explanation,
        operator_impact=conflict_copy.operator_impact,
        next_step=conflict_copy.next_step,
        evidence_refs=[*extra_evidence_refs, event.execution_id],
        cause_facts=ExecutionCoverageConflictCause(
            order_ref=order_ref,
            execution_id=event.execution_id,
        ).to_mapping(),
    )
    return TransitionInput(
        strategy_instance_id=owner.strategy_instance_id,
        run_id=owner.run_id,
        command_id=owner.command_id,
        effect_operation_id=owner.effect_operation_id,
        order_ref=order_ref,
        broker_order_id=broker_order_id,
        transition_kind="UNCERTAINTY_RAISED",
        custody_owner="ACCOUNT_CLERK",
        execution_authority="ACCOUNT_CLERK",
        operation_state="succeeded",
        proof_reference=proof_reference,
        source_event_at_ms=event.occurred_at_ms,
        clerk_observed_at_ms=repo.clock(),
        summary_code=EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
        facts_json=conflict_facts.to_facts_json(),
    )
