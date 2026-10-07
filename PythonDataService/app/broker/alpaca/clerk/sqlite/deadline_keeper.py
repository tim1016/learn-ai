"""The Clerk's own task for the deadlines it owes (ADR 0076; ADR 0048 as amended).

A resting option entry must have its unfilled part cancelled when the
owner's wait runs out, within two seconds. The reconciliation pass cannot
promise that: it runs every 15 seconds and backs off to 300 while the broker
cannot be read, a Dry Run has no periodic pass at all, and every entry on the
account is refused while a pass runs. So the Clerk keeps the deadline with a
task of its own.

**It keeps no list.** Each round reads what is due from the books and acts
on it; nothing is remembered between rounds, so nothing is lost with the
task. The reconciliation pass runs the same step on every turn: a keeper
that has died makes a deadline late by one pass and never loses it.

**It starts no pass.** A cancel needs none, and is always allowed. Every act
takes the operation claim of the order it acts on (ADR 0035 decision 5), so
the keeper, the pass, an EXIT and a Stop never act on one order at once: a
claim held by another makes the keeper skip that order until its next round.

**A round is a list of steps.** Today there is one: cancel what has not
filled of every entry past its wait (``resting_entries``). A position's
scheduled close is one more step of the same round, not a second task.

**How long it sleeps.** Until the next stored deadline, and never longer
than the idle wake. While something past its deadline is still not proven
ended it looks again every second for the first ten seconds past that
deadline, then at the idle wake. A nudge from the facade, sent when it
accepts an option entry, ends the sleep at once. The nudge carries no
meaning: lost, the idle wake and the pass still find the entry.

The keeper evaluates no reason's age policy, and no reason gains a clock
from it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass

from app.broker.alpaca.clerk.sqlite.off_loop import OffLoop, to_thread
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.resting_entries import (
    WaitEndedRound,
    cancel_entries_past_their_wait,
    resting_entries,
)
from app.broker.contract.ports import BrokerTradePort

logger = logging.getLogger(__name__)

type Rest = Callable[[float, asyncio.Event], Awaitable[None]]


async def rest_until_nudged(seconds: float, nudged: asyncio.Event) -> None:
    """Sleep for ``seconds``, or until ``nudged`` is set, whichever is first."""
    with suppress(TimeoutError):
        await asyncio.wait_for(nudged.wait(), timeout=seconds)


# The longest the keeper sleeps with nothing due: how late a deadline can be found
# when its nudge was lost.
IDLE_WAKE_MS = 5_000
# How soon the keeper looks again at an entry past its deadline and not yet proven ended.
UNPROVEN_RETRY_MS = 1_000
# For how long past a deadline it retries that often, before falling back to the idle wake.
UNPROVEN_RETRY_WINDOW_MS = 10_000


@dataclass(frozen=True, slots=True)
class KeeperRound:
    """What one round did, and how long the keeper may sleep after it."""

    entries: WaitEndedRound
    wake_in_ms: int


class DeadlineKeeper:
    """Acts on the Clerk's durable deadlines on time. One per account authority.

    :meth:`tick` is one whole round: it reads what is due, acts, and returns
    what it did and when to look again, which is what lets the timing
    promises be asserted over a clock the test moves, without sleeping.
    """

    def __init__(
        self,
        *,
        repo: ClerkSqliteRepository,
        trade: BrokerTradePort,
        rest: Rest = rest_until_nudged,
        off_loop: OffLoop = to_thread,
        max_rounds: int | None = None,
    ) -> None:
        self._repo = repo
        self._trade = trade
        self._rest = rest
        self._off_loop = off_loop
        self._max_rounds = max_rounds
        self._nudged = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def nudge(self) -> None:
        """End the keeper's sleep: a deadline may have been stored that is sooner than it knew."""
        self._nudged.set()

    async def tick(self) -> KeeperRound:
        """One round: act on every deadline that is due, then say when the next one is."""
        entries = await cancel_entries_past_their_wait(self._repo, trade=self._trade, off_loop=self._off_loop)
        return KeeperRound(entries=entries, wake_in_ms=await self._off_loop(self._wake_in_ms))

    def _wake_in_ms(self) -> int:
        """How long until something may be due, read from the books: at most the idle wake."""
        now_ms = self._repo.clock()
        wake_in_ms = IDLE_WAKE_MS
        for entry in resting_entries(self._repo):
            overdue_ms = now_ms - entry.cancel_at_ms
            if overdue_ms < 0:
                # Not due yet: sleep to its deadline, never past it.
                wake_in_ms = min(wake_in_ms, -overdue_ms)
            elif overdue_ms < UNPROVEN_RETRY_WINDOW_MS:
                # Due and still not proven ended: look again soon.
                wake_in_ms = min(wake_in_ms, UNPROVEN_RETRY_MS)
        return wake_in_ms

    async def run(self) -> None:
        rounds = 0
        while self._max_rounds is None or rounds < self._max_rounds:
            # Cleared before the round reads the books: an entry accepted after
            # this is either seen by the read or sets the nudge again.
            self._nudged.clear()
            wake_in_ms = IDLE_WAKE_MS
            try:
                wake_in_ms = (await self.tick()).wake_in_ms
            except Exception:
                # A failed round must not end the task: the next round reads the
                # same books and acts on whatever is still due.
                logger.exception(
                    "the deadline keeper's round failed",
                    extra={"action": "deadline_keeper_round_failed", "account_id": self._repo.account_id},
                )
            rounds += 1
            await self._rest(wake_in_ms / 1000, self._nudged)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="alpaca-sqlite-deadline-keeper")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


__all__ = [
    "IDLE_WAKE_MS",
    "UNPROVEN_RETRY_MS",
    "UNPROVEN_RETRY_WINDOW_MS",
    "DeadlineKeeper",
    "KeeperRound",
]
