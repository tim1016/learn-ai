"""Per-instance exposure + timeline projections over the Alpaca order journal.

Formula: exposure[account, namespace, symbol] = Σ (+execution_quantity for BUY,
  -execution_quantity for SELL), once per (account_id, execution ``event_key``);
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

from collections.abc import Iterable
from dataclasses import dataclass

from app.broker.alpaca.clerk.models import ClerkEntryKind, OrderJournalEntry
from app.broker.contract.models import BrokerOrder, BrokerPosition, OrderSide
from app.engine.live.journal_exposure import (
    ExecutionExposureEffect,
    fold_execution_exposure,
)
from app.engine.live.order_identity import (
    NAMESPACE_ROOT,
    build_bot_order_namespace,
    order_ref_namespace_matches,
    parse_order_ref,
)


class FlattenRefusedError(Exception):
    """A managed flatten failed projection verification (P3 invariant b).

    Raised BEFORE any broker call: the instance's journal-owned exposure
    projection does not contain the targeted exposure (symbol, quantity,
    namespace), so submitting the flatten could close a sibling's position.
    """

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail


ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES = frozenset(
    {"filled", "canceled", "expired", "rejected", "replaced"}
)


def signed_broker_position_quantity(position: BrokerPosition) -> float:
    """Normalize Alpaca's absolute quantity + side into one signed quantity."""

    quantity = abs(position.quantity)
    return -quantity if position.side.lower() == "short" else quantity


@dataclass(frozen=True)
class InstanceExposure:
    """One non-zero signed exposure bucket from Alpaca journal fill effects."""

    account_id: str
    namespace: str
    strategy_instance_id: str | None
    symbol: str
    quantity: float


def project_expected_account_exposure(
    entries: Iterable[OrderJournalEntry],
) -> dict[str, float]:
    """Project broker inventory expected by the Alpaca account journal.

    Formula: expected[symbol] = latest operator-confirmed broker baseline +
      the latest terminal filled quantity for each Clerk order recorded after
      that baseline, signed BUY positive / SELL negative. Without a baseline,
      the fold starts from zero and retains the legacy full-journal behavior.

    A later baseline is an explicit accounting cutover: older order history
    remains durable evidence, but it no longer contributes to current account
    inventory. Baselines are account-scoped and never assign their positions
    to an instance; they do retire pre-cutover instance exposure below.

    Reference: ADR 0030 (account-rooted, journal-canonical custody).
    Canonical implementation: this function.
    Validated against: tests/broker/alpaca/clerk/test_derive.py::
      test_inventory_baseline_preserves_history_without_phantom_exposure.
    """

    rows = tuple(entries)
    baseline_index = -1
    expected: dict[str, float] = {}
    for index, entry in enumerate(rows):
        baseline = entry.broker_evidence_baseline
        if entry.kind is not ClerkEntryKind.BROKER_EVIDENCE_BASELINE or baseline is None:
            continue
        baseline_index = index
        expected = {}
        for position in baseline.positions:
            symbol = position.symbol.upper()
            expected[symbol] = expected.get(symbol, 0.0) + position.signed_quantity

    latest_order_by_ref: dict[str, BrokerOrder] = {}
    for entry in rows[baseline_index + 1 :]:
        if entry.order_ref and entry.order is not None:
            latest_order_by_ref[entry.order_ref] = entry.order

    for order in latest_order_by_ref.values():
        if order.status.lower() not in ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES:
            continue
        signed_quantity = order.filled_quantity
        if order.side.lower() == "sell":
            signed_quantity = -signed_quantity
        symbol = order.symbol.upper()
        expected[symbol] = expected.get(symbol, 0.0) + signed_quantity

    return {
        symbol: quantity
        for symbol, quantity in sorted(expected.items())
        if quantity != 0.0
    }


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
    - only owned ``ORDER_EVENT`` fills or partial fills carrying a broker execution identity
      accumulate;
    - dedup on ``(account_id, event_key)`` — never the journal position;
    - the latest broker inventory baseline retires all earlier effects from
      current instance custody without deleting their journal rows;
    - zero balances are omitted; output sorted for determinism.

    ``namespace`` filters to one ownership scope by exact equality.
    """
    rows = tuple(entries)
    baseline_index = -1
    for index, entry in enumerate(rows):
        if entry.kind is ClerkEntryKind.BROKER_EVIDENCE_BASELINE:
            baseline_index = index

    effects: list[ExecutionExposureEffect] = []
    for entry in rows[baseline_index + 1 :]:
        if entry.kind is not ClerkEntryKind.ORDER_EVENT or entry.owned is not True:
            continue
        event = entry.event
        if (
            event is None
            or event.event_type not in ("fill", "partial_fill")
            or event.quantity is None
        ):
            continue
        if not entry.event_key:
            # A fill without a broker execution identity cannot be safely
            # deduplicated; the consumer always keys fills by execution_id,
            # so this mirrors the canonical "non-empty exec_id" gate.
            continue
        leg = entry.leg
        if leg is None or not entry.order_ref:
            continue
        try:
            entry_namespace, _intent_id = parse_order_ref(entry.order_ref)
        except ValueError:
            continue
        signed = event.quantity if leg.side is OrderSide.BUY else -event.quantity
        effects.append(
            ExecutionExposureEffect(
                account_id=entry.account_id,
                group_id=entry_namespace,
                symbol=leg.symbol,
                execution_id=entry.event_key,
                signed_quantity=signed,
            )
        )

    out: list[InstanceExposure] = []
    for (account_id, entry_namespace, symbol), quantity in (
        fold_execution_exposure(effects).items()
    ):
        if namespace is not None and entry_namespace != namespace:
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
