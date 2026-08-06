"""Shared broker order-evidence folding (#1377, promoted for #1379).

The R4 ("never fabricate a terminal outcome") and R7 ("order identity
resolution by exact ``client_order_id``") discipline applies identically
whether the order being resolved is an ENTER's own submit or an EXIT's
cancel-the-entry / submit-the-reducing-order steps — both domain modules
route through this one gate rather than each keeping its own copy
(CLAUDE.md guiding-philosophy #5: single source of truth). Nothing here
decides *when* to call the broker or what to do next; it only records what
an observed (or absent, or lost) ``BrokerOrder`` snapshot means.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.sqlite.facts import (
    EnterAcceptedFacts,
    OrderFillObservedFacts,
    OrderSubmitFailedFacts,
    OrderSubmitUncertainFacts,
)
from app.broker.alpaca.clerk.sqlite.folds import FILL_QTY_EPSILON
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerOrder

__all__ = ["entry_order_symbol", "fold_failed", "fold_order_evidence", "fold_uncertain"]


def entry_order_symbol(repo: ClerkSqliteRepository, order_ref: str) -> str:
    """Read an entry's symbol from its immutable acceptance fact."""
    for transition in repo.transitions_for_order(order_ref):
        if transition["transition_kind"] == "ENTER_ACCEPTED":
            return EnterAcceptedFacts.from_facts_json(transition["facts_json"]).leg["symbol"]
    raise AssertionError(f"no ENTER_ACCEPTED transition found for {order_ref!r}")


def fold_order_evidence(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    order: BrokerOrder,
) -> None:
    """The one gate for "what does an observed ``BrokerOrder`` mean" — a
    happy-path submit ack, a resolution's found-order case, and an EXIT's
    cancel-outcome poll all route through this, not three copies. Folds the
    fill (if the snapshot reports progress; idempotent and
    namespace-attributed via ``_fold_order_fill_observed``) *before* the
    acknowledgement (idempotent on ``orders.broker_state`` via §3c's
    no-regression rule) — deliberately in that order, not the reverse.
    These are two independent ``append_transition`` calls, not one atomic
    commit; if the process dies between them, the recovery sweep re-polls
    the exact identity and folds the cumulative snapshot again. The fill
    delta check and acknowledgement monotonicity make that replay safe.

    A snapshot reporting ``filled_quantity > 0`` with ``filled_avg_price is
    None`` is an anomalous broker response, not a normal transient state —
    there is no way to record a fill without a price (``fills.price`` is
    ``NOT NULL``). Folding the ack anyway could temporarily hide the missing
    accounting evidence, so this withholds *both* the fill and the ack and
    folds ``unknown`` instead,
    with the anomaly recorded in ``why`` — a later resolution will retry
    with (hopefully) complete evidence.
    """
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    order_ref = order.client_order_id
    assert order_ref is not None
    transitions = repo.transitions_for_order(order_ref)
    latest_ack = next(
        (
            transition
            for transition in reversed(transitions)
            if transition["transition_kind"] == "ORDER_SUBMIT_ACKED"
        ),
        None,
    )
    ack_changed = latest_ack is None or (
        latest_ack["broker_order_id"] != order.order_id
        or latest_ack["broker_state"] != order.status
        or latest_ack["source_event_at_ms"] != order.updated_at_ms
    )
    recorded_fill_qty = sum(fill["qty"] for fill in repo.fills_for_order(order_ref))
    fill_changed = order.filled_quantity - recorded_fill_qty >= FILL_QTY_EPSILON

    if order.filled_quantity > 0 and order.filled_avg_price is None:
        fold_uncertain(
            repo,
            effect_operation_id=effect_operation_id,
            order_ref=order_ref,
            why=(
                f"broker reported filled_quantity={order.filled_quantity} with no "
                "filled_avg_price; withholding the ack to avoid losing the fill"
            ),
        )
        return

    if fill_changed:
        fill_facts = OrderFillObservedFacts(
            symbol=order.symbol,
            side=order.side.upper(),
            cumulative_filled_quantity=order.filled_quantity,
            avg_price=order.filled_avg_price,
            is_correction=False,
        )
        repo.append_transition(
            TransitionInput(
                strategy_instance_id=effect.strategy_instance_id,
                run_id=effect.run_id,
                command_id=effect.command_id,
                effect_operation_id=effect_operation_id,
                order_ref=order_ref,
                transition_kind="ORDER_FILL_OBSERVED",
                custody_owner="ACCOUNT_CLERK",
                execution_authority="ACCOUNT_CLERK",
                operation_state="in_progress",
                source_event_at_ms=order.updated_at_ms,
                clerk_observed_at_ms=repo.clock(),
                summary_code="ORDER_FILL_OBSERVED",
                facts_json=fill_facts.to_facts_json(),
            )
        )

    if ack_changed:
        repo.append_transition(
            TransitionInput(
                strategy_instance_id=effect.strategy_instance_id,
                run_id=effect.run_id,
                command_id=effect.command_id,
                effect_operation_id=effect_operation_id,
                order_ref=order_ref,
                broker_order_id=order.order_id,
                broker_state=order.status,
                transition_kind="ORDER_SUBMIT_ACKED",
                custody_owner="ACCOUNT_CLERK",
                execution_authority="ACCOUNT_CLERK",
                operation_state="in_progress",
                source_event_at_ms=order.updated_at_ms,
                clerk_observed_at_ms=repo.clock(),
                summary_code="ORDER_SUBMIT_ACKED",
                facts_json=canonicalize({}),
            )
        )


def fold_uncertain(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    order_ref: str,
    why: str,
    transition_kind: str = "ORDER_SUBMIT_UNCERTAIN",
) -> None:
    """A lost/timed-out broker response — never a terminal outcome (R4).

    ``transition_kind`` defaults to the submit-flavored kind ENTER uses;
    EXIT passes ``"ORDER_CANCEL_UNCERTAIN"`` for a lost cancel response —
    same fold semantics (effect/command move to ``unknown``, no receipt),
    a distinct name for audit-trail honesty about which broker call was
    actually attempted.
    """
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    facts = OrderSubmitUncertainFacts(why=why)
    repo.append_transition(
        TransitionInput(
            strategy_instance_id=effect.strategy_instance_id,
            run_id=effect.run_id,
            command_id=effect.command_id,
            effect_operation_id=effect_operation_id,
            order_ref=order_ref,
            transition_kind=transition_kind,
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="unknown",
            clerk_observed_at_ms=repo.clock(),
            summary_code=transition_kind,
            facts_json=facts.to_facts_json(),
        )
    )


def fold_failed(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    order_ref: str,
    summary_code: str,
    reason: str,
    why: str,
    transition_kind: str = "ORDER_SUBMIT_FAILED",
) -> None:
    """A definitive terminal failure (vendor 4xx/409/auth/rate-limit), or an
    absence proven past the R4 uncertainty grace window."""
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    facts = OrderSubmitFailedFacts(reason=reason, why=why)
    repo.append_transition(
        TransitionInput(
            strategy_instance_id=effect.strategy_instance_id,
            run_id=effect.run_id,
            command_id=effect.command_id,
            effect_operation_id=effect_operation_id,
            order_ref=order_ref,
            transition_kind=transition_kind,
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="failed",
            clerk_observed_at_ms=repo.clock(),
            summary_code=summary_code,
            facts_json=facts.to_facts_json(),
        )
    )
