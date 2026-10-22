"""Own IBKR quote/status subscriptions independently of browser lifetimes.

Callbacks own evidence. The one-second supervisor only reconciles demand and
repairs stalled subscriptions; neither polling nor publishing refreshes data.
See ADR 0067 for readiness, halt persistence, and recovery policy.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Hashable
from decimal import Decimal
from inspect import isawaitable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter

from app.broker.ibkr.client import IbkrClient, get_client
from app.broker.ibkr.contracts import qualify_underlying
from app.broker.ibkr.live.protocol import QuoteUpdate
from app.broker.ibkr.live.quote_callbacks import REQUEST_FAILURE_CODES
from app.broker.ibkr.live.quotes import QuoteFailure
from app.broker.ibkr.market_subscription import (
    DECISION_DATA_MAX_AGE_MS,
    SOURCE,
    SUBSCRIPTION_TIMEOUT_MS,
    MarketSubscription,
    retry_delay_ms,
)
from app.options.quotes import HeldQuote, quote_fault
from app.schemas.market_liveness import MarketStatusSnapshot, SymbolTradingStatusEvidence
from app.utils.atomic_file import atomic_write_bytes
from app.utils.timestamps import Clock, now_ms_utc

if TYPE_CHECKING:
    # The option side brings the resolver, the calendar and the pricer with it. A Clerk that watches no
    # option contract never loads them.
    from app.broker.ibkr.option_quotes import LineUse, OptionQuoteLines
    from app.options.resolver import Chain, FeedState

logger = logging.getLogger(__name__)
_HALT_RECORDS = TypeAdapter(dict[str, SymbolTradingStatusEvidence])
_MAX_SUBSCRIPTIONS = 64
_MAX_PARALLEL_QUALIFICATIONS = 4


class IbkrMarketStatusSource:
    """One clerk's subscription supervisor and immutable evidence publisher."""

    def __init__(
        self, *, symbols: Callable[[], tuple[str, ...] | Awaitable[tuple[str, ...]]],
        client: Callable[[], IbkrClient | None] = get_client,
        clock: Clock = now_ms_utc,
        publish: Callable[[MarketStatusSnapshot], None] | None = None,
        halt_path: Path | None = None,
        options: OptionQuoteLines | None = None,
    ) -> None:
        self._symbols, self._client, self._clock = symbols, client, clock
        # The option side of this supervisor (ADR 0076). None: this Clerk watches no option contract.
        self._options = options
        # A step of the option side failed, and no whole pass has gone through since.
        self._option_fault = self._option_fault_this_pass = False
        self._publish_snapshot = publish
        self._halt_path = halt_path
        self._halted = self._load_halts()
        self._pending_halts: dict[str, SymbolTradingStatusEvidence] | None = None
        self._halt_write_task: asyncio.Task[None] | None = None
        self._persistence_failed = False
        self._owner: IbkrClient | None = None
        self._generation: Hashable | None = None
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
        # Desired durable state is separate from the effective safety latch.
        # A failed HALTED write must remain dirty even after its latch is set.
        previous = dict(self._halted if self._pending_halts is None else self._pending_halts)
        records = dict(previous)
        if evidence.state == "HALTED":
            records[evidence.symbol] = records.get(evidence.symbol, evidence)
            self._halted.setdefault(evidence.symbol, evidence)
        elif evidence.state == "TRADABLE":
            records.pop(evidence.symbol, None)
        else:
            return
        if records == previous:
            return
        if self._halt_path is None:
            self._halted = records
            return
        # Retain explicit clears until storage confirms them, including a
        # clear arriving while an older halt write is in flight.
        self._pending_halts = records

    async def _flush_halts(self) -> None:
        if self._halt_write_task is None or self._halt_write_task.done():
            if self._pending_halts is None:
                return
            self._halt_write_task = asyncio.create_task(self._write_halts(self._pending_halts))
        # Cancellation must not discard the owner of an in-flight file write.
        # close() drains this same task before another source can take over.
        await asyncio.shield(self._halt_write_task)

    async def _write_halts(self, records: dict[str, SymbolTradingStatusEvidence]) -> None:
        assert self._halt_path is not None
        try:
            await asyncio.to_thread(atomic_write_bytes, self._halt_path, _HALT_RECORDS.dump_json(records))
        except OSError:
            self._persistence_failed = True
            logger.exception("Cannot persist market halt evidence", extra={"action": "market_halt_persistence_failed"})
        else:
            self._halted = dict(records)
            if self._pending_halts is records:
                self._pending_halts = None
            else:
                # New negative evidence wins over the completed older write.
                # A newer clear still waits for its own durable acknowledgement.
                self._halted.update(self._pending_halts or {})
            self._persistence_failed = False
        self._publish()

    async def close(self) -> None:
        """Fence callbacks, release requests, then drain the owned halt write."""
        self._generation = None
        pending = tuple(self._pending.values())
        for task in pending:
            task.cancel()
        for subscription in self._subscriptions.values():
            self._release(subscription)
        self._subscriptions.clear()
        self._option_side(lambda lines: lines.release())
        self._owner = None
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._pending.clear()
        while self._pending_halts is not None:
            await self._flush_halts()
            if self._persistence_failed:
                break

    def _release(self, subscription: MarketSubscription) -> None:
        lease = subscription.lease
        if lease is None:
            return
        lease.updates -= subscription.callback
        lease.failures -= subscription.failure_callback
        lease.release()

    @staticmethod
    def _client_generation(client: IbkrClient) -> Hashable:
        return client.live_data.status_generation

    async def __call__(self) -> MarketStatusSnapshot:
        await self._flush_halts()
        self._option_fault_this_pass = False
        requested = self._symbols()
        symbols = await requested if isawaitable(requested) else requested
        # An option contract is watched beside its underlying's own quote.
        symbols = (*symbols, *self._option_side(lambda lines: lines.underlyings(), ()))
        self._demand = tuple(dict.fromkeys(symbol.upper() for symbol in symbols))
        client, now = self._client(), self._clock()
        if client is None or not client.live_data.is_connected():
            if client is None or not client.live_data.retains_requests():
                await self.close()
            return self._snapshot(self._clock(), connected=False)
        generation = self._client_generation(client)
        if client is not self._owner or generation != self._generation:
            maintained = (self._owner is client and self._generation is not None
                          and client.live_data.maintains(self._generation))
            if maintained:
                for symbol, previous in tuple(self._subscriptions.items()):
                    if previous.lease is not None:
                        previous.lease.updates -= previous.callback
                        previous.lease.failures -= previous.failure_callback
                        self._subscriptions[symbol] = MarketSubscription(symbol, now, lease=previous.lease)
            else:
                await self.close()
                self._owner = client
            self._generation = generation
            self._connection_changed_at_ms = now
            self._attempts.clear()
            self._retry_at.clear()
            self._option_side(lambda lines: lines.adopt(client, now, maintained=maintained))
            if maintained:
                for subscription in self._subscriptions.values():
                    if subscription.lease is not None:
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
                if subscription.lease is not None and not subscription.failure and now - progress > SUBSCRIPTION_TIMEOUT_MS:
                    self._release(subscription)
                    subscription.lease = None
                    self._retry_at[symbol] = now + self._retry_delay(symbol)
                    logger.warning("Repairing stalled symbol subscription", extra={"action": "market_subscription_stalled", "symbol": symbol})
            if (
                symbol not in self._pending and (subscription is None or subscription.lease is None)
                and now >= self._retry_at.get(symbol, 0)
            ):
                subscription = MarketSubscription(symbol, now)
                self._subscriptions[symbol] = subscription
                self._pending[symbol] = asyncio.create_task(self._subscribe(subscription, client, generation))
        self._option_side(lambda lines: lines.reconcile(client, now, self._underlying_mid))
        if not self._option_fault_this_pass:
            self._option_fault = False
        return self._snapshot(self._clock(), connected=self._connected())

    def _option_side(self, step: Callable[[OptionQuoteLines], Any], otherwise: Any = None) -> Any:
        """Run one step of the option lines, when this Clerk has them. A fault there is logged and never
        takes the stock quotes down with it; the option feed reads unhealthy until a whole pass goes through."""
        if self._options is None:
            return otherwise
        try:
            return step(self._options)
        except Exception:
            self._option_fault = self._option_fault_this_pass = True
            logger.exception("The option quote lines failed a step", extra={"action": "option_lines_step_failed"})
            return otherwise

    def underlying_quote(self, symbol: str) -> HeldQuote | None:
        """The underlying's quote as it was received, in the option feed's generation. None while this
        Clerk watches no option contract, or nothing whole has been received for the symbol."""
        subscription = self._subscriptions.get(symbol.upper())
        if self._options is None or subscription is None or not self._connected():
            return None
        return subscription.held(self._options.generation)

    def option_feed(self) -> FeedState | None:
        """The option feed's generation and health, as a contract resolution is handed them. None while
        this Clerk watches no option contract."""
        if self._options is None:
            return None
        from app.options.resolver import FeedState

        unhealthy = None
        if not self._connected():
            unhealthy = "the IBKR market-data connection is not up"
        elif self._option_fault:
            # The lines may be part way between what was wanted and what is: no decision is made on them.
            unhealthy = "the option quote lines failed a step and have not completed a pass since"
        return FeedState(generation=self._options.generation, unhealthy=unhealthy)

    def option_chain(self, underlying: str) -> Chain | None:
        """The underlying's chain as it is known now, with the quote held for each watched contract. None
        while this Clerk watches no option contract, or the chain's expiries are not known yet."""
        return None if self._options is None else self._options.chain(underlying)

    def option_line_use(self) -> LineUse | None:
        """How this Clerk's option quote lines stand. None while it watches no option contract."""
        return None if self._options is None else self._options.line_use()

    def _underlying_mid(self, symbol: str) -> Decimal | None:
        """Where a window is centred when no bar close is given: the mid of a quote a decision could use."""
        quote = self.underlying_quote(symbol)
        if quote is None or quote.bid is None or quote.ask is None:
            return None
        fault = quote_fault(
            quote,
            at_ms=self._clock(),
            generation=quote.generation,
            max_age_ms=DECISION_DATA_MAX_AGE_MS,
            needs_a_bid=True,
        )
        return None if fault is not None else (quote.bid.price + quote.ask.price) / 2

    def _retry_delay(self, symbol: str) -> int:
        attempt = self._attempts.get(symbol, 0)
        self._attempts[symbol] = attempt + 1
        return retry_delay_ms(attempt)

    async def _subscribe(self, subscription: MarketSubscription, client: IbkrClient, generation: Hashable) -> None:
        symbol = subscription.symbol
        try:
            async with self._qualifications:
                contract = await asyncio.wait_for(qualify_underlying(client, symbol), timeout=3)
                if self._generation != generation or not client.live_data.is_connected():
                    return
                subscription.lease = client.live_data.quote(contract)
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

    def _attach(self, subscription: MarketSubscription, client: IbkrClient, generation: Hashable) -> None:
        def receive(update: QuoteUpdate) -> None:
            if (
                self._subscriptions.get(subscription.symbol) is not subscription
                or self._generation != generation or self._client_generation(client) != generation
                or not client.live_data.is_connected()
            ):
                return
            for transition in subscription.ingest(update):
                self._retain_halt(transition)
            self._publish()

        def failed(failure: QuoteFailure) -> None:
            subscription.update = None
            subscription.bid_priced = subscription.ask_priced = False
            subscription.bid_received_at_ms = subscription.ask_received_at_ms = None
            subscription.trade_timestamp_ms = subscription.trade_received_at_ms = None
            if failure.code in REQUEST_FAILURE_CODES:
                subscription.failure = failure.reason
            self._publish()

        subscription.callback, subscription.failure_callback = receive, failed
        assert subscription.lease is not None
        subscription.lease.updates += receive
        subscription.lease.failures += failed

    def _publish(self) -> None:
        if self._publish_snapshot is not None:
            self._publish_snapshot(self._snapshot(self._clock(), connected=self._connected()))

    def _connected(self) -> bool:
        return (
            self._owner is not None and self._owner.live_data.is_connected()
            and self._generation == self._client_generation(self._owner)
        )

    def _snapshot(self, now: int, *, connected: bool) -> MarketStatusSnapshot:
        subscriptions, statuses, quotes = [], [], []
        admitted = set(self._demand[:_MAX_SUBSCRIPTIONS])
        for symbol in self._demand:
            subscription = self._subscriptions.get(symbol)
            if subscription is None:
                subscription = MarketSubscription(symbol, now, failure="Market-data subscription capacity is unavailable." if symbol not in admitted else None)
            evidence = subscription.evidence(now, recovering=self._attempts.get(symbol, 0) > 0)
            if not connected:
                evidence = evidence.model_copy(update={
                    "state": "RECOVERING", "valid_until_ms": None,
                    "reason_code": "MARKET_DATA_DISCONNECTED",
                    "reason": "Market-data connection is recovering; current evidence is required.",
                })
            if self._persistence_failed and symbol in self._halted:
                evidence = evidence.model_copy(update={
                    "state": "UNAVAILABLE", "valid_until_ms": None,
                    "reason_code": "MARKET_HALT_PERSISTENCE_FAILED",
                    "reason": "Halt evidence could not be saved. Restore clerk storage before trading; the service will retry.",
                })
            subscriptions.append(evidence)
            statuses.append(self._halted.get(symbol) or subscription.reported_status(now))
            if connected and (quote := subscription.quote(now)) is not None:
                quotes.append(quote)
        return MarketStatusSnapshot(
            source=SOURCE, connected=connected, observed_at_ms=now,
            connection_changed_at_ms=self._connection_changed_at_ms,
            symbol_statuses=tuple(statuses), subscriptions=tuple(subscriptions), quotes=tuple(quotes),
        )
