"""Liveness and dynamic-scope tests for the SQLite Clerk intake fence."""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from typing import Protocol

import pytest

from app.broker.alpaca.clerk.sqlite.broker_port_guard import (
    BrokerCallUnderIntakeError,
    GuardedBrokerReadPort,
    GuardedBrokerTradePort,
    guard_broker_ports,
    missing_guarded_async_methods,
)
from app.broker.alpaca.clerk.sqlite.runtime import IntakeFenceYieldError, ReentrantAsyncLock
from app.broker.contract.models import BrokerOrderLeg
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort


class _Broker:
    broker_id = "alpaca"

    def __init__(self) -> None:
        self.account_calls = 0
        self.submit_calls = 0

    async def get_account(self) -> object:
        self.account_calls += 1
        return object()

    async def submit(self, _leg: BrokerOrderLeg, *, client_order_id: str) -> object:
        self.submit_calls += 1
        return client_order_id


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


async def test_guarded_ports_reject_inherited_fenced_scope_before_contact() -> None:
    fence = ReentrantAsyncLock()
    broker = _Broker()
    read, trade = guard_broker_ports(read=broker, trade=broker, intake=fence)

    async with fence:
        with pytest.raises(BrokerCallUnderIntakeError, match="get_account"):
            await read.get_account()
        child = asyncio.create_task(
            trade.submit(BrokerOrderLeg(symbol="SPY", side="buy", quantity=1), client_order_id="ref-1")
        )
        with pytest.raises(BrokerCallUnderIntakeError, match="submit"):
            await child

    assert broker.account_calls == 0
    assert broker.submit_calls == 0


async def test_guarded_ports_allow_unrelated_unfenced_task() -> None:
    fence = ReentrantAsyncLock()
    broker = _Broker()
    read, _trade = guard_broker_ports(read=broker, trade=broker, intake=fence)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_intake() -> None:
        async with fence:
            entered.set()
            await release.wait()

    holder = asyncio.create_task(hold_intake())
    await entered.wait()
    assert await read.get_account() is not None
    release.set()
    await holder

    assert broker.account_calls == 1


def test_guarded_ports_are_explicitly_async_protocol_complete() -> None:
    assert missing_guarded_async_methods(BrokerReadPort, GuardedBrokerReadPort) == frozenset()
    assert missing_guarded_async_methods(BrokerTradePort, GuardedBrokerTradePort) == frozenset()


def test_protocol_parity_detects_a_future_async_method_missing_from_wrapper() -> None:
    class _FutureReadPort(Protocol):
        async def new_broker_read(self) -> object: ...

    assert inspect.iscoroutinefunction(_FutureReadPort.new_broker_read)
    assert missing_guarded_async_methods(_FutureReadPort, GuardedBrokerReadPort) == {"new_broker_read"}


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


# ── the sanctioned off_loop hop (#1993) ───────────────────────────────────────


async def test_off_loop_runs_fold_on_worker_thread_with_sanctioned_yield_only() -> None:
    fence = ReentrantAsyncLock(strict_yield_detection=True)
    main_thread = threading.current_thread()
    observed: dict[str, object] = {}

    def fold() -> int:
        observed["thread"] = threading.current_thread()
        # ``asyncio.to_thread`` copies the caller's context, so the worker
        # inherits the fenced dynamic scope: broker ports would still reject
        # contact from inside the fold.
        observed["scope_depth"] = fence.current_scope_depth()
        return 7

    assert await fence.off_loop(fold) == 7

    assert fence.yielded_fence_count == 0
    assert not fence.held_by_current_task()
    assert fence.current_scope_depth() == 0
    assert observed["thread"] is not main_thread
    assert observed["scope_depth"] == 1
    # The sanctioned hop must not blind the detector to every other hold:
    # a plain yield under a raw hold still fails strict mode.
    with pytest.raises(IntakeFenceYieldError, match="yielded while held"):
        async with fence:
            await asyncio.sleep(0)


async def test_off_loop_cancellation_holds_intake_until_abandoned_fold_completes() -> None:
    """The minimum bar from #1993: no second fold enters while an abandoned
    fold is still writing, and the abandoned fold completes anyway."""
    fence = ReentrantAsyncLock(strict_yield_detection=True)
    fold_started = threading.Event()
    fold_release = threading.Event()
    events: list[str] = []

    def slow_fold() -> int:
        events.append("fold-start")
        fold_started.set()
        fold_release.wait(timeout=10)
        events.append("fold-end")
        return 42

    task = asyncio.create_task(fence.off_loop(slow_fold))
    await asyncio.get_running_loop().run_in_executor(None, fold_started.wait, 10)
    task.cancel()

    second = asyncio.create_task(fence.off_loop(lambda: events.append("second-fold")))
    await asyncio.sleep(0.05)
    assert events == ["fold-start"]
    assert not second.done()

    fold_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == ["fold-start", "fold-end"]

    await asyncio.wait_for(second, timeout=5)
    assert events == ["fold-start", "fold-end", "second-fold"]
    assert not fence.held_by_current_task()


async def test_off_loop_abandoned_fold_timeout_releases_fence_with_critical_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fence = ReentrantAsyncLock(abandoned_hop_timeout_s=0.05)
    fold_started = threading.Event()
    fold_release = threading.Event()

    def stuck_fold() -> None:
        fold_started.set()
        fold_release.wait(timeout=10)

    task = asyncio.create_task(fence.off_loop(stuck_fold))
    await asyncio.get_running_loop().run_in_executor(None, fold_started.wait, 10)
    with caplog.at_level(logging.CRITICAL):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # A wedged fold cannot brick the fence: it releases (loudly) after the
    # bounded wait, and a later fold can enter.
    async with fence:
        assert fence.held_by_current_task()
    record = next(
        record for record in caplog.records if record.name.endswith("intake_fence")
    )
    assert record.intake_fence_event == "abandoned_hop_timeout"
    fold_release.set()


async def test_off_loop_abandoned_fold_error_is_logged_not_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fence = ReentrantAsyncLock()
    fold_started = threading.Event()
    fold_release = threading.Event()

    def failing_fold() -> None:
        fold_started.set()
        fold_release.wait(timeout=10)
        raise RuntimeError("abandoned fold failed")

    task = asyncio.create_task(fence.off_loop(failing_fold))
    await asyncio.get_running_loop().run_in_executor(None, fold_started.wait, 10)
    with caplog.at_level(logging.ERROR):
        task.cancel()
        fold_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    record = next(
        record for record in caplog.records if record.name.endswith("intake_fence")
    )
    assert record.intake_fence_event == "abandoned_hop_error"
