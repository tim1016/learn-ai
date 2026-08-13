"""Exact-evidence completion rule for a one-leg SQLite manual ticket."""

from __future__ import annotations

from app.broker.alpaca.clerk.sqlite.execution_coverage import FILL_QTY_EPSILON
from app.broker.alpaca.clerk.sqlite.facts import ManualOrderAcceptedFacts
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerOrderLeg


def manual_order_has_exact_terminal_coverage(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    order_ref: str,
    broker_state: str,
) -> bool:
    """Whether exact evidence proves one manual tracer leg has fully filled."""
    if broker_state.lower() != "filled":
        return False
    effect = repo.effect_operation(effect_operation_id)
    if effect is None or effect.kind != "MANUAL_ORDER":
        return False
    acceptance = next(
        (
            transition
            for transition in reversed(repo.transitions_for_order(order_ref))
            if transition["transition_kind"] == "MANUAL_ORDER_ACCEPTED"
        ),
        None,
    )
    if acceptance is None:
        raise RuntimeError(f"manual effect {effect_operation_id!r} has no acceptance transition")
    facts = ManualOrderAcceptedFacts.from_facts_json(acceptance["facts_json"])
    expected_quantity = BrokerOrderLeg.model_validate(facts.leg).quantity
    effective_quantity, _ = repo.effective_fill_totals_for_order(order_ref)
    return abs(effective_quantity - expected_quantity) <= FILL_QTY_EPSILON


__all__ = ["manual_order_has_exact_terminal_coverage"]
