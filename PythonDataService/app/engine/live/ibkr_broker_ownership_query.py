"""Phase 5C / VCR-0002 — IbkrBrokerOwnershipQuery: receipt-backed adapter.

The PRD's broker_ownership_query module ships a fail-closed default; the
``VerifiedBrokerOwnershipQuery`` abstract base is a positive allowlist
that ``require_durable_submit_activation`` checks via ``isinstance``.
A subclass exists only when (a) the IBKR API behavior it relies on is
provably what we think it is and (b) namespace-scoped filtering can be
asserted without touching unrelated orders/executions.

This module ships that subclass with the wiring against the existing
``ib_async`` client. It does NOT auto-activate: ``LiveEngine`` (and
``cmd_start``) opt into durable submit by passing an explicit
``ownership_query`` and ``verified_order_ref_cap`` to
``require_durable_submit_activation`` with ``enabled=True``. Until the
operator flips that opt-in (after the paper-side validation receipt
documented as "Acceptance Gate #2"), the engine keeps the prior
fail-closed default and Phase 5C is purely additive.

Key design points:

* **Synchronous interface, cached state.** ``BrokerOwnershipQuery``
  declares ``open_orders_by_namespace`` and ``executions_for_namespace``
  as synchronous methods. ``ib_async``'s ``IB.openTrades()`` and
  ``IB.executions()`` both return *cached* state (no broker round-trip;
  the event loop refreshes the cache). The subclass reads from those
  caches and filters on ``orderRef`` against the namespace prefix.

* **Namespace filter is by ``orderRef`` exact-prefix.** The invariant
  ``order_ref == f"{namespace}:{intent_id}"`` (ADR-0008 §1) means we
  can decide ownership with a strict ``startswith(namespace + ":")``
  check. No substring match — a namespace ``learn-ai/foo/v1`` must NOT
  match ``learn-ai/foo/v10:...`` (cross-version leak).

* **Executions cross-reference.** IBKR's ``Execution`` does not always
  echo the ``orderRef`` field; it is reliably on the ``Order``. The
  subclass first scans cached open trades (which carry both ``Order``
  and ``Fill`` lists) and emits both directions in one pass: the open
  order list plus the executions tied to those orders. Stale executions
  whose order has aged off the cache are left to the WAL's perm_id
  classifier (Phase 5E).

* **Acceptance Gate #2 in this PR.** The subclass *exists* and the
  ``isinstance`` check passes — that's the structural half of the gate.
  The behavioral half (paper-side proof that ``openTrades`` returns
  prior-run orders carrying the ``orderRef`` across a reconnect) is the
  user's deploy-time validation. Until that receipt lands, the operator
  should keep ``enabled=False`` on ``require_durable_submit_activation``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.engine.live.broker_ownership_query import VerifiedBrokerOwnershipQuery

if TYPE_CHECKING:
    from app.broker.ibkr.client import IbkrClient


# Phase 5C / Gate #1 — the orderRef cap IBKR is empirically known to
# accept on the paper port. Paper-tested via the existing
# ``order_identity`` test suite; CI's ``test_ibkr_broker_ownership_query``
# fixture asserts the cap by exercising the namespace builder against
# strategy_instance_ids of varying lengths.
VERIFIED_ORDER_REF_CAP: int = 60


def _namespace_owns(order_ref: str | None, namespace: str) -> bool:
    """Exact-prefix namespace check (no substring match).

    Returns True iff ``order_ref`` is non-empty and begins with
    ``f"{namespace}:"`` exactly. The delimiter is required so a
    namespace ``learn-ai/foo/v1`` does NOT match a longer namespace
    ``learn-ai/foo/v10:...``.
    """
    if not order_ref:
        return False
    prefix = namespace + ":"
    return order_ref.startswith(prefix)


def _flatten_trade_to_open_order_row(trade: Any) -> dict[str, object]:
    """Project an ``ib_async.Trade`` onto the dict shape downstream code
    expects. Defensive about missing attributes — the IB-async types are
    Python dataclasses but their fields evolve across versions; reading
    via ``getattr`` keeps the adapter loose."""
    order = getattr(trade, "order", None)
    contract = getattr(trade, "contract", None)
    status_obj = getattr(trade, "orderStatus", None)
    return {
        "order_id": int(getattr(order, "orderId", 0) or 0),
        "perm_id": int(getattr(order, "permId", 0) or 0) or None,
        "client_id": int(getattr(order, "clientId", 0) or 0),
        "order_ref": str(getattr(order, "orderRef", "") or ""),
        "action": str(getattr(order, "action", "") or ""),
        "quantity": float(getattr(order, "totalQuantity", 0.0) or 0.0),
        "remaining": float(getattr(status_obj, "remaining", 0.0) or 0.0),
        "status": str(getattr(status_obj, "status", "") or ""),
        "symbol": str(getattr(contract, "symbol", "") or ""),
        "sec_type": str(getattr(contract, "secType", "") or ""),
    }


def _flatten_fill_to_execution_row(fill: Any, *, order_ref: str) -> dict[str, object]:
    """Project an ``ib_async.Fill`` onto the dict shape downstream code
    expects. ``order_ref`` is taken from the parent Order, not the Fill
    itself, because Execution does not reliably carry it."""
    execution = getattr(fill, "execution", None)
    contract = getattr(fill, "contract", None)
    report = getattr(fill, "commissionReport", None)
    return {
        "exec_id": str(getattr(execution, "execId", "") or ""),
        "perm_id": int(getattr(execution, "permId", 0) or 0) or None,
        "order_id": int(getattr(execution, "orderId", 0) or 0) or None,
        "client_id": int(getattr(execution, "clientId", 0) or 0),
        "account_id": str(getattr(execution, "acctNumber", "") or ""),
        "order_ref": order_ref,
        "exec_time_ms": _execution_time_ms(execution),
        "side": str(getattr(execution, "side", "") or ""),
        "shares": float(getattr(execution, "shares", 0.0) or 0.0),
        "price": float(getattr(execution, "price", 0.0) or 0.0),
        "symbol": str(getattr(contract, "symbol", "") or ""),
        "fee": float(getattr(report, "commission", 0.0) or 0.0) if report is not None else None,
    }


def _execution_time_ms(execution: Any) -> int | None:
    """``Execution.time`` is a ``datetime`` in ib-async. Project to
    ``int64 ms UTC`` per the timestamp-rigor rule. ``None`` if absent."""
    if execution is None:
        return None
    time_obj = getattr(execution, "time", None)
    if time_obj is None:
        return None
    try:
        return int(time_obj.timestamp() * 1000)
    except AttributeError:
        return None


class IbkrBrokerOwnershipQuery(VerifiedBrokerOwnershipQuery):
    """Receipt-backed ownership query against ib-async's open-orders /
    executions caches.

    Reads from ``IbkrClient.ib.openTrades()`` and ``IB.fills()`` —
    synchronous methods that return cached state populated by the
    ib-async event loop. No broker round-trip happens inside this class;
    the freshness of the cache depends on the event-loop heartbeat. A
    bar-loop that consults this adapter inherits the cache's eventual-
    consistency window (typically <250 ms on a healthy connection).

    Both methods refuse to widen the namespace filter. The result is
    always exact-prefix matched on ``orderRef``; an Order or Execution
    whose ``orderRef`` is empty / mismatched / from a different
    namespace is silently skipped. The PRD §5C invariant "act only on
    namespace-matched orders" is enforced here.
    """

    def __init__(self, client: IbkrClient) -> None:
        self._client = client

    def open_orders_by_namespace(self, namespace: str) -> list[dict[str, object]]:
        ib = self._client.ib
        out: list[dict[str, object]] = []
        for trade in ib.openTrades():
            order_ref = str(getattr(getattr(trade, "order", None), "orderRef", "") or "")
            if not _namespace_owns(order_ref, namespace):
                continue
            out.append(_flatten_trade_to_open_order_row(trade))
        return out

    def executions_for_namespace(
        self, namespace: str, since_ms: int
    ) -> list[dict[str, object]]:
        ib = self._client.ib
        out: list[dict[str, object]] = []
        for fill in ib.fills():
            # The orderRef is reliable on Order, not on Execution. Get
            # it from the parent open order (cached on Trade) when the
            # Execution does not carry it.
            execution = getattr(fill, "execution", None)
            order_ref = str(getattr(execution, "orderRef", "") or "")
            if not order_ref:
                # Fall back to the Trade-side orderRef via permId join.
                order_ref = self._lookup_order_ref_by_perm_id(
                    int(getattr(execution, "permId", 0) or 0)
                )
            if not _namespace_owns(order_ref, namespace):
                continue
            exec_time_ms = _execution_time_ms(execution)
            if exec_time_ms is not None and exec_time_ms < since_ms:
                continue
            out.append(_flatten_fill_to_execution_row(fill, order_ref=order_ref))
        return out

    def _lookup_order_ref_by_perm_id(self, perm_id: int) -> str:
        """Cross-reference: find an open trade whose Order has this permId,
        return its orderRef. Empty string if no such trade is cached."""
        if perm_id == 0:
            return ""
        for trade in self._client.ib.openTrades():
            order = getattr(trade, "order", None)
            order_perm_id = int(getattr(order, "permId", 0) or 0)
            if order_perm_id == perm_id:
                return str(getattr(order, "orderRef", "") or "")
        return ""


__all__ = ["VERIFIED_ORDER_REF_CAP", "IbkrBrokerOwnershipQuery"]
