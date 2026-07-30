from __future__ import annotations

import asyncio

import pytest

from app.services.offline_replay_clock import OfflineReplayClock, ReplayTickPermission


@pytest.mark.asyncio
async def test_pause_and_step_release_exactly_one_tick() -> None:
    clock = OfflineReplayClock(60)
    await clock.pause()

    first = asyncio.create_task(clock.before_tick(warmup=False))
    await asyncio.sleep(0)
    assert not first.done()

    await clock.step()
    assert await asyncio.wait_for(first, timeout=1) is ReplayTickPermission.STEP

    second = asyncio.create_task(clock.before_tick(warmup=False))
    await asyncio.sleep(0)
    assert not second.done()
    await clock.resume()
    assert await asyncio.wait_for(second, timeout=1) is ReplayTickPermission.RUN


@pytest.mark.asyncio
async def test_stop_releases_a_paused_clock() -> None:
    clock = OfflineReplayClock(10)
    await clock.pause()
    waiting = asyncio.create_task(clock.before_tick(warmup=False))

    await clock.stop()

    assert await asyncio.wait_for(waiting, timeout=1) is ReplayTickPermission.STOP


@pytest.mark.asyncio
async def test_warmup_runs_without_wall_clock_delay() -> None:
    clock = OfflineReplayClock(1)
    await clock.pause()

    assert await clock.before_tick(warmup=True) is ReplayTickPermission.RUN


@pytest.mark.asyncio
async def test_pace_uses_selected_acceleration() -> None:
    observed_delays: list[float] = []

    async def capture_sleep(delay: float) -> None:
        observed_delays.append(delay)

    clock = OfflineReplayClock(60, sleep=capture_sleep)
    await clock.pace(ReplayTickPermission.RUN)
    await clock.set_speed(10)
    await clock.pace(ReplayTickPermission.RUN)
    await clock.pace(ReplayTickPermission.STEP)

    assert observed_delays == [1.0, 6.0]
