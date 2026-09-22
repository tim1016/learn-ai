"""Own IBKR quote/status subscriptions independently of browser lifetimes.

Callbacks own evidence. The one-second supervisor only reconciles demand and
repairs stalled subscriptions; neither polling nor publishing refreshes data.
See ADR 0067 for readiness, halt persistence, and recovery policy.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from copy import copy
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from app.broker.ibkr.client import IbkrClient, get_client
from app.broker.ibkr.contracts import qualify_underlying
from app.broker.ibkr.market_subscription import SOURCE, SUBSCRIPTION_TIMEOUT_MS, MarketSubscription
from app.schemas.market_liveness import MarketStatusSnapshot, SymbolTradingStatusEvidence
from app.utils.atomic_file import atomic_write_bytes
from app.utils.timestamps import Clock, now_ms_utc

logger = logging.getLogger(__name__)
_HALT_RECORDS = TypeAdapter(dict[str, SymbolTradingStatusEvidence])
_MAX_SUBSCRIPTIONS = 64
_MAX_PARALLEL_QUALIFICATIONS = 4
_MAX_RETRY_MS = 30_000
_REQUEST_FAILURE_CODES = frozenset({101, 200, 354, 10089, 10090, 10167, 10186})


class IbkrMarketStatusSource:
    """One clerk's subscription supervisor and immutable evidence publisher."""

    def __init__(
        self, *, symbols: Callable[[], tuple[str, ...]],
        client: Callable[[], IbkrClient | None] = get_client,
        clock: Clock = now_ms_utc,
        publish: Callable[[MarketStatusSnapshot], None] | None = None,
        halt_path: Path | None = None,
    ) -> None:
        self._symbols, self._client, self._clock = symbols, client, clock
        self._publish_snapshot = publish
        self._halt_path = halt_path
        self._halted = self._load_halts()
        self._persistence_failed = False
        self._owner: IbkrClient | None = None
        self._generation: tuple[int, int, int, int] | None = None
        self._subscriptions: dict[str, MarketSubscription] = {}
        self._pending: dict[str, asyncio.Task[None]] = {}
        self._attempts: dict[str, int] = {}
        self._retry_at: dict[str, int] = {}
        self._demand: tuple[str, ...] = ()
        self._connection_changed_at_ms = 0
        self._qualifications = asyncio.Semaphore(_MAX_PARALLEL_QUALIFICATIONS)

    def _load_halts(self) -> dict[str, SymbolTradingStatusEvidence]:
        if self._halt_path is None:
            return {}
        try:
            records = _HALT_RECORDS.validate_json(self._halt_path.read_bytes())
        except FileNotFoundError:
            return {}
        if any(key != record.symbol or record.state != "HALTED" or record.source != SOURCE for key, record in records.items()):
            raise ValueError("Invalid retained IBKR halt evidence.")
        return records

    def _retain_halt(self, evidence: SymbolTradingStatusEvidence) -> None:
        records = dict(self._halted)
        if evidence.state == "HALTED":
            records[evidence.symbol] = records.get(evidence.symbol, evidence)
        elif evidence.state == "TRADABLE":
            records.pop(evidence.symbol, None)
        if records == self._halted:
            return
        try:
            if self._halt_path is not None:
                atomic_write_bytes(self._halt_path, _HALT_RECORDS.dump_json(records))
        except OSError:
            self._persistence_failed = True
            # A failed write can never clear a known halt or authorize entry.
            if evidence.state == "HALTED":
                self._halted[evidence.symbol] = evidence
            logger.exception("Cannot persist market halt evidence", extra={"action": "market_halt_persistence_failed"})
            return
        self._halted = records
        self._persistence_failed = False

    async def close(self) -> None:
        """Release our own requests and tasks, retaining durable halt facts."""
        for task in self._pending.values():
            task.cancel()
        if self._pending:
            await asyncio.gather(*self._pending.values(), return_exceptions=True)
        self._pending.clear()
        for subscription in self._subscriptions.values():
            self._release(subscription)
        self._subscriptions.clear()
        if self._owner is not None:
            self._owner.ib.errorEvent -= self._on_error
        self._owner, self._generation = None, None

    def _release(self, subscription: MarketSubscription) -> None:
        ticker = subscription.ticker
        if ticker is None:
            return
        ticker.updateEvent -= subscription.callback
        if self._owner is not None and self._owner.is_connected():
            self._owner.ib.cancelMktData(ticker.contract)

    @staticmethod
    def _client_generation(client: IbkrClient) -> tuple[int, int, int, int]:
        return id(client), client.connection_generation, client.connectivity_lost_count, client.last_event_ms

    async def __call__(self) -> MarketStatusSnapshot:
        client, now = self._client(), self._clock()
        self._demand = tuple(dict.fromkeys(symbol.upper() for symbol in self._symbols()))
        if client is None or not client.is_connected():
            await self.close()
            return self._snapshot(now, connected=False)
        if client.connection_state != "connected":
            # 1100 may recover as 1102 (data maintained). Keep ownership while
            # refusing all data; the restore transition fences prior receipts.
            if client.connection_state != "soft_lost":
                await self.close()
            return self._snapshot(now, connected=False)
        generation = self._client_generation(client)
        if generation != self._generation:
            maintained = (
                self._owner is client and self._generation is not None
                and self._generation[:2] == generation[:2] and client.last_ibkr_code == 1102
            )
            if maintained:
                for symbol, previous in tuple(self._subscriptions.items()):
                    if previous.ticker is not None:
                        previous.ticker.updateEvent -= previous.callback
                        self._subscriptions[symbol] = MarketSubscription(symbol, now, ticker=previous.ticker)
            else:
                await self.close()
                self._owner = client
                client.ib.errorEvent += self._on_error
            self._generation = generation
            self._connection_changed_at_ms = now
            self._attempts.clear()
            self._retry_at.clear()
            if maintained:
                for subscription in self._subscriptions.values():
                    if subscription.ticker is not None:
                        self._attach(subscription, client, generation)
        admitted = self._demand[:_MAX_SUBSCRIPTIONS]
        for symbol in tuple(self._subscriptions):
            if symbol not in admitted:
                task = self._pending.pop(symbol, None)
                if task is not None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                self._release(self._subscriptions.pop(symbol))
                self._attempts.pop(symbol, None)
                self._retry_at.pop(symbol, None)
        for symbol in admitted:
            subscription = self._subscriptions.get(symbol)
            if subscription is not None:
                if subscription.evidence(now).state == "READY":
                    self._attempts[symbol] = 0
                progress = subscription.last_received_at_ms or subscription.started_at_ms
                if subscription.ticker is not None and not subscription.failure and now - progress > SUBSCRIPTION_TIMEOUT_MS:
                    self._release(subscription)
                    subscription.ticker = None
                    self._retry_at[symbol] = now + self._retry_delay(symbol)
                    logger.warning("Repairing stalled symbol subscription", extra={"action": "market_subscription_stalled", "symbol": symbol})
            if (
                symbol not in self._pending and (subscription is None or subscription.ticker is None)
                and now >= self._retry_at.get(symbol, 0)
            ):
                subscription = MarketSubscription(symbol, now)
                self._subscriptions[symbol] = subscription
                self._pending[symbol] = asyncio.create_task(self._subscribe(subscription, client, generation))
        return self._snapshot(now, connected=True)

    def _retry_delay(self, symbol: str) -> int:
        attempt = self._attempts.get(symbol, 0)
        self._attempts[symbol] = attempt + 1
        return min(1_000 * 2 ** min(attempt, 5), _MAX_RETRY_MS)

    async def _subscribe(self, subscription: MarketSubscription, client: IbkrClient, generation: tuple[int, int, int, int]) -> None:
        symbol = subscription.symbol
        try:
            async with self._qualifications:
                contract = await asyncio.wait_for(qualify_underlying(client, symbol), timeout=3)
                if self._generation != generation or client.connection_state != "connected":
                    return
                # ib_async keys market-data requests by contract object identity.
                # A dedicated object prevents another consumer cancelling ours.
                subscription.ticker = client.ib.reqMktData(copy(contract), "233", False, False)
                self._attach(subscription, client, generation)
        except asyncio.CancelledError:
            raise
        except Exception:
            subscription.failure = "IBKR could not establish this symbol's subscription; retrying."
            self._retry_at[symbol] = self._clock() + self._retry_delay(symbol)
            logger.exception("Market subscription failed", extra={"action": "market_subscription_failed", "symbol": symbol})
        finally:
            self._pending.pop(symbol, None)
            self._publish()

    def _attach(self, subscription: MarketSubscription, client: IbkrClient, generation: tuple[int, int, int, int]) -> None:
        def receive(ticker: Any) -> None:
            if (
                self._subscriptions.get(subscription.symbol) is not subscription
                or self._generation != generation or self._client_generation(client) != generation
                or client.connection_state != "connected"
            ):
                return
            for transition in subscription.ingest(ticker, self._clock()):
                self._retain_halt(transition)
            self._publish()
        subscription.callback = receive
        subscription.ticker.updateEvent += receive
        request_id = client.ib.wrapper.ticker2ReqId["mktData"].get(subscription.ticker)
        subscription.request_id = request_id if isinstance(request_id, int) else None

    def _on_error(self, req_id: int, code: int, _message: str, _contract: Any) -> None:
        if code in _REQUEST_FAILURE_CODES:
            for subscription in self._subscriptions.values():
                if subscription.request_id == req_id:
                    subscription.failure = "IBKR refused this symbol's live market-data subscription. Check permissions and instrument availability."
        # Connectivity callbacks invalidate a READY view immediately. The
        # supervisor performs the async repair on its next independent tick.
        self._publish()

    def _publish(self) -> None:
        if self._publish_snapshot is not None:
            connected = self._owner is not None and self._owner.connection_state == "connected" and self._generation == self._client_generation(self._owner)
            self._publish_snapshot(self._snapshot(self._clock(), connected=connected))

    def _snapshot(self, now: int, *, connected: bool) -> MarketStatusSnapshot:
        subscriptions, statuses, quotes = [], [], []
        for symbol in self._demand:
            subscription = self._subscriptions.get(symbol)
            if subscription is None:
                subscription = MarketSubscription(symbol, now, failure="Market-data subscription capacity is unavailable." if symbol not in self._demand[:_MAX_SUBSCRIPTIONS] else None)
            evidence = subscription.evidence(now, recovering=self._attempts.get(symbol, 0) > 0)
            if not connected:
                evidence = evidence.model_copy(update={
                    "state": "RECOVERING", "valid_until_ms": None,
                    "reason_code": "MARKET_DATA_DISCONNECTED",
                    "reason": "Market-data connection is recovering; current evidence is required.",
                })
            if self._persistence_failed:
                evidence = evidence.model_copy(update={
                    "state": "UNAVAILABLE", "valid_until_ms": None,
                    "reason_code": "MARKET_DATA_UNAVAILABLE",
                    "reason": "Halt evidence could not be saved. Restore clerk storage before trading.",
                })
            subscriptions.append(evidence)
            statuses.append(self._halted.get(symbol) or subscription.reported_status(now))
            if connected and not self._persistence_failed and (quote := subscription.quote(now)) is not None:
                quotes.append(quote)
        return MarketStatusSnapshot(
            source=SOURCE, connected=connected, observed_at_ms=now,
            connection_changed_at_ms=self._connection_changed_at_ms,
            symbol_statuses=tuple(statuses), subscriptions=tuple(subscriptions), quotes=tuple(quotes),
        )
