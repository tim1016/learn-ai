"""One WAL observer, bounded ring, and bounded fan-out per event channel."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

from app.engine.live.identity import confine_path_to_root

logger = logging.getLogger(__name__)

RowT = TypeVar("RowT")


@dataclass(frozen=True)
class EventCursor:
    stream_id: str
    seq: int

    def encode(self) -> str:
        return f"{self.stream_id}:{self.seq}"

    @classmethod
    def parse(cls, value: str | None) -> EventCursor | None:
        if value is None or not value.strip():
            return None
        stream_id, separator, raw_seq = value.rpartition(":")
        if not separator or not stream_id or not raw_seq.isdigit():
            raise ValueError("event cursor must be '<durable_stream_id>:<seq>'")
        return cls(stream_id=stream_id, seq=int(raw_seq))


@dataclass(frozen=True)
class EventRecord(Generic[RowT]):  # noqa: UP046 - Python 3.11 runtime.
    cursor: EventCursor
    row: RowT


@dataclass(frozen=True)
class EventReset:
    stream_id: str


@dataclass(frozen=True)
class EventGap:
    stream_id: str
    last_safe_cursor: EventCursor


@dataclass(frozen=True)
class EventEnd:
    pass


EventMessage = EventRecord[RowT] | EventReset | EventGap | EventEnd


def event_message_sse(  # noqa: UP047 - Python 3.11 runtime.
    message: EventMessage[RowT],
    *,
    encode_row: Callable[[RowT], str],
) -> str:
    if isinstance(message, EventRecord):
        return (
            f"id: {message.cursor.encode()}\n"
            f"event: row\n"
            f"data: {encode_row(message.row)}\n\n"
        )
    if isinstance(message, EventReset):
        return "event: reset\ndata: " + json.dumps(
            {"durable_stream_id": message.stream_id}
        ) + "\n\n"
    if isinstance(message, EventGap):
        return "event: gap\ndata: " + json.dumps(
            {
                "durable_stream_id": message.stream_id,
                "last_safe_cursor": message.last_safe_cursor.encode(),
            }
        ) + "\n\n"
    return "event: end\ndata: {}\n\n"


@dataclass(eq=False)
class EventSubscription(Generic[RowT]):  # noqa: UP046 - Python 3.11 runtime.
    queue: asyncio.Queue[EventMessage[RowT]]
    last_safe_cursor: EventCursor
    active: bool = True

    def acknowledge(self, cursor: EventCursor) -> None:
        self.last_safe_cursor = cursor


@dataclass(frozen=True)
class DurableEventChannelResourceLimits:
    """Bounded memory/resource shape for one durable event channel."""

    ring_size: int
    subscriber_queue_size: int
    subscriber_count: int


class DurableEventChannel(Generic[RowT]):  # noqa: UP046 - Python 3.11 runtime.
    """Observe one WAL once and serve all clients from one bounded ring."""

    def __init__(
        self,
        *,
        channel_key: str,
        wal_path: Path,
        trusted_root: Path,
        load_rows: Callable[[], list[RowT]],
        seq_of: Callable[[RowT], int],
        poll_interval_seconds: float = 0.25,
        ring_size: int = 512,
        subscriber_queue_size: int = 64,
    ) -> None:
        if ring_size < 1 or subscriber_queue_size < 2:
            raise ValueError("event channel bounds must be positive")
        self.channel_key = channel_key
        self._wal_path = wal_path
        self._trusted_root = trusted_root
        self._safe_wal_path()
        self._load_rows = load_rows
        self._seq_of = seq_of
        self._poll_interval_seconds = poll_interval_seconds
        self._ring: deque[EventRecord[RowT]] = deque(maxlen=ring_size)
        self._subscriber_queue_size = subscriber_queue_size
        self._subscribers: set[EventSubscription[RowT]] = set()
        self._signature = self._wal_signature()
        self._stream_id = self._identity_stream_id()
        self._last_seq = 0
        self._history_truncated = False
        self._stop_event = asyncio.Event()
        self._tailer_task: asyncio.Task[None] | None = None
        self.scan_count = 0

    @property
    def stream_id(self) -> str:
        return self._stream_id

    @property
    def last_cursor(self) -> EventCursor:
        return EventCursor(self._stream_id, self._last_seq)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def resource_limits(self) -> DurableEventChannelResourceLimits:
        return DurableEventChannelResourceLimits(
            ring_size=self._ring.maxlen or 0,
            subscriber_queue_size=self._subscriber_queue_size,
            subscriber_count=len(self._subscribers),
        )

    def refresh(self) -> None:
        """Synchronize WAL identity and newly durable rows when changed."""

        self._refresh()

    def start(self) -> None:
        if self._tailer_task is not None and not self._tailer_task.done():
            return
        self._stop_event.clear()
        self._refresh(force=True)
        self._tailer_task = asyncio.create_task(
            self._tail_loop(),
            name=f"durable-event-channel:{self.channel_key}",
        )

    async def stop(self, *, timeout_seconds: float = 2.0) -> None:
        self._stop_event.set()
        task = self._tailer_task
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(task, timeout=timeout_seconds)
        self._tailer_task = None
        for subscription in tuple(self._subscribers):
            self._terminate(subscription, EventEnd())
        self._subscribers.clear()

    def subscribe(self, cursor: EventCursor | None) -> EventSubscription[RowT]:
        queue: asyncio.Queue[EventMessage[RowT]] = asyncio.Queue(
            maxsize=self._subscriber_queue_size
        )
        baseline = cursor or EventCursor(self._stream_id, 0)
        subscription = EventSubscription(queue=queue, last_safe_cursor=baseline)
        if self._stop_event.is_set():
            subscription.active = False
            queue.put_nowait(EventEnd())
            return subscription

        if cursor is not None and cursor.stream_id != self._stream_id:
            queue.put_nowait(EventReset(stream_id=self._stream_id))
            baseline = EventCursor(self._stream_id, 0)
            subscription.last_safe_cursor = baseline

        rows = [record for record in self._ring if record.cursor.seq > baseline.seq]
        oldest_seq = self._ring[0].cursor.seq if self._ring else self._last_seq + 1
        needs_backfill = (
            self._history_truncated and baseline.seq + 1 < oldest_seq
        )
        if needs_backfill or len(rows) > self._subscriber_queue_size - queue.qsize():
            queue.put_nowait(
                EventGap(
                    stream_id=self._stream_id,
                    last_safe_cursor=subscription.last_safe_cursor,
                )
            )
            subscription.active = False
            return subscription

        for record in rows:
            queue.put_nowait(record)
        self._subscribers.add(subscription)
        return subscription

    def subscribe_with_backfill(
        self,
        after_seq: int,
    ) -> tuple[list[EventRecord[RowT]], EventSubscription[RowT]]:
        """Deep-backfill a legacy sequence cursor, then subscribe live.

        Composite-cursor clients use the bounded ring and explicit gap
        recovery. Until Stage 4 replaces sequence-only browser clients, this
        compatibility path preserves their former full-history behavior while
        subscribing at the durable high-water mark before rows are emitted.
        """

        for _attempt in range(3):
            self._refresh()
            stream_id = self._stream_id
            rows = sorted(self._load_rows(), key=self._seq_of)
            self.scan_count += 1
            records = [
                EventRecord(
                    cursor=EventCursor(stream_id, self._seq_of(row)),
                    row=row,
                )
                for row in rows
                if self._seq_of(row) > after_seq
            ]
            self._refresh()
            if stream_id == self._stream_id:
                break
        else:
            raise RuntimeError("durable event WAL identity did not stabilize")

        high_water_seq = records[-1].cursor.seq if records else after_seq
        subscription = self.subscribe(EventCursor(stream_id, high_water_seq))
        subscription.last_safe_cursor = EventCursor(stream_id, after_seq)
        return records, subscription

    def unsubscribe(self, subscription: EventSubscription[RowT]) -> None:
        subscription.active = False
        self._subscribers.discard(subscription)

    def publish(self, row: RowT) -> None:
        """Publish a row already durably appended by the channel owner."""

        seq = self._seq_of(row)
        signature = self._wal_signature()
        identity_appeared = self._signature is None and signature is not None
        replaced = (
            self._signature is not None
            and signature is not None
            and signature[:2] != self._signature[:2]
        )
        truncated = (
            self._signature is not None
            and signature is not None
            and signature[:2] == self._signature[:2]
            and signature[2] < self._signature[2]
        )
        if identity_appeared or replaced or truncated:
            self._refresh(force=True)
        if seq <= self._last_seq:
            return
        self._append_record(row)
        self._signature = signature

    def _refresh(self, *, force: bool = False) -> None:
        signature = self._wal_signature()
        if not force and signature == self._signature:
            return
        rows = sorted(self._load_rows(), key=self._seq_of)
        self.scan_count += 1
        identity_changed = (
            self._signature is not None
            and signature is not None
            and signature[:2] != self._signature[:2]
        )
        identity_appeared = self._signature is None and signature is not None
        truncated = (
            self._signature is not None
            and signature is not None
            and signature[:2] == self._signature[:2]
            and signature[2] < self._signature[2]
        )
        last_on_disk = self._seq_of(rows[-1]) if rows else 0
        sequence_replaced = self._last_seq > 0 and last_on_disk < self._last_seq
        if identity_appeared or identity_changed or truncated or sequence_replaced:
            nonce = signature if truncated or sequence_replaced else None
            self._stream_id = self._identity_stream_id(nonce=nonce)
            self._ring.clear()
            self._last_seq = 0
            self._history_truncated = False
            self._broadcast_control(EventReset(stream_id=self._stream_id))

        new_rows = [row for row in rows if self._seq_of(row) > self._last_seq]
        if self._last_seq == 0 and len(new_rows) > self._ring.maxlen:
            self._history_truncated = True
            new_rows = new_rows[-self._ring.maxlen :]
        for row in new_rows:
            self._append_record(row)
        self._signature = signature

    def _append_record(self, row: RowT) -> None:
        seq = self._seq_of(row)
        if len(self._ring) == self._ring.maxlen:
            self._history_truncated = True
        record = EventRecord(cursor=EventCursor(self._stream_id, seq), row=row)
        self._ring.append(record)
        self._last_seq = seq
        for subscription in tuple(self._subscribers):
            if (
                subscription.last_safe_cursor.stream_id == self._stream_id
                and seq <= subscription.last_safe_cursor.seq
            ):
                continue
            try:
                subscription.queue.put_nowait(record)
            except asyncio.QueueFull:
                self._terminate(
                    subscription,
                    EventGap(
                        stream_id=self._stream_id,
                        last_safe_cursor=subscription.last_safe_cursor,
                    ),
                )

    def _broadcast_control(self, message: EventReset) -> None:
        for subscription in tuple(self._subscribers):
            self._terminate(subscription, message)

    def _terminate(
        self,
        subscription: EventSubscription[RowT],
        message: EventMessage[RowT],
    ) -> None:
        while not subscription.queue.empty():
            subscription.queue.get_nowait()
        subscription.queue.put_nowait(message)
        subscription.active = False
        self._subscribers.discard(subscription)

    async def _tail_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._poll_interval_seconds,
                )
            except TimeoutError:
                try:
                    self._refresh()
                except Exception:
                    logger.exception(
                        "durable event channel refresh failed",
                        extra={"channel_key": self.channel_key},
                    )

    def _wal_signature(self) -> tuple[int, int, int, int] | None:
        # Keep the realpath confinement adjacent to the filesystem sink. The
        # shared helper enforces the same invariant at construction and for
        # identity stamping, while this local proof lets CodeQL verify that a
        # route-derived instance id cannot influence ``stat`` outside the
        # service-owned root.
        root_real = os.path.realpath(os.fspath(self._trusted_root))
        candidate = os.path.realpath(os.fspath(self._wal_path))
        root_prefix = root_real.rstrip(os.sep) + os.sep
        if not candidate.startswith(root_prefix):
            raise ValueError(
                f"durable event channel WAL path {candidate} escapes root {root_real}"
            )
        try:
            stat = os.stat(candidate)
        except FileNotFoundError:
            return None
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)

    def _identity_stream_id(self, *, nonce: object | None = None) -> str:
        signature = self._wal_signature()
        identity = signature[:2] if signature is not None else (0, 0)
        raw = f"{self.channel_key}|{self._safe_wal_path()}|{identity}|{nonce or ''}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def _safe_wal_path(self) -> Path:
        return confine_path_to_root(
            self._wal_path,
            self._trusted_root,
            label="durable event channel WAL",
        )


__all__ = [
    "DurableEventChannel",
    "DurableEventChannelResourceLimits",
    "EventCursor",
    "EventEnd",
    "EventGap",
    "EventRecord",
    "EventReset",
    "EventSubscription",
    "event_message_sse",
]
