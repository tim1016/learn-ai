from __future__ import annotations

import time
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
    EventSubscription,
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
        trusted_root=path.parent,
        load_rows=lambda: list(rows),
        seq_of=lambda row: row.seq,
        poll_interval_seconds=3_600,
        ring_size=ring_size,
        subscriber_queue_size=queue_size,
    )


def _p95_ns(samples: list[int]) -> int:
    ordered = sorted(samples)
    index = max(0, int(len(ordered) * 0.95) - 1)
    return ordered[index]


async def _publish_and_measure(
    channel: DurableEventChannel[_Row],
    subscription: EventSubscription[_Row],
    *,
    seq: int,
) -> int:
    started_ns = time.perf_counter_ns()
    channel.publish(_Row(seq))
    message = await subscription.queue.get()
    elapsed_ns = time.perf_counter_ns() - started_ns
    assert isinstance(message, EventRecord)
    assert message.row.seq == seq
    subscription.acknowledge(message.cursor)
    return elapsed_ns


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
async def test_cursor_immediately_before_ring_replays_contiguous_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text("many\n")
    rows = [_Row(seq) for seq in range(1, 7)]
    channel = _channel(path, rows, ring_size=3)
    channel.start()

    subscription = channel.subscribe(EventCursor(channel.stream_id, 3))
    messages = [await subscription.queue.get() for _ in range(3)]

    assert [message.row.seq for message in messages if isinstance(message, EventRecord)] == [
        4,
        5,
        6,
    ]
    assert all(isinstance(message, EventRecord) for message in messages)
    await channel.stop()


@pytest.mark.asyncio
async def test_ring_replay_uses_exact_queue_capacity_without_gap(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text("four\n")
    rows = [_Row(seq) for seq in range(1, 5)]
    channel = _channel(path, rows, ring_size=4, queue_size=4)
    channel.start()

    subscription = channel.subscribe(EventCursor(channel.stream_id, 0))
    messages = [await subscription.queue.get() for _ in range(4)]

    assert [message.row.seq for message in messages if isinstance(message, EventRecord)] == [
        1,
        2,
        3,
        4,
    ]
    assert subscription.active is True
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
async def test_first_wal_appearance_resets_placeholder_identity_and_survives_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    rows: list[_Row] = []
    channel = _channel(path, rows)
    channel.start()
    placeholder_stream_id = channel.stream_id
    subscription = channel.subscribe(EventCursor(placeholder_stream_id, 0))

    path.write_text("first\n")
    rows.append(_Row(1))
    channel.publish(rows[0])

    reset = await subscription.queue.get()
    assert isinstance(reset, EventReset)
    assert reset.stream_id != placeholder_stream_id
    current_stream_id = reset.stream_id
    replay = channel.subscribe(EventCursor(current_stream_id, 0))
    record = await replay.queue.get()
    assert isinstance(record, EventRecord)
    assert record.row.seq == 1
    await channel.stop()

    restarted = _channel(path, rows)
    restarted.start()
    assert restarted.stream_id == current_stream_id
    await restarted.stop()


@pytest.mark.asyncio
async def test_legacy_backfill_can_exceed_live_queue_capacity(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text("many\n")
    rows = [_Row(seq) for seq in range(1, 8)]
    channel = _channel(path, rows, ring_size=4, queue_size=4)
    channel.start()

    records, subscription = channel.subscribe_with_backfill(0)

    assert [record.row.seq for record in records] == list(range(1, 8))
    assert subscription.active is True
    assert subscription.last_safe_cursor == EventCursor(channel.stream_id, 0)
    assert subscription.queue.empty()
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
async def test_thousand_event_spike_on_one_channel_preserves_sibling_bounds(
    tmp_path: Path,
) -> None:
    hot_path = tmp_path / "hot.jsonl"
    quiet_path = tmp_path / "quiet.jsonl"
    hot_path.write_text("")
    quiet_path.write_text("")
    hot_rows: list[_Row] = []
    quiet_rows: list[_Row] = []
    hot = _channel(hot_path, hot_rows, ring_size=8, queue_size=4)
    quiet = _channel(quiet_path, quiet_rows, ring_size=8, queue_size=4)
    hot.start()
    quiet.start()
    hot_slow = hot.subscribe(None)
    quiet_fast = quiet.subscribe(None)

    for seq in range(1, 1_001):
        hot.publish(_Row(seq))

    hot_gap = await hot_slow.queue.get()
    assert isinstance(hot_gap, EventGap)
    assert hot.resource_limits.ring_size == 8
    assert hot.resource_limits.subscriber_queue_size == 4
    assert hot_slow.queue.qsize() <= hot.resource_limits.subscriber_queue_size
    assert quiet.resource_limits.ring_size == 8
    assert quiet.resource_limits.subscriber_queue_size == 4
    assert quiet_fast.queue.empty()

    quiet.publish(_Row(1))
    quiet_record = await quiet_fast.queue.get()
    assert isinstance(quiet_record, EventRecord)
    assert quiet_record.row.seq == 1

    await hot.stop()
    await quiet.stop()


@pytest.mark.asyncio
async def test_thousand_event_spike_preserves_sibling_p95_latency_budget(
    tmp_path: Path,
) -> None:
    hot_path = tmp_path / "hot.jsonl"
    quiet_path = tmp_path / "quiet.jsonl"
    hot_path.write_text("")
    quiet_path.write_text("")
    hot = _channel(hot_path, [], ring_size=8, queue_size=4)
    quiet = _channel(quiet_path, [], ring_size=8, queue_size=4)
    hot.start()
    quiet.start()
    hot_slow = hot.subscribe(None)
    quiet_fast = quiet.subscribe(None)

    baseline_samples = [
        await _publish_and_measure(quiet, quiet_fast, seq=seq)
        for seq in range(1, 33)
    ]
    baseline_p95_ns = _p95_ns(baseline_samples)
    # The derived budget keeps the proof tied to the local runner while leaving
    # room for CI scheduler jitter unrelated to channel fan-out.
    p95_budget_ns = max(baseline_p95_ns * 20, baseline_p95_ns + 50_000_000)

    spike_samples: list[int] = []
    hot_seq = 0
    for quiet_seq in range(1_001, 1_033):
        for _ in range(1_000):
            hot_seq += 1
            hot.publish(_Row(hot_seq))
        if hot_seq == 1_000:
            assert isinstance(await hot_slow.queue.get(), EventGap)
            assert hot_slow.queue.qsize() <= hot.resource_limits.subscriber_queue_size
            assert hot.resource_limits.subscriber_count == 0
        spike_samples.append(
            await _publish_and_measure(quiet, quiet_fast, seq=quiet_seq)
        )

    spike_p95_ns = _p95_ns(spike_samples)
    assert quiet_fast.queue.empty()
    assert quiet.resource_limits.subscriber_count == 1
    assert quiet.resource_limits.subscriber_queue_size == 4
    assert spike_p95_ns <= p95_budget_ns, (
        "quiet sibling p95 exceeded derived budget under hot-bot spike: "
        f"baseline_p95_ns={baseline_p95_ns}, "
        f"budget_ns={p95_budget_ns}, "
        f"spike_p95_ns={spike_p95_ns}"
    )

    await hot.stop()
    await quiet.stop()


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


def test_channel_rejects_wal_outside_trusted_root(tmp_path: Path) -> None:
    trusted_root = tmp_path / "trusted"
    trusted_root.mkdir()

    with pytest.raises(ValueError, match="escapes root"):
        DurableEventChannel(
            channel_key="bot:events",
            wal_path=tmp_path / "outside.jsonl",
            trusted_root=trusted_root,
            load_rows=list,
            seq_of=lambda row: row.seq,
        )


def test_channel_rechecks_symlink_escape_before_filesystem_access(
    tmp_path: Path,
) -> None:
    trusted_root = tmp_path / "trusted"
    trusted_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    wal_path = trusted_root / "events.jsonl"
    wal_path.write_text("")
    channel = _channel(wal_path, [])

    wal_path.unlink()
    wal_path.symlink_to(outside / "events.jsonl")

    with pytest.raises(ValueError, match="escapes root"):
        channel.refresh()
