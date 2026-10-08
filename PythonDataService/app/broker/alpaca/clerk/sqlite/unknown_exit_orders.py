"""Which closing orders an operator may settle: the durable read (#2854).

An EXIT's reducing order whose outcome is unknown is one the Clerk keeps
custody of and cannot settle by itself: its submit answer was lost, a send
made again keeps being refused, or the broker answered under another client
order id. The operator's ``settle_unknown_exit_order`` action is offered for
such an order, and re-checked for it as it commits, from this one read.

It reads only what the ledger holds. Every row is one bounded, indexed read
(#1942): an unknown order gains an ``ORDER_SUBMIT_UNCERTAIN`` on every pass,
and a page is rebuilt every few seconds.

A leaf module: the projection reader and the repository both read through
it, and neither imports the other.
"""

from __future__ import annotations

import sqlite3
from typing import Literal, NamedTuple

from app.broker.alpaca.clerk.sqlite import reads
from app.broker.alpaca.clerk.sqlite.facts import (
    FACTS_SCHEMA_VERSION,
    ExitReducingOrderCreatedFacts,
    OrderSubmitUncertainFacts,
    UncertaintyRaisedFacts,
)
from app.broker.alpaca.clerk.sqlite.projection_models import ProjectedUnknownExitOrder
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    ORDER_OUTCOME_UNKNOWN_REASON_CODE,
    OrderOutcomeUnknownCause,
)


class _Row(NamedTuple):
    sequence: int
    recorded_at_ms: int
    facts_json: str


def _one(
    conn: sqlite3.Connection,
    order_ref: str,
    transition_kind: str,
    direction: Literal["ASC", "DESC"],
    *,
    after_sequence: int | None = None,
) -> _Row | None:
    """One transition of one kind off the order's index: its first, or its last."""
    row = conn.execute(
        "SELECT sequence, recorded_at_ms, facts_json FROM custody_transitions "
        "WHERE order_ref = ? AND transition_kind = ? AND sequence > ? "
        f"ORDER BY sequence {direction} LIMIT 1",
        (order_ref, transition_kind, 0 if after_sequence is None else after_sequence),
    ).fetchone()
    return None if row is None else _Row(row["sequence"], row["recorded_at_ms"], row["facts_json"])


def send_wait_from_ms(*, sent_at_ms: int, answer_recorded_at_ms: int | None) -> int:
    """Where the wait after an order's latest send is counted from (#2845).

    From the send's ``ORDER_SUBMIT_REQUESTED``, or from the first
    ``ORDER_SUBMIT_UNCERTAIN`` after it -- that send's lost answer, or the
    first pass to find the order absent -- whichever was recorded with the
    later time. ``recorded_at_ms`` is wall time, so the later of the two is
    not always the later by sequence: a host clock that steps backwards
    between them must not shorten the wait.

    The one rule for the Clerk's own re-send (``exit_resolution``) and for
    when an operator may settle the order as absent.
    """
    return sent_at_ms if answer_recorded_at_ms is None else max(sent_at_ms, answer_recorded_at_ms)


def _unknown_identities(episode: dict) -> frozenset[tuple[str, str]]:
    """The ``(effect, order)`` identities the open unknown-outcome episode names.

    None for an episode this build cannot read: it names no order the Clerk
    can prove, so nothing is offered for it. The episode itself stays open
    and is reported where it is projected.
    """
    if episode["facts_schema_version"] != FACTS_SCHEMA_VERSION:
        return frozenset()
    try:
        cause = OrderOutcomeUnknownCause.from_mapping(
            UncertaintyRaisedFacts.from_facts_json(episode["facts_json"]).cause_facts
        )
    except (TypeError, ValueError, KeyError):
        return frozenset()
    return frozenset((identity.effect_operation_id, identity.order_ref) for identity in cause.identities)


def exit_captures_several_contracts(conn: sqlite3.Connection, effect_operation_id: str) -> bool:
    """An operator's one-contract proof cannot settle an EXIT that captured multiple contracts."""
    rows = conn.execute(
        "SELECT DISTINCT COALESCE(bound.symbol, leg.symbol) AS symbol FROM operation_order_links link "
        "JOIN orders entry ON entry.order_ref = link.order_ref AND entry.role = 'ENTRY' "
        "LEFT JOIN order_contracts bound ON bound.order_ref = entry.order_ref "
        "LEFT JOIN option_order_legs leg ON leg.parent_order_ref = entry.order_ref "
        "WHERE link.effect_operation_id = ? AND COALESCE(bound.symbol, leg.symbol) IS NOT NULL LIMIT 2",
        (effect_operation_id,),
    ).fetchall()
    return len(rows) > 1


def read_unknown_exit_orders(
    conn: sqlite3.Connection, strategy_instance_id: str
) -> tuple[ProjectedUnknownExitOrder, ...]:
    """One bot's closing orders an operator may settle, in order-ref order.

    Each is the REDUCING order of an EXIT in state ``unknown`` that the bot's
    open ``ORDER_OUTCOME_UNKNOWN`` episode names, sent at least once, with no
    broker order id, no acknowledgement and no recorded fill -- what
    ``order_evidence.order_never_reached_broker`` asks of an order before it
    reads absence as an answer. An order the broker has acknowledged or
    filled is the Clerk's own to fold, and an entry is never here. Multi-leg
    closes retain automatic exact lookups; this single-contract operator
    attestation is not offered for their parents (ADR 0076 decision 28).
    """
    episode = reads.active_uncertainty(
        conn,
        scope="CUSTODY_SUBJECT",
        reason_code=ORDER_OUTCOME_UNKNOWN_REASON_CODE,
        strategy_instance_id=strategy_instance_id,
    )
    if episode is None:
        return ()
    identities = _unknown_identities(episode)
    candidates = conn.execute(
        "SELECT o.order_ref, o.effect_operation_id FROM orders o "
        "JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id "
        "WHERE e.kind = 'EXIT' AND e.state = 'unknown' AND e.strategy_instance_id = ? "
        "AND o.role = 'REDUCING' AND o.broker_order_id IS NULL AND NOT EXISTS ("
        "SELECT 1 FROM option_order_legs leg WHERE leg.parent_order_ref = o.order_ref) ORDER BY o.order_ref",
        (strategy_instance_id,),
    ).fetchall()
    orders: list[ProjectedUnknownExitOrder] = []
    for candidate in candidates:
        order_ref, effect_operation_id = candidate["order_ref"], candidate["effect_operation_id"]
        if (effect_operation_id, order_ref) not in identities or exit_captures_several_contracts(conn, effect_operation_id):
            continue
        created = _one(conn, order_ref, "EXIT_REDUCING_ORDER_CREATED", "ASC")
        first_send = _one(conn, order_ref, "ORDER_SUBMIT_REQUESTED", "ASC")
        latest_send = _one(conn, order_ref, "ORDER_SUBMIT_REQUESTED", "DESC")
        if created is None or first_send is None or latest_send is None:
            continue
        if _one(conn, order_ref, "ORDER_SUBMIT_ACKED", "ASC") is not None:
            continue
        if reads.fills_for_order(conn, order_ref):
            continue
        # The send's answer is the first uncertainty recorded after it; every
        # later one is a pass's own record that the order is still absent.
        answer = _one(
            conn, order_ref, "ORDER_SUBMIT_UNCERTAIN", "ASC", after_sequence=latest_send.sequence
        )
        leg = ExitReducingOrderCreatedFacts.from_facts_json(created.facts_json)
        orders.append(
            ProjectedUnknownExitOrder(
                effect_operation_id=effect_operation_id,
                order_ref=order_ref,
                symbol=leg.symbol,
                side=leg.side,
                quantity=leg.quantity,
                created_at_ms=created.recorded_at_ms,
                first_sent_at_ms=first_send.recorded_at_ms,
                latest_send_sequence=latest_send.sequence,
                lookup_wait_from_ms=send_wait_from_ms(
                    sent_at_ms=latest_send.recorded_at_ms,
                    answer_recorded_at_ms=None if answer is None else answer.recorded_at_ms,
                ),
                answered_as_client_order_id=(
                    None
                    if answer is None
                    else OrderSubmitUncertainFacts.from_facts_json(
                        answer.facts_json
                    ).answered_as_client_order_id
                ),
                uncertainty_id=episode["uncertainty_id"],
            )
        )
    return tuple(orders)


__all__ = ["exit_captures_several_contracts", "read_unknown_exit_orders", "send_wait_from_ms"]
