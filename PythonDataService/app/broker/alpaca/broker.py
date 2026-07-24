"""AlpacaBroker — the read-port and trade-port implementation (Broker System v2).

Composes the SDK client, the adapter, and the capability descriptor into a
single object implementing both :class:`BrokerReadPort` and (from phase 2)
:class:`BrokerTradePort`. Each slice adds one method here; the router and Clerk
only ever see contract models.

The port is cheap to construct (the underlying client builds credentials and
network lazily), so it can be registered at startup without keys.
"""

from __future__ import annotations

from app.broker.alpaca import adapter
from app.broker.alpaca.client import AlpacaTradingClient
from app.broker.alpaca.config import BROKER_ID
from app.broker.contract.capabilities import BrokerCapabilities
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import (
    BrokerAccountSnapshot,
    BrokerActivity,
    BrokerAsset,
    BrokerClockEvidence,
    BrokerOrder,
    BrokerOrderLeg,
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
    """Alpaca implementation of :class:`BrokerReadPort` and :class:`BrokerTradePort`."""

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
        limit: int = 100,
    ) -> list[BrokerActivity]:
        if after_ms is None:
            payloads = await self._client.list_activities(limit=limit)
            return [adapter.from_alpaca_activity(payload) for payload in payloads]

        # The vendor page cursor is ordered by a different timestamp than our
        # occurred_at_ms contract cursor. Continue through every vendor page,
        # then apply the contract filter and output cap.
        activities: list[BrokerActivity] = []
        page_token: str | None = None
        seen_page_tokens: set[str] = set()
        while True:
            payloads = await self._client.list_activities(
                limit=limit,
                page_token=page_token,
            )
            activities.extend(
                activity
                for activity in (adapter.from_alpaca_activity(payload) for payload in payloads)
                if activity.occurred_at_ms is not None and activity.occurred_at_ms >= after_ms
            )
            if len(payloads) < limit:
                break

            next_page_token = payloads[-1].get("id")
            if not isinstance(next_page_token, str) or not next_page_token:
                raise BrokerUnavailable(
                    "Alpaca activity pagination could not continue.",
                    broker=self.broker_id,
                    detail="A full activity page did not include a usable activity ID.",
                )
            if next_page_token in seen_page_tokens:
                raise BrokerUnavailable(
                    "Alpaca activity pagination repeated a page token.",
                    broker=self.broker_id,
                    detail="Stopping to avoid returning incomplete or duplicated activities.",
                )
            seen_page_tokens.add(next_page_token)
            page_token = next_page_token

        return activities[:limit]

    async def list_assets(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[BrokerAsset]:
        payloads = await self._client.list_assets(status=status, limit=limit)
        return [adapter.from_alpaca_asset(payload) for payload in payloads]

    async def get_clock_evidence(self) -> BrokerClockEvidence:
        payload = await self._client.get_clock()
        return adapter.from_alpaca_clock(payload)

    # ── Trade port (phase 2) ────────────────────────────────────────────────

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        """Submit one equity leg; map the accepted order to a ``BrokerOrder``.

        The vendor request body is built by the adapter (contract → vendor),
        POSTed over the capturing session, and the raw response mapped back
        (vendor → contract). A vendor rejection raises a ``BrokerError`` the
        Clerk journals as ``submit_failed``.
        """
        body = adapter.to_alpaca_order_request(leg, client_order_id=client_order_id)
        payload = await self._client.submit_order(body)
        return adapter.from_alpaca_order(payload)

    async def cancel(self, order_id: str) -> None:
        """Cancel one working order by its broker-assigned id (S3).

        Delegates to the SDK client's ``DELETE /v2/orders/{order_id}``. A
        non-cancelable order (already filled/canceled) surfaces as a vendor 422,
        which ``map_api_error`` translates to a ``BrokerError`` the Clerk
        journals as ``cancel_failed``. There is no order body to map back —
        Alpaca returns 204 — so this returns ``None``.
        """
        await self._client.cancel_order(order_id)

    async def get_order_by_client_order_id(
        self, client_order_id: str
    ) -> BrokerOrder | None:
        """Look an order up by the ``client_order_id`` the Clerk minted (S5).

        Resolves an uncertain submit: the Clerk asks whether the order it may
        have sent actually landed. Delegates to the SDK client's
        ``GET /v2/orders:by_client_order_id`` and maps the raw payload to a
        ``BrokerOrder`` when found. Returns ``None`` when Alpaca reports the
        order definitively absent (HTTP 404 — it never landed); a
        ``BrokerUnavailable`` from the client (timeout / 5xx / network) keeps the
        outcome uncertain and propagates so the Clerk leaves the intent
        unresolved rather than fabricating a terminal state.
        """
        payload = await self._client.get_order_by_client_order_id(client_order_id)
        if payload is None:
            return None
        return adapter.from_alpaca_order(payload)


def register_default_brokers(registry: BrokerRegistry | None = None) -> BrokerRegistry:
    """Register the phase-1 brokers (Alpaca only) into the registry."""
    registry = registry or get_broker_registry()
    registry.register(AlpacaBroker())
    return registry
