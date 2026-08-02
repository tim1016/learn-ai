"""Pure Clerk custody projections over one reconciled journal cut."""

from __future__ import annotations

from dataclasses import dataclass

from app.broker.alpaca.clerk import derive
from app.broker.alpaca.clerk.effects import project_effect_receipts_for_instance
from app.broker.alpaca.clerk.exposure import (
    project_instance_exposure,
    project_instance_timeline,
)
from app.broker.alpaca.clerk.models import (
    ClerkCustodySnapshot,
    CustodyCountFact,
    CustodyExposureFact,
    EffectOperationState,
    InstanceCustodyProof,
    OrderJournalEntry,
    ReconciliationVerdict,
)
from app.broker.contract.models import BrokerOrder
from app.engine.live.order_identity import (
    build_bot_order_namespace,
    order_ref_namespace_matches,
)

_TERMINAL_EFFECT_STATES = frozenset(
    {
        EffectOperationState.NOOP,
        EffectOperationState.REJECTED,
        EffectOperationState.FLAT,
    }
)


@dataclass(frozen=True)
class CustodyReconciliationResult:
    """One atomic journal cut and the proof derived from that same cut."""

    verdict: ReconciliationVerdict
    proof: InstanceCustodyProof | None
    entries: tuple[OrderJournalEntry, ...]


def project_instance_custody_proof(
    strategy_instance_id: str | None,
    *,
    account_id: str,
    entries: list[OrderJournalEntry],
    working_orders: list[BrokerOrder],
    verdict: ReconciliationVerdict,
    observed_at_ms: int,
) -> InstanceCustodyProof | None:
    """Project one instance proof from a single reconciled journal cut."""
    if strategy_instance_id is None:
        return None
    namespace = build_bot_order_namespace(strategy_instance_id)
    allowed_namespaces = frozenset({namespace})
    exposure = {row.symbol: row.quantity for row in project_instance_exposure(entries, namespace=namespace)}
    unresolved = tuple(
        entry.order_ref
        for entry in derive.unresolved_intents(entries)
        if order_ref_namespace_matches(entry.order_ref, allowed_namespaces)
    )
    working_refs = tuple(
        order.client_order_id
        for order in working_orders
        if order.client_order_id and order_ref_namespace_matches(order.client_order_id, allowed_namespaces)
    )
    return InstanceCustodyProof(
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
        reconciliation_verdict=verdict,
        freeze=derive.account_freeze_state(entries),
        exposure=exposure,
        working_order_refs=working_refs,
        unresolved_intent_refs=unresolved,
        observed_at_ms=observed_at_ms,
    )


def _count_fact(count: int) -> CustodyCountFact:
    return CustodyCountFact(state="non_zero" if count else "zero", count=count)


def _known_custody_facts(
    proof: InstanceCustodyProof,
    entries: tuple[OrderJournalEntry, ...],
) -> tuple[
    CustodyExposureFact,
    CustodyCountFact,
    CustodyCountFact,
    CustodyCountFact,
    CustodyCountFact,
]:
    positions = {symbol: quantity for symbol, quantity in proof.exposure.items() if quantity != 0}
    timeline = project_instance_timeline(entries, proof.strategy_instance_id)
    latest_order_by_ref = {
        entry.order_ref: entry.order for entry in timeline if entry.order_ref and entry.order is not None
    }
    terminal_order_count = sum(
        order.status.lower() in derive.RECONCILIATION_TERMINAL_ORDER_STATUSES for order in latest_order_by_ref.values()
    )
    unresolved_effect_count = sum(
        receipt.state not in _TERMINAL_EFFECT_STATES
        for receipt in project_effect_receipts_for_instance(entries, proof.strategy_instance_id)
    )
    return (
        CustodyExposureFact(
            state="non_zero" if positions else "zero",
            positions=positions,
        ),
        _count_fact(len(proof.working_order_refs)),
        _count_fact(len(proof.unresolved_intent_refs)),
        _count_fact(terminal_order_count),
        _count_fact(unresolved_effect_count),
    )


def project_custody_snapshot(
    *,
    broker: str,
    clerk_generation: str,
    result: CustodyReconciliationResult,
    observed_at_ms: int,
) -> ClerkCustodySnapshot:
    """Project one fail-closed snapshot without rereading mutable evidence."""
    proof = result.proof
    if proof is None:
        raise ValueError("a custody snapshot requires instance reconciliation proof")

    if result.verdict == "clean":
        (
            exposure,
            working_orders,
            pending_orders,
            terminal_orders,
            unresolved_effects,
        ) = _known_custody_facts(proof, result.entries)
        reason_code = "CLERK_CUSTODY_PROVEN"
        next_step = None
    else:
        exposure = CustodyExposureFact(state="unknown")
        working_orders = CustodyCountFact(state="unknown")
        pending_orders = CustodyCountFact(state="unknown")
        terminal_orders = CustodyCountFact(state="unknown")
        unresolved_effects = CustodyCountFact(state="unknown")
        reason_code = "CLERK_CUSTODY_UNPROVABLE" if result.verdict == "stale" else "CLERK_CUSTODY_UNATTRIBUTABLE"
        next_step = proof.freeze.next_step or ("Reconcile the account before starting new exposure.")

    journal_sequence = len(result.entries)
    return ClerkCustodySnapshot(
        broker=broker,
        account_id=proof.account_id,
        strategy_instance_id=proof.strategy_instance_id,
        clerk_generation=clerk_generation,
        journal_sequence=journal_sequence,
        reconciliation_state=result.verdict,
        reconciliation_fresh=result.verdict != "stale",
        reconciled_at_ms=proof.observed_at_ms,
        exposure=exposure,
        working_orders=working_orders,
        pending_orders=pending_orders,
        terminal_orders=terminal_orders,
        unresolved_effects=unresolved_effects,
        hold=derive.hold_state(result.entries),
        freeze=proof.freeze,
        reason_code=reason_code,
        evidence_refs=(
            f"alpaca-clerk-journal:{proof.account_id}:{journal_sequence}",
            f"alpaca-reconciliation:{proof.observed_at_ms}",
        ),
        next_step=next_step,
        observed_at_ms=observed_at_ms,
    )
