"""Fail-closed broker ownership query + durable-submit activation guard.

ADR-0008 §5, PRD #446 (Acceptance Gate #2). The cold-start reconciler queries
the broker for orders and executions **within this instance's namespace**. The
IBKR calls that back those queries are an **unproven assumption**, not a given:

* The recovery-flatten fills that poison relaunch are *filled* (completed)
  orders, which ``reqOpenOrders`` does **not** return at all.
* ``orderRef`` lives on the ``Order``, not the ``Execution`` — attributing a
  prior-run execution to our namespace may require a ``permId``/``orderId``
  join that is unavailable for a run we have no local trace of.

Until a paper receipt proves which calls return prior-run orders/executions
carrying ``orderRef``+``permId`` across reconnect (Gate #2), these methods
**fail closed** and activation of the durable submit protocol is **refused**.
Guessing here risks false adoption, false poisoning, and double-submit — the
exact failures this whole design exists to prevent.
"""

from __future__ import annotations

import abc
from typing import Protocol, runtime_checkable

from app.engine.live.order_identity import ORDER_REF_FIXED_OVERHEAD


@runtime_checkable
class BrokerOwnershipQuery(Protocol):
    """The narrow, namespace-scoped broker surface the reconciler may call.

    Deliberately no broad ``reqAllOpenOrders``: the Resolution-2 invariant is
    *act only on namespace-matched orders*, independent of the fetch primitive
    the verified adapter ends up using.
    """

    def open_orders_by_namespace(self, namespace: str) -> list[dict[str, object]]: ...

    def executions_for_namespace(
        self, namespace: str, since_ms: int
    ) -> list[dict[str, object]]: ...


class VerifiedBrokerOwnershipQuery(abc.ABC):
    """Nominal marker for a RECEIPT-BACKED ownership-query adapter.

    Activation requires an *instance of this base* — a positive allowlist — so
    "not the fail-closed stub" is NOT sufficient: a duck-typed object, ``None``,
    or a bare ``object()`` cannot activate the live ownership path. No subclass
    exists until Acceptance Gate #2 is satisfied and a verified IBKR adapter is
    written; until then ``require_durable_submit_activation`` always refuses.
    """

    @abc.abstractmethod
    def open_orders_by_namespace(self, namespace: str) -> list[dict[str, object]]: ...

    @abc.abstractmethod
    def executions_for_namespace(
        self, namespace: str, since_ms: int
    ) -> list[dict[str, object]]: ...


class BrokerOwnershipQueryUnavailable(RuntimeError):
    """The namespace-scoped broker query is not implemented/verified.

    Fail-closed signal: the ownership path must never guess IBKR call semantics.
    Resolve via Acceptance Gate #2 (paper receipt), then wire a verified adapter.
    """


class FailClosedBrokerOwnershipQuery:
    """Default ownership-query provider — every call fails closed.

    Structural, not a runtime flag: there is no code path here that reaches a
    real IBKR call. Replaced by a verified adapter only once Gate #2 is
    satisfied.
    """

    def open_orders_by_namespace(self, namespace: str) -> list[dict[str, object]]:
        raise BrokerOwnershipQueryUnavailable(
            "open_orders_by_namespace is not implemented/verified — "
            "PRD #446 Acceptance Gate #2 (broker-query receipt) is unmet"
        )

    def executions_for_namespace(
        self, namespace: str, since_ms: int
    ) -> list[dict[str, object]]:
        raise BrokerOwnershipQueryUnavailable(
            "executions_for_namespace is not implemented/verified — "
            "PRD #446 Acceptance Gate #2 (broker-query receipt) is unmet"
        )


class DurableSubmitNotActivatable(RuntimeError):
    """Raised when activation is requested but a gate receipt is still missing."""


def require_durable_submit_activation(
    *,
    enabled: bool,
    verified_order_ref_cap: int | None,
    ownership_query: object,
) -> None:
    """Refuse to activate the durable submit protocol until BOTH receipts exist.

    Activation requires: the feature explicitly enabled; a verified ``orderRef``
    cap (Gate #1 — ``C`` no longer unset); and a real, non-fail-closed ownership
    query (Gate #2). Until then the engine keeps the legacy path and never
    pretends the live broker semantics are known.

    A no-op when ``enabled`` is False (the default), so importing/using the
    deterministic core never trips the guard.
    """
    if not enabled:
        return
    if verified_order_ref_cap is None or verified_order_ref_cap <= ORDER_REF_FIXED_OVERHEAD:
        # `is None` alone is not enough: a cap <= the 35-char fixed overhead
        # leaves zero room for a strategy_instance_id — a silent truncation
        # guarantee dressed up as a verified value.
        raise DurableSubmitNotActivatable(
            f"order_ref cap {verified_order_ref_cap!r} is unverified or below the "
            f"{ORDER_REF_FIXED_OVERHEAD}-char fixed overhead (no room for a "
            "strategy_instance_id) — PRD #446 Acceptance Gate #1: place one paper "
            "order and confirm IBKR echoes the full orderRef untruncated"
        )
    if not isinstance(ownership_query, VerifiedBrokerOwnershipQuery):
        # Positive allowlist: only a receipt-backed adapter subclass activates.
        raise DurableSubmitNotActivatable(
            "broker ownership query unverified (PRD #446 Acceptance Gate #2): the "
            "provider must be a VerifiedBrokerOwnershipQuery subclass proving which "
            "IBKR call returns prior-run orders/executions carrying orderRef — a "
            "fail-closed stub, duck-typed object, or bare object cannot activate"
        )


__all__ = [
    "BrokerOwnershipQuery",
    "BrokerOwnershipQueryUnavailable",
    "DurableSubmitNotActivatable",
    "FailClosedBrokerOwnershipQuery",
    "VerifiedBrokerOwnershipQuery",
    "require_durable_submit_activation",
]
