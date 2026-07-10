"""Read/project bot-event WAL history for HTTP consumers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from app.schemas.bot_events import BotEventRow
from app.services.bot_event_projection import project_bot_event_rows
from app.services.bot_event_wal import (
    BotEventRawWal,
    BotEventWalCorruptError,
    run_bot_event_wal_path,
)
from app.services.durable_event_channel import (
    DurableEventChannel,
    EventEnd,
    EventRecord,
)


class BotEventStreamUnavailableError(RuntimeError):
    """Raised when persisted bot-event history cannot be projected."""


@dataclass(frozen=True)
class BotEventStreamPage:
    rows: list[BotEventRow]
    next_seq: int | None


class BotEventStreamService:
    """Backfill authored bot-event rows from a run-scoped raw WAL."""

    def __init__(self) -> None:
        self._channels: dict[Path, DurableEventChannel[BotEventRow]] = {}

    def _load_rows(self, run_dir: Path) -> list[BotEventRow]:
        try:
            raw_events = BotEventRawWal(
                run_bot_event_wal_path(run_dir), trusted_root=run_dir
            ).read_all()
            return sorted(
                project_bot_event_rows(raw_events),
                key=lambda row: row.seq,
            )
        except (BotEventWalCorruptError, ValueError) as exc:
            raise BotEventStreamUnavailableError(
                "bot-event stream history cannot be projected"
            ) from exc

    def _new_channel(self, key: Path) -> DurableEventChannel[BotEventRow]:
        return DurableEventChannel(
            channel_key=f"bot-events:{key.name}",
            wal_path=run_bot_event_wal_path(key),
            trusted_root=key,
            load_rows=lambda: self._load_rows(key),
            seq_of=lambda row: row.seq,
        )

    def channel_for_run(
        self,
        run_dir: Path,
    ) -> DurableEventChannel[BotEventRow]:
        return self._new_channel(run_dir.resolve())

    def live_channel_for_run(
        self,
        run_dir: Path,
    ) -> DurableEventChannel[BotEventRow]:
        key = run_dir.resolve()
        channel = self._channels.get(key)
        if channel is None:
            channel = self._new_channel(key)
            self._channels[key] = channel
        try:
            channel.start()
        except Exception:
            self._channels.pop(key, None)
            raise
        return channel

    async def release_live_channel(
        self,
        run_dir: Path,
        channel: DurableEventChannel[BotEventRow],
    ) -> None:
        key = run_dir.resolve()
        if self._channels.get(key) is not channel or channel.subscriber_count:
            return
        self._channels.pop(key, None)
        await channel.stop()

    async def stop_all(self) -> None:
        channels = list(self._channels.values())
        self._channels.clear()
        await asyncio.gather(*(channel.stop() for channel in channels))

    def backfill_run(
        self,
        *,
        run_dir: Path,
        after_seq: int,
        limit: int,
    ) -> BotEventStreamPage:
        rows = self._load_rows(run_dir)

        remaining = [row for row in rows if row.seq > after_seq]
        page_rows = remaining[:limit]
        next_seq = page_rows[-1].seq if len(remaining) > len(page_rows) else None
        return BotEventStreamPage(rows=page_rows, next_seq=next_seq)

    async def stream_run(
        self,
        *,
        run_dir: Path,
        since_seq: int,
        poll_interval_s: float,
        page_limit: int = 500,
    ) -> AsyncIterator[BotEventRow]:
        """Yield ring history after ``since_seq``, then follow live rows."""

        del poll_interval_s, page_limit
        channel = self.live_channel_for_run(run_dir)
        records, subscription = channel.subscribe_with_backfill(since_seq)
        try:
            for record in records:
                yield record.row
                subscription.acknowledge(record.cursor)
            while True:
                message = await subscription.queue.get()
                if isinstance(message, EventRecord):
                    subscription.acknowledge(message.cursor)
                    yield message.row
                elif isinstance(message, EventEnd):
                    return
                else:
                    return
        finally:
            channel.unsubscribe(subscription)
            await self.release_live_channel(run_dir, channel)


_SERVICE = BotEventStreamService()


def get_bot_event_stream_service() -> BotEventStreamService:
    return _SERVICE


__all__ = [
    "BotEventStreamPage",
    "BotEventStreamService",
    "BotEventStreamUnavailableError",
    "get_bot_event_stream_service",
]
