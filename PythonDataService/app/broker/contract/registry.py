"""Broker registry (Broker System v2, Layer 3).

Maps a ``broker_id`` to its :class:`BrokerReadPort` implementation. The router
resolves the ``{broker}`` path segment here; an unknown broker raises
:class:`UnknownBrokerError`, which the router surfaces as ``404``. Phase 1
registers only ``alpaca``.

The registry holds port *instances*, but a port must be cheap to construct —
vendor credentials and network clients are built lazily on first call, so
registration at app startup never needs keys and the service boots in
credential-free environments (CI, tests).
"""

from __future__ import annotations

from app.broker.contract.errors import UnknownBrokerError
from app.broker.contract.ports import BrokerReadPort


class BrokerRegistry:
    """A process-wide ``broker_id -> BrokerReadPort`` map."""

    def __init__(self) -> None:
        self._ports: dict[str, BrokerReadPort] = {}

    def register(self, port: BrokerReadPort) -> None:
        """Bind (or rebind) a read port under its ``broker_id``."""
        self._ports[port.broker_id] = port

    def resolve(self, broker_id: str) -> BrokerReadPort:
        """Return the port for ``broker_id`` or raise :class:`UnknownBrokerError`."""
        port = self._ports.get(broker_id)
        if port is None:
            known = ", ".join(sorted(self._ports)) or "none"
            raise UnknownBrokerError(
                f"Unknown broker '{broker_id}'.",
                broker=broker_id,
                detail=f"Registered brokers: {known}.",
            )
        return port

    def registered_brokers(self) -> list[str]:
        """Return the sorted list of registered broker ids."""
        return sorted(self._ports)

    def reset(self) -> None:
        """Drop all registrations — test hygiene only."""
        self._ports.clear()


_registry: BrokerRegistry | None = None


def get_broker_registry() -> BrokerRegistry:
    """Return the process-wide broker registry."""
    global _registry
    if _registry is None:
        _registry = BrokerRegistry()
    return _registry


def reset_broker_registry_for_testing() -> None:
    """Reset the process-wide registry so a test starts from an empty map."""
    global _registry
    _registry = None
