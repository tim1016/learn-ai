"""A worker's view of the shared source, independent of its history socket."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Hashable
from contextlib import aclosing
from typing import TYPE_CHECKING, Any

from app.broker.ibkr.live.protocol import FeedUnavailable, Frame, Instrument, SourceEpoch, Subscription
from app.broker.ibkr.live.quotes import QuoteFailure, QuoteLease
from app.broker.ibkr.live.source import BarRequest, RawBarReceipt
from app.broker.ibkr.live.transport import FeedTransport
from app.utils.timestamps import now_ms_utc

if TYPE_CHECKING:
    from app.broker.ibkr.config import IbkrSettings

logger = logging.getLogger(__name__)


class SharedConnection:
    def __init__(self, settings: IbkrSettings, *, qualify: Callable[[str], Awaitable[Instrument]]) -> None:
        assert settings.shared_feed_url is not None and settings.shared_feed_token is not None
        self.settings = settings
        self._qualify = qualify
        self._quote_tasks: set[asyncio.Task[None]] = set()
        self._underlyings: dict[str, Instrument] = {}
        self._transport = FeedTransport(str(settings.shared_feed_url), settings.shared_feed_token.get_secret_value())
        self._epoch: SourceEpoch | None = None
        self._generation = 0
        self._generations: dict[SourceEpoch, int] = {}
        self._last_health = float("-inf")
        self._connected = False
        self._poll: asyncio.Task[None] | None = None

    @property
    def connection_generation(self) -> int:
        return self._generation

    def is_connected(self) -> bool:
        return self._connected and time.monotonic() - self._last_health < 5

    def _observe_epoch(self, epoch: SourceEpoch) -> None:
        if self._epoch is not None and epoch.order_key < self._epoch.order_key:
            raise FeedUnavailable("Shared source frame belongs to a retired generation")
        if epoch != self._epoch:
            self._epoch = epoch
            self._generation += 1
            if len(self._generations) >= 64:
                self._generations.pop(next(iter(self._generations)))
            self._generations[epoch] = self._generation

    def bar_generation(self, epoch: SourceEpoch) -> int:
        # A running bar stream may drain real pre-disconnect observations
        # after the health poll has seen the replacement socket (ADR 0053).
        # Quotes never use this allowance.
        if epoch not in self._generations:
            self._observe_epoch(epoch)
        return self._generations[epoch]

    @property
    def status_generation(self) -> Hashable:
        return self._generation

    def maintains(self, previous: Hashable) -> bool:
        return False

    def retains_requests(self) -> bool:
        return False

    def ready(self, recovery_healthy: bool) -> bool:
        return self.is_connected()

    async def history_recovered(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Worker history recovery has no authority over the shared live source."""

    def raw_bars(self, symbol: str, request: BarRequest) -> AsyncIterator[RawBarReceipt | None]:
        from app.broker.ibkr.live.bars import shared_raw_bars

        return shared_raw_bars(self, symbol, **vars(request))

    async def underlying(self, symbol: str, timeout: float) -> Instrument:

        identity = self._underlyings.get(symbol)
        if identity is None:
            identity = await asyncio.wait_for(self._qualify(symbol), timeout=timeout)
            if len(self._underlyings) >= 256:
                self._underlyings.pop(next(iter(self._underlyings)))
            self._underlyings[symbol] = identity
        return identity

    def quote(self, contract: Any) -> QuoteLease:
        from app.broker.ibkr.live.protocol import Instrument

        key = Subscription(
            kind="quotes",
            instrument=Instrument(
                con_id=int(contract.conId),
                exchange=contract.exchange,
                currency=contract.currency,
                sec_type=contract.secType,
            ),
        )
        lease = QuoteLease(lambda: task.cancel())
        task = asyncio.create_task(self._receive_quotes(lease, key))
        self._quote_tasks.add(task)
        task.add_done_callback(self._quote_tasks.discard)
        return lease

    async def _receive_quotes(self, lease: QuoteLease, key: Subscription) -> None:
        epoch: SourceEpoch | None = None
        retry_s = 1
        try:
            while True:
                try:
                    async with aclosing(self.stream(key)) as frames:
                        async for frame in frames:
                            if frame.kind == "error":
                                lease.fail(QuoteFailure(frame.reason, frame.error_code))
                                raise FeedUnavailable(frame.reason)
                            if frame.epoch != epoch:
                                lease.fail(QuoteFailure("Shared quote source generation changed"))
                                epoch = frame.epoch
                            if frame.quote is not None:
                                lease.publish(frame.quote)
                                retry_s = 1
                    raise FeedUnavailable("Shared quote stream ended")
                except FeedUnavailable as exc:
                    lease.fail(QuoteFailure(str(exc)))
                    logger.warning(
                        "Shared quote unavailable",
                        extra={
                            "action": "shared_quote_unavailable",
                            "con_id": key.instrument.con_id,
                            "reason": str(exc),
                        },
                    )
                    await asyncio.sleep(retry_s)
                    retry_s = min(retry_s * 2, 30)
        finally:
            lease.fail(QuoteFailure("Shared quote lease closed"))

    def start(self) -> None:
        if self._poll is None:
            self._poll = asyncio.create_task(self._poll_health())

    async def close(self) -> None:
        self._connected = False
        tasks = tuple(self._quote_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._poll is not None:
            self._poll.cancel()
            await asyncio.gather(self._poll, return_exceptions=True)
            self._poll = None

    async def _poll_health(self) -> None:
        while True:
            try:
                health = await self._transport.health()
                self._observe_epoch(health.epoch)
                self._connected = health.connected and 0 <= now_ms_utc() - health.observed_at_ms < 5000
                self._last_health = time.monotonic()
            except FeedUnavailable as exc:
                self._connected = False
                logger.warning(
                    "Shared source health unavailable",
                    extra={"action": "shared_source_health_failed", "reason": str(exc)},
                )
            await asyncio.sleep(1)

    async def stream(self, subscription: Subscription) -> AsyncIterator[Frame]:
        async with aclosing(self._transport.stream(subscription)) as frames:
            async for frame in frames:
                if frame.kind != "error":
                    assert frame.epoch is not None
                    if subscription.kind == "bars":
                        self.bar_generation(frame.epoch)
                    else:
                        self._observe_epoch(frame.epoch)
                yield frame
