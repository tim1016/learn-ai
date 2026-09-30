"""Following a manual order Alpaca replaced to the chain's own ending (#2656).

The owner can edit a Clerk manual order's price or quantity on Alpaca's own
website. Alpaca then books a new order under a new broker id; ``replaced_by``
on the original and ``replaces`` on the new order name the link. ``replaced``
is not an ending (#2647 stands: the order lives on), so the manual leg keeps
one durable chain head: its ``orders`` row's broker identity, advanced by a
``MANUAL_ORDER_REPLACED`` link only on proof that the new order took over.

A replace can fail at Alpaca: the new order is rejected and the original
keeps working, or has already filled. So ``replaces`` alone proves nothing.
The head advances only when

- the head itself reports ``replaced`` and names a followable ``replaced_by``, or
- the new order has fills -- it can only fill once it took the original's place.

:func:`resolve_captured_order` is the one resolver every observation route
calls (the ``trade_updates`` sink, the reconciliation snapshot, the sweep's
exact lookup and a Clerk cancel). It answers the captured leg an observation
belongs to and its standing in the chain; only the head's observation may
state the leg's lifecycle. A former member, or a new order whose takeover is
unproven, is credit-only: its fills fold to the leg, and it never moves the
head or ends the leg.

Replacements of bot-owned (non-manual) orders are out of scope; a bot order
is always its own head.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from app.broker.alpaca.clerk.sqlite.execution_coverage import FILL_QTY_EPSILON
from app.broker.alpaca.clerk.sqlite.external_orders import record_unfoldable_broker_order
from app.broker.alpaca.clerk.sqlite.facts import (
    ManualOrderReplacedFacts,
    followable_broker_order_id,
)
from app.broker.alpaca.clerk.sqlite.models import (
    EffectOperationResource,
    OrderResource,
    TransitionInput,
)
from app.broker.alpaca.clerk.sqlite.reads import NONTERMINAL_EFFECT_STATES
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerOrder

logger = logging.getLogger(__name__)

MANUAL_ORDER_REPLACED_TRANSITION = "MANUAL_ORDER_REPLACED"

#: The owner's words for a leg whose order Alpaca replaced, shown on the
#: manual ticket while the Clerk follows the new order. Backend copy only;
#: the frontend renders it verbatim.
REPLACED_AT_ALPACA_COPY = "Replaced at Alpaca; now following the new order."

#: Why a readable ``replaced`` answer is contained on #2363's entry fence.
#: An *unreadable* ``replaced_by`` never gets here: the adapter marks it in
#: ``unreadable_fields`` and #2679's gate withholds the whole answer.
UNNAMED_REPLACEMENT_REASON = (
    "Alpaca reported this manual order replaced without naming, in replaced_by, "
    "the order that replaced it."
)
CYCLIC_REPLACEMENT_REASON = (
    "Alpaca reported this manual order replaced by an order already in its "
    "replacement chain."
)

ChainStanding = Literal["head", "former", "unproven"]


@dataclass(frozen=True)
class CapturedOrder:
    """The captured leg one broker observation belongs to, and its standing.

    ``head``: the observation is the leg's live broker identity (a bot order
    always is); it states the leg's lifecycle. ``former``: an earlier member
    of a manual leg's replacement chain. ``unproven``: a new order naming a
    chain member in ``replaces`` with no proof it took over. Both of the last
    two are credit-only.
    """

    row: OrderResource
    standing: ChainStanding

    @property
    def order_ref(self) -> str:
        return self.row.order_ref

    @property
    def credit_only(self) -> bool:
        return self.standing != "head"


def live_manual_effect(
    repo: ClerkSqliteRepository, row: OrderResource
) -> EffectOperationResource | None:
    """The row's ``MANUAL_ORDER`` effect while it is still working, else ``None``."""
    effect = repo.effect_operation(row.effect_operation_id)
    if effect is None or effect.kind != "MANUAL_ORDER" or effect.state not in NONTERMINAL_EFFECT_STATES:
        return None
    return effect


def resolve_captured_order(
    repo: ClerkSqliteRepository, order: BrokerOrder
) -> CapturedOrder | None:
    """The captured leg and chain standing of one broker observation (#2656).

    Our own client order id resolves first; a miss -- no client id, or the
    one Alpaca generates for a replacement -- falls back to the manual
    replacement chains: the observation is a chain member, or names one in
    ``replaces``. ``None`` means no captured order owns it: it is foreign.

    The one proof the observation itself carries is recorded before the
    standing is answered (idempotent, under the repository write lock like
    the terminal fold), so the standing is always the post-observation
    truth: a head reporting ``replaced`` answers ``former``, a new order
    with fills answers ``head``. A readable ``replaced`` naming no
    replacement, or one already in the chain, is contained on its own
    (#2363's entry fence) and never raises out of the caller's loop.

    A row the adapter could not fully read (``unreadable_fields``) proves
    nothing -- its absent values read as zero fills or no link -- so it never
    moves the chain, whatever its readable fields say. Its standing is still
    answered: every route then withholds it through #2679's one gate
    (``withhold_unnamed_order``), which names the unreadable fields.
    """
    with repo._write_lock:
        captured = _resolve(repo, order)
        if captured is None:
            return None
        effect = live_manual_effect(repo, captured.row)
        if effect is None or order.unreadable_fields:
            return captured
        return _apply_proof(repo, captured=captured, effect=effect, order=order)


def _resolve(repo: ClerkSqliteRepository, order: BrokerOrder) -> CapturedOrder | None:
    """Read-only resolution against the durable chains (no proof applied)."""
    observed_id = order.order_id.strip()
    row = repo.order(order.client_order_id) if order.client_order_id else None
    if row is not None:
        # Under our client id Alpaca only ever answers for the leg's original
        # order: once a manual leg's head moved past it, that answer is the
        # chain's history, never its head. A bot order is always its own head.
        former = (
            row.broker_order_id is not None
            and observed_id != row.broker_order_id
            and repo.manual_chain_order_ref(row.broker_order_id) == row.order_ref
        )
        return CapturedOrder(row=row, standing="former" if former else "head")
    member_ref = repo.manual_chain_order_ref(observed_id) if observed_id else None
    if member_ref is not None:
        row = repo.order(member_ref)
        assert row is not None
        return CapturedOrder(row=row, standing="head" if row.broker_order_id == observed_id else "former")
    replaces = (order.replaces or "").strip()
    replaced_ref = repo.manual_chain_order_ref(replaces) if replaces else None
    if replaced_ref is not None:
        row = repo.order(replaced_ref)
        assert row is not None
        return CapturedOrder(row=row, standing="unproven")
    return None


def _apply_proof(
    repo: ClerkSqliteRepository,
    *,
    captured: CapturedOrder,
    effect: EffectOperationResource,
    order: BrokerOrder,
) -> CapturedOrder:
    """Apply the one chain advance this readable observation proves; answer the new standing.

    ``captured`` itself when nothing is proven. A head reporting
    ``replaced`` whose ``replaced_by`` names no followable order, or one
    already in the chain (a cycle), stays the head -- honestly outstanding --
    and is contained on the entry fence with the cause.
    """
    head = captured.row.broker_order_id
    if head is None:
        return captured
    if captured.standing == "head" and order.status.strip().lower() == "replaced":
        successor = followable_broker_order_id(order.replaced_by)
        if successor is None:
            _contain_unfollowable_replacement(repo, captured=captured, order=order, reason=UNNAMED_REPLACEMENT_REASON)
            return captured
        if repo.manual_chain_order_ref(successor) is not None:
            _contain_unfollowable_replacement(repo, captured=captured, order=order, reason=CYCLIC_REPLACEMENT_REASON)
            return captured
        _append_link(repo, effect=effect, row=captured.row, successor=successor)
        return CapturedOrder(row=_reread(repo, captured.row), standing="former")
    if (
        captured.standing == "unproven"
        and (order.replaces or "").strip() == head
        and order.filled_quantity >= FILL_QTY_EPSILON
    ):
        successor = followable_broker_order_id(order.order_id)
        if successor is None:
            return captured
        _append_link(repo, effect=effect, row=captured.row, successor=successor)
        return CapturedOrder(row=_reread(repo, captured.row), standing="head")
    return captured


def _reread(repo: ClerkSqliteRepository, row: OrderResource) -> OrderResource:
    refreshed = repo.order(row.order_ref)
    assert refreshed is not None
    return refreshed


def _append_link(
    repo: ClerkSqliteRepository,
    *,
    effect: EffectOperationResource,
    row: OrderResource,
    successor: str,
) -> None:
    assert row.broker_order_id is not None
    facts = ManualOrderReplacedFacts(replaces=row.broker_order_id, replaced_by=successor)
    repo.append_transition(
        TransitionInput(
            command_id=effect.command_id,
            effect_operation_id=effect.effect_operation_id,
            order_ref=row.order_ref,
            broker_order_id=successor,
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
            "order_ref": row.order_ref,
            "effect_operation_id": effect.effect_operation_id,
            "replaces": row.broker_order_id,
            "replaced_by": successor,
        },
    )


def _contain_unfollowable_replacement(
    repo: ClerkSqliteRepository, *, captured: CapturedOrder, order: BrokerOrder, reason: str
) -> None:
    """Name a readable ``replaced`` answer the Clerk cannot follow, on its own entry fence.

    The leg stays on its head -- outstanding, with a Clerk cancel refused as
    terminal -- because nothing followable names the order that took its
    place. That is the #2656 stuck state, so it is never left silent: the
    order joins #2363's ``UNFOLDABLE_BROKER_ORDER`` episode under ``reason``,
    durable and owner-visible until acknowledged, idempotent while its broker
    state is unchanged. An unreadable link is not this path's: #2679's gate
    withholds that answer and names the field.
    """
    logger.warning(
        "A replaced manual order names no replacement the Clerk can follow",
        extra={
            "action": "manual_order_replacement_unfollowable",
            "order_ref": captured.order_ref,
            "broker_order_id": order.order_id,
            "replaced_by": order.replaced_by,
            "reason": reason,
        },
    )
    record_unfoldable_broker_order(repo, order=order, reason=reason, proof_reference=captured.order_ref)


def manual_chain_head_beyond(
    repo: ClerkSqliteRepository, *, order_ref: str, beyond: str
) -> str | None:
    """The working manual leg's chain head, when it is not the order just observed.

    ``None`` for every unreplaced order, every bot order and every ended
    leg: only a working manual leg that Alpaca replaced follows a broker
    identity its client-id lookup cannot answer for.
    """
    row = repo.order(order_ref)
    if row is None or row.broker_order_id is None or row.broker_order_id == beyond:
        return None
    return row.broker_order_id if live_manual_effect(repo, row) is not None else None


def manual_order_replacement_note(
    repo: ClerkSqliteRepository, *, order_ref: str
) -> str | None:
    """The replacement sentence the manual ticket shows, while one is followed.

    ``None`` before any replacement and once the leg ended: the ticket names
    a replacement only while the Clerk is actually following one.
    """
    row = repo.order(order_ref)
    if (
        row is None
        or live_manual_effect(repo, row) is None
        or not repo.has_order_transition(order_ref=order_ref, transition_kind=MANUAL_ORDER_REPLACED_TRANSITION)
    ):
        return None
    return REPLACED_AT_ALPACA_COPY


__all__ = [
    "CYCLIC_REPLACEMENT_REASON",
    "MANUAL_ORDER_REPLACED_TRANSITION",
    "REPLACED_AT_ALPACA_COPY",
    "UNNAMED_REPLACEMENT_REASON",
    "CapturedOrder",
    "ChainStanding",
    "live_manual_effect",
    "manual_chain_head_beyond",
    "manual_order_replacement_note",
    "resolve_captured_order",
]
