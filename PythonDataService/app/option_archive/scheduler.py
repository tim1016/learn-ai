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
from concurrent.futures import Future
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

from app.data_lake import catalog_client
from app.schemas.option_archive import ArchiveJobName

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
    name: ArchiveJobName
    due: Callable[[int], bool]
    run: Callable[[], Awaitable[None]]


class ArchiveScheduler:
    """Runs each due job under ownership and records how every run started and ended.

    ``record(name, **fields)`` writes the job's receipt. Whatever way a run ends
    (done, failed or cancelled), the job is not due again before ``retry_backoff_ms``.
    """

    def __init__(self, *, jobs: tuple[ArchiveJob, ...], ownership: JobOwnership, clock: Callable[[], int],
                 record: Callable[..., Awaitable[None]], retry_backoff_ms: int,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.jobs, self.ownership, self.clock, self.sleep = jobs, ownership, clock, sleep
        self.record, self.retry_backoff_ms = record, retry_backoff_ms
        self._task: asyncio.Task[None] | None = None
        self._running: dict[str, asyncio.Task[None]] = {}
        # Each running pass's ownership answer, so a trigger that joins it learns it too.
        self._owned_answers: dict[str, asyncio.Future[bool]] = {}

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="option-archive-scheduler")

    async def stop(self) -> None:
        tasks = [task for task in (self._task, *self._running.values()) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None

    def _idle(self, name: str) -> bool:
        running = self._running.get(name)
        return running is None or running.done()

    def _spawn(self, job: ArchiveJob) -> asyncio.Task[None]:
        if self._idle(job.name):
            answer: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            task = asyncio.create_task(self._owned(job, answer), name=f"option-archive-{job.name}")
            task.add_done_callback(self._finished)
            self._running[job.name], self._owned_answers[job.name] = task, answer
        return self._running[job.name]

    async def trigger(self, name: ArchiveJobName, lock_decision: Future[bool] | None = None) -> None:
        """Runs the job now, or joins its running pass, until that pass ends.

        A caller that will not wait for the pass learns from ``lock_decision``
        whether it holds the job's ownership (a joined pass answers for itself),
        or why ownership could not be asked for. A caller that stopped waiting
        has cancelled it; the pass itself goes on, exactly as a due job would.
        """
        task = self._spawn(next(job for job in self.jobs if job.name == name))
        if lock_decision is not None:
            answer = self._owned_answers[name]

            def decide(_: object) -> None:
                # Both callbacks run on this loop; the caller's thread can only cancel a pending decision.
                if lock_decision.done() or not (answer.done() or task.done()):
                    return
                if not lock_decision.set_running_or_notify_cancel():
                    return
                if answer.done():
                    lock_decision.set_result(answer.result())
                else:
                    failure = None if task.cancelled() else task.exception()
                    lock_decision.set_exception(failure or RuntimeError(f"the {name} pass ended before its ownership was decided"))
            answer.add_done_callback(decide)
            task.add_done_callback(decide)
        await asyncio.shield(task)

    async def _owned(self, job: ArchiveJob, answer: asyncio.Future[bool]) -> None:
        async with self.ownership.hold(job.name) as owned:
            answer.set_result(owned)
            if not owned:
                return
            await self.record(job.name, started_ms=self.clock(), ended_ms=None, failure=None)
            failure: str | None = None
            try:
                await job.run()
            except Exception as exc:
                failure = str(exc)
                raise
            finally:
                ended = self.clock()
                await self.record(job.name, ended_ms=ended, failure=failure, retry_after_ms=ended + self.retry_backoff_ms)

    async def tick(self) -> None:
        for job in self.jobs:
            if self._idle(job.name) and job.due(self.clock()):
                self._spawn(job)

    @staticmethod
    def _finished(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.error("archive job failed", exc_info=task.exception(), extra={"action": "archive_job_failed", "job": task.get_name()})

    async def _run(self) -> None:
        while True:
            await self.tick()
            await self.sleep(30)
