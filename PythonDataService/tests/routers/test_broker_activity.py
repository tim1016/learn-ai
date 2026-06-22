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
from app.services.broker_activity_publisher import (
    BrokerActivityPublisher,
    get_publisher_registry,
)
from app.services.broker_activity_wal import (
    BrokerActivityWal,
    stable_broker_activity_wal_path,
)

pytestmark = pytest.mark.asyncio


SID = "sid-router-test"
NS = f"learn-ai/{SID}/v1"


def _row(seq: int, exec_id: str = None) -> BrokerActivityRow:
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
    wal = BrokerActivityWal(stable_broker_activity_wal_path(run_dir))
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
    # Restore for the next test (stop anything we left running).
    for sid in list(registry._by_instance):
        # Best-effort sync cleanup; the publishers we register in tests
        # use the empty-source factory and stop quickly under cancel.
        pass
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

    await publisher.stop()


async def test_backfill_paginates_via_after_seq_and_limit(
    tmp_path: Path, fresh_registry
) -> None:
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
        assert payload["next_seq"] == 3  # caller can resume here

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
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            async with client.stream(
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
