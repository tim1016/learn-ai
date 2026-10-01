"""Durable containment of broker orders outside registered bot custody.

This module is intentionally the only path that turns a foreign Alpaca order
into an ``external_orders`` row.  It does not create an ``orders`` row, a
``fills`` row, or a position update, so external account activity can never
be misattributed to a bot's economics.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, replace
from typing import Literal

from app.broker.alpaca.clerk.sqlite import reads
from app.broker.alpaca.clerk.sqlite.facts import (
    ExternalOrderAcknowledgedFacts,
    ExternalOrderObservedFacts,
    UncertaintyRaisedFacts,
)
from app.broker.alpaca.clerk.sqlite.models import ExternalOrderResource, TransitionInput
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.repository_external_order_api import (
    ExternalOrderNotFoundError,
)
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    TransitionProvenance,
    raise_uncertainty,
    resolve_operator_acknowledged_uncertainty,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    UNFOLDABLE_BROKER_ORDER_REASON_CODE,
    UnfoldableBrokerOrder,
    UnfoldableBrokerOrderCause,
)
from app.broker.contract.models import BrokerOrder

logger = logging.getLogger(__name__)


class ExternalOrderObservationError(ValueError):
    """The broker supplied insufficient evidence for a truthful observation."""


def observe_external_order(
    repo: ClerkSqliteRepository,
    *,
    order: BrokerOrder,
    proof_reference: str | None = None,
) -> ExternalOrderResource:
    """Record one foreign order and atomically fence new bot exposure.

    The resulting transition has no bot identity.  Its fold owns the
    external-order row plus the specific ``UNEXPLAINED_ORDER`` hold cause.
    Repeated identical broker snapshots return the existing row without
    growing either the hash chain or the hold evidence.
    """
    expected = _observation_from_broker_order(order)
    facts = ExternalOrderObservedFacts(
        external_order_id=expected.external_order_id,
        broker_order_id=expected.broker_order_id,
        client_order_id=expected.client_order_id,
        symbol=expected.symbol,
        side=expected.side,
        qty=expected.qty,
        order_type=expected.order_type,
        limit_price=expected.limit_price,
        stop_price=expected.stop_price,
        filled_avg_price=expected.filled_avg_price,
        observed_at_ms=expected.observed_at_ms,
        evidence_refs=list(expected.evidence_refs),
        broker_state=expected.broker_state,
        filled_quantity=expected.filled_quantity,
    )
    return repo.append_external_order_observation_if_changed(
        expected=expected,
        build_transition=lambda: TransitionInput(
            transition_kind="EXTERNAL_ORDER_OBSERVED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            broker_order_id=expected.broker_order_id,
            proof_reference=proof_reference or expected.broker_order_id,
            source_event_at_ms=expected.observed_at_ms,
            clerk_observed_at_ms=repo.clock(),
            summary_code="EXTERNAL_ORDER_OBSERVED",
            facts_json=facts.to_facts_json(),
        ),
    )


def observe_or_record_unfoldable(
    repo: ClerkSqliteRepository,
    *,
    order: BrokerOrder,
    proof_reference: str | None = None,
) -> Literal["observed", "unfoldable"]:
    """The one entrance every path uses to take custody of a foreign order (#2363).

    Both the trade-update sink and the reconciliation verdict call this, so a
    broker order no ``external_orders`` row can state truthfully (a multi-leg
    parent with a null ``side``, or a row naming ``unreadable_fields``, #2648)
    is contained identically on both: recorded by name under
    ``UNFOLDABLE_BROKER_ORDER`` and never allowed to raise out of the
    caller's loop. Only :class:`ExternalOrderObservationError` is
    contained -- it is raised before anything is appended. Storage and
    transport errors still propagate.
    """
    try:
        observe_external_order(repo, order=order, proof_reference=proof_reference)
    except ExternalOrderObservationError as exc:
        record_unfoldable_broker_order(
            repo, order=order, reason=str(exc), proof_reference=proof_reference
        )
        return "unfoldable"
    return "observed"


# Every order the broker supplied no id for collapses into one entry, so a
# stream of anonymous poison cannot grow the episode without bound. Because
# the sentinel names no particular order, a review of it is never remembered
# as a review of whatever anonymous order arrives next: each later anonymous
# observation fences entries again (#2363 review).
UNIDENTIFIED_BROKER_ORDER_ID = "unidentified-broker-order"


def _broker_state(order: BrokerOrder) -> str:
    """The order's broker lifecycle state and cumulative fill, as one token.

    A change in it is *activity* -- a fill, a cancel, an expiry -- which is
    new broker evidence an earlier review never saw. A replay or a sweep
    re-seeing the same state is not. A fill count the broker sent unreadably
    (#2648) is never stated as a zero: it reads ``filled=unreadable``, and
    the broker's own last-update time stands in for it, so a fill that
    leaves the status unchanged is still activity.
    """
    status = order.status.strip().lower()
    if "filled_qty" in order.unreadable_fields:
        return f"{status} filled=unreadable updated_at_ms={order.updated_at_ms!r}"
    return f"{status} filled={order.filled_quantity!r}"


def record_unfoldable_broker_order(
    repo: ClerkSqliteRepository,
    *,
    order: BrokerOrder,
    reason: str,
    proof_reference: str | None = None,
) -> str:
    """Durably name one foreign order :func:`observe_external_order` refused (#2363).

    The order joins the one account-scoped ``UNFOLDABLE_BROKER_ORDER``
    episode, keyed by broker order id: new exposure is fenced account-wide
    until an operator acknowledges it, reductions stay admitted. An order
    already in the episode in the same broker state appends nothing, so a
    replay or a sweep re-seeing a resting order is free. A change of state
    re-states the entry with a new ``last_activity_at_ms`` (its first
    observation is kept). An acknowledged order is not re-fenced while its
    broker state is the one the operator reviewed -- mirroring an
    acknowledged external-order row -- but new activity on it is new
    evidence and fences again. Returns ``"acknowledged"`` for a reviewed,
    unchanged order, else :func:`raise_uncertainty`'s outcome.
    """
    entry = UnfoldableBrokerOrder(
        broker_order_id=order.order_id.strip() or UNIDENTIFIED_BROKER_ORDER_ID,
        client_order_id=order.client_order_id or None,
        reason=reason,
        observed_at_ms=order.observed_at_ms,
        broker_state=_broker_state(order),
        last_activity_at_ms=order.observed_at_ms,
    )
    with repo.unfoldable_order_write_serialized():
        review = (
            None
            if entry.broker_order_id == UNIDENTIFIED_BROKER_ORDER_ID
            else repo.unfoldable_broker_order_acknowledgements().get(entry.broker_order_id)
        )
        if review is not None and review.broker_state == entry.broker_state:
            outcome = "acknowledged"
        else:
            known = _active_unfoldable_orders(repo)
            prior = known.get(entry.broker_order_id)
            if prior is None:
                known[entry.broker_order_id] = entry
            elif prior.broker_state != entry.broker_state:
                known[entry.broker_order_id] = replace(
                    entry,
                    observed_at_ms=prior.observed_at_ms,
                    last_activity_at_ms=max(entry.observed_at_ms, prior.observed_at_ms),
                )
            outcome = _state_unfoldable_episode(
                repo,
                orders=known,
                provenance=TransitionProvenance(
                    broker_order_id=entry.broker_order_id,
                    proof_reference=proof_reference or entry.broker_order_id,
                    source_event_at_ms=order.observed_at_ms,
                ),
            )
    # Loud when the fence changed; a replay or sweep re-seeing the same order
    # (every 15 s while it rests) is still logged, at INFO, never dropped.
    logger.log(
        logging.ERROR if outcome in ("raised", "refreshed") else logging.INFO,
        "a broker order the Clerk cannot record was set aside",
        extra={
            "action": "unfoldable_broker_order_recorded",
            "broker_order_id": entry.broker_order_id,
            "client_order_id": entry.client_order_id,
            "symbol": order.symbol,
            "broker_state": entry.broker_state,
            "reason": reason,
            "proof_reference": proof_reference,
            "uncertainty_outcome": outcome,
        },
    )
    return outcome


@dataclass(frozen=True)
class UnfoldableBrokerOrderAcknowledgement:
    """The durable operator review of one unfoldable broker order.

    Field names match :class:`ExternalOrderResource`'s acknowledgement so the
    one external-order acknowledgement route answers both.
    """

    external_order_id: str
    acknowledged_at_ms: int
    ack_operator: str


def unfoldable_broker_order_is_unreviewed(
    repo: ClerkSqliteRepository, *, broker_order_id: str
) -> bool:
    """Whether the active entry fence still names this broker order."""
    return broker_order_id in _active_unfoldable_orders(repo)


def acknowledge_unfoldable_broker_order(
    repo: ClerkSqliteRepository,
    *,
    broker_order_id: str,
    operator: str,
) -> UnfoldableBrokerOrderAcknowledgement:
    """Release exactly one unfoldable order from the entry fence (#2363).

    The review is recorded as the episode's resolution (hash-chained, naming
    the order and the operator); any other orders still unreviewed are
    re-stated as a fresh episode, so the fence stays up until each is
    reviewed. An order the active fence no longer names returns its latest
    review. Raises :class:`ExternalOrderNotFoundError` for an order no
    episode ever named.
    """
    if not broker_order_id:
        raise ValueError("broker_order_id must be non-empty")
    if not operator or len(operator) > 64:
        raise ValueError("operator must be between 1 and 64 characters")
    with repo.unfoldable_order_write_serialized():
        remaining = _active_unfoldable_orders(repo)
        if remaining.pop(broker_order_id, None) is None:
            prior = repo.unfoldable_broker_order_acknowledgements().get(broker_order_id)
            if prior is None:
                raise ExternalOrderNotFoundError(
                    f"unfoldable broker order {broker_order_id!r} was not found"
                )
            return _acknowledgement(broker_order_id, prior)
        resolve_operator_acknowledged_uncertainty(
            repo,
            reason_code=UNFOLDABLE_BROKER_ORDER_REASON_CODE,
            summary_code=reads.UNFOLDABLE_BROKER_ORDER_ACKNOWLEDGED_SUMMARY_CODE,
            evidence_refs=(
                f"{reads.UNFOLDABLE_ACK_ORDER_REF_PREFIX}{broker_order_id}",
                f"{reads.UNFOLDABLE_ACK_OPERATOR_REF_PREFIX}{operator}",
            ),
        )
        if remaining:
            _state_unfoldable_episode(repo, orders=remaining, provenance=TransitionProvenance())
        review = repo.unfoldable_broker_order_acknowledgements()[broker_order_id]
    return _acknowledgement(broker_order_id, review)


def _acknowledgement(
    broker_order_id: str, review: reads.UnfoldableBrokerOrderReview
) -> UnfoldableBrokerOrderAcknowledgement:
    return UnfoldableBrokerOrderAcknowledgement(
        external_order_id=broker_order_id,
        acknowledged_at_ms=review.acknowledged_at_ms,
        ack_operator=review.operator,
    )


def unfoldable_broker_orders_active_since(repo: ClerkSqliteRepository, *, since_ms: int) -> int:
    """Distinct unfoldable orders first seen or active at or after ``since_ms``, released or not."""
    return repo.unfoldable_broker_orders_active_since(
        reason_code=UNFOLDABLE_BROKER_ORDER_REASON_CODE, since_ms=since_ms
    )


def _active_unfoldable_orders(repo: ClerkSqliteRepository) -> dict[str, UnfoldableBrokerOrder]:
    active = repo.active_uncertainty(
        scope="ACCOUNT_CLERK",
        reason_code=UNFOLDABLE_BROKER_ORDER_REASON_CODE,
        strategy_instance_id=None,
    )
    if active is None:
        return {}
    cause = UnfoldableBrokerOrderCause.from_mapping(
        UncertaintyRaisedFacts.from_facts_json(active["facts_json"]).cause_facts
    )
    return {order.broker_order_id: order for order in cause.orders}


def _state_unfoldable_episode(
    repo: ClerkSqliteRepository,
    *,
    orders: dict[str, UnfoldableBrokerOrder],
    provenance: TransitionProvenance,
) -> str:
    cause = UnfoldableBrokerOrderCause(
        orders=tuple(orders[broker_order_id] for broker_order_id in sorted(orders))
    )
    named = "; ".join(f"{order.broker_order_id}: {order.reason}" for order in cause.orders)
    return raise_uncertainty(
        repo,
        strategy_instance_id=None,
        reason_code=UNFOLDABLE_BROKER_ORDER_REASON_CODE,
        headline="A broker order could not be recorded",
        explanation=(
            f"The Clerk could not record {len(cause.orders)} broker order(s) ({named}). "
            "Each was set aside so every other order keeps folding, but the Clerk "
            "cannot prove new exposure would be attributable while one is unreviewed."
        ),
        operator_impact=(
            "The account is on hold for new entries. Exits, stuck-exit re-drives and "
            "cancels still run."
        ),
        next_step=(
            "Inspect each named order at Alpaca, then acknowledge it to release the "
            "entry hold."
        ),
        # Broker order ids only: each is exactly what the acknowledgement
        # route takes. Client order ids stay in the cause and explanation.
        evidence_refs=tuple(order.broker_order_id for order in cause.orders),
        cause_facts=cause.to_mapping(),
        severity="error",
        provenance=provenance,
    )


def acknowledge_external_order(
    repo: ClerkSqliteRepository,
    *,
    external_order_id: str,
    operator: str,
) -> ExternalOrderResource:
    """Mark exactly one external order reviewed and scope its hold release."""
    if not external_order_id:
        raise ValueError("external_order_id must be non-empty")
    if not operator or len(operator) > 64:
        raise ValueError("operator must be between 1 and 64 characters")
    facts = ExternalOrderAcknowledgedFacts(
        external_order_id=external_order_id,
        ack_operator=operator,
    )
    return repo.acknowledge_external_order_if_unreviewed(
        external_order_id=external_order_id,
        build_transition=lambda existing: TransitionInput(
            transition_kind="EXTERNAL_ORDER_ACKNOWLEDGED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            broker_order_id=existing.broker_order_id,
            proof_reference=existing.external_order_id,
            source_event_at_ms=existing.observed_at_ms,
            clerk_observed_at_ms=repo.clock(),
            summary_code="EXTERNAL_ORDER_ACKNOWLEDGED",
            facts_json=facts.to_facts_json(),
        ),
    )


def _observation_from_broker_order(order: BrokerOrder) -> ExternalOrderResource:
    # Ahead of every other check, so the recorded reason names the real
    # cause: the adapter reads a value it could not parse as absent and names
    # it rather than refusing the whole orders answer (#2648), and an absent
    # quantity or price is not a truthful observation.
    if order.unreadable_fields:
        raise ExternalOrderObservationError(
            f"broker sent values this app could not read: {', '.join(order.unreadable_fields)}"
        )
    broker_order_id = order.order_id.strip()
    symbol = order.symbol.strip().upper()
    side = order.side.upper()
    qty = order.quantity if order.quantity is not None else order.filled_quantity
    order_type = order.order_type.strip().lower()
    if not broker_order_id:
        raise ExternalOrderObservationError("broker order id must be non-empty")
    if not symbol:
        raise ExternalOrderObservationError("external order symbol must be non-empty")
    if side not in {"BUY", "SELL"}:
        raise ExternalOrderObservationError("external order side must be buy or sell")
    if not math.isfinite(qty) or qty < 0:
        raise ExternalOrderObservationError("external order quantity must be finite and non-negative")
    if not math.isfinite(order.filled_quantity) or order.filled_quantity < 0:
        raise ExternalOrderObservationError("external filled quantity must be finite and non-negative")
    if not order_type:
        raise ExternalOrderObservationError("external order type must be non-empty")
    # The adapter reads missing text as "" rather than refusing the whole
    # orders answer (#2643); an order without them is contained here.
    if not order.status.strip():
        raise ExternalOrderObservationError("external order status must be non-empty")
    if not order.time_in_force.strip():
        raise ExternalOrderObservationError("external order time in force must be non-empty")
    for price_name, price in (
        ("limit", order.limit_price),
        ("stop", order.stop_price),
        ("filled average", order.filled_avg_price),
    ):
        if price is not None and not math.isfinite(price):
            raise ExternalOrderObservationError(
                f"external order {price_name} price must be finite when supplied"
            )
    return ExternalOrderResource(
        # Broker order identity is stable and unique in the SQLite fold;
        # reusing it makes the row directly auditable against broker evidence.
        external_order_id=broker_order_id,
        broker_order_id=broker_order_id,
        client_order_id=(
            order.client_order_id
            if order.client_order_id is not None
            else f"missing-client-order-id:{broker_order_id}"
        ),
        symbol=symbol,
        side=side,
        qty=qty,
        order_type=order_type,
        limit_price=order.limit_price,
        stop_price=order.stop_price,
        filled_avg_price=order.filled_avg_price,
        observed_at_ms=order.observed_at_ms,
        acknowledged_at_ms=None,
        ack_operator=None,
        evidence_refs=(broker_order_id,),
        broker_state=order.status.strip().lower(),
        filled_quantity=order.filled_quantity,
    )


__all__ = [
    "UNIDENTIFIED_BROKER_ORDER_ID",
    "ExternalOrderObservationError",
    "UnfoldableBrokerOrderAcknowledgement",
    "acknowledge_external_order",
    "acknowledge_unfoldable_broker_order",
    "observe_external_order",
    "observe_or_record_unfoldable",
    "record_unfoldable_broker_order",
    "unfoldable_broker_order_is_unreviewed",
    "unfoldable_broker_orders_active_since",
]
