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


class ColdStartReconciler:
    def verify(
        self,
        *,
        broker: _BrokerProtocol,
        sidecar: "LiveStateSidecarRepo",  # noqa: F821 — forward string ref
    ) -> SafeToResume:
        envelope = sidecar.read()
        assert envelope is not None  # cycle 1 happy path
        return SafeToResume(from_bar_ms=envelope.last_processed_bar_ms)
