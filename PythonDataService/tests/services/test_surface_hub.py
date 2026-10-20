"""Tests for producer-owned versioned snapshots (ADR 0035 amendment (b))."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from app.services import surface_hub
from app.services.broker_v2_panel import live_projection, panel_data_source
from app.services.surface_hub import (
    SnapshotStalledError,
    SnapshotUnavailableError,
    SurfaceHub,
    SurfaceHubRefreshFailure,
    SurfaceHubRegistry,
    SurfaceHubStall,
)


class _Snapshot(BaseModel):
    stream_epoch: str = ""
    surface_version: int = 0
    fetched_at_ms: int = 0
    daemon_fetched_at_ms: int | None = None
    generated_at_ms: int
    source_state: str
    evidence_at_ms: int


class _MarketPulse(BaseModel):
    age_ms: int
    observed_at_ms: int


class _Panel(BaseModel):
    market_pulse: _MarketPulse


class _PanelSnapshot(BaseModel):
    stream_epoch: str = ""
    surface_version: int = 0
    panel: _Panel


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
@pytest.mark.parametrize(
    ("strategy_instance_id", "fetch_field"),
    [
        pytest.param("bot-a", "fetched_at_ms", id="identical-semantics"),
        pytest.param("__fleet_roster__", "daemon_fetched_at_ms", id="daemon-fetch-timestamp"),
    ],
)
async def test_a_changed_fetch_timestamp_alone_does_not_advance_surface_version(
    strategy_instance_id: str, fetch_field: str
) -> None:
    fetched_at_ms = 1_700_000_000_100

    async def assemble() -> _Snapshot:
        nonlocal fetched_at_ms
        value = _snapshot(generated_at_ms=1_700_000_000_100).model_copy(update={fetch_field: fetched_at_ms})
        fetched_at_ms += 1_000
        return value

    hub = SurfaceHub(strategy_instance_id=strategy_instance_id, assemble=assemble)

    first = await hub.refresh()
    queue = hub.subscribe()
    assert await queue.get() == first, "a new subscriber is handed the first snapshot"
    second = await hub.refresh()

    assert first.surface_version == 1, "the first publish is version 1"
    assert second.surface_version == 1, "a later fetch timestamp alone does not advance the version"
    assert getattr(second, fetch_field) is not None, "the second snapshot carries the fetch timestamp"
    assert getattr(first, fetch_field) is not None, "the first snapshot carries the fetch timestamp"
    assert getattr(second, fetch_field) > getattr(first, fetch_field), "the fetch timestamp really moved"
    assert queue.empty(), "nothing is published to subscribers for it"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_cycle", "second_cycle"),
    [
        pytest.param((1_700_000_000_100, "FRESH"), (1_700_000_001_100, "FRESH"), id="source-timestamp"),
        pytest.param((1_700_000_001_000, "FRESH"), (1_700_000_001_000, "STALE"), id="freshness-transition"),
    ],
)
async def test_a_changed_source_timestamp_or_freshness_advances_surface_version(
    first_cycle: tuple[int, str], second_cycle: tuple[int, str]
) -> None:
    cycles = iter((first_cycle, second_cycle))

    async def assemble() -> _Snapshot:
        generated_at_ms, source_state = next(cycles)
        return _snapshot(generated_at_ms=generated_at_ms, source_state=source_state)

    hub = SurfaceHub(strategy_instance_id="bot-a", assemble=assemble)

    first = await hub.refresh()
    second = await hub.refresh()

    assert first.surface_version == 1, "the first publish is version 1"
    assert second.surface_version == 2, "the change advances the version"


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
async def test_concurrent_starters_wait_for_the_same_initial_cycle() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def assemble() -> _Snapshot:
        started.set()
        await release.wait()
        return _snapshot(generated_at_ms=1_700_000_000_100)

    hub = SurfaceHub(
        strategy_instance_id="bot-a",
        assemble=assemble,
        refresh_interval_seconds=3_600,
    )
    first = asyncio.create_task(hub.start())
    await started.wait()
    second = asyncio.create_task(hub.start())
    await asyncio.sleep(0)

    assert second.done() is False
    release.set()
    await asyncio.gather(first, second)
    assert (await hub.snapshot()).surface_version == 1
    await hub.stop(timeout_seconds=0.1)


@pytest.mark.asyncio
async def test_refresh_failure_notifies_watchers_and_invalidates_snapshot() -> None:
    should_fail = False

    async def assemble() -> _Snapshot:
        if should_fail:
            raise RuntimeError("private source detail")
        return _snapshot(generated_at_ms=1_700_000_000_100)

    hub = SurfaceHub(strategy_instance_id="bot-a", assemble=assemble)
    await hub.refresh()
    queue = hub.subscribe()
    await queue.get()
    should_fail = True

    with pytest.raises(RuntimeError, match="private source detail"):
        await hub.refresh()

    failure = await queue.get()
    assert failure == SurfaceHubRefreshFailure()
    assert "private source detail" not in failure.message
    with pytest.raises(SnapshotUnavailableError):
        await hub.snapshot()


@pytest.mark.asyncio
async def test_hung_assembly_stalls_the_snapshot_by_the_producer_stamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2353: an assembly that never completes must not leave the last
    snapshot served as current. Liveness is the producer's own completion
    stamp on the server's monotonic clock; recovery republishes even when
    nothing semantic changed, so a client told "stale" hears the producer is
    back."""
    clock = {"now_ms": 1_700_000_000_000, "monotonic": 1_000.0}
    monkeypatch.setattr(surface_hub, "now_ms_utc", lambda: clock["now_ms"])
    monkeypatch.setattr(surface_hub, "monotonic", lambda: clock["monotonic"])
    release = asyncio.Event()
    calls = {"n": 0}

    async def assemble() -> _Snapshot:
        calls["n"] += 1
        if calls["n"] == 2:
            await release.wait()
        return _snapshot(generated_at_ms=1_700_000_000_100)

    hub = SurfaceHub(
        strategy_instance_id="bot-a",
        assemble=assemble,
        refresh_interval_seconds=5.0,
    )
    first = await hub.refresh()
    queue = hub.subscribe()
    assert await queue.get() == first
    hung = asyncio.create_task(hub.refresh())
    await asyncio.sleep(0)

    clock["now_ms"] += 20_000
    clock["monotonic"] += 20.0
    assert hub.stall() is None
    assert await hub.snapshot() == first
    assert hub.seconds_until_stall() == pytest.approx(0.001)

    clock["now_ms"] += 1
    clock["monotonic"] += 0.001
    stall = SurfaceHubStall(
        last_produced_at_ms=1_700_000_000_000,
        observed_at_ms=1_700_000_020_001,
        stall_after_ms=20_000,
    )
    assert hub.stall() == stall
    # A passed deadline is "stall now", never "no deadline".
    clock["monotonic"] += 10.0
    assert hub.seconds_until_stall() == pytest.approx(0.001)
    clock["monotonic"] -= 10.0
    with pytest.raises(SnapshotStalledError) as refused:
        await hub.snapshot()
    assert refused.value.stall == stall
    assert hub.latest == first

    release.set()
    recovered = await hung
    assert recovered.surface_version == first.surface_version
    assert await asyncio.wait_for(queue.get(), timeout=1.0) == recovered
    assert hub.stall() is None
    assert await hub.snapshot() == recovered


@pytest.mark.asyncio
async def test_a_hung_first_assembly_is_cut_off_so_start_returns_and_the_hub_recovers() -> None:
    """#3217: the first assembly never returned, so ``start()`` never did, and
    the hub stayed registered with a producer nothing could recycle. The
    assembly is cancelled once it outlives the stall budget, the producer
    retries, and the hub serves the retry."""
    calls = 0
    cancelled = asyncio.Event()

    async def assemble() -> _Snapshot:
        nonlocal calls
        calls += 1
        if calls == 1:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        return _snapshot(generated_at_ms=1_700_000_000_100)

    hub = SurfaceHub(
        strategy_instance_id="bot-a",
        assemble=assemble,
        refresh_interval_seconds=0.05,
    )
    try:
        await asyncio.wait_for(hub.start(), timeout=3.0)

        assert cancelled.is_set()
        assert hub.is_running is True
        with pytest.raises(SnapshotUnavailableError):
            await hub.snapshot()
        for _ in range(100):
            if hub.is_available:
                break
            await asyncio.sleep(0.02)
        assert (await hub.snapshot()).surface_version == 1
        assert calls >= 2
    finally:
        await hub.stop(timeout_seconds=0.1)


@pytest.mark.asyncio
async def test_a_source_that_keeps_hanging_is_retried_and_the_hub_stays_stalled() -> None:
    """#3217: a hung cycle on a hub that already served used to be flagged
    stalled and then sit there for ever. Each hung cycle is now cancelled (not
    abandoned beside the next one) and the producer tries again; meanwhile the
    hub is stalled, not failed, so its last snapshot is still served as stale
    (#2353), and the first cycle that completes republishes."""
    release = asyncio.Event()
    calls = 0
    cancelled = 0
    running = 0
    overlapped = False

    async def assemble() -> _Snapshot:
        nonlocal calls, cancelled, running, overlapped
        calls += 1
        running += 1
        overlapped = overlapped or running > 1
        try:
            if calls > 1:
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled += 1
                    raise
            return _snapshot(
                generated_at_ms=1_700_000_000_000 + calls,
                source_state=f"cycle-{calls}",
            )
        finally:
            running -= 1

    hub = SurfaceHub(
        strategy_instance_id="bot-a",
        assemble=assemble,
        refresh_interval_seconds=0.02,
    )
    try:
        await hub.start()
        queue = hub.subscribe()
        first = await queue.get()
        assert first.source_state == "cycle-1"

        for _ in range(200):
            if cancelled >= 2:
                break
            await asyncio.sleep(0.02)
        assert cancelled >= 2
        assert hub.is_available is True
        with pytest.raises(SnapshotStalledError):
            await hub.snapshot()
        assert hub.latest == first
        assert queue.empty()

        release.set()
        recovered = await asyncio.wait_for(queue.get(), timeout=3.0)

        assert recovered.source_state != "cycle-1"
        assert hub.stall() is None
        assert overlapped is False
    finally:
        await hub.stop(timeout_seconds=0.1)


@pytest.mark.asyncio
async def test_wall_clock_steps_neither_fake_nor_hide_a_stall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stall is judged on the monotonic clock; the wall clock only labels it."""
    clock = {"now_ms": 1_700_000_000_000, "monotonic": 1_000.0}
    monkeypatch.setattr(surface_hub, "now_ms_utc", lambda: clock["now_ms"])
    monkeypatch.setattr(surface_hub, "monotonic", lambda: clock["monotonic"])
    hub = SurfaceHub(
        strategy_instance_id="bot-a",
        assemble=lambda: asyncio.sleep(0, result=_snapshot(generated_at_ms=1)),
        refresh_interval_seconds=5.0,
    )
    await hub.refresh()

    # An NTP step forward by an hour while only one second really passed.
    clock["now_ms"] += 3_600_000
    clock["monotonic"] += 1.0
    assert hub.stall() is None

    # A step backward by an hour while the producer really is 30 s silent.
    clock["now_ms"] -= 2 * 3_600_000
    clock["monotonic"] += 29.0
    stall = hub.stall()
    assert stall is not None
    assert stall.last_produced_at_ms == 1_700_000_000_000
    with pytest.raises(SnapshotStalledError):
        await hub.snapshot()


@pytest.mark.asyncio
async def test_market_pulse_age_advances_surface_version() -> None:
    age_ms = 5_000

    async def assemble() -> _PanelSnapshot:
        return _PanelSnapshot(
            panel=_Panel(
                market_pulse=_MarketPulse(
                    age_ms=age_ms,
                    observed_at_ms=1_700_000_000_000 + age_ms,
                )
            )
        )

    hub = SurfaceHub(strategy_instance_id="bot-a", assemble=assemble)
    first = await hub.refresh()
    age_ms = 10_000
    second = await hub.refresh()

    assert second.surface_version == first.surface_version + 1


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
async def test_live_projection_hub_retires_after_last_client_leaves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await live_projection.stop_live_projection_hubs()
    live_projection.reset_live_projection_hubs_for_testing()
    monkeypatch.setattr(live_projection, "_HUB_IDLE_SECONDS", 0)
    key = live_projection._hub_key("alpaca", "PA-1", "sid-1", "5s")
    hub = live_projection._HUBS.get_or_create(
        key,
        assemble=lambda: asyncio.sleep(
            0,
            result=_snapshot(generated_at_ms=1_700_000_000_100),
        ),
        refresh_interval_seconds=3_600,
    )
    await hub.start()
    queue = hub.subscribe()

    live_projection.release_live_projection_hub(
        "alpaca",
        "PA-1",
        "sid-1",
        resolution="5s",
        hub=hub,
    )
    retirement = live_projection._HUB_RETIREMENT_TASKS[key]
    await retirement

    assert live_projection._HUBS.get(key) is hub
    assert hub.is_running is True

    hub.unsubscribe(queue)
    live_projection.release_live_projection_hub(
        "alpaca",
        "PA-1",
        "sid-1",
        resolution="5s",
        hub=hub,
    )
    retirement = live_projection._HUB_RETIREMENT_TASKS[key]
    await retirement

    assert live_projection._HUBS.get(key) is None
    assert hub.is_running is False


@pytest.mark.asyncio
async def test_a_caller_that_gives_up_on_a_hung_first_assembly_still_retires_the_hub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#3217: the retirement timer used to be armed only after ``start()``
    returned, so a first assembly that hung past the caller's deadline left a
    registered hub with a live producer and no timer, wedged until restart."""
    await live_projection.stop_live_projection_hubs()
    live_projection.reset_live_projection_hubs_for_testing()
    monkeypatch.setattr(live_projection, "_HUB_IDLE_SECONDS", 0.3)

    async def hung(*_args: object, **_kwargs: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(panel_data_source, "get_live_snapshot_parts", hung)
    key = live_projection._hub_key("alpaca", "PA-1", "sid-1", "5s")

    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                live_projection.get_or_start_live_projection_hub(
                    "alpaca", "PA-1", "sid-1", resolution="5s"
                ),
                timeout=0.1,
            )
        hub = live_projection._HUBS.get(key)
        assert hub is not None
        retirement = live_projection._HUB_RETIREMENT_TASKS.get(key)
        assert retirement is not None

        await asyncio.wait_for(retirement, timeout=3.0)

        assert live_projection._HUBS.get(key) is None
        assert hub.is_running is False
    finally:
        await live_projection.stop_live_projection_hubs()


@pytest.mark.asyncio
async def test_every_spelling_of_an_account_shares_one_live_projection_producer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#3217: hubs keyed on the raw path ``account_id`` gave each spelling the
    account validation accepts (``PA3``, ``pa3``, ``Pa3``, `` PA3``) its own
    producer, and a post-action prompt for one spelling missed the others."""
    await live_projection.stop_live_projection_hubs()
    live_projection.reset_live_projection_hubs_for_testing()
    assemblies = 0

    async def failing(*_args: object, **_kwargs: object) -> None:
        nonlocal assemblies
        assemblies += 1
        raise RuntimeError("source unavailable")

    monkeypatch.setattr(panel_data_source, "get_live_snapshot_parts", failing)
    try:
        hubs = [
            await live_projection.get_or_start_live_projection_hub(
                "alpaca", spelling, "sid-1", resolution="5s"
            )
            for spelling in ("PA3", "pa3", "Pa3", " PA3")
        ]

        assert all(hub is hubs[0] for hub in hubs)
        assert assemblies == 1
        assert len(live_projection._HUB_RETIREMENT_TASKS) == 1

        await live_projection.refresh_live_projection_hubs("alpaca", "pa3", "sid-1")
        assert assemblies == 2
    finally:
        await live_projection.stop_live_projection_hubs()


@pytest.mark.asyncio
async def test_control_action_prompt_refresh_publishes_only_changed_semantics() -> None:
    await live_projection.stop_live_projection_hubs()
    live_projection.reset_live_projection_hubs_for_testing()
    source_state = "revision-1"

    async def assemble() -> _Snapshot:
        return _snapshot(
            generated_at_ms=1_700_000_000_100,
            source_state=source_state,
        )

    key = live_projection._hub_key("alpaca", "PA-1", "sid-1", "5s")
    hub = live_projection._HUBS.get_or_create(key, assemble=assemble)
    first = await hub.refresh()
    queue = hub.subscribe()
    assert await queue.get() == first

    await live_projection.refresh_live_projection_hubs("alpaca", "PA-1", "sid-1")
    assert hub.surface_version == 1
    assert queue.empty()

    source_state = "revision-2"
    await live_projection.refresh_live_projection_hubs("alpaca", "PA-1", "sid-1")
    changed = await queue.get()

    assert changed.surface_version == 2
    assert changed.source_state == "revision-2"
    await live_projection.stop_live_projection_hubs()


@pytest.mark.asyncio
async def test_scheduled_control_prompt_is_tracked_without_blocking_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await live_projection.stop_live_projection_hubs()
    live_projection.reset_live_projection_hubs_for_testing()
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_refresh(_broker: str, _account_id: str, _sid: str) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(live_projection, "refresh_live_projection_hubs", blocked_refresh)

    live_projection.schedule_live_projection_refresh("alpaca", "PA-1", "sid-1")
    await started.wait()

    assert len(live_projection._HUB_PROMPT_TASKS) == 1
    release.set()
    await live_projection.stop_live_projection_hubs()
    assert not live_projection._HUB_PROMPT_TASKS


@pytest.mark.asyncio
async def test_snapshot_watchers_are_latest_wins_queue_one() -> None:
    source_state = "one"

    async def assemble() -> _Snapshot:
        return _snapshot(
            generated_at_ms=1_700_000_000_100,
            source_state=source_state,
        )

    hub = SurfaceHub(strategy_instance_id="bot-a", assemble=assemble)
    first = await hub.refresh()
    queue = hub.subscribe()
    assert await queue.get() == first

    source_state = "two"
    await hub.refresh()
    source_state = "three"
    latest = await hub.refresh()

    assert queue.qsize() == 1
    assert await queue.get() == latest
    assert latest.surface_version == 3


@pytest.mark.asyncio
async def test_stop_closes_snapshot_watchers_and_a_subscribe_after_stop_is_terminal_and_not_retained() -> None:
    hub = SurfaceHub(
        strategy_instance_id="bot-a",
        assemble=lambda: asyncio.sleep(
            0,
            result=_snapshot(generated_at_ms=1_700_000_000_100),
        ),
        refresh_interval_seconds=3_600,
    )
    await hub.start()
    watcher = hub.subscribe()
    await watcher.get()

    await hub.stop(timeout_seconds=0.1)
    after_stop = hub.subscribe()

    assert await watcher.get() is None, "stop closes a snapshot watcher that was subscribed before it"
    assert await after_stop.get() is None, "a subscriber after stop is handed the terminal marker"
    assert after_stop not in hub._watchers, "a subscriber after stop is not retained"


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
