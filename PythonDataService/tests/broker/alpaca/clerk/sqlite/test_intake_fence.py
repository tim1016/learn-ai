"""Liveness and dynamic-scope tests for the SQLite Clerk intake fence."""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.broker.alpaca.clerk.sqlite.runtime import IntakeFenceYieldError, ReentrantAsyncLock


async def test_reentrant_intake_tracks_task_ownership_and_dynamic_scope_depth() -> None:
    fence = ReentrantAsyncLock()

    assert not fence.held_by_current_task()
    assert fence.current_scope_depth() == 0

    async with fence:
        assert fence.held_by_current_task()
        assert fence.current_scope_depth() == 1

        async with fence:
            assert fence.held_by_current_task()
            assert fence.current_scope_depth() == 2

        assert fence.held_by_current_task()
        assert fence.current_scope_depth() == 1

    assert not fence.held_by_current_task()
    assert fence.current_scope_depth() == 0


async def test_child_inherits_fenced_scope_and_unrelated_task_does_not() -> None:
    fence = ReentrantAsyncLock()
    observe_scope = asyncio.Event()

    async def observe() -> tuple[int, bool]:
        await observe_scope.wait()
        return fence.current_scope_depth(), fence.held_by_current_task()

    unrelated = asyncio.create_task(observe())
    async with fence:
        child = asyncio.create_task(observe())
        observe_scope.set()
        child_scope, unrelated_scope = await asyncio.gather(child, unrelated)

    assert child_scope == (1, False)
    assert unrelated_scope == (0, False)


async def test_yield_while_fenced_fails_on_outermost_release_in_strict_mode() -> None:
    fence = ReentrantAsyncLock(strict_yield_detection=True)

    with pytest.raises(IntakeFenceYieldError, match="yielded while held"):
        async with fence:
            await asyncio.sleep(0)

    assert not fence.held_by_current_task()
    assert fence.current_scope_depth() == 0


async def test_strict_yield_detection_does_not_mask_task_cancellation() -> None:
    fence = ReentrantAsyncLock(strict_yield_detection=True)
    entered = asyncio.Event()
    parked = asyncio.Event()

    async def hold_until_cancelled() -> None:
        async with fence:
            entered.set()
            await parked.wait()

    holder = asyncio.create_task(hold_until_cancelled())
    await entered.wait()
    await asyncio.sleep(0)
    holder.cancel()

    with pytest.raises(asyncio.CancelledError):
        await holder

    assert not fence.held_by_current_task()
    assert fence.current_scope_depth() == 0


async def test_synchronously_completed_await_does_not_trip_strict_yield_detection() -> None:
    fence = ReentrantAsyncLock(strict_yield_detection=True)

    async def completes_without_yielding() -> None:
        return None

    async with fence:
        await completes_without_yielding()

    assert fence.yielded_fence_count == 0


async def test_exception_and_cancellation_restore_fence_state() -> None:
    fence = ReentrantAsyncLock()

    with pytest.raises(ValueError, match="expected"):
        async with fence:
            raise ValueError("expected")

    entered = asyncio.Event()
    parked = asyncio.Event()

    async def hold_until_cancelled() -> None:
        async with fence:
            entered.set()
            await parked.wait()

    holder = asyncio.create_task(hold_until_cancelled())
    await entered.wait()
    holder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder

    assert not fence.held_by_current_task()
    assert fence.current_scope_depth() == 0
    async with fence:
        assert fence.held_by_current_task()


async def test_cancelled_acquisition_does_not_poison_later_intake() -> None:
    fence = ReentrantAsyncLock()
    acquisition_started = asyncio.Event()

    async def wait_for_fence() -> None:
        acquisition_started.set()
        async with fence:
            return None

    async with fence:
        waiter = asyncio.create_task(wait_for_fence())
        await acquisition_started.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

    async with fence:
        assert fence.held_by_current_task()


async def test_production_yield_records_structured_warning_counter_and_hold_duration(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fence = ReentrantAsyncLock()

    with caplog.at_level(logging.WARNING):
        async with fence:
            await asyncio.sleep(0)

    assert fence.yielded_fence_count == 1
    assert fence.last_hold_duration_seconds is not None
    assert fence.last_hold_duration_seconds >= 0
    record = next(record for record in caplog.records if record.name.endswith("intake_fence"))
    assert record.intake_fence_event == "yielded_while_held"
    assert record.yielded_fence_count == 1
