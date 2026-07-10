"""ADR-0028 Stage 2 tests for producer-owned versioned snapshots."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from app.services.surface_hub import SurfaceHub


class _Snapshot(BaseModel):
    stream_epoch: str = ""
    surface_version: int = 0
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
    assert second.surface_version == 1
    assert second.generated_at_ms > first.generated_at_ms


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
    await hub.start(invoke_start_hook=False)
    first = await hub.snapshot()
    await hub.stop(timeout_seconds=0.1)
    await hub.start(invoke_start_hook=False)
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
async def test_start_hook_is_lifecycle_owned_and_stop_is_bounded() -> None:
    starts = 0

    async def on_start() -> None:
        nonlocal starts
        starts += 1

    hub = SurfaceHub(
        strategy_instance_id="bot-a",
        assemble=lambda: asyncio.sleep(
            0,
            result=_snapshot(generated_at_ms=1_700_000_000_100),
        ),
        on_start=on_start,
        refresh_interval_seconds=3_600,
    )

    await hub.start()
    await hub.snapshot()
    await hub.snapshot()
    await hub.stop(timeout_seconds=0.1)

    assert starts == 1
    assert hub.is_running is False
