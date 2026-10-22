"""A worker's view of the shared source, independent of its history socket."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import TYPE_CHECKING

from app.marketdata.shared.protocol import FeedUnavailable, Frame, Instrument, Subscription
from app.marketdata.shared.transport import FeedTransport
from app.utils.timestamps import now_ms_utc

if TYPE_CHECKING:
    from app.broker.ibkr.config import IbkrSettings

logger = logging.getLogger(__name__)


class SharedConnection:
    def __init__(self, settings: IbkrSettings) -> None:
        assert settings.shared_feed_url is not None and settings.shared_feed_token is not None
        self.settings = settings
        self._transport = FeedTransport(str(settings.shared_feed_url), settings.shared_feed_token.get_secret_value())
        self._epoch: tuple[int, str, int, int, int] | None = None
        self._generation = 0
        self._generations: dict[str, int] = {}
        self._last_health = float("-inf")
        self._connected = False
        self._poll: asyncio.Task[None] | None = None
        self.bar_receipts: dict[str, int] = {}
        self.underlyings: dict[str, Instrument] = {}

    @property
    def connection_generation(self) -> int:
        return self._generation

    def is_connected(self) -> bool:
        return self._connected and time.monotonic() - self._last_health < 5

    def _observe_epoch(self, value: str) -> None:
        try:
            boot, generation, lost, connectivity = value.split(":")
            started, _ = boot.split("-", 1)
            epoch = (int(started), boot, int(generation), int(lost), int(connectivity))
        except ValueError as exc:
            raise FeedUnavailable("Shared source epoch is invalid") from exc
        if self._epoch is not None and epoch < self._epoch:
            raise FeedUnavailable("Shared source frame belongs to a retired generation")
        if epoch != self._epoch:
            self._epoch = epoch
            self._generation += 1
            if len(self._generations) >= 64:
                self._generations.pop(next(iter(self._generations)))
            self._generations[value] = self._generation

    def bar_generation(self, epoch: str) -> int:
        # A running bar stream may drain real pre-disconnect observations
        # after the health poll has seen the replacement socket (ADR 0053).
        # Quotes never use this allowance.
        if epoch not in self._generations:
            self._observe_epoch(epoch)
        return self._generations[epoch]

    def start(self) -> None:
        if self._poll is None:
            self._poll = asyncio.create_task(self._poll_health())

    async def close(self) -> None:
        self._connected = False
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
                    if subscription.kind == "bars":
                        self.bar_generation(frame.epoch)
                    else:
                        self._observe_epoch(frame.epoch)
                yield frame
