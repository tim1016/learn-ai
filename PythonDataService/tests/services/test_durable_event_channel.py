from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from app.services.durable_event_channel import (
    DurableEventChannel,
    EventCursor,
    EventEnd,
    EventGap,
    EventRecord,
    EventReset,
)


@dataclass(frozen=True)
class _Row:
    seq: int


def _channel(
    path: Path,
    rows: list[_Row],
    *,
    ring_size: int = 4,
    queue_size: int = 4,
) -> DurableEventChannel[_Row]:
    return DurableEventChannel(
        channel_key="bot:events",
        wal_path=path,
        load_rows=lambda: list(rows),
        seq_of=lambda row: row.seq,
        poll_interval_seconds=3_600,
        ring_size=ring_size,
        subscriber_queue_size=queue_size,
    )


@pytest.mark.asyncio
async def test_five_clients_share_one_initial_wal_scan(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text("one\n")
    rows = [_Row(1)]
    channel = _channel(path, rows)
    channel.start()

    subscriptions = []
    for _ in range(5):
        channel.refresh()
        subscriptions.append(channel.subscribe(None))

    assert channel.scan_count == 1
    messages = [await subscription.queue.get() for subscription in subscriptions]
    assert all(isinstance(message, EventRecord) for message in messages)
    await channel.stop()


@pytest.mark.asyncio
async def test_cursor_before_ring_gets_gap_with_last_safe_cursor(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text("many\n")
    rows = [_Row(seq) for seq in range(1, 7)]
    channel = _channel(path, rows, ring_size=3)
    channel.start()

    subscription = channel.subscribe(EventCursor(channel.stream_id, 1))
    message = await subscription.queue.get()

    assert message == EventGap(
        stream_id=channel.stream_id,
        last_safe_cursor=EventCursor(channel.stream_id, 1),
    )
    assert subscription.active is False
    await channel.stop()


@pytest.mark.asyncio
async def test_replacement_emits_reset_and_changes_stream_id(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text("old\n")
    rows = [_Row(1)]
    channel = _channel(path, rows)
    channel.start()
    old_stream_id = channel.stream_id
    subscription = channel.subscribe(EventCursor(old_stream_id, 1))

    replacement = tmp_path / "replacement.jsonl"
    replacement.write_text("new\n")
    replacement.replace(path)
    rows[:] = [_Row(1)]
    channel.refresh()

    message = await subscription.queue.get()
    assert isinstance(message, EventReset)
    assert message.stream_id != old_stream_id
    await channel.stop()


@pytest.mark.asyncio
async def test_owner_publish_detects_wal_replacement_before_fan_out(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text("old\n")
    rows = [_Row(1)]
    channel = _channel(path, rows)
    channel.start()
    old_stream_id = channel.stream_id
    subscription = channel.subscribe(EventCursor(old_stream_id, 1))

    replacement = tmp_path / "replacement.jsonl"
    replacement.write_text("new\n")
    replacement.replace(path)
    rows[:] = [_Row(1), _Row(2)]
    channel.publish(rows[-1])

    message = await subscription.queue.get()
    assert isinstance(message, EventReset)
    assert message.stream_id != old_stream_id
    await channel.stop()


@pytest.mark.asyncio
async def test_queue_overflow_emits_gap_and_isolates_other_clients(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text("")
    rows: list[_Row] = []
    channel = _channel(path, rows, queue_size=2)
    channel.start()
    slow = channel.subscribe(None)
    fast = channel.subscribe(None)

    channel.publish(_Row(1))
    first = await fast.queue.get()
    assert isinstance(first, EventRecord)
    fast.acknowledge(first.cursor)
    channel.publish(_Row(2))
    second = await fast.queue.get()
    assert isinstance(second, EventRecord)
    channel.publish(_Row(3))

    assert await slow.queue.get() == EventGap(
        stream_id=channel.stream_id,
        last_safe_cursor=EventCursor(channel.stream_id, 0),
    )
    third = await fast.queue.get()
    assert isinstance(third, EventRecord)
    assert third.row.seq == 3
    await channel.stop()


@pytest.mark.asyncio
async def test_cursor_ahead_of_channel_does_not_receive_older_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text("")
    channel = _channel(path, [])
    channel.start()
    subscription = channel.subscribe(EventCursor(channel.stream_id, 5))

    channel.publish(_Row(1))

    assert subscription.queue.empty()
    await channel.stop()


@pytest.mark.asyncio
async def test_stop_closes_clients_and_post_stop_subscriptions(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text("")
    channel = _channel(path, [])
    channel.start()
    existing = channel.subscribe(None)

    await channel.stop(timeout_seconds=0.1)
    after_stop = channel.subscribe(None)

    assert isinstance(await existing.queue.get(), EventEnd)
    assert isinstance(await after_stop.queue.get(), EventEnd)


def test_composite_cursor_round_trip_and_validation() -> None:
    cursor = EventCursor("streamabc", 42)

    assert EventCursor.parse(cursor.encode()) == cursor
    with pytest.raises(ValueError):
        EventCursor.parse("42")
