"""Exact-evidence terminal rules for a one-leg SQLite manual ticket."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from app.broker.alpaca.clerk.sqlite.execution_coverage import FILL_QTY_EPSILON
from app.broker.alpaca.clerk.sqlite.facts import ManualOrderAcceptedFacts
from app.broker.alpaca.clerk.sqlite.manual_order_replacement import MANUAL_ORDER_REPLACED_TRANSITION
from app.broker.alpaca.clerk.sqlite.models import OrderResource
from app.broker.alpaca.clerk.sqlite.order_projection import UNFILLED_TERMINAL_STATES
from app.broker.alpaca.clerk.sqlite.reads import NONTERMINAL_EFFECT_STATES
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
    head_quantity: float | None,
) -> bool:
    """Whether exact evidence proves one manual tracer leg has fully filled.

    Formula: ``abs(exact_effective_qty - governing_qty) <= FILL_QTY_EPSILON``.
    Reference: docs/references/clerk-invariants.md §2 — an absolute
    ``1e-9`` tolerance admits float64 aggregation residue without treating a
    material fractional-share remainder as complete.
    Canonical implementation: this predicate, reused by order evidence.
    Validated against: PythonDataService/tests/broker/alpaca/clerk/sqlite/
      test_manual_orders.py::test_manual_order_exact_coverage_tolerance.

    ``broker_state`` and ``head_quantity`` come from one observation of the
    leg's chain head -- only a head's observation reaches this predicate.
    The governing quantity is the accepted leg's until Alpaca replaced the
    order (#2656); from then on it is the head's own requested quantity,
    because the owner may have edited it and ``filled`` means the head filled
    *its* quantity. One figure, never either of two: a head filled at a
    raised quantity is not proven complete by exact fills that only reach
    the original one.
    """
    if broker_state.lower() != "filled":
        return False
    effect = repo.effect_operation(effect_operation_id)
    if effect is None or effect.kind != "MANUAL_ORDER":
        return False
    governing_quantity = (
        head_quantity
        if repo.has_order_transition(order_ref=order_ref, transition_kind=MANUAL_ORDER_REPLACED_TRANSITION)
        else _accepted_leg(repo, order_ref=order_ref).quantity
    )
    if governing_quantity is None:
        return False
    effective_quantity, _ = repo.effective_exact_fill_totals_for_order(order_ref)
    return abs(effective_quantity - governing_quantity) <= FILL_QTY_EPSILON


def _require_copy_for_every_unfilled_state(
    copy_states: frozenset[str], unfilled_states: frozenset[str]
) -> None:
    """Fail at import, never mid-fold, when the owner copy and the ending states drift apart.

    The terminal fold ends a manual order in any of
    ``UNFILLED_TERMINAL_STATES`` and writes this module's copy as its
    ``why``. A state with no copy would raise inside the acknowledgement
    fold, which stops the trade_updates sink and the account's whole
    reconciliation pass; checked here, a widened set stops the process from
    starting instead.
    """
    if copy_states != unfilled_states:
        raise RuntimeError(
            "manual-order ending copy must cover exactly UNFILLED_TERMINAL_STATES: "
            f"missing {sorted(unfilled_states - copy_states)}, extra {sorted(copy_states - unfilled_states)}"
        )


# broker state -> the owner's words, given the accepted leg and whether the
# owner's cancel went through the Clerk rather than Alpaca's own website.
_ENDING_COPY: Mapping[str, Callable[[BrokerOrderLeg, bool], str]] = {
    "canceled": lambda _leg, through_clerk: "Cancelled" if through_clerk else "Cancelled at Alpaca",
    "expired": lambda leg, _through_clerk: (
        "Expired at the close" if leg.time_in_force == TimeInForce.DAY else "Expired at Alpaca"
    ),
    "rejected": lambda _leg, _through_clerk: "Rejected by Alpaca",
}
_require_copy_for_every_unfilled_state(frozenset(_ENDING_COPY), UNFILLED_TERMINAL_STATES)


def _shares(quantity: float) -> str:
    """A share count in plain digits: no exponent, no float residue past ``FILL_QTY_EPSILON``."""
    return f"{quantity:.9f}".rstrip("0").rstrip(".")


def manual_order_ending_copy(repo: ClerkSqliteRepository, *, order: OrderResource) -> str:
    """How Alpaca ended a manual order it did not fill in full, in the owner's words (#2647).

    The one source of this copy: the terminal fold records it as its ``why``
    and the manual ticket shows it. ``order``'s broker state must be one of
    ``UNFILLED_TERMINAL_STATES``, which the copy covers exactly (checked at
    import), so this never raises for an order the fold may end. A DAY leg
    expires at the close (manual legs are regular-session only); a GTC leg
    Alpaca expires on its own schedule. A cancel the owner sent through the
    Clerk reads as a plain cancel, not as one made at Alpaca. Shares a
    partial fill kept are named, because they stay the owner's position.
    """
    leg = _accepted_leg(repo, order_ref=order.order_ref)
    through_clerk = repo.manual_order_cancellation(order_ref=order.order_ref) is not None
    ending = _ENDING_COPY[(order.broker_state or "").lower()](leg, through_clerk)
    filled_quantity, _ = repo.effective_fill_totals_for_order(order.order_ref)
    if filled_quantity < FILL_QTY_EPSILON:
        return f"{ending}."
    return f"{ending} with {_shares(filled_quantity)} of {_shares(leg.quantity)} shares filled."


def manual_order_broker_ending(repo: ClerkSqliteRepository, *, order_ref: str) -> str | None:
    """The ending the manual ticket shows: set once the Clerk has ended an unfilled remainder.

    ``None`` while the order works, once it filled, or while the fold still
    waits for a fill the Clerk has not recorded -- decided here from the
    owning effect, so no caller re-derives "has this leg ended".
    """
    order = repo.order(order_ref)
    if order is None or (order.broker_state or "").lower() not in UNFILLED_TERMINAL_STATES:
        return None
    effect = repo.effect_operation(order.effect_operation_id)
    if effect is None or effect.kind != "MANUAL_ORDER" or effect.state in NONTERMINAL_EFFECT_STATES:
        return None
    return manual_order_ending_copy(repo, order=order)


__all__ = [
    "manual_order_broker_ending",
    "manual_order_ending_copy",
    "manual_order_has_exact_terminal_coverage",
]
