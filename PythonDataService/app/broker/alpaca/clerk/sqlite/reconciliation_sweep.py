"""Periodic driver for the ordered account reconciliation pass."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress

from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort

logger = logging.getLogger(__name__)
type Sleep = Callable[[float], Awaitable[None]]
_LEASE_HEARTBEATS_PER_TTL = 3


class ReconciliationSweep:
    """Periodic account recovery loop with deterministic test seams."""

    def __init__(
        self,
        *,
        repo: ClerkSqliteRepository,
        read: BrokerReadPort,
        trade: BrokerTradePort,
        interval_s: float = 15.0,
        max_backoff_s: float = 300.0,
        sleep: Sleep = asyncio.sleep,
        lease_sleep: Sleep = asyncio.sleep,
        max_passes: int | None = None,
    ) -> None:
        self._repo = repo
        self._read = read
        self._trade = trade
        self._interval_s = interval_s
        self._max_backoff_s = max(max_backoff_s, interval_s)
        self._sleep = sleep
        self._lease_sleep = lease_sleep
        self._lease_heartbeat_interval_s = max(
            repo.lease_ttl_ms / (_LEASE_HEARTBEATS_PER_TTL * 1000),
            0.001,
        )
        self._max_passes = max_passes
        self._task: asyncio.Task[None] | None = None
        self._lease_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self.run(), name="alpaca-sqlite-reconciliation-sweep"
            )
        if self._lease_task is None or self._lease_task.done():
            self._lease_task = asyncio.create_task(
                self._run_lease_heartbeat(),
                name="alpaca-sqlite-execution-lease-heartbeat",
            )

    async def stop(self) -> None:
        tasks = tuple(
            task for task in (self._task, self._lease_task) if task is not None
        )
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        self._task = None
        self._lease_task = None

    async def _run_lease_heartbeat(self) -> None:
        while True:
            await self._lease_sleep(self._lease_heartbeat_interval_s)
            try:
                await asyncio.to_thread(self._repo.renew_execution_lease)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.critical(
                    "alpaca sqlite execution lease heartbeat failed; writes remain fail-closed",
                    extra={
                        "action": "execution_lease_heartbeat_failed",
                        "account_id": self._repo.account_id,
                    },
                    exc_info=True,
                )
                return

    async def run(self) -> None:
        passes = 0
        consecutive_failures = 0
        while True:
            succeeded = await self._run_one_pass()
            consecutive_failures = 0 if succeeded else consecutive_failures + 1
            passes += 1
            if self._max_passes is not None and passes >= self._max_passes:
                return
            delay = (
                self._interval_s
                if consecutive_failures == 0
                else min(
                    self._interval_s * (2**consecutive_failures),
                    self._max_backoff_s,
                )
            )
            await self._sleep(delay)

    async def _run_one_pass(self) -> bool:
        try:
            result = await reconcile_account(
                self._repo,
                read=self._read,
                trade=self._trade,
                trigger="AUTOMATIC",
            )
            return result.verdict != "stale"
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "alpaca sqlite reconciliation sweep pass errored; retrying",
                extra={"action": "reconcile_sweep_error", "account_id": self._repo.account_id},
                exc_info=True,
            )
            return False


__all__ = ["ReconciliationSweep"]
