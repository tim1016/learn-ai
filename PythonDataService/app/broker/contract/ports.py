"""Broker port protocols (Broker System v2, Layer 3).

Ports are the structural interface a vendor layer implements. Phase 1 defines
the read port; phase 2 adds ``BrokerTradePort`` (submit; cancel lands in S3);
``BrokerBarStreamPort`` (live bars) arrives in phase 3. All methods are
``async`` — vendor implementations wrap synchronous SDK calls in a threadpool so
the service layer stays non-blocking.
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
    BrokerOrderLeg,
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


@runtime_checkable
class BrokerTradePort(Protocol):
    """Write surface: submit an order (phase 2); cancel a working order (S3).

    ``submit`` takes a single, already-identity-minted leg plus the
    ``client_order_id`` the caller (the Clerk) minted — the port does **not**
    mint identity; that is the Clerk's job so the journal and the wire carry the
    same ``order_ref``. The vendor layer maps the accepted order to a
    ``BrokerOrder`` (the same contract type the read path returns) or raises a
    ``BrokerError`` subclass. A submit-side ``BrokerUnavailable`` is not a
    definitive failure: the Clerk resolves it through
    ``get_order_by_client_order_id`` before journaling a terminal outcome.

    ``cancel`` requests cancellation of a working order by its **broker-assigned
    order id** (the ``BrokerOrder.order_id`` — a UUID for Alpaca), not the
    ``client_order_id``. Alpaca's cancel endpoint returns no order body (HTTP
    204), so the port returns ``None`` on success; a non-cancelable order (HTTP
    422) raises a ``BrokerError`` subclass the Clerk journals as
    ``cancel_failed``. The port does not read the journal or resolve ownership —
    that is the Clerk's concern; the port only speaks to the vendor.
    """

    broker_id: str

    async def submit(
        self, leg: BrokerOrderLeg, *, client_order_id: str
    ) -> BrokerOrder: ...

    async def cancel(self, order_id: str) -> None: ...

    async def get_order_by_client_order_id(
        self, client_order_id: str
    ) -> BrokerOrder | None: ...
