"""One upstream producer per compatible instrument, bounded delivery per reader."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Protocol

from app.marketdata.shared.protocol import FeedUnavailable, Frame, Subscription
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)


class Source(Protocol):
    def stream(self, subscription: Subscription) -> AsyncIterator[Frame]: ...


@dataclass(eq=False)
class _Line:
    kind: str
    readers: set[asyncio.Queue[Frame]] = field(default_factory=set)
    producer: asyncio.Task[None] | None = None
    sequence: int = 0


class FeedHub:
    """Slow readers fail separately; source loss terminates that source's readers.

    Joiners receive future callbacks only. Replaying a lossy selection of
    quote ticks cannot reconstruct the side's state or its receipt safely.
    All state changes between awaits belong to this one event loop.
    """

    def __init__(self, source: Source, *, queue_size: int = 256, max_lines: int = 100) -> None:
        if queue_size < 1 or max_lines < 1:
            raise ValueError("Feed limits must be positive")
        self._source, self._queue_size, self._max_lines = source, queue_size, max_lines
        self._lines: dict[Subscription, _Line] = {}

    async def stream(self, subscription: Subscription) -> AsyncIterator[Frame]:
        line = self._lines.get(subscription)
        if line is None:
            if len(self._lines) >= self._max_lines:
                raise FeedUnavailable("Shared feed line capacity exhausted")
            line = self._lines[subscription] = _Line(kind=subscription.kind)
        queue: asyncio.Queue[Frame] = asyncio.Queue(self._queue_size)
        line.readers.add(queue)
        if line.producer is None:
            line.producer = asyncio.create_task(self._pump(subscription, line))
        try:
            while True:
                frame = await queue.get()
                yield frame
                if frame.kind == "error":
                    return
        finally:
            line.readers.discard(queue)
            if not line.readers:
                if self._lines.get(subscription) is line:
                    del self._lines[subscription]
                assert line.producer is not None
                line.producer.cancel()
                await asyncio.gather(line.producer, return_exceptions=True)

    def _publish(self, line: _Line, frame: Frame) -> None:
        line.sequence += 1
        frame = frame.model_copy(update={"sequence": line.sequence})
        for queue in tuple(line.readers):
            if frame.kind == "idle":
                if queue.empty():
                    queue.put_nowait(frame)
                continue
            if frame.kind == "error" and line.kind == "bars" and not queue.full():
                queue.put_nowait(frame)
            elif frame.kind == "error":
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(frame)
            elif queue.full():
                line.readers.remove(queue)
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(
                    Frame(
                        kind="error",
                        epoch=frame.epoch,
                        received_at_ms=now_ms_utc(),
                        reason="Shared feed consumer overflow; continuity is lost",
                    )
                )
                logger.warning("Shared feed consumer overflow", extra={"action": "shared_feed_overflow"})
            else:
                queue.put_nowait(frame)

    async def _pump(self, subscription: Subscription, line: _Line) -> None:
        try:
            async with aclosing(self._source.stream(subscription)) as frames:
                async for frame in frames:
                    self._publish(line, frame)
                    if frame.kind == "error" or not line.readers:
                        return
            raise FeedUnavailable("Shared source ended")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Shared source ended", extra={"action": "shared_feed_source_failed"})
            self._publish(
                line,
                Frame(
                    kind="error",
                    epoch="",
                    received_at_ms=now_ms_utc(),
                    reason=str(exc),
                    cause=exc.cause if isinstance(exc, FeedUnavailable) else None,
                ),
            )
        finally:
            if self._lines.get(subscription) is line:
                del self._lines[subscription]

    async def close(self) -> None:
        for line in self._lines.values():
            self._publish(
                line, Frame(kind="error", epoch="", received_at_ms=now_ms_utc(), reason="Shared feed stopped")
            )
        tasks = [line.producer for line in self._lines.values() if line.producer is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._lines.clear()
