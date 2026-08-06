"""Periodic driver for the ordered account reconciliation pass."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort

logger = logging.getLogger(__name__)
type Sleep = Callable[[float], Awaitable[None]]


class ReconciliationSweep:
    """Periodic account recovery loop with deterministic test seams."""

    def __init__(
        self,
        *,
        repo: ClerkSqliteRepository,
        read: BrokerReadPort,
        trade: BrokerTradePort,
        interval_s: float = 15.0,
        sleep: Sleep = asyncio.sleep,
        max_passes: int | None = None,
    ) -> None:
        self._repo = repo
        self._read = read
        self._trade = trade
        self._interval_s = interval_s
        self._sleep = sleep
        self._max_passes = max_passes
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self.run(), name="alpaca-sqlite-reconciliation-sweep"
            )

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def run(self) -> None:
        passes = 0
        while True:
            await self._run_one_pass()
            passes += 1
            if self._max_passes is not None and passes >= self._max_passes:
                return
            await self._sleep(self._interval_s)

    async def _run_one_pass(self) -> None:
        try:
            await reconcile_account(
                self._repo,
                read=self._read,
                trade=self._trade,
                trigger="AUTOMATIC",
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "alpaca sqlite reconciliation sweep pass errored; retrying",
                extra={"action": "reconcile_sweep_error", "account_id": self._repo.account_id},
                exc_info=True,
            )


__all__ = ["ReconciliationSweep"]
