"""AlpacaBroker — the read-port implementation (Broker System v2).

Composes the SDK client, the adapter, and the capability descriptor into a
single :class:`BrokerReadPort`. Each read-path slice adds one method here; the
router only ever sees contract models.

The port is cheap to construct (the underlying client builds credentials and
network lazily), so it can be registered at startup without keys.
"""

from __future__ import annotations

from app.broker.alpaca import adapter
from app.broker.alpaca.client import AlpacaTradingClient
from app.broker.alpaca.config import BROKER_ID
from app.broker.contract.capabilities import BrokerCapabilities
from app.broker.contract.models import (
    BrokerAccountSnapshot,
    BrokerActivity,
    BrokerAsset,
    BrokerClockEvidence,
    BrokerOrder,
    BrokerPosition,
)
from app.broker.contract.registry import BrokerRegistry, get_broker_registry

# Alpaca free / paper-account capabilities, verified 2026-07 (spec §3). Honest
# differences declared as data so callers gate on capability, not identity:
# IEX gaps on illiquid symbols (bars_may_gap), 30-symbol / 1-connection stream
# cap, 200 REST calls/min. Upgrading to Algo Trader Plus flips data_feed to
# "sip" with no architecture change.
ALPACA_PAPER_CAPABILITIES = BrokerCapabilities(
    broker=BROKER_ID,
    paper_only=True,
    supports_fractional=True,
    supports_extended_hours=True,
    supported_order_types=("market", "limit", "stop", "stop_limit", "trailing_stop"),
    data_feed="iex",
    bars_may_gap=True,
    max_stream_symbols=30,
    max_concurrent_streams=1,
    rest_rate_limit_per_min=200,
)


class AlpacaBroker:
    """Alpaca implementation of :class:`BrokerReadPort`."""

    broker_id = BROKER_ID

    def __init__(self, client: AlpacaTradingClient | None = None) -> None:
        self._client = client or AlpacaTradingClient()

    def capabilities(self) -> BrokerCapabilities:
        return ALPACA_PAPER_CAPABILITIES

    async def get_account(self) -> BrokerAccountSnapshot:
        payload = await self._client.get_account()
        return adapter.from_alpaca_account(payload)

    async def list_positions(self) -> list[BrokerPosition]:
        payloads = await self._client.list_positions()
        return [adapter.from_alpaca_position(payload) for payload in payloads]

    async def list_orders(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
        after_ms: int | None = None,
    ) -> list[BrokerOrder]:
        payloads = await self._client.list_orders(
            status=status, limit=limit, after_ms=after_ms
        )
        return [adapter.from_alpaca_order(payload) for payload in payloads]

    async def list_activities(
        self,
        *,
        after_ms: int | None = None,
    ) -> list[BrokerActivity]:
        payloads = await self._client.list_activities(after_ms=after_ms)
        return [adapter.from_alpaca_activity(payload) for payload in payloads]

    async def list_assets(
        self,
        *,
        status: str | None = None,
    ) -> list[BrokerAsset]:
        payloads = await self._client.list_assets(status=status)
        return [adapter.from_alpaca_asset(payload) for payload in payloads]

    async def get_clock_evidence(self) -> BrokerClockEvidence:
        payload = await self._client.get_clock()
        return adapter.from_alpaca_clock(payload)


def register_default_brokers(registry: BrokerRegistry | None = None) -> BrokerRegistry:
    """Register the phase-1 brokers (Alpaca only) into the registry."""
    registry = registry or get_broker_registry()
    registry.register(AlpacaBroker())
    return registry
