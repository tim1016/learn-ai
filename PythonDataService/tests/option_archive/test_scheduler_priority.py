from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.data_lake.option_request_priority import OptionRequestGate, archive_request
from app.option_archive.scheduler import ArchiveJob, ArchiveScheduler


class Ownership:
    def __init__(self) -> None:
        self.held: set[str] = set()

    @asynccontextmanager
    async def hold(self, name: str) -> AsyncIterator[bool]:
        if name in self.held:
            yield False
            return
        self.held.add(name)
        try:
            yield True
        finally:
            self.held.remove(name)


async def record(name: str, **fields: object) -> None:
    return None


def scheduler(jobs: tuple[ArchiveJob, ...], ownership: Ownership) -> ArchiveScheduler:
    return ArchiveScheduler(jobs=jobs, ownership=ownership, clock=lambda: 0, record=record, retry_backoff_ms=300_000)


async def test_repeated_trigger_joins_and_second_scheduler_cannot_overlap() -> None:
    entered, finish = asyncio.Event(), asyncio.Event()
    count = 0
    async def run() -> None:
        nonlocal count
        count += 1
        entered.set()
        await finish.wait()
    job = ArchiveJob("capture", lambda now: True, run)
    owner = Ownership()
    first = scheduler((job,), owner)
    second = scheduler((job,), owner)
    tasks = [asyncio.create_task(first.trigger("capture")) for _ in range(2)]
    await entered.wait()
    await second.trigger("capture")
    assert count == 1
    finish.set()
    await asyncio.gather(*tasks)
    assert not owner.held


async def test_backup_runs_independently_of_long_capture() -> None:
    entered, finish, backed_up = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def capture() -> None:
        entered.set()
        await finish.wait()
    async def backup() -> None:
        backed_up.set()
    jobs = scheduler((ArchiveJob("capture", lambda now: True, capture), ArchiveJob("backup", lambda now: True, backup)), Ownership())
    await jobs.tick()
    await entered.wait()
    await backed_up.wait()
    finish.set()
    await jobs.stop()


async def test_foreground_request_takes_next_slot_before_archive_backlog() -> None:
    gate = OptionRequestGate(1)
    await gate.acquire()
    order: list[str] = []
    async def ask(name: str, background: bool) -> None:
        token = archive_request.set(background)
        try:
            async with gate:
                order.append(name)
        finally:
            archive_request.reset(token)
    background = asyncio.create_task(ask("archive", True))
    await asyncio.sleep(0)
    foreground = asyncio.create_task(ask("foreground", False))
    await asyncio.sleep(0)
    gate.release()
    await asyncio.gather(background, foreground)
    assert order == ["foreground", "archive"]
    assert gate._value == 1


async def test_cancelled_waiter_does_not_leak_gate_capacity() -> None:
    gate = OptionRequestGate(1)
    await gate.acquire()
    waiter = asyncio.create_task(gate.acquire())
    await asyncio.sleep(0)
    gate.release()
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    async with gate:
        assert gate.locked()
    assert gate._value == 1


async def test_a_slot_freed_while_the_only_waiter_is_granted_but_not_resumed_is_taken_at_once() -> None:
    gate = OptionRequestGate(2)
    await gate.acquire()
    await gate.acquire()
    granted = asyncio.create_task(gate.acquire())
    await asyncio.sleep(0)
    gate.release()  # hands this slot to the queued waiter, which has not resumed yet
    gate.release()  # no one is pending: this slot is free
    await asyncio.wait_for(gate.acquire(), timeout=1)
    await granted
    assert gate._value == 0
