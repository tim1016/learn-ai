"""Following a manual order Alpaca replaced to the chain's own ending (#2656).

The owner can edit a Clerk manual order's price or quantity on Alpaca's own
website. Alpaca ends the original order id as ``replaced`` and books a new
order under a new broker id; ``replaced_by`` on the original and ``replaces``
on the new order name the link. ``replaced`` is not an ending (#2647 stands:
the order lives on), so this module gives the manual leg one durable chain
head instead: the ``orders`` row's broker identity advances to the live
order, and every observation route — the ``trade_updates`` sink, the
reconciliation snapshot and a Clerk cancel alike — resolves the chain before
folding, so fills on any chain member credit the owner's manual position
exactly once and the leg ends only when the chain's last order ends.

Replacements of bot-owned (non-manual) orders are out of scope; every entry
point here declines them.
"""

from __future__ import annotations

import logging

from app.broker.alpaca.clerk.sqlite.facts import (
    ManualOrderReplacedFacts,
    validate_manual_order_replaced_facts,
)
from app.broker.alpaca.clerk.sqlite.models import OrderResource, TransitionInput
from app.broker.alpaca.clerk.sqlite.reads import NONTERMINAL_EFFECT_STATES
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerOrder

logger = logging.getLogger(__name__)

MANUAL_ORDER_REPLACED_TRANSITION = "MANUAL_ORDER_REPLACED"

#: The owner's words for a leg whose order Alpaca replaced, shown on the
#: manual ticket while the Clerk follows the new order. Backend copy only;
#: the frontend renders it verbatim.
REPLACED_AT_ALPACA_COPY = "Replaced at Alpaca; now following the new order."


def record_manual_order_replacement(
    repo: ClerkSqliteRepository,
    *,
    order: BrokerOrder,
    order_ref: str,
) -> bool:
    """Record the chain advance one broker observation proves, once per link (#2656).

    Two directions prove a link, both keyed on the row's current head so the
    chain can only ever grow forward:

    - the head itself is reported ``replaced`` and names ``replaced_by``;
    - a new order names the head in ``replaces``.

    Anything else — no linkage, a blank or missing replacement id, a
    non-manual or already-terminal leg — declines without appending, so one
    unreadable replacement record can never raise out of the fold that saw
    it (#2363 posture): the sweep keeps folding every other order and the
    leg stays honestly outstanding. The check and the append share the
    repository write lock, like the terminal fold beside it.
    """
    with repo._write_lock:
        row = repo.order(order_ref)
        if row is None or row.broker_order_id is None:
            return False
        effect = repo.effect_operation(row.effect_operation_id)
        if (
            effect is None
            or effect.kind != "MANUAL_ORDER"
            or effect.state not in NONTERMINAL_EFFECT_STATES
        ):
            return False
        head = row.broker_order_id
        replacement = ""
        replaces = order.replaces.strip() if order.replaces else ""
        if replaces and replaces == head:
            replacement = (order.order_id or "").strip()
        elif (
            (order.status or "").strip().lower() == "replaced"
            and (order.order_id or "").strip() == head
        ):
            replacement = (order.replaced_by or "").strip()
        if not replacement or replacement == head:
            return False
        facts = ManualOrderReplacedFacts(
            replaces=head,
            replaced_by=replacement,
            why=REPLACED_AT_ALPACA_COPY,
        )
        try:
            validate_manual_order_replaced_facts(facts)
        except ValueError:
            # Contained here, before the append: a link the fold would refuse
            # must decline quietly (#2363 posture), never raise out of the
            # acknowledgement fold that saw it.
            logger.warning(
                "A manual order replacement link the Clerk could not validate was set aside",
                extra={
                    "action": "manual_order_replacement_unreadable",
                    "order_ref": order_ref,
                    "replaces": head,
                    "replaced_by": replacement,
                },
            )
            return False
        repo.append_transition(
            TransitionInput(
                command_id=effect.command_id,
                effect_operation_id=effect.effect_operation_id,
                order_ref=order_ref,
                broker_order_id=replacement,
                transition_kind=MANUAL_ORDER_REPLACED_TRANSITION,
                custody_owner="ACCOUNT_CLERK",
                execution_authority="ACCOUNT_CLERK",
                operation_state="in_progress",
                clerk_observed_at_ms=repo.clock(),
                summary_code=MANUAL_ORDER_REPLACED_TRANSITION,
                facts_json=facts.to_facts_json(),
            )
        )
    logger.info(
        "Alpaca replaced a manual order; the Clerk now follows the new broker order",
        extra={
            "action": "manual_order_replaced",
            "order_ref": order_ref,
            "effect_operation_id": effect.effect_operation_id,
            "replaces": head,
            "replaced_by": replacement,
        },
    )
    return True


def resolve_manual_replacement(
    repo: ClerkSqliteRepository,
    *,
    order: BrokerOrder,
) -> OrderResource | None:
    """The manual leg an observation without our client id continues, if any.

    An order the broker replaced books under a new broker order id with no
    client order id of ours, so neither the sink nor the snapshot can resolve
    it by ``client_order_id``. This resolves it through the chain instead —
    the observation either *is* the leg's current head, or *replaces* the
    head and :func:`record_manual_order_replacement` will advance to it.
    Broker order ids are unique at the broker, so an exact id match cannot
    misattribute. Only a nonterminal manual leg can be continued; every
    other order stays foreign (#2656 scope).
    """
    for broker_order_id in (order.order_id.strip() or None, (order.replaces or "").strip() or None):
        if not broker_order_id:
            continue
        row = repo.order_for_broker_order_id(broker_order_id)
        if row is None:
            continue
        effect = repo.effect_operation(row.effect_operation_id)
        if (
            effect is None
            or effect.kind != "MANUAL_ORDER"
            or effect.state not in NONTERMINAL_EFFECT_STATES
        ):
            continue
        return row
    return None


def manual_order_replacement_note(
    repo: ClerkSqliteRepository, *, order_ref: str
) -> str | None:
    """The replacement sentence the manual ticket shows, while one is followed.

    ``None`` before any replacement and once the leg ended: the ticket names
    a replacement only while the Clerk is actually following one.
    """
    effect_id = None
    for transition in repo.transitions_for_order(order_ref):
        if transition["transition_kind"] == MANUAL_ORDER_REPLACED_TRANSITION:
            effect_id = transition["effect_operation_id"]
    if effect_id is None:
        return None
    effect = repo.effect_operation(effect_id)
    if effect is None or effect.kind != "MANUAL_ORDER" or effect.state not in NONTERMINAL_EFFECT_STATES:
        return None
    return REPLACED_AT_ALPACA_COPY


__all__ = [
    "MANUAL_ORDER_REPLACED_TRANSITION",
    "REPLACED_AT_ALPACA_COPY",
    "manual_order_replacement_note",
    "record_manual_order_replacement",
    "resolve_manual_replacement",
]
