"""Account fee evidence producer, independent of UI visits and ENTER requests.

Each tick also credits a bot order the executions the stream never delivered,
from the activity the evidence retained (#2787).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.contract.ports import BrokerActivityEvidencePort, BrokerReadPort

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
    from app.broker.contract.options import BrokerOptionContract

logger = logging.getLogger(__name__)
_OPTION_CONTRACTS_PER_TICK = 8


class FeeEvidenceSync:
    def __init__(self, *, repo: ClerkSqliteRepository, read: BrokerReadPort, intake: ReentrantAsyncLock) -> None:
        self._repo = repo
        self._read = read
        # The account's one intake fence, shared with the sweep and the
        # trade-updates sink: a bot order's recovered executions fold under
        # it, and no broker read ever runs inside it.
        self._intake = intake
        self._task: asyncio.Task[None] | None = None
        self._contract_scan_offset = 0

    async def tick(self) -> bool:
        """Read the account's activity, then credit what it names for bot orders (#2787).

        Returns whether custody grew. The recovery reads the activity the
        evidence retained, never this tick's read alone, so an execution read
        on any earlier tick is still picked up.
        """
        from app.broker.alpaca.clerk.sqlite.bot_order_executions import record_bot_order_executions

        grew = await self._read_activity_evidence()
        recovered = await self._intake.off_loop(record_bot_order_executions, self._repo)
        contracts = await self._read_outside_option_contracts()
        return grew or contracts or recovered

    async def _read_outside_option_contracts(self) -> bool:
        """Resolve retained outside option fills, including ones read on earlier ticks."""
        from app.broker.alpaca.clerk.sqlite.fee_evidence import (
            missing_outside_option_contracts,
            record_fee_option_contract,
        )
        from app.options.contract import OptionContract

        missing = await asyncio.to_thread(missing_outside_option_contracts, self._repo)
        if not missing:
            return False
        start = self._contract_scan_offset % len(missing)
        count = min(_OPTION_CONTRACTS_PER_TICK, len(missing))
        batch = tuple(missing[(start + index) % len(missing)] for index in range(count))
        self._contract_scan_offset = (start + count) % len(missing)
        bound = asyncio.Semaphore(4)

        async def read(symbol: str) -> BrokerOptionContract | None:
            async with bound:
                return await asyncio.wait_for(
                    self._read.get_option_contract(OptionContract.from_occ_symbol(symbol)), timeout=3
                )

        answers = await asyncio.gather(*(read(symbol) for symbol in batch), return_exceptions=True)
        grew = False
        for symbol, answer in zip(batch, answers, strict=True):
            if isinstance(answer, asyncio.CancelledError):
                raise answer
            if isinstance(answer, BaseException):
                logger.warning(
                    "Outside option contract reference could not be read for %s: %s",
                    symbol, answer,
                    extra={"action": "clerk_fee_option_contract_unread", "account_id": self._repo.account_id},
                )
            elif answer is not None:
                grew = await asyncio.to_thread(
                    record_fee_option_contract, self._repo, expected_symbol=symbol, reference=answer
                ) or grew
        return grew

    async def _read_activity_evidence(self) -> bool:
        from app.broker.alpaca.clerk.sqlite.fee_evidence import fee_evidence_cursor, record_fee_evidence

        # The bounded read reaches across dates, including late historical FEE
        # activities. A truncated read never proves the missing fee population.
        # The declared capability port replaces a getattr capability probe:
        # adapters without the cross-date walk fall back to the bounded read.
        if not isinstance(self._read, BrokerActivityEvidencePort):
            activities = await asyncio.wait_for(self._read.list_activities(after_ms=0, limit=100), timeout=20)
            return await asyncio.to_thread(
                record_fee_evidence, self._repo, activities, checked_at_ms=self._repo.clock()
            )
        head = await asyncio.wait_for(self._read.read_activity_evidence(), timeout=20)
        grew = await asyncio.to_thread(
            record_fee_evidence,
            self._repo,
            head.activities,
            checked_at_ms=self._repo.clock(),
            history_complete=head.history_complete,
            next_page_token=head.next_page_token,
        )
        # A busy account's newest window never reaches an older fill day.
        # Walk older history one bounded read per tick, resuming from the
        # cursor custody retained (so a restart continues), until it reaches
        # custody's history floor. Coverage stays refused until it passes a day.
        # A head read that no longer meets the history retained below it
        # (more rows arrived between polls than one read returns) is walked
        # first, from its own cursor, until it does (#2557).
        cursor = await asyncio.to_thread(fee_evidence_cursor, self._repo)
        if cursor is None:
            return grew
        older = await asyncio.wait_for(self._read.read_activity_evidence(page_token=cursor), timeout=20)
        walked = await asyncio.to_thread(
            record_fee_evidence,
            self._repo,
            older.activities,
            checked_at_ms=self._repo.clock(),
            history_complete=older.history_complete,
            page_token=cursor,
            next_page_token=older.next_page_token,
        )
        return grew or walked

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception(
                    "Account fee evidence could not be refreshed",
                    extra={"action": "clerk_fee_evidence_tick_failed", "account_id": self._repo.account_id},
                )
            await asyncio.sleep(15)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="clerk-fee-evidence")
            self._task.add_done_callback(self._report_if_ended_on_an_error)

    def _report_if_ended_on_an_error(self, task: asyncio.Task[None]) -> None:
        if task.cancelled() or task.exception() is None:
            return
        logger.critical(
            "Account fee evidence producer ended; new spending will lose fresh fee proof",
            extra={"action": "clerk_fee_evidence_ended", "account_id": self._repo.account_id},
            exc_info=task.exception(),
        )

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Fee evidence task failed before shutdown", extra={"action": "clerk_fee_evidence_stop_failed"})
