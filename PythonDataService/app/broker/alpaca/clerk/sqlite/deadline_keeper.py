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
takes the operation claim of the effect it acts under (ADR 0035 decision 5):
the entry's own ENTER, or the EXIT that owns the entry. So two acts under one
effect never overlap, and a claim held by another makes the keeper skip that
order until its next round. The keeper and an EXIT act under different
effects and can each ask the broker to cancel one order; asking twice for
the same order is a retry, never a second intent.

**A round is a list of steps.** There are two. The first cancels what has
not filled of every entry past its wait (``resting_entries``). The second
sends every scheduled close of an option position that is due, drives on
each one not finished, and records each one its session ended on
(``scheduled_close``). The second is handed its lock and its pricing once
the Clerk's facade exists (:meth:`DeadlineKeeper.keep_scheduled_closes`).

**One round reads each worklist once.** What is due, and when to wake, both
come from that one read.

**How long it sleeps.** Until the next stored deadline, the next close that
comes due or the end of the session one is owed in, and never longer than
the idle wake. An entry past its deadline and still not proven is
asked about again every second for the first ten seconds past that
deadline, then no more often than the idle wake, however often the keeper
wakes for other entries. That pause is kept in memory only. It is not a
list of what is due: forget it, and an entry is asked about one round
sooner. A cancel that keeps failing the same way writes one record, not
one a round. A nudge from the facade, sent when it accepts an option entry,
ends the sleep at once. The nudge carries no meaning: lost, the idle wake
and the pass still find the entry.

**It always stops.** A stop that lands inside a broker call can surface as
another error, because the claim is renewed after the call. The loop asks
whether its task is being cancelled whenever it catches anything, and ends.

The keeper evaluates no reason's age policy, and no reason gains a clock
from it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field

from app.broker.alpaca.clerk.recovery_reduction import RecoveryPricing
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.off_loop import OffLoop, to_thread
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.resting_entries import (
    RestingEntry,
    WaitEndedRound,
    being_cancelled,
    cancel_resting_entries,
    resting_entries,
)
from app.broker.alpaca.clerk.sqlite.scheduled_close import ScheduledCloseRound, drive_scheduled_closes
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


def _retry_after_ms(entry: RestingEntry, now_ms: int) -> int:
    """How long a due entry rests between two asks: a second while its deadline is fresh, then the idle wake."""
    return UNPROVEN_RETRY_MS if now_ms - entry.cancel_at_ms < UNPROVEN_RETRY_WINDOW_MS else IDLE_WAKE_MS


@dataclass(frozen=True, slots=True)
class KeeperRound:
    """What one round did, and how long the keeper may sleep after it."""

    entries: WaitEndedRound
    wake_in_ms: int
    closes: ScheduledCloseRound = field(default_factory=ScheduledCloseRound)


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
        # When each due entry was last asked about, on the Clerk's clock. A pause, not a worklist.
        self._asked_at_ms: dict[str, int] = {}
        # The lock and the pricing the scheduled closes are accepted under and driven with.
        self._closes: tuple[ReentrantAsyncLock, RecoveryPricing] | None = None

    def keep_scheduled_closes(self, *, intake: ReentrantAsyncLock, pricing: RecoveryPricing) -> None:
        """Give the keeper its second step: the scheduled closes of option positions (ADR 0076).

        Handed over once the Clerk's facade exists. A close is an EXIT, and
        an EXIT is driven with the facade's one pricing seam, while the
        facade is itself built with this keeper's nudge. A keeper never
        given them keeps entries only, and the reconciliation pass, which
        always runs the same step, is then the only one to send a close.
        """
        self._closes = (intake, pricing)

    def nudge(self) -> None:
        """End the keeper's sleep: a deadline may have been stored that is sooner than it knew."""
        self._nudged.set()

    async def tick(self) -> KeeperRound:
        """One round: act on every deadline that is due, then say when the next one is."""
        resting = await self._off_loop(lambda: resting_entries(self._repo))
        now_ms = self._repo.clock()
        to_ask = [entry for entry in resting if entry.due(now_ms) and self._may_ask_again(entry, now_ms)]
        for entry in to_ask:
            self._asked_at_ms[entry.order.order_ref] = now_ms
        entries = await cancel_resting_entries(self._repo, to_ask, trade=self._trade, off_loop=self._off_loop)
        listed = {entry.order.order_ref for entry in resting}
        self._asked_at_ms = {
            order_ref: asked for order_ref, asked in self._asked_at_ms.items()
            if order_ref in listed and order_ref not in entries.ended
        }
        closes = ScheduledCloseRound()
        if self._closes is not None:
            intake, pricing = self._closes
            closes = await drive_scheduled_closes(
                self._repo, trade=self._trade, intake=intake, pricing=pricing, off_loop=self._off_loop
            )
        wake_in_ms = min(self._wake_in_ms(resting, ended=entries.ended), self._wake_for_closes(closes))
        return KeeperRound(entries=entries, wake_in_ms=wake_in_ms, closes=closes)

    def _wake_for_closes(self, closes: ScheduledCloseRound) -> int:
        """How long until a close comes due or the session it is owed in ends: at most the idle wake.

        A close that is due and not finished is looked at again at the idle
        wake: a market order fills at once, and a held one waits on a halt
        or a hold that does not lift by the second.
        """
        if closes.next_change_ms is None:
            return IDLE_WAKE_MS
        return min(IDLE_WAKE_MS, max(closes.next_change_ms - self._repo.clock(), 0))

    def _may_ask_again(self, entry: RestingEntry, now_ms: int) -> bool:
        """Whether a due entry's pause since it was last asked about is over."""
        asked_at_ms = self._asked_at_ms.get(entry.order.order_ref)
        # A clock that reads before the last ask was set back: the pause cannot be measured, so ask.
        return asked_at_ms is None or now_ms < asked_at_ms or now_ms - asked_at_ms >= _retry_after_ms(entry, now_ms)

    def _wake_in_ms(self, resting: list[RestingEntry], *, ended: tuple[str, ...]) -> int:
        """How long until something may be due, from the round's one read of the books: at most the idle wake."""
        now_ms = self._repo.clock()
        wake_in_ms = IDLE_WAKE_MS
        for entry in resting:
            order_ref = entry.order.order_ref
            if order_ref in ended:
                continue
            if not entry.due(now_ms):
                # Not due yet: sleep to its deadline, never past it.
                wake_in_ms = min(wake_in_ms, entry.cancel_at_ms - now_ms)
                continue
            # Due and not proven: look again when its pause is over. One that came due
            # while this round worked has not been asked about yet, and is asked at once.
            asked_at_ms = self._asked_at_ms.get(order_ref)
            waited_ms = 0 if asked_at_ms is None else max(now_ms - asked_at_ms, 0)
            pause_ms = 0 if asked_at_ms is None else _retry_after_ms(entry, now_ms)
            wake_in_ms = min(wake_in_ms, max(pause_ms - waited_ms, 0))
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
                # same books and acts on whatever is still due. A stop that came
                # out of a broker call as this error is no failure, and ends below.
                if not being_cancelled():
                    logger.exception(
                        "the deadline keeper's round failed",
                        extra={"action": "deadline_keeper_round_failed", "account_id": self._repo.account_id},
                    )
            if being_cancelled():
                # However the round ended, a keeper that is being stopped stops.
                raise asyncio.CancelledError
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
