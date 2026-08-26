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

from typing import TYPE_CHECKING

from app.broker.alpaca.clerk.sqlite.execution_coverage import FILL_QTY_EPSILON
from app.broker.alpaca.clerk.sqlite.facts import (
    EnterAcceptedFacts,
    OrderFillObservedFacts,
    OrderSubmitFailedFacts,
    OrderSubmitUncertainFacts,
)
from app.broker.alpaca.clerk.sqlite.folds import (
    order_observation_advances,
)
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.manual_order_completion import manual_order_has_exact_terminal_coverage
from app.broker.alpaca.clerk.sqlite.models import OrderResource, TransitionInput
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import VoidAfter, reason_age_policy
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import ORDER_OUTCOME_UNKNOWN_REASON_CODE
from app.broker.contract.errors import BrokerError
from app.broker.contract.models import BrokerOrder

if TYPE_CHECKING:
    from app.broker.contract.ports import BrokerTradePort


def submit_absence_grace_ms() -> int:
    """The declared ``VoidAfter`` grace window for ``ORDER_OUTCOME_UNKNOWN``.

    Sourced from the reason-policy registry (``uncertainty._REASON_POLICIES``,
    ADR 0048 Decision 1) rather than a locally hardcoded constant, so the
    R4 submit-absence age rule has exactly one declaration.
    """
    policy = reason_age_policy(ORDER_OUTCOME_UNKNOWN_REASON_CODE)
    assert isinstance(policy, VoidAfter)
    return policy.grace_ms


__all__ = [
    "entry_never_accepted_durably",
    "entry_order_symbol",
    "fold_entry_never_accepted",
    "fold_failed",
    "fold_order_acknowledgement",
    "fold_order_evidence",
    "fold_order_submission_acknowledgement",
    "fold_submit_absence_void",
    "fold_uncertain",
    "order_never_reached_broker",
    "resolve_order_submission",
    "submit_absence_grace_ms",
]


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
    append_stale_ack: bool = True,
) -> None:
    """Fold a REST/reconciliation aggregate order observation.

    This is the **cumulative-recovery-only** path.  A ``BrokerOrder`` carries
    an order-level ``filled_quantity`` / VWAP, not a durable execution
    identity, so this helper remains appropriate for a bounded REST recovery
    or reconciliation snapshot but must never be used for a ``trade_updates``
    websocket frame.  The latter routes the immutable execution slice through
    ``EXECUTION_SLICE_FILLED`` and calls :func:`fold_order_acknowledgement`
    separately.

    The cumulative fill (if the snapshot reports progress; idempotent and
    namespace-attributed via ``_fold_order_fill_observed``) is deliberately
    folded before the acknowledgement. These are two independent
    ``append_transition`` calls; if the process dies between them, recovery
    re-polls the exact identity and its delta/acknowledgement monotonicity
    makes the replay safe.

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
    recorded_fill_qty, _ = repo.effective_fill_totals_for_order(order_ref)
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

    fold_order_acknowledgement(
        repo,
        effect_operation_id=effect_operation_id,
        order=order,
        append_stale_ack=append_stale_ack,
    )


def fold_order_acknowledgement(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    order: BrokerOrder,
    append_stale_ack: bool = True,
) -> None:
    """Fold only monotonic aggregate order acknowledgement evidence.

    The broker order snapshot records its lifecycle state but not a distinct
    execution slice.  Keeping this acknowledgement separate means a websocket
    frame can record its ``execution_id``/quantity/price exactly without
    accidentally re-deriving another fill from ``order.filled_quantity``.
    ``fold_order_evidence`` above remains the explicitly labelled recovery
    path for sources that have no execution identity.
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

    current_order = repo.order(order_ref)
    ack_advances = order_observation_advances(
        current_state=(current_order.broker_state if current_order is not None else None),
        prior_source_time=(latest_ack["source_event_at_ms"] if latest_ack else None),
        incoming_state=order.status,
        incoming_source_time=order.updated_at_ms,
    )
    if (ack_changed or effect.state == "unknown") and (
        append_stale_ack or ack_advances
    ):
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
    if (
        manual_order_has_exact_terminal_coverage(
            repo,
            effect_operation_id=effect_operation_id,
            order_ref=order_ref,
            broker_state=order.status,
        )
        and not repo.has_order_transition(
            order_ref=order_ref,
            transition_kind="MANUAL_ORDER_FILLED",
        )
    ):
        repo.append_transition(
            TransitionInput(
                strategy_instance_id=effect.strategy_instance_id,
                run_id=effect.run_id,
                command_id=effect.command_id,
                effect_operation_id=effect_operation_id,
                order_ref=order_ref,
                broker_order_id=order.order_id,
                broker_state=order.status,
                transition_kind="MANUAL_ORDER_FILLED",
                custody_owner="ACCOUNT_CLERK",
                execution_authority="ACCOUNT_CLERK",
                operation_state="succeeded",
                source_event_at_ms=order.updated_at_ms,
                clerk_observed_at_ms=repo.clock(),
                summary_code="MANUAL_ORDER_FILLED",
                facts_json=canonicalize({}),
            )
        )


def fold_order_submission_acknowledgement(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    order: BrokerOrder,
) -> None:
    """Acknowledge an immediate broker-submit response without fill math.

    Submit responses can embed an aggregate order snapshot.  They are not an
    execution feed, so they must not bypass the websocket's execution-ID
    capture or create an ``ORDER_FILL_OBSERVED`` row.  A filled quantity with
    no aggregate price remains anomalous and is still made uncertain rather
    than being acknowledged as if the accounting evidence were complete.
    Exact REST recovery and reconciliation use :func:`fold_order_evidence`.
    """
    order_ref = order.client_order_id
    assert order_ref is not None
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
    fold_order_acknowledgement(
        repo,
        effect_operation_id=effect_operation_id,
        order=order,
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


SUBMIT_ABSENCE_SUMMARY_CODE = "ORDER_SUBMIT_FAILED_ABSENT"
"""Summary code of the definitive-absence void — the durable proof that one
exact order identity never reached the broker."""


def order_never_reached_broker(repo: ClerkSqliteRepository, order: OrderResource) -> bool:
    """Is a definitively-absent exact lookup terminal proof for this order?

    The one predicate every caller uses to read an absent lookup as an answer
    rather than a silence. Absence is terminal only when nothing else in the
    ledger says the broker ever knew this order:

    - no broker identity, no acknowledgement, and no durable fill. Any of the
      three contradicts absence, and a contradiction is something to
      investigate, never something to confirm. The fill check is not
      redundant with the identity check: the trade-update sink records an
      execution slice *before* it acknowledges the order, so a filled order
      can still be carrying a null broker order id.
    - the R4 submit-absence grace window has closed, anchored exactly where
      :func:`resolve_order_submission` anchors it, so a submit still inside
      the broker's own visibility window is never mistaken for one that never
      happened.
    """
    if order.broker_order_id is not None:
        return False
    if repo.has_order_transition(order_ref=order.order_ref, transition_kind="ORDER_SUBMIT_ACKED"):
        return False
    if repo.fills_for_order(order.order_ref):
        return False
    return repo.clock() - _uncertain_since_ms(repo, order.order_ref) >= submit_absence_grace_ms()


def entry_never_accepted_durably(repo: ClerkSqliteRepository, order: OrderResource) -> bool:
    """Is this entry order provably never-accepted from durable evidence alone?

    Two durable proofs, neither needing a broker call: a prior pass already
    recorded the never-accepted confirmation, or the owning ENTER effect
    reached its terminal receipt on the definitive-absence void. Both require
    the order to carry no broker identity — a failure mode that did produce a
    broker order id is a different, still-cancellable animal.
    """
    if order.broker_order_id is not None:
        return False
    transitions = repo.transitions_for_order(order.order_ref)
    if any(transition["transition_kind"] == "ENTRY_NEVER_ACCEPTED" for transition in transitions):
        return True
    owner = repo.effect_operation(order.effect_operation_id)
    if owner is None or owner.state != "failed" or owner.terminal_receipt_id is None:
        return False
    return any(
        transition["summary_code"] == SUBMIT_ABSENCE_SUMMARY_CODE for transition in transitions
    )


def fold_submit_absence_void(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    order_ref: str,
) -> None:
    """Void one exact order identity the broker definitively never had.

    The single producer of the definitive-absence terminal receipt, so the
    submit resolver and an EXIT that reaches the proof first write the same
    evidence rather than two dialects of it.
    """
    fold_failed(
        repo,
        effect_operation_id=effect_operation_id,
        order_ref=order_ref,
        summary_code=SUBMIT_ABSENCE_SUMMARY_CODE,
        reason="The order did not reach the broker.",
        why="Alpaca has no order for this client_order_id (definitively absent).",
    )


def fold_entry_never_accepted(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    order_ref: str,
    why: str,
) -> None:
    """Record proof that an enumerated ENTRY order never reached the broker.

    Deliberately not :func:`fold_failed`: this transition belongs to the EXIT
    that enumerated the dead entry, and that EXIT is not the thing that
    failed. It records the entry's proven end state and releases the exact
    identity from any open unknown-outcome episode, leaving the EXIT free to
    continue to its own outcome.
    """
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    facts = OrderSubmitFailedFacts(
        reason="The entry order never reached the broker.",
        why=why,
    )
    repo.append_transition(
        TransitionInput(
            strategy_instance_id=effect.strategy_instance_id,
            run_id=effect.run_id,
            command_id=effect.command_id,
            effect_operation_id=effect_operation_id,
            order_ref=order_ref,
            transition_kind="ENTRY_NEVER_ACCEPTED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="in_progress",
            clerk_observed_at_ms=repo.clock(),
            summary_code="ENTRY_NEVER_ACCEPTED",
            facts_json=facts.to_facts_json(),
        )
    )


async def resolve_order_submission(
    repo: ClerkSqliteRepository,
    *,
    order_ref: str,
    trade: BrokerTradePort,
) -> None:
    """Recover any captured order by its exact client identity.

    This belongs with shared order evidence rather than an ENTER-only module:
    manual custody, ENTER, and a future reducing order all use the same R4/R7
    claim, exact lookup, grace-window, and monotonic fold discipline.
    """
    order_row = repo.order(order_ref)
    assert order_row is not None
    effect = repo.effect_operation(order_row.effect_operation_id)
    assert effect is not None

    if effect.state in ("succeeded", "failed", "rejected"):
        return

    # ClaimedBrokerIO itself uses ``fold_uncertain`` above. Importing it only
    # after this module is fully initialized preserves that shared dependency
    # direction without an import-time cycle.
    from app.broker.alpaca.clerk.sqlite.claimed_broker_io import ClaimedBrokerIO

    claim = repo.claim_before_broker_contact(effect.effect_operation_id)
    broker = ClaimedBrokerIO(
        repo=repo,
        effect_operation_id=effect.effect_operation_id,
        claim_token=claim.token,
        trade=trade,
    )
    uncertain_since_ms = _uncertain_since_ms(repo, order_ref)
    try:
        try:
            order = await broker.lookup(order_ref)
        except BrokerError as exc:
            fold_uncertain(
                repo,
                effect_operation_id=effect.effect_operation_id,
                order_ref=order_ref,
                why=f"Exact broker lookup failed before outcome proof: {exc}",
            )
            return

        if order is not None and order.client_order_id != order_ref:
            fold_uncertain(
                repo,
                effect_operation_id=effect.effect_operation_id,
                order_ref=order_ref,
                why=(
                    f"broker lookup returned client_order_id={order.client_order_id!r}, "
                    f"expected {order_ref!r}"
                ),
            )
            return

        if order is None:
            if order_row.broker_order_id is not None:
                return
            grace_active = (repo.clock() - uncertain_since_ms) < submit_absence_grace_ms()
            if grace_active:
                return
            fold_submit_absence_void(
                repo,
                effect_operation_id=effect.effect_operation_id,
                order_ref=order_ref,
            )
        else:
            fold_order_evidence(repo, effect_operation_id=effect.effect_operation_id, order=order)
    finally:
        repo.release_operation_claim(
            effect_operation_id=effect.effect_operation_id,
            token=claim.token,
        )


def _uncertain_since_ms(repo: ClerkSqliteRepository, order_ref: str) -> int:
    """Find the durable accept boundary for the exact order's outcome."""
    transitions = repo.transitions_for_order(order_ref)
    return transitions[0]["recorded_at_ms"]
