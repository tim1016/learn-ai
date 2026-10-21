"""Service-lifetime scheduler with independent jobs and Postgres ownership.

A session advisory lock is held outside a transaction throughout each job.
A connection watchdog cancels work on loss; a second process cannot take the
lock while its predecessor still has database authority.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

from app.data_lake import catalog_client

logger = logging.getLogger(__name__)


class JobOwnership(Protocol):
    def hold(self, name: str) -> contextlib.AbstractAsyncContextManager[bool]: ...


class PostgresJobOwnership:
    @asynccontextmanager
    async def hold(self, name: str) -> AsyncIterator[bool]:
        await catalog_client.init_pool()
        key = int.from_bytes(hashlib.sha256(("option-archive:" + name).encode()).digest()[:8], "big", signed=True)
        async with catalog_client.connection() as connection:
            owned = await connection.fetchval("SELECT pg_try_advisory_lock($1)", key)
            if not owned:
                yield False
                return
            owner = asyncio.current_task()

            async def watch() -> None:
                while True:
                    await asyncio.sleep(5)
                    try:
                        await connection.fetchval("SELECT 1")
                    except Exception:
                        logger.exception("archive job lost Postgres ownership", extra={"action": "archive_ownership_lost", "job": name})
                        if owner is not None:
                            owner.cancel()
                        return

            watchdog = asyncio.create_task(watch())
            try:
                yield True
            finally:
                watchdog.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watchdog
                if not connection.is_closed():
                    await connection.fetchval("SELECT pg_advisory_unlock($1)", key)


@dataclass(frozen=True)
class ArchiveJob:
    name: str
    due: Callable[[int], bool]
    run: Callable[[], Awaitable[None]]


class ArchiveScheduler:
    def __init__(self, *, jobs: tuple[ArchiveJob, ...], ownership: JobOwnership,
                 clock: Callable[[], int], sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.jobs, self.ownership, self.clock, self.sleep = jobs, ownership, clock, sleep
        self._task: asyncio.Task[None] | None = None
        self._running: dict[str, asyncio.Task[None]] = {}

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="option-archive-scheduler")

    async def stop(self) -> None:
        tasks = [task for task in (self._task, *self._running.values()) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None

    async def trigger(self, name: str) -> None:
        running = self._running.get(name)
        if running is None or running.done():
            job = next(job for job in self.jobs if job.name == name)
            running = asyncio.create_task(self._owned(job), name=f"option-archive-{name}")
            self._running[name] = running
        await asyncio.shield(running)

    async def _owned(self, job: ArchiveJob) -> None:
        async with self.ownership.hold(job.name) as owned:
            if owned:
                await job.run()

    async def tick(self) -> None:
        for job in self.jobs:
            running = self._running.get(job.name)
            if job.due(self.clock()) and (running is None or running.done()):
                task = asyncio.create_task(self._owned(job), name=f"option-archive-{job.name}")
                self._running[job.name] = task
                task.add_done_callback(self._finished)

    @staticmethod
    def _finished(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.error("archive job failed", exc_info=task.exception(), extra={"action": "archive_job_failed", "job": task.get_name()})

    async def _run(self) -> None:
        while True:
            await self.tick()
            await self.sleep(30)
