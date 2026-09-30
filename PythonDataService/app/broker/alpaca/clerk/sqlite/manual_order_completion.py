"""Exact-evidence terminal rules for a one-leg SQLite manual ticket."""

from __future__ import annotations

from app.broker.alpaca.clerk.sqlite.execution_coverage import FILL_QTY_EPSILON
from app.broker.alpaca.clerk.sqlite.facts import ManualOrderAcceptedFacts
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerOrderLeg, TimeInForce


def _accepted_leg(repo: ClerkSqliteRepository, *, order_ref: str) -> BrokerOrderLeg:
    """The immutable leg the manual order was accepted with."""
    acceptance = repo.last_order_transition(order_ref=order_ref, transition_kind="MANUAL_ORDER_ACCEPTED")
    if acceptance is None:
        raise RuntimeError(f"manual order {order_ref!r} has no acceptance transition")
    facts = ManualOrderAcceptedFacts.from_facts_json(acceptance["facts_json"])
    return BrokerOrderLeg.model_validate(facts.leg)


def manual_order_has_exact_terminal_coverage(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    order_ref: str,
    broker_state: str,
) -> bool:
    """Whether exact evidence proves one manual tracer leg has fully filled.

    Formula: ``abs(exact_effective_qty - requested_qty) <= FILL_QTY_EPSILON``.
    Reference: docs/references/clerk-invariants.md §2 — an absolute
    ``1e-9`` tolerance admits float64 aggregation residue without treating a
    material fractional-share remainder as complete.
    Canonical implementation: this predicate, reused by order evidence.
    Validated against: PythonDataService/tests/broker/alpaca/clerk/sqlite/
      test_manual_orders.py::test_manual_order_exact_coverage_tolerance.
    """
    if broker_state.lower() != "filled":
        return False
    effect = repo.effect_operation(effect_operation_id)
    if effect is None or effect.kind != "MANUAL_ORDER":
        return False
    expected_quantity = _accepted_leg(repo, order_ref=order_ref).quantity
    effective_quantity, _ = repo.effective_exact_fill_totals_for_order(order_ref)
    return abs(effective_quantity - expected_quantity) <= FILL_QTY_EPSILON


def manual_order_broker_ending(repo: ClerkSqliteRepository, *, order_ref: str) -> str | None:
    """How Alpaca ended a manual order it did not fill in full, in the owner's words (#2647).

    The one source of this copy: the manual ticket shows it, and the terminal
    fold records it as its ``why``. ``None`` unless the order's broker state
    is ``canceled``, ``expired`` or ``rejected``. A DAY leg expires at the
    close (manual legs are regular-session only); a GTC leg Alpaca expires on
    its own schedule. Shares a partial fill kept are named, because they stay
    the owner's position.
    """
    order = repo.order(order_ref)
    broker_state = (order.broker_state or "").lower() if order is not None else ""
    if broker_state not in {"canceled", "expired", "rejected"}:
        return None
    leg = _accepted_leg(repo, order_ref=order_ref)
    ending = {
        "canceled": "Cancelled at Alpaca",
        "expired": "Expired at the close" if leg.time_in_force == TimeInForce.DAY else "Expired at Alpaca",
        "rejected": "Rejected by Alpaca",
    }[broker_state]
    filled_quantity, _ = repo.effective_fill_totals_for_order(order_ref)
    if filled_quantity < FILL_QTY_EPSILON:
        return f"{ending}."
    return f"{ending} with {filled_quantity:g} of {leg.quantity:g} shares filled."


__all__ = ["manual_order_broker_ending", "manual_order_has_exact_terminal_coverage"]
