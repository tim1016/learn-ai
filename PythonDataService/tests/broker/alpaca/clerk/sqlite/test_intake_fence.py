"""Liveness and dynamic-scope tests for the SQLite Clerk intake fence."""

from __future__ import annotations

import asyncio
import inspect
import logging
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
