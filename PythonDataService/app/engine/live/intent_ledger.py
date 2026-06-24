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
from dataclasses import dataclass, field, replace
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
class SizingResolution:
    """ADR 0009 § 11 — sizing decision frozen into the WAL for the per-trade
    audit list. Captured at order-construction time (not at fill, not at
    session boundary); the cockpit joins it back to fills on ``intent_id``.
    ``reference_price`` is stored as a decimal string (money never floats).
    """

    policy_kind: str
    policy_value: str
    intended_qty: int
    reference_price: str
    sizing_provenance_at_resolve_time: str
    sized_via: str
    ts_ms: int | None = None


@dataclass(frozen=True)
class SubmittedOrderView:
    """One intent's folded state, keyed by ``intent_id`` in the ledger.

    ``order_spec`` is the order's IbkrOrderSpec.model_dump() captured at
    PENDING_INTENT emit time (Phase 5A). Phase 5E reads ``order_spec["symbol"]``
    and ``order_spec["action"]`` / ``order_spec["quantity"]`` to reconstruct
    a fill's ``_OrderMeta`` when only the broker's ``perm_id`` survives a
    restart (the in-memory ``LiveEngine._order_meta`` is empty on cold
    start). ``None`` for events that pre-date Phase 5A or that fold
    SIZING_RESOLVED before the PENDING_INTENT lands.
    """

    intent_id: str
    bot_order_namespace: str
    order_ref: str
    status: IntentEventType
    intent_kind: IntentKind = IntentKind.STRATEGY
    order_id: int | None = None
    perm_id: int | None = None
    exec_ids: tuple[str, ...] = ()
    sizing_resolution: SizingResolution | None = None
    order_spec: Mapping[str, object] | None = None
    # PR 3 / operator-notice — fold-side legacy classification. Set to
    # ``"legacy_sizing_only_dropped"`` for SIZING_RESOLVED sentinel views whose
    # ``ts_ms`` is before the engine-start cutoff (meaning the engine that
    # produced the SIZING_RESOLVED WAL event is gone; no terminal event will
    # ever follow it in this session). The publisher reads this to emit
    # activity.dropped_paused_intent without triggering for in-flight intents.
    # ``None`` when not classified (the common case: all pre-PR-3 callers).
    classification: str | None = None


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


def fold(
    projection: LedgerProjection,
    wal_events: Sequence[IntentEvent],
    *,
    legacy_sizing_only_cutoff_ms: int | None = None,
) -> LedgerView:
    """Fold ``wal_events`` over ``projection`` into a ``LedgerView``.

    Pure: mutates nothing it is given. Events with ``seq <=
    projection.last_intent_wal_seq`` are already folded and skipped, so applying
    the same tail twice is idempotent.

    ``legacy_sizing_only_cutoff_ms`` — PR 3 / operator-notice. When provided,
    any SIZING_RESOLVED sentinel that has no prior view (no PENDING_INTENT has
    landed yet) AND whose ``ts_ms < cutoff`` is stamped with
    ``classification="legacy_sizing_only_dropped"``. The publisher uses this to
    dedupe orphaned sizing records from before the engine-start boundary.
    Existing callers that omit this parameter observe no change in behaviour
    (``classification=None`` on every view, as before PR 3).
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

        # ADR 0009 § 11 — SIZING_RESOLVED is an audit-only event that decorates
        # the projection without changing the submit lifecycle status. We
        # capture the sizing resolution on the view but keep the prior status
        # (so a sole SIZING_RESOLVED event doesn't masquerade as "submitted").
        if event.event_type is IntentEventType.SIZING_RESOLVED:
            if event.policy_kind is None or event.intended_qty is None:
                # Malformed audit event — skip without breaking the fold.
                last_seq = event.seq
                continue
            sizing_resolution = SizingResolution(
                policy_kind=event.policy_kind,
                policy_value=event.policy_value or "",
                intended_qty=event.intended_qty,
                reference_price=event.reference_price or "",
                sizing_provenance_at_resolve_time=event.sizing_provenance_at_resolve_time
                or "live_override",
                sized_via=event.sized_via or "policy_set_holdings",
                ts_ms=event.ts_ms,
            )
            if existing is not None:
                orders[event.intent_id] = replace(
                    existing, sizing_resolution=sizing_resolution
                )
            else:
                # Edge case: SIZING_RESOLVED before PENDING_INTENT (e.g. test
                # fixture or a paused-drop scenario). Synthesize a sentinel
                # view; the real PENDING_INTENT/SUBMITTED follow-up will
                # overwrite status.
                #
                # PR 3 / operator-notice — when a cutoff is provided and the
                # event is before the cutoff, stamp ``classification`` so the
                # publisher can dedupe orphaned sizing records from before the
                # engine-start boundary (these will never receive a terminal
                # event in this session). Post-cutoff or no-cutoff → None.
                #
                # Reviewer finding 2: compare appended_at_ms (process wall-clock
                # at WAL write time) NOT ts_ms (strategy bar timestamp). For
                # SIZING_RESOLVED events, ts_ms is the bar close time from
                # set_holdings(..., time). In delayed live feeds or historical
                # runs a current-run bar can have a close time BEFORE the engine
                # process start, causing ts_ms < cutoff even for in-flight
                # current-run intents. appended_at_ms is always in the same
                # time domain as legacy_sizing_only_cutoff_ms (engine_started_at_ms).
                # Backward-compat: events on disk before this field existed have
                # appended_at_ms=None; treat as pre-cutoff (safe default — the
                # publisher will classify and dedupe them as legacy orphans, which
                # is exactly what they are: events from a prior engine process).
                legacy_classification: str | None = None
                if legacy_sizing_only_cutoff_ms is not None and (
                    event.appended_at_ms is None
                    or event.appended_at_ms < legacy_sizing_only_cutoff_ms
                ):
                    legacy_classification = "legacy_sizing_only_dropped"
                orders[event.intent_id] = SubmittedOrderView(
                    intent_id=event.intent_id,
                    bot_order_namespace=event.bot_order_namespace,
                    order_ref=event.order_ref,
                    status=IntentEventType.PENDING_INTENT,
                    intent_kind=event.intent_kind,
                    sizing_resolution=sizing_resolution,
                    classification=legacy_classification,
                )
            last_seq = event.seq
            continue

        # Phase 5E — preserve the first non-None ``order_spec`` we see on
        # the lifecycle (PENDING_INTENT carries it). Later events overwrite
        # status / order_id / perm_id but keep the original spec so a
        # cross-restart fill classifier can recover symbol / quantity even
        # when the in-memory ``_order_meta`` is empty.
        order_spec = (
            event.order_spec
            if event.order_spec is not None
            else (existing.order_spec if existing else None)
        )
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
            sizing_resolution=existing.sizing_resolution if existing else None,
            order_spec=order_spec,
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
