"""Module B — intent ledger fold (deep, pure). ADR-0008 §2, PRD #446.

Pure functions that fold the WAL event stream over a projection snapshot to
produce the in-memory ledger view the reconciler and halt logic read. The
"intent ledger" is a *reconstructed logical view* — it persists **nothing**
(the run-scoped WAL + ``live_state.json`` are the system of record). The fold
replays only events past the projection's cursor (``last_intent_wal_seq``),
applying each exactly once.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING

from app.engine.live.intent_events import IntentEvent, IntentEventType, IntentKind

if TYPE_CHECKING:
    from app.engine.live.live_state_sidecar import LiveStateEnvelope

# Statuses that still need resolution against the broker at cold start. A
# proven-absent (INTENT_NOT_ACCEPTED) or known-present (SUBMITTED /
# SUBMITTED_RECOVERED / ADOPTED_BROKER_ORDER) intent is already resolved.
_UNRESOLVED_STATUSES = frozenset(
    {
        IntentEventType.PENDING_INTENT,
        IntentEventType.ACK_FAILED_UNCERTAIN,
        IntentEventType.SUBMIT_UNCERTAIN_HALTED,
    }
)


@dataclass(frozen=True)
class SubmittedOrderView:
    """One intent's folded state, keyed by ``intent_id`` in the ledger."""

    intent_id: str
    bot_order_namespace: str
    order_ref: str
    status: IntentEventType
    intent_kind: IntentKind = IntentKind.STRATEGY
    order_id: int | None = None
    perm_id: int | None = None
    exec_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class LedgerProjection:
    """The durable snapshot the WAL tail folds over."""

    submitted_orders: Mapping[str, SubmittedOrderView] = field(default_factory=dict)
    known_perm_ids: frozenset[int] = frozenset()
    known_exec_ids: frozenset[str] = frozenset()
    last_intent_wal_seq: int = 0


@dataclass(frozen=True)
class LedgerView:
    """The folded, in-memory ledger consumed by the reconciler / halt logic."""

    submitted_orders: Mapping[str, SubmittedOrderView]
    known_perm_ids: frozenset[int]
    known_exec_ids: frozenset[str]
    last_seq: int
    unresolved_intent_ids: frozenset[str]


def fold(projection: LedgerProjection, wal_events: Sequence[IntentEvent]) -> LedgerView:
    """Fold ``wal_events`` over ``projection`` into a ``LedgerView``.

    Pure: mutates nothing it is given. Events with ``seq <=
    projection.last_intent_wal_seq`` are already folded and skipped, so applying
    the same tail twice is idempotent.
    """
    orders: dict[str, SubmittedOrderView] = dict(projection.submitted_orders)
    known_perm_ids: set[int] = set(projection.known_perm_ids)
    known_exec_ids: set[str] = set(projection.known_exec_ids)
    last_seq = projection.last_intent_wal_seq

    for event in sorted(wal_events, key=lambda e: e.seq):
        if event.seq <= last_seq:
            continue  # already folded into the projection; apply exactly once
        existing = orders.get(event.intent_id)
        exec_ids = existing.exec_ids if existing else ()
        if event.exec_id is not None and event.exec_id not in exec_ids:
            exec_ids = (*exec_ids, event.exec_id)
        orders[event.intent_id] = SubmittedOrderView(
            intent_id=event.intent_id,
            bot_order_namespace=event.bot_order_namespace,
            order_ref=event.order_ref,
            status=event.event_type,
            intent_kind=event.intent_kind,
            order_id=event.order_id
            if event.order_id is not None
            else (existing.order_id if existing else None),
            perm_id=event.perm_id
            if event.perm_id is not None
            else (existing.perm_id if existing else None),
            exec_ids=exec_ids,
        )
        if event.perm_id is not None:
            known_perm_ids.add(event.perm_id)
        if event.exec_id is not None:
            known_exec_ids.add(event.exec_id)
        last_seq = event.seq

    unresolved = frozenset(
        iid for iid, view in orders.items() if view.status in _UNRESOLVED_STATUSES
    )
    return LedgerView(
        submitted_orders=MappingProxyType(orders),
        known_perm_ids=frozenset(known_perm_ids),
        known_exec_ids=frozenset(known_exec_ids),
        last_seq=last_seq,
        unresolved_intent_ids=unresolved,
    )


def _order_view_from_raw(intent_id: str, raw: Mapping[str, object]) -> SubmittedOrderView:
    status_raw = raw.get("status")
    kind_raw = raw.get("intent_kind")
    exec_ids_raw = raw.get("exec_ids") or ()
    order_id_raw = raw.get("order_id")
    perm_id_raw = raw.get("perm_id")
    return SubmittedOrderView(
        intent_id=intent_id,
        bot_order_namespace=str(raw.get("bot_order_namespace", "")),
        order_ref=str(raw.get("order_ref", "")),
        status=IntentEventType(status_raw) if status_raw else IntentEventType.SUBMITTED,
        intent_kind=IntentKind(kind_raw) if kind_raw else IntentKind.STRATEGY,
        order_id=int(order_id_raw) if order_id_raw is not None else None,
        perm_id=int(perm_id_raw) if perm_id_raw is not None else None,
        exec_ids=tuple(str(e) for e in exec_ids_raw),
    )


def projection_from_envelope(envelope: LiveStateEnvelope) -> LedgerProjection:
    """Adapt the persisted ``LiveStateEnvelope`` into a ``LedgerProjection``.

    ``submitted_orders`` is keyed by ``intent_id`` with loosely-typed entry
    dicts; parse defensively. ``last_intent_wal_seq`` defaults to 0 on older
    envelopes that predate the field.
    """
    orders = {
        intent_id: _order_view_from_raw(intent_id, raw)
        for intent_id, raw in (envelope.submitted_orders or {}).items()
    }
    return LedgerProjection(
        submitted_orders=MappingProxyType(orders),
        known_perm_ids=frozenset(envelope.known_perm_ids),
        known_exec_ids=frozenset(envelope.known_exec_ids),
        last_intent_wal_seq=getattr(envelope, "last_intent_wal_seq", 0),
    )


__all__ = [
    "LedgerProjection",
    "LedgerView",
    "SubmittedOrderView",
    "fold",
    "projection_from_envelope",
]
