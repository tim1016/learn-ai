"""Per-instance exposure + timeline projections over the Alpaca order journal.

Formula: exposure[account, namespace, symbol] = Σ (+fill_quantity for BUY,
  -fill_quantity for SELL), once per (account_id, execution ``event_key``);
  zero balances are omitted.
Reference: learn-ai issue #1261 (P3 ownership invariants; 07-27 wave-one
  ownership defect), ADR 0008 (exact-equality namespace matching).
Canonical implementation: app/engine/live/journal_exposure.py — the Account
  Clerk fold this module mirrors for the Alpaca order journal. The duplicate
  exists for vendor parity (different journal schema, same fold semantics)
  per CLAUDE.md guiding-philosophy #5.
Validated against: tests/broker/alpaca/clerk/test_instance_orders.py::
  test_projection_fold_is_idempotent_under_duplicate_and_out_of_order_delivery.

Journal append order is a delivery identity. It must never deduplicate an
execution effect: only the broker execution identity — the consumer-resolved
``event_key`` (``exec:{execution_id}`` for fills) — owns that responsibility.

The per-bot timeline (P12) is a namespace-filtered projection of the account
journal — there is deliberately NO per-run sidecar ledger.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from app.broker.alpaca.clerk.models import ClerkEntryKind, OrderJournalEntry
from app.broker.contract.models import OrderSide
from app.engine.live.order_identity import (
    NAMESPACE_ROOT,
    build_bot_order_namespace,
    order_ref_namespace_matches,
    parse_order_ref,
)

_ZERO_ABS_TOL = 1e-9


class FlattenRefusedError(Exception):
    """A managed flatten failed projection verification (P3 invariant b).

    Raised BEFORE any broker call: the instance's journal-owned exposure
    projection does not contain the targeted exposure (symbol, quantity,
    namespace), so submitting the flatten could close a sibling's position.
    """

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail


@dataclass(frozen=True)
class InstanceExposure:
    """One non-zero signed exposure bucket from Alpaca journal fill effects."""

    account_id: str
    namespace: str
    strategy_instance_id: str | None
    symbol: str
    quantity: float


def strategy_instance_id_for_namespace(namespace: str) -> str | None:
    """Return the sid for a ``learn-ai/{sid}/v1`` namespace, else ``None``.

    Exact structural match (three segments, fixed root, fixed version) —
    never a prefix test (ADR 0008 §1).
    """
    parts = namespace.split("/")
    if len(parts) == 3 and parts[0] == NAMESPACE_ROOT and parts[2] == "v1" and parts[1]:
        return parts[1]
    return None


def project_instance_exposure(
    entries: Iterable[OrderJournalEntry],
    *,
    namespace: str | None = None,
) -> tuple[InstanceExposure, ...]:
    """Fold owned journal fill effects into per-namespace exposure buckets.

    Mirrors the canonical fold's discipline exactly:
    - only owned ``ORDER_EVENT`` fills carrying a broker execution identity
      accumulate;
    - dedup on ``(account_id, event_key)`` — never the journal position;
    - zero balances are omitted; output sorted for determinism.

    ``namespace`` filters to one ownership scope by exact equality.
    """
    totals: dict[tuple[str, str, str], float] = {}
    seen_execution_effects: set[tuple[str, str]] = set()
    for entry in entries:
        if entry.kind is not ClerkEntryKind.ORDER_EVENT or entry.owned is not True:
            continue
        event = entry.event
        if event is None or event.event_type != "fill" or event.quantity is None:
            continue
        if not entry.event_key:
            # A fill without a broker execution identity cannot be safely
            # deduplicated; the consumer always keys fills by execution_id,
            # so this mirrors the canonical "non-empty exec_id" gate.
            continue
        leg = entry.leg
        if leg is None or not entry.order_ref:
            continue
        dedup_key = (entry.account_id, entry.event_key)
        if dedup_key in seen_execution_effects:
            continue
        seen_execution_effects.add(dedup_key)
        try:
            entry_namespace, _intent_id = parse_order_ref(entry.order_ref)
        except ValueError:
            continue
        signed = event.quantity if leg.side is OrderSide.BUY else -event.quantity
        bucket = (entry.account_id, entry_namespace, leg.symbol)
        totals[bucket] = totals.get(bucket, 0.0) + signed

    out: list[InstanceExposure] = []
    for (account_id, entry_namespace, symbol), quantity in sorted(totals.items()):
        if namespace is not None and entry_namespace != namespace:
            continue
        if math.isclose(quantity, 0.0, rel_tol=0.0, abs_tol=_ZERO_ABS_TOL):
            continue
        out.append(
            InstanceExposure(
                account_id=account_id,
                namespace=entry_namespace,
                strategy_instance_id=strategy_instance_id_for_namespace(entry_namespace),
                symbol=symbol,
                quantity=quantity,
            )
        )
    return tuple(out)


def verify_flatten(
    entries: Iterable[OrderJournalEntry],
    *,
    namespace: str,
    symbol: str,
    quantity: float,
) -> OrderSide:
    """Verify a flatten against the instance projection; return the reducing side.

    P3 invariant (b): a managed flatten is checked against the journal-owned
    projection — symbol, quantity, namespace — and refused with
    :class:`FlattenRefusedError` when the projection does not contain the
    targeted exposure. Pure: the clerk calls this inside its intake lock so no
    concurrent fill can invalidate the verdict before the submit.
    """
    if quantity <= 0:
        raise FlattenRefusedError(
            "Flatten quantity must be a positive share count.",
            detail=f"Requested {quantity!r} for {symbol}.",
        )
    owned = {
        bucket.symbol: bucket
        for bucket in project_instance_exposure(entries, namespace=namespace)
    }
    bucket = owned.get(symbol)
    if bucket is None:
        raise FlattenRefusedError(
            f"Namespace '{namespace}' owns no {symbol} exposure.",
            detail=(
                f"The journal-owned projection has no {symbol} bucket; refusing "
                "to submit a flatten that could close a sibling's position."
            ),
        )
    if quantity > abs(bucket.quantity):
        raise FlattenRefusedError(
            f"Flatten of {quantity} {symbol} exceeds the owned exposure of "
            f"{bucket.quantity}.",
            detail=f"Namespace '{namespace}' owns {bucket.quantity} {symbol}.",
        )
    return OrderSide.SELL if bucket.quantity > 0 else OrderSide.BUY


def project_instance_timeline(
    entries: Iterable[OrderJournalEntry],
    strategy_instance_id: str,
) -> tuple[OrderJournalEntry, ...]:
    """The per-bot order/fill timeline: a namespace-filtered journal projection.

    P12 — no second event store. An entry belongs to the bot iff its order ref
    (owning ``order_ref``, else the wire ``client_order_id``) matches the
    bot's namespace by the canonical exact-equality matcher.
    """
    allowed = frozenset({build_bot_order_namespace(strategy_instance_id)})
    out: list[OrderJournalEntry] = []
    for entry in entries:
        ref = entry.order_ref or entry.client_order_id
        if ref and order_ref_namespace_matches(ref, allowed):
            out.append(entry)
    return tuple(out)
