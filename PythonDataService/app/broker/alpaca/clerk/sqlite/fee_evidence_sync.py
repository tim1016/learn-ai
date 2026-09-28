"""Account fee evidence producer, independent of UI visits and ENTER requests."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import suppress
from typing import TYPE_CHECKING

from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteError
from app.broker.contract.errors import BrokerError
from app.broker.contract.ports import BrokerActivityEvidencePort, BrokerReadPort

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository

logger = logging.getLogger(__name__)


class FeeEvidenceSync:
    def __init__(self, *, repo: ClerkSqliteRepository, read: BrokerReadPort) -> None:
        self._repo = repo
        self._read = read
        self._task: asyncio.Task[None] | None = None

    async def tick(self) -> bool:
        from app.broker.alpaca.clerk.sqlite.fee_evidence import record_fee_evidence

        # The bounded read reaches across dates, including late historical FEE
        # activities. A truncated read never proves the missing fee population.
        # The declared capability port replaces a getattr capability probe:
        # adapters without the cross-date walk fall back to the bounded read.
        if isinstance(self._read, BrokerActivityEvidencePort):
            evidence = await asyncio.wait_for(self._read.read_activity_evidence(), timeout=20)
            activities, complete = evidence.activities, evidence.history_complete
        else:
            activities = await asyncio.wait_for(self._read.list_activities(after_ms=0, limit=100), timeout=20)
            complete = False
        return await asyncio.to_thread(
            record_fee_evidence,
            self._repo,
            activities,
            checked_at_ms=self._repo.clock(),
            history_complete=complete,
        )

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except (BrokerError, TimeoutError, ValueError, sqlite3.Error, ClerkSqliteError):
                logger.warning("Account fee evidence could not be refreshed", exc_info=True)
            await asyncio.sleep(15)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="clerk-fee-evidence")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
