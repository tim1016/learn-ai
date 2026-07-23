"""Broker-neutral contract (Broker System v2, Layer 3 — the center of gravity).

Everything that crosses the router boundary lives here: the models, the
capability descriptor, the error taxonomy, the port protocols, and the
registry. Vendor layers (``app/broker/alpaca/``) depend on this package; this
package depends on no vendor.
"""

from __future__ import annotations

from app.broker.contract.capabilities import BrokerCapabilities
from app.broker.contract.errors import (
    BrokerAuthError,
    BrokerError,
    BrokerOrderRejected,
    BrokerRateLimited,
    BrokerRequestInvalid,
    BrokerUnavailable,
    UnknownBrokerError,
)
from app.broker.contract.models import (
    BrokerAccountSnapshot,
    BrokerActivity,
    BrokerAsset,
    BrokerClockEvidence,
    BrokerOrder,
    BrokerOrderEvent,
    BrokerPosition,
)
from app.broker.contract.ports import BrokerReadPort
from app.broker.contract.registry import (
    BrokerRegistry,
    get_broker_registry,
    reset_broker_registry_for_testing,
)

__all__ = [
    "BrokerAccountSnapshot",
    "BrokerActivity",
    "BrokerAsset",
    "BrokerAuthError",
    "BrokerCapabilities",
    "BrokerClockEvidence",
    "BrokerError",
    "BrokerOrder",
    "BrokerOrderEvent",
    "BrokerOrderRejected",
    "BrokerPosition",
    "BrokerRateLimited",
    "BrokerReadPort",
    "BrokerRegistry",
    "BrokerRequestInvalid",
    "BrokerUnavailable",
    "UnknownBrokerError",
    "get_broker_registry",
    "reset_broker_registry_for_testing",
]
