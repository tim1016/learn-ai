"""Router tests for ``/api/live-instances/{sid}/broker-activity``.

Tests focus on the routing + serialization contract; the publisher's
internal behavior is covered in ``tests/services/test_broker_activity_publisher.py``.

We bypass the auto-bootstrap path (which requires a connected IBKR
client + on-disk live envelope) by pre-registering a fully-built
publisher in the module-level registry. This isolates router tests
from broker / filesystem fixtures.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.schemas.broker_activity import (
    BrokerActivityRow,
    ReconciliationTimingPolicy,
    Verdict,
)
from app.services.broker_activity_publisher import BrokerActivityPublisher
from app.services.broker_activity_publisher_registry import get_publisher_registry
from app.services.broker_activity_wal import (
    BrokerActivityWal,
    instance_broker_activity_wal_path,
)

pytestmark = pytest.mark.asyncio


SID = "sid-router-test"
NS = f"learn-ai/{SID}/v1"


def _row(seq: int, exec_id: str | None = None) -> BrokerActivityRow:
    return BrokerActivityRow(
        seq=seq,
        ts_ms=1_700_000_000_000 + seq,
        exec_id=exec_id or f"exec-{seq}",
        perm_id=999,
        order_ref=f"{NS}:intent-{seq}",
        symbol="SPY",
        side="BUY",
        quantity=100.0,
        price=450.0,
        order_type="MKT",
        verdict=Verdict.EXPECTED,
        template_key="normal_fill",
        template_version=1,
        headline=f"row-{seq}",
        narrative=f"row-{seq}",
    )


async def _empty_source() -> AsyncIterator:
    """Event source that yields nothing then sleeps. The router tests
    seed the WAL directly so the publisher never needs to consume."""
    if False:
        yield  # type: ignore[unreachable]
    await asyncio.sleep(3600)


def _seed_publisher(tmp_path: Path, rows: list[BrokerActivityRow]) -> BrokerActivityPublisher:
    """Build a publisher with a pre-populated WAL and no event source."""
    artifacts = tmp_path / "artifacts"
    run_dir = tmp_path / "run-dir"
    # Seed envelope so the publisher's sidecar reads don't fail.
    from app.engine.live.live_state_sidecar import (
        LiveStateEnvelope,
        LiveStateSidecarRepo,
        stable_live_state_path,
    )

    envelope = LiveStateEnvelope(
        strategy_instance_id=SID,
        run_id="run-router",
        bot_order_namespace=NS,
        ib_client_id=42,
        last_processed_bar_ms=1,
        last_artifact_flush_ms=1,
    )
    repo = LiveStateSidecarRepo(stable_live_state_path(artifacts, SID))
    repo._path.parent.mkdir(parents=True, exist_ok=True)
    repo.write(envelope)
    # Seed WAL.
    wal = BrokerActivityWal(instance_broker_activity_wal_path(artifacts, SID))
    for row in rows:
        wal.allocate_seq()
        wal.append_row(row)

    publisher = BrokerActivityPublisher(
        strategy_instance_id=SID,
        bot_order_namespace=NS,
        run_dir=run_dir,
        artifacts_root=artifacts,
        timing_policy=ReconciliationTimingPolicy(),
        event_source_factory=lambda: _empty_source(),
    )
    return publisher


async def _make_app():
    """Build a fresh FastAPI app with only the broker_activity router
    mounted. Avoids the full ``app.main`` import surface (broker client,
    polygon settings, etc.)."""
    from fastapi import FastAPI

    from app.routers import broker_activity as router_module

    app = FastAPI()
    app.include_router(router_module.router)
    return app


@pytest.fixture
def fresh_registry():
    """Snapshot and restore the module-level publisher registry so each
    test starts with no publishers and other tests aren't polluted."""
    registry = get_publisher_registry()
    snapshot = dict(registry._by_instance)
    registry._by_instance.clear()
    yield registry
    # Best-effort sync cleanup; the publishers we register in tests use
    # the empty-source factory and stop quickly under cancel.
    registry._by_instance.clear()
    registry._by_instance.update(snapshot)


async def test_backfill_returns_seeded_rows(
    tmp_path: Path, fresh_registry
) -> None:
    publisher = _seed_publisher(tmp_path, [_row(1), _row(2)])
    await fresh_registry.register(publisher, strategy_instance_id=SID)

    app = await _make_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/api/live-instances/{SID}/broker-activity")
        assert resp.status_code == 200
        payload = resp.json()
        assert [r["seq"] for r in payload["rows"]] == [1, 2]
        # All rows returned, no next_seq.
        assert payload["next_seq"] is None
        assert payload["durable_stream_id"] == publisher.event_channel.stream_id
        assert payload["high_water_cursor"].endswith(":2")

    await publisher.stop()


async def test_backfill_paginates_via_after_seq_and_limit(
    tmp_path: Path, fresh_registry
) -> None:
    """``next_seq`` is the cursor the caller passes verbatim as the next
    ``after_seq`` — equal to the highest seq returned in the page. No
    off-by-one arithmetic on the client side."""
    publisher = _seed_publisher(
        tmp_path, [_row(1), _row(2), _row(3), _row(4), _row(5)]
    )
    await fresh_registry.register(publisher, strategy_instance_id=SID)

    app = await _make_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/api/live-instances/{SID}/broker-activity",
            params={"after_seq": 0, "limit": 2},
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert [r["seq"] for r in payload["rows"]] == [1, 2]
        # next_seq = highest seq in the page (NOT seq + 1).
        assert payload["next_seq"] == 2
        assert payload["next_cursor"].endswith(":2")

        # The composite cursor is the identity-safe pagination contract.
        resp = await client.get(
            f"/api/live-instances/{SID}/broker-activity",
            params={"cursor": payload["next_cursor"], "limit": 2},
        )
        payload = resp.json()
        assert [r["seq"] for r in payload["rows"]] == [3, 4]
        assert payload["next_seq"] == 4

    await publisher.stop()


async def test_backfill_rejects_replaced_stream_cursor(
    tmp_path: Path, fresh_registry
) -> None:
    publisher = _seed_publisher(tmp_path, [_row(1)])
    await fresh_registry.register(publisher, strategy_instance_id=SID)

    app = await _make_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/api/live-instances/{SID}/broker-activity",
            params={"cursor": "replaced-stream:0"},
        )

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "EVENT_STREAM_REPLACED"
    await publisher.stop()


async def test_backfill_rejects_negative_after_seq(
    tmp_path: Path, fresh_registry
) -> None:
    publisher = _seed_publisher(tmp_path, [_row(1)])
    await fresh_registry.register(publisher, strategy_instance_id=SID)

    app = await _make_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/api/live-instances/{SID}/broker-activity",
            params={"after_seq": -1},
        )
        assert resp.status_code == 422

    await publisher.stop()


async def test_sse_stream_emits_end_event_when_publisher_stops(
    tmp_path: Path, fresh_registry
) -> None:
    publisher = _seed_publisher(tmp_path, [])
    await fresh_registry.register(publisher, strategy_instance_id=SID)

    app = await _make_app()
    transport = ASGITransport(app=app)

    async def _collect_stream():
        async with AsyncClient(transport=transport, base_url="http://test") as client, client.stream(
            "GET", f"/api/live-instances/{SID}/broker-activity/stream"
        ) as resp:
            assert resp.status_code == 200
            text = ""
            async for chunk in resp.aiter_text():
                text += chunk
                if "event: end" in text:
                    return text
            return text

    collector = asyncio.create_task(_collect_stream())
    # Let the subscriber register, then stop the publisher so the SSE
    # handler emits "event: end" and the stream closes.
    await asyncio.sleep(0.05)
    await publisher.stop()
    text = await asyncio.wait_for(collector, timeout=2.0)
    assert "event: end" in text


async def test_sse_stream_backfills_from_since_seq_then_goes_live(
    tmp_path: Path, fresh_registry
) -> None:
    """Standard cockpit flow: SSE subscription with ``since_seq=<N>``
    drains every WAL row with ``seq > N`` first (the backfill), then
    transitions seamlessly into live rows. The transition is gap-free
    because the queue is subscribed BEFORE the drain begins.

    We stop the publisher after pushing the live row so the SSE handler
    emits ``event: end`` and the ASGITransport flushes its buffer —
    otherwise httpx may hold the stream open indefinitely.
    """
    publisher = _seed_publisher(tmp_path, [_row(1), _row(2), _row(3)])
    await fresh_registry.register(publisher, strategy_instance_id=SID)
    stream_id = publisher.event_channel.stream_id

    app = await _make_app()
    transport = ASGITransport(app=app)

    async def _collect_stream():
        async with AsyncClient(transport=transport, base_url="http://test") as client, client.stream(
            "GET",
            f"/api/live-instances/{SID}/broker-activity/stream",
            params={"cursor": f"{stream_id}:1"},
        ) as resp:
            assert resp.status_code == 200
            text = ""
            async for chunk in resp.aiter_text():
                text += chunk
                if "event: end" in text:
                    return text
            return text

    collector = asyncio.create_task(_collect_stream())
    # Let the subscriber register and drain backfill.
    await asyncio.sleep(0.05)
    # Push one live row via the publisher's broadcast surface.
    live_row = _row(4, exec_id="live-after-backfill")
    wal = BrokerActivityWal(
        instance_broker_activity_wal_path(tmp_path / "artifacts", SID)
    )
    wal.allocate_seq()
    wal.append_row(live_row)
    publisher.event_channel.publish(live_row)
    await asyncio.sleep(0.05)
    # Stop the publisher so the SSE handler emits "event: end" and the
    # collector loop terminates.
    await publisher.stop()

    text = await asyncio.wait_for(collector, timeout=2.0)
    # Parse the row order out of the SSE text.
    import re

    seqs = [int(s) for s in re.findall(r'"seq":(\d+)', text)]
    # Backfill drained rows 2 and 3 (since_seq=1 excludes seq=1), then
    # live row 4 appeared. No duplicates.
    assert seqs == [2, 3, 4], f"unexpected seq order in stream: {seqs}"
    assert f"id: {stream_id}:2" in text
    assert f"id: {stream_id}:4" in text


async def test_sse_stream_delivers_owner_published_row_after_ring_replay(
    tmp_path: Path, fresh_registry
) -> None:
    """Last-Event-ID replays the ring, then continues with owner publishes."""
    publisher = _seed_publisher(tmp_path, [_row(1), _row(2)])
    await fresh_registry.register(publisher, strategy_instance_id=SID)
    stream_id = publisher.event_channel.stream_id

    racing_row = _row(3, exec_id="racing-row")
    wal = BrokerActivityWal(
        instance_broker_activity_wal_path(tmp_path / "artifacts", SID)
    )
    wal.allocate_seq()
    wal.append_row(racing_row)

    app = await _make_app()
    transport = ASGITransport(app=app)

    async def _collect_stream():
        async with AsyncClient(transport=transport, base_url="http://test") as client, client.stream(
            "GET",
            f"/api/live-instances/{SID}/broker-activity/stream",
            params={"cursor": f"{stream_id}:0"},
            headers={"Last-Event-ID": f"{stream_id}:0"},
        ) as resp:
            assert resp.status_code == 200
            text = ""
            async for chunk in resp.aiter_text():
                text += chunk
                if "event: end" in text:
                    return text
            return text

    collector = asyncio.create_task(_collect_stream())
    await asyncio.sleep(0.05)
    publisher.event_channel.publish(racing_row)
    await asyncio.sleep(0.05)
    # Stop the publisher so the SSE handler emits "event: end".
    await publisher.stop()

    text = await asyncio.wait_for(collector, timeout=2.0)
    import re

    seqs = [int(s) for s in re.findall(r'"seq":(\d+)', text)]
    assert seqs == [1, 2, 3]


async def test_sse_reconnect_prefers_newer_last_event_id_over_query_cursor(
    tmp_path: Path, fresh_registry
) -> None:
    publisher = _seed_publisher(tmp_path, [_row(1), _row(2)])
    await fresh_registry.register(publisher, strategy_instance_id=SID)
    stream_id = publisher.event_channel.stream_id
    app = await _make_app()
    transport = ASGITransport(app=app)

    async def _collect_stream():
        async with AsyncClient(transport=transport, base_url="http://test") as client, client.stream(
            "GET",
            f"/api/live-instances/{SID}/broker-activity/stream",
            params={"cursor": f"{stream_id}:0"},
            headers={"Last-Event-ID": f"{stream_id}:1"},
        ) as resp:
            assert resp.status_code == 200
            return await resp.aread()

    collector = asyncio.create_task(_collect_stream())
    await asyncio.sleep(0.05)
    await publisher.stop()
    text = (await asyncio.wait_for(collector, timeout=2.0)).decode()

    assert '"seq":1' not in text
    assert '"seq":2' in text


async def test_sse_overflow_gap_then_rest_backfill_recovers_missed_rows(
    tmp_path: Path, fresh_registry
) -> None:
    """Tracer for the full overflow-recovery loop through real HTTP.

    A publish burst that outruns the subscriber queue must terminate the
    stream with ``event: gap`` carrying ``last_safe_cursor``; REST backfill
    from that cursor must return every missed row; a fresh subscription at
    the backfill high-water mark must then serve live rows again.
    """
    publisher = _seed_publisher(tmp_path, [_row(1), _row(2)])
    await fresh_registry.register(publisher, strategy_instance_id=SID)
    stream_id = publisher.event_channel.stream_id
    queue_size = publisher.event_channel.resource_limits.subscriber_queue_size

    app = await _make_app()
    transport = ASGITransport(app=app)

    async def _collect_stream():
        async with AsyncClient(transport=transport, base_url="http://test") as client, client.stream(
            "GET",
            f"/api/live-instances/{SID}/broker-activity/stream",
            params={"cursor": f"{stream_id}:0"},
        ) as resp:
            assert resp.status_code == 200
            text = ""
            async for chunk in resp.aiter_text():
                text += chunk
                if "event: gap" in text:
                    return text
            return text

    collector = asyncio.create_task(_collect_stream())
    # Let the subscriber drain the two seeded ring rows.
    await asyncio.sleep(0.05)
    # Publish a burst larger than the subscriber queue without yielding to
    # the event loop, so the SSE generator cannot drain between publishes.
    wal = BrokerActivityWal(
        instance_broker_activity_wal_path(tmp_path / "artifacts", SID)
    )
    burst = [_row(seq) for seq in range(3, 3 + queue_size + 8)]
    for row in burst:
        wal.allocate_seq()
        wal.append_row(row)
    for row in burst:
        publisher.event_channel.publish(row)

    text = await asyncio.wait_for(collector, timeout=2.0)
    assert "event: gap" in text
    import json as json_module
    import re

    gap_payload = json_module.loads(
        re.search(r"event: gap\ndata: (.+)\n", text).group(1)
    )
    last_safe = gap_payload["last_safe_cursor"]
    assert last_safe.startswith(f"{stream_id}:")
    safe_seq = int(last_safe.rpartition(":")[2])
    assert safe_seq >= 2

    # REST backfill from the gap's safe cursor recovers every missed row.
    recovered: list[int] = []
    cursor = last_safe
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        while cursor is not None:
            resp = await client.get(
                f"/api/live-instances/{SID}/broker-activity",
                params={"cursor": cursor, "limit": 50},
            )
            assert resp.status_code == 200
            payload = resp.json()
            recovered.extend(r["seq"] for r in payload["rows"])
            cursor = payload["next_cursor"] if payload["rows"] else None
    assert recovered == list(range(safe_seq + 1, burst[-1].seq + 1))

    # A fresh subscription at the recovered high-water mark goes live again.
    async def _collect_resubscribe():
        async with AsyncClient(transport=transport, base_url="http://test") as client, client.stream(
            "GET",
            f"/api/live-instances/{SID}/broker-activity/stream",
            params={"cursor": f"{stream_id}:{burst[-1].seq}"},
        ) as resp:
            assert resp.status_code == 200
            return await resp.aread()

    resubscriber = asyncio.create_task(_collect_resubscribe())
    await asyncio.sleep(0.05)
    live_seq = burst[-1].seq + 1
    live_row = _row(live_seq, exec_id="post-gap-live")
    wal.allocate_seq()
    wal.append_row(live_row)
    publisher.event_channel.publish(live_row)
    await asyncio.sleep(0.05)
    await publisher.stop()
    tail_text = (await asyncio.wait_for(resubscriber, timeout=2.0)).decode()
    assert "event: gap" not in tail_text
    assert f'"seq":{live_seq}' in tail_text


async def test_sse_mid_stream_wal_replacement_emits_reset_and_closes(
    tmp_path: Path, fresh_registry
) -> None:
    """WAL identity change mid-stream must surface ``event: reset`` through
    HTTP and close the stream so the client re-bootstraps."""
    publisher = _seed_publisher(tmp_path, [_row(1), _row(2)])
    await fresh_registry.register(publisher, strategy_instance_id=SID)
    old_stream_id = publisher.event_channel.stream_id

    app = await _make_app()
    transport = ASGITransport(app=app)

    async def _collect_stream():
        async with AsyncClient(transport=transport, base_url="http://test") as client, client.stream(
            "GET",
            f"/api/live-instances/{SID}/broker-activity/stream",
            params={"cursor": f"{old_stream_id}:0"},
        ) as resp:
            assert resp.status_code == 200
            text = ""
            async for chunk in resp.aiter_text():
                text += chunk
                if "event: reset" in text:
                    return text
            return text

    collector = asyncio.create_task(_collect_stream())
    await asyncio.sleep(0.05)

    # Replace the WAL with a new file (new inode) as an external rotation.
    wal_path = instance_broker_activity_wal_path(tmp_path / "artifacts", SID)
    replacement_path = tmp_path / "replacement-wal.jsonl"
    replacement = BrokerActivityWal(replacement_path)
    replacement.allocate_seq()
    replacement.append_row(_row(1, exec_id="post-replacement"))
    import os as os_module

    os_module.replace(replacement_path, wal_path)
    publisher.event_channel.refresh()

    text = await asyncio.wait_for(collector, timeout=2.0)
    assert "event: reset" in text
    new_stream_id = publisher.event_channel.stream_id
    assert new_stream_id != old_stream_id
    assert f'"durable_stream_id": "{new_stream_id}"' in text
    await publisher.stop()


async def test_legacy_sse_deep_backfills_beyond_queue_capacity(
    tmp_path: Path, fresh_registry
) -> None:
    publisher = _seed_publisher(tmp_path, [_row(seq) for seq in range(1, 71)])
    await fresh_registry.register(publisher, strategy_instance_id=SID)
    app = await _make_app()
    transport = ASGITransport(app=app)

    async def _collect_stream():
        async with AsyncClient(transport=transport, base_url="http://test") as client, client.stream(
            "GET",
            f"/api/live-instances/{SID}/broker-activity/stream",
            params={"since_seq": 0},
        ) as resp:
            assert resp.status_code == 200
            return await resp.aread()

    collector = asyncio.create_task(_collect_stream())
    await asyncio.sleep(0.05)
    await publisher.stop()
    text = (await asyncio.wait_for(collector, timeout=2.0)).decode()

    import re

    assert "event: gap" not in text
    assert [int(seq) for seq in re.findall(r'"seq":(\d+)', text)] == list(
        range(1, 71)
    )
