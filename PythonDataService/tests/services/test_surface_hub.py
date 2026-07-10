"""ADR-0028 Stage 2 tests for producer-owned versioned snapshots."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from app.services.surface_hub import SnapshotUnavailableError, SurfaceHub, SurfaceHubRegistry


class _Snapshot(BaseModel):
    stream_epoch: str = ""
    surface_version: int = 0
    fetched_at_ms: int = 0
    generated_at_ms: int
    source_state: str
    evidence_at_ms: int


def _snapshot(*, generated_at_ms: int, source_state: str = "FRESH") -> _Snapshot:
    return _Snapshot(
        generated_at_ms=generated_at_ms,
        source_state=source_state,
        evidence_at_ms=1_700_000_000_000,
    )


@pytest.mark.asyncio
async def test_surface_hub_preserves_payload_and_adds_identity() -> None:
    original = _snapshot(generated_at_ms=1_700_000_000_100)
    hub = SurfaceHub(
        strategy_instance_id="bot-a",
        assemble=lambda: asyncio.sleep(0, result=original),
        process_epoch="data-plane-a",
    )

    stored = await hub.refresh()

    assert stored.model_dump(exclude={"stream_epoch", "surface_version"}) == original.model_dump(
        exclude={"stream_epoch", "surface_version"}
    )
    assert stored.stream_epoch.startswith("data-plane-a:")
    assert stored.surface_version == 1


@pytest.mark.asyncio
async def test_identical_semantics_do_not_advance_surface_version() -> None:
    fetched_at_ms = 1_700_000_000_100

    async def assemble() -> _Snapshot:
        nonlocal fetched_at_ms
        value = _snapshot(generated_at_ms=1_700_000_000_100).model_copy(update={"fetched_at_ms": fetched_at_ms})
        fetched_at_ms += 1_000
        return value

    hub = SurfaceHub(strategy_instance_id="bot-a", assemble=assemble)

    first = await hub.refresh()
    second = await hub.refresh()

    assert first.surface_version == 1
    assert second.surface_version == 1
    assert second.fetched_at_ms > first.fetched_at_ms


@pytest.mark.asyncio
async def test_source_timestamp_advances_surface_version() -> None:
    generated_at_ms = 1_700_000_000_100

    async def assemble() -> _Snapshot:
        nonlocal generated_at_ms
        value = _snapshot(generated_at_ms=generated_at_ms)
        generated_at_ms += 1_000
        return value

    hub = SurfaceHub(strategy_instance_id="bot-a", assemble=assemble)

    first = await hub.refresh()
    second = await hub.refresh()

    assert first.surface_version == 1
    assert second.surface_version == 2


@pytest.mark.asyncio
async def test_freshness_transition_advances_surface_version() -> None:
    source_state = "FRESH"

    async def assemble() -> _Snapshot:
        return _snapshot(
            generated_at_ms=1_700_000_001_000,
            source_state=source_state,
        )

    hub = SurfaceHub(strategy_instance_id="bot-a", assemble=assemble)
    first = await hub.refresh()
    source_state = "STALE"
    second = await hub.refresh()

    assert first.surface_version == 1
    assert second.surface_version == 2


@pytest.mark.asyncio
async def test_new_producer_lifecycle_changes_stream_epoch() -> None:
    async def assemble() -> _Snapshot:
        return _snapshot(generated_at_ms=1_700_000_000_100)

    first = SurfaceHub(
        strategy_instance_id="bot-a",
        assemble=assemble,
        process_epoch="data-plane-a",
    )
    second = SurfaceHub(
        strategy_instance_id="bot-a",
        assemble=assemble,
        process_epoch="data-plane-a",
    )

    assert (await first.refresh()).stream_epoch != (await second.refresh()).stream_epoch


@pytest.mark.asyncio
async def test_restarting_same_hub_changes_epoch_and_resets_version() -> None:
    async def assemble() -> _Snapshot:
        return _snapshot(generated_at_ms=1_700_000_000_100)

    hub = SurfaceHub(
        strategy_instance_id="bot-a",
        assemble=assemble,
        process_epoch="data-plane-a",
        refresh_interval_seconds=3_600,
    )
    await hub.start()
    first = await hub.snapshot()
    await hub.stop(timeout_seconds=0.1)
    await hub.start()
    second = await hub.snapshot()
    await hub.stop(timeout_seconds=0.1)

    assert second.stream_epoch != first.stream_epoch
    assert second.surface_version == 1


@pytest.mark.asyncio
async def test_concurrent_refreshes_share_one_assembly_cycle() -> None:
    calls = 0
    release = asyncio.Event()

    async def assemble() -> _Snapshot:
        nonlocal calls
        calls += 1
        await release.wait()
        return _snapshot(generated_at_ms=1_700_000_000_100)

    hub = SurfaceHub(strategy_instance_id="bot-a", assemble=assemble)
    first = asyncio.create_task(hub.refresh())
    second = asyncio.create_task(hub.refresh())
    await asyncio.sleep(0)
    release.set()

    assert await first == await second
    assert calls == 1


@pytest.mark.asyncio
async def test_snapshot_observer_is_producer_owned_and_stop_is_bounded() -> None:
    observations = 0

    async def on_snapshot(_snapshot: _Snapshot) -> None:
        nonlocal observations
        observations += 1

    hub = SurfaceHub(
        strategy_instance_id="bot-a",
        assemble=lambda: asyncio.sleep(
            0,
            result=_snapshot(generated_at_ms=1_700_000_000_100),
        ),
        on_snapshot=on_snapshot,
        refresh_interval_seconds=3_600,
    )

    await hub.start()
    await hub.snapshot()
    await hub.snapshot()
    await hub.stop(timeout_seconds=0.1)

    assert observations == 1
    assert hub.is_running is False


@pytest.mark.asyncio
async def test_initial_failure_keeps_producer_running_and_retries() -> None:
    calls = 0

    async def assemble() -> _Snapshot:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("source unavailable")
        return _snapshot(generated_at_ms=1_700_000_000_100)

    hub = SurfaceHub(
        strategy_instance_id="bot-a",
        assemble=assemble,
        refresh_interval_seconds=0.01,
    )

    await hub.start()
    assert hub.is_running is True
    with pytest.raises(SnapshotUnavailableError):
        await hub.snapshot()
    for _ in range(20):
        if hub.latest is not None:
            break
        await asyncio.sleep(0.01)
    await hub.stop(timeout_seconds=0.1)

    assert calls >= 2
    assert hub.latest is not None


@pytest.mark.asyncio
async def test_snapshot_observer_failure_is_retried_by_producer() -> None:
    observations = 0

    async def on_snapshot(_snapshot: _Snapshot) -> None:
        nonlocal observations
        observations += 1
        if observations == 1:
            raise RuntimeError("bootstrap unavailable")

    hub = SurfaceHub(
        strategy_instance_id="bot-a",
        assemble=lambda: asyncio.sleep(
            0,
            result=_snapshot(generated_at_ms=1_700_000_000_100),
        ),
        on_snapshot=on_snapshot,
        refresh_interval_seconds=0.01,
    )

    await hub.start()
    for _ in range(20):
        if observations >= 2:
            break
        await asyncio.sleep(0.01)
    await hub.stop(timeout_seconds=0.1)

    assert observations >= 2


@pytest.mark.asyncio
async def test_registry_remove_stops_and_forgets_hub() -> None:
    registry = SurfaceHubRegistry[_Snapshot]()
    hub = registry.get_or_create(
        "bot-a",
        assemble=lambda: asyncio.sleep(
            0,
            result=_snapshot(generated_at_ms=1_700_000_000_100),
        ),
        refresh_interval_seconds=3_600,
    )
    await hub.start()

    await registry.remove("bot-a")

    assert registry.get("bot-a") is None
    assert hub.is_running is False


@pytest.mark.asyncio
async def test_stop_restart_fences_an_inflight_old_generation() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls = 0

    async def assemble() -> _Snapshot:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            try:
                await release_first.wait()
            except asyncio.CancelledError:
                await release_first.wait()
        return _snapshot(
            generated_at_ms=1_700_000_000_000 + calls,
            source_state=f"generation-{calls}",
        )

    hub = SurfaceHub(
        strategy_instance_id="bot-a",
        assemble=assemble,
        process_epoch="data-plane-a",
        refresh_interval_seconds=3_600,
    )
    start_task = asyncio.create_task(hub.start())
    await first_started.wait()
    await hub.stop(timeout_seconds=0.01)
    release_first.set()
    await asyncio.gather(start_task, return_exceptions=True)

    await hub.start()
    restarted = await hub.snapshot()
    await hub.stop(timeout_seconds=0.1)

    assert restarted.source_state == "generation-2"
    assert restarted.surface_version == 1
