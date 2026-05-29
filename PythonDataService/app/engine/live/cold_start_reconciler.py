"""Cold-start reconciliation — encodes the 7-step procedure of
docs/ibkr-paper-deployment-plan.md §16.1 Resolution 2.

Contract: ``verify(broker, sidecar) -> SafeToResume | Poisoned``.
The bot must run this on every cold start; if the result is
Poisoned, the engine writes a poisoned.flag and refuses to submit
new orders until the operator inspects the situation. There is no
offline path — failing to reach the broker is itself Poisoned.

Grown vertically via TDD; each cycle adds one outcome branch or
one side effect.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class _BrokerProtocol(Protocol):
    """The narrow broker surface the reconciler is permitted to call.

    Notably absent: reqAllOpenOrders. Resolution 2 forbids that — the
    reconciler must query only via its namespaced orderRef /
    client_order_id.
    """

    def open_orders_by_namespace(self, namespace: str) -> list[dict[str, object]]: ...


@dataclass(frozen=True)
class SafeToResume:
    from_bar_ms: int


@dataclass(frozen=True)
class Poisoned:
    reason: str


ReconciliationResult = SafeToResume | Poisoned


class ColdStartReconciler:
    def verify(
        self,
        *,
        broker: _BrokerProtocol,
        sidecar: "LiveStateSidecarRepo",  # noqa: F821 — forward string ref
        shadow_mode: bool = False,
    ) -> ReconciliationResult:
        envelope = sidecar.read()
        assert envelope is not None  # cycle 1 happy path

        try:
            broker_orders = broker.open_orders_by_namespace(envelope.bot_order_namespace)
        except Exception:
            # Resolution 2: no broker connection, no verified resume.
            # We deliberately catch broadly because any exception path
            # from the broker call — connection refused, timeout,
            # auth failure — means we cannot distinguish a clean cold
            # start from one with hidden divergence.
            return Poisoned(reason="cannot_verify_offline")
        broker_order_ids = {order.get("client_order_id") for order in broker_orders}

        # Shadow strategies never submit; the namespace must be empty.
        if shadow_mode:
            if broker_order_ids:
                return Poisoned(reason="shadow_namespace_nonempty")
            return SafeToResume(from_bar_ms=envelope.last_processed_bar_ms)

        for order_id in broker_order_ids:
            if order_id not in envelope.submitted_orders:
                return Poisoned(reason="unexpected_order_at_broker")

        for sidecar_order_id in envelope.submitted_orders:
            if sidecar_order_id not in broker_order_ids:
                return Poisoned(reason="expected_order_missing_at_broker")

        return SafeToResume(from_bar_ms=envelope.last_processed_bar_ms)
