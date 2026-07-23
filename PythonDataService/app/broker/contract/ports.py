"""Broker port protocols (Broker System v2, Layer 3).

Ports are the structural interface a vendor layer implements. Phase 1 defines
the read port; ``BrokerTradePort`` (submit/cancel) and ``BrokerBarStreamPort``
(live bars) arrive in phases 2 and 3. All methods are ``async`` — vendor
implementations wrap synchronous SDK calls in a threadpool so the service layer
stays non-blocking.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.broker.contract.capabilities import BrokerCapabilities
from app.broker.contract.models import (
    BrokerAccountSnapshot,
    BrokerActivity,
    BrokerAsset,
    BrokerClockEvidence,
    BrokerOrder,
    BrokerPosition,
)


@runtime_checkable
class BrokerReadPort(Protocol):
    """Read-only broker surface: account, positions, orders, activities, assets, clock.

    Implementations must expose ``broker_id`` (the registry key and the
    ``{broker}`` path segment) and ``capabilities()``. Every data method may
    raise a ``BrokerError`` subclass; the router translates those to HTTP.
    """

    broker_id: str

    def capabilities(self) -> BrokerCapabilities: ...

    async def get_account(self) -> BrokerAccountSnapshot: ...

    async def list_positions(self) -> list[BrokerPosition]: ...

    async def list_orders(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
        after_ms: int | None = None,
    ) -> list[BrokerOrder]: ...

    async def list_activities(
        self,
        *,
        after_ms: int | None = None,
        limit: int = 100,
    ) -> list[BrokerActivity]: ...

    async def list_assets(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[BrokerAsset]: ...

    async def get_clock_evidence(self) -> BrokerClockEvidence: ...
