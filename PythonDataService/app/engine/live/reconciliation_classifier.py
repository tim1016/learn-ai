"""Module E — reconciliation classifier (deep, pure). ADR-0008 §5, PRD #446.

Given the folded projection, this run's WAL tail (already folded in), the
immediately-prior run's un-acked ``PENDING_INTENT`` tail, the prior run's
emergency-flatten audit artifact, and a **synthetic** broker snapshot, return
one of three verdicts:

* ``Continue``  — broker state all matches the projection / WAL.
* ``Adopt``     — an owned orphan (parsed ``order_ref`` namespace EXACTLY equals
                  ours, intent unknown to the projection) is recovered, not
                  poisoned. A KNOWN-but-unresolved intent (``PENDING_INTENT`` /
                  ``ACK_FAILED_UNCERTAIN``) the broker confirms is live is
                  recovered the same way — never silently continued.
                  ``pause=True`` when an adopted order is still active and
                  creates ambiguous exposure.
* ``Poison``    — outside mutation: unknown / foreign / unparseable namespace,
                  no ``order_ref``, or a foreign ``perm_id``. ``order_id`` alone
                  never proves ownership.

Ownership precedence (ADR-0008 §1): a known ``perm_id``/``exec_id`` proves the
order is ours (broker-assigned, globally unique, recorded only because we placed
it). Otherwise the ``order_ref`` namespace must parse and match **exactly** — a
present-but-foreign namespace is outside mutation, never silently continued.

Pure: the broker snapshot is an INPUT (the real IBKR query is fail-closed and
gated — Acceptance Gate #2). No I/O here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.engine.live.intent_events import IntentEvent
from app.engine.live.intent_ledger import LedgerView
from app.engine.live.order_identity import OrderRefParseError, parse_order_ref

# Statuses at which a broker order is still live and could move position.
_ACTIVE_STATUSES = frozenset(
    {"PendingSubmit", "PreSubmitted", "Submitted", "ApiPending"}
)

_ADOPTION_STEPS: tuple[str, ...] = (
    "parse_and_verify_namespace_and_intent",
    "capture_broker_fields",
    "append_ADOPTED_BROKER_ORDER",
    "fold_into_projection",
    "persist_live_state_before_new_submits",
)


@dataclass(frozen=True)
class BrokerOrderView:
    """One open order as the (synthetic) broker snapshot reports it."""

    order_ref: str | None
    perm_id: int | None = None
    order_id: int | None = None
    status: str | None = None
    symbol: str | None = None
    remaining: float = 0.0
    filled: float = 0.0


@dataclass(frozen=True)
class BrokerExecutionView:
    """One execution as the (synthetic) broker snapshot reports it."""

    order_ref: str | None
    perm_id: int | None = None
    exec_id: str | None = None
    symbol: str | None = None
    quantity: float = 0.0
    exec_time_ms: int | None = None


@dataclass(frozen=True)
class BrokerSnapshot:
    open_orders: tuple[BrokerOrderView, ...] = ()
    executions: tuple[BrokerExecutionView, ...] = ()


@dataclass(frozen=True)
class OwnedOrphan:
    order_ref: str
    intent_id: str
    perm_id: int | None
    order_id: int | None
    active: bool
    source: str


@dataclass(frozen=True)
class Continue:
    pass


@dataclass(frozen=True)
class Adopt:
    orphans: tuple[OwnedOrphan, ...]
    steps: tuple[str, ...] = _ADOPTION_STEPS
    pause: bool = False
    pause_reason: str | None = None


@dataclass(frozen=True)
class Poison:
    reason: str


ReconcileVerdict = Continue | Adopt | Poison


def _ref_sources(
    prior_run_unacked_tail: Sequence[IntentEvent],
    emergency_audit: Sequence[IntentEvent],
) -> dict[str, str]:
    sources: dict[str, str] = {}
    for ev in prior_run_unacked_tail:
        sources.setdefault(ev.order_ref, "prior_run_tail")
    for ev in emergency_audit:
        sources[ev.order_ref] = "emergency_flatten"  # emergency takes precedence
    return sources


def classify(
    *,
    projection: LedgerView,
    broker_snapshot: BrokerSnapshot,
    allowed_namespaces: frozenset[str],
    prior_run_unacked_tail: Sequence[IntentEvent] = (),
    emergency_audit: Sequence[IntentEvent] = (),
    ignore_unknown_namespaces_before_ms: int | None = None,
) -> ReconcileVerdict:
    """Reconcile the broker snapshot against the projection. See module docstring.

    Poison takes precedence over adoption: any genuinely foreign state is fatal,
    even alongside recoverable orphans.
    """
    known_intent_ids = frozenset(projection.submitted_orders.keys())
    ref_sources = _ref_sources(prior_run_unacked_tail, emergency_audit)

    poison_reasons: set[str] = set()
    orphans_by_intent: dict[str, OwnedOrphan] = {}

    def consider(
        *,
        order_ref: str | None,
        perm_id: int | None,
        exec_id: str | None,
        order_id: int | None,
        active: bool,
        exec_time_ms: int | None,
    ) -> None:
        # A known perm_id / exec_id proves the order is ours regardless of the
        # ref string — broker-assigned, globally unique, recorded only because we
        # placed it (ADR-0008 §1 rungs 3-4).
        known_by_id = (perm_id is not None and perm_id in projection.known_perm_ids) or (
            exec_id is not None and exec_id in projection.known_exec_ids
        )
        if order_ref is None:
            if known_by_id:
                return  # ours, already known
            poison_reasons.add("foreign_perm_id" if perm_id is not None else "no_order_ref")
            return
        try:
            namespace, intent_id = parse_order_ref(order_ref)
        except OrderRefParseError:
            if known_by_id:
                return  # ours despite a garbled ref echo
            poison_reasons.add("unparseable_order_ref")
            return
        if namespace not in allowed_namespaces:
            if known_by_id:
                return  # ours by perm/exec despite a foreign-looking ref
            if (
                not active
                and ignore_unknown_namespaces_before_ms is not None
                and exec_time_ms is not None
                and exec_time_ms <= ignore_unknown_namespaces_before_ms
            ):
                return
            poison_reasons.add("unknown_namespace")  # exact-match, never prefix
            return
        # Namespace is exactly ours.
        resolved_known = (
            intent_id in known_intent_ids
            and intent_id not in projection.unresolved_intent_ids
        )
        if resolved_known or known_by_id:
            return  # known AND resolved — a true match, nothing to do
        # Either an UNKNOWN intent in our namespace (owned orphan), or a
        # KNOWN-but-UNRESOLVED intent (PENDING_INTENT / ACK_FAILED_UNCERTAIN —
        # a crash before the SUBMITTED flush) that the broker now confirms is
        # live. Both must be recovered/adopted — never a silent Continue, or we
        # resume with the in-flight order unresolved and reopen the double-submit
        # window the WAL exists to close.
        is_unresolved_known = intent_id in known_intent_ids
        orphan = OwnedOrphan(
            order_ref=order_ref,
            intent_id=intent_id,
            perm_id=perm_id,
            order_id=order_id,
            active=active,
            source="this_run_unresolved"
            if is_unresolved_known
            else ref_sources.get(order_ref, "broker"),
        )
        existing = orphans_by_intent.get(intent_id)
        # Keep the active variant if any (an open order beats a bare execution).
        if existing is None or (orphan.active and not existing.active):
            orphans_by_intent[intent_id] = orphan

    for order in broker_snapshot.open_orders:
        consider(
            order_ref=order.order_ref,
            perm_id=order.perm_id,
            exec_id=None,
            order_id=order.order_id,
            active=_is_active(order),
            exec_time_ms=None,
        )
    for execution in broker_snapshot.executions:
        consider(
            order_ref=execution.order_ref,
            perm_id=execution.perm_id,
            exec_id=execution.exec_id,
            order_id=None,
            active=False,
            exec_time_ms=execution.exec_time_ms,
        )

    if poison_reasons:
        return Poison(reason="; ".join(sorted(poison_reasons)))

    orphans = tuple(orphans_by_intent.values())
    if orphans:
        pause = any(o.active for o in orphans)
        return Adopt(
            orphans=orphans,
            pause=pause,
            pause_reason="ambiguous_exposure" if pause else None,
        )
    return Continue()


def _is_active(order: BrokerOrderView) -> bool:
    if order.status in _ACTIVE_STATUSES:
        return True
    return order.remaining > 0.0


__all__ = [
    "Adopt",
    "BrokerExecutionView",
    "BrokerOrderView",
    "BrokerSnapshot",
    "Continue",
    "OwnedOrphan",
    "Poison",
    "ReconcileVerdict",
    "classify",
]
