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
sends every scheduled close of an option position that is due and records
each one its session ended on (``scheduled_close``). On a Dry Run's
account, which has no pass on a timer, it also drives on a close that was
taken and has not finished, and takes a failed close again.

**Each step runs by itself.** One that fails is logged with its name and
the other still runs: an entry that cannot be listed must not keep a close
from going out, nor a close an entry's cancel. A stop is no failure and
ends the round. A lost execution lease is no step's own failure: nothing
more can be written, so it ends the round too.

**The keeper is built whole.** Its second step is driven with the facade's
own pricing, and the facade is built with the keeper's nudge. So the nudge
is made first (:class:`Nudge`), the facade with it, and the keeper last.

**One thing the round starts and does not wait for:** the read of what ended
an option position with no order of ours (``option_events``, ADR 0076
decision 22). It walks the account's activities at the broker, which can
take seconds, and neither a resting entry's cancel nor a scheduled close
may wait behind it. So the round starts it once both steps have had their
turn, when none is running, and goes on. It keeps its own cadence,
and between reads it touches nothing. It keeps no list either: a read that
is lost with the task is made again by the next one, and by the pass.

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
one a round, while the keeper works alone; with passes running and the
lookup failing it is written again after each pass (ADR 0076 decision 19).
A nudge from the facade, sent when it accepts an option entry, ends the
sleep at once. The nudge carries no meaning: lost, the idle wake
and the pass still find the entry.

**It always stops.** A stop that lands inside a broker call can surface as
another error, because the claim is renewed after the call. The loop asks
whether its task is being cancelled whenever it catches anything, and ends.

**A Clerk that closes makes one last round** (:meth:`DeadlineKeeper.last_round`).
It cancels and proves every entry on the worklist, whatever is left of its
wait: an order left at the broker by a Clerk that has closed has nobody to
cancel it at its deadline. It is the same step, handed the whole worklist.
It has a few seconds and no more, so a broker that does not answer cannot
hold the process's shutdown. What is not proven by then is left as any
unproven cancel is left, and the next start finishes it. No other step of
a round runs in it, and no read of option events is started: a closing
Clerk sends no new order.

The keeper evaluates no reason's age policy, and no reason gains a clock
from it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field

from app.broker.alpaca.clerk.account_authority import is_synthetic_account_id
from app.broker.alpaca.clerk.recovery_reduction import RecoveryPricing
from app.broker.alpaca.clerk.simulated_option_settlement import option_settlement_simulation
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.off_loop import OffLoop, to_thread
from app.broker.alpaca.clerk.sqlite.option_events import OptionEventPoll, poll_option_events
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository, ExecutionLeaseLost
from app.broker.alpaca.clerk.sqlite.resting_entries import (
    RestingEntry,
    WaitEndedRound,
    being_cancelled,
    cancel_every_resting_entry,
    cancel_resting_entries,
    resting_entries,
)
from app.broker.alpaca.clerk.sqlite.scheduled_close import ScheduledCloseRound, drive_scheduled_closes
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort

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
# The longest a closing Clerk's last round may take: a broker that does not answer must not hold the shutdown.
LAST_ROUND_LIMIT_MS = 3_000


def _retry_after_ms(entry: RestingEntry, now_ms: int) -> int:
    """How long a due entry rests between two asks: a second while its deadline is fresh, then the idle wake."""
    return UNPROVEN_RETRY_MS if now_ms - entry.cancel_at_ms < UNPROVEN_RETRY_WINDOW_MS else IDLE_WAKE_MS


@dataclass(frozen=True, slots=True)
class KeeperRound:
    """What one round did, and how long the keeper may sleep after it."""

    entries: WaitEndedRound
    wake_in_ms: int
    closes: ScheduledCloseRound = field(default_factory=ScheduledCloseRound)
    # The steps that failed in this round, by name. Each is logged where it failed.
    failed: tuple[str, ...] = ()


class Nudge:
    """What ends the keeper's sleep at once: sent by the facade when it accepts an option entry.

    Made before the keeper, so that the facade can be built with it and the
    keeper built whole once the facade exists. It carries no meaning: lost,
    the idle wake and the pass still find what is due.
    """

    def __init__(self) -> None:
        self.sent = asyncio.Event()

    def __call__(self) -> None:
        self.sent.set()


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
        intake: ReentrantAsyncLock,
        pricing: RecoveryPricing,
        read: BrokerReadPort | None = None,
        nudge: Nudge | None = None,
        rest: Rest = rest_until_nudged,
        off_loop: OffLoop = to_thread,
        max_rounds: int | None = None,
    ) -> None:
        self._repo = repo
        self._trade = trade
        self._read = read
        # The lock a scheduled close is accepted under and the pricing it is driven with: the
        # facade's own (ADR 0076). A close is an EXIT like any other.
        self._intake = intake
        self._pricing = pricing
        self._rest = rest
        self._off_loop = off_loop
        self._max_rounds = max_rounds
        self._nudge = nudge if nudge is not None else Nudge()
        self._nudged = self._nudge.sent
        self._task: asyncio.Task[None] | None = None
        # When each due entry was last asked about, on the Clerk's clock. A pause, not a worklist.
        self._asked_at_ms: dict[str, int] = {}
        # The read of option events the last round started, while it runs.
        self._option_events: asyncio.Task[OptionEventPoll | None] | None = None

    def nudge(self) -> None:
        """End the keeper's sleep: a deadline may have been stored that is sooner than it knew."""
        self._nudge()

    async def tick(self) -> KeeperRound:
        """One round: each step by itself, then when to look again."""
        failed: list[str] = []
        resting, entries = await self._step(
            "resting_entries", self._cancel_entries_past_their_wait, ([], WaitEndedRound()), failed
        )
        closes = await self._step("scheduled_closes", self._take_scheduled_closes, ScheduledCloseRound(), failed)
        # Started once both steps have had their turn, and never waited for: a deadline comes first.
        self._start_reading_option_events()
        wake_in_ms = min(self._wake_in_ms(resting, ended=entries.ended), self._wake_for_closes(closes))
        return KeeperRound(entries=entries, wake_in_ms=wake_in_ms, closes=closes, failed=tuple(failed))

    async def _step[Done](
        self, name: str, step: Callable[[], Awaitable[Done]], nothing_done: Done, failed: list[str]
    ) -> Done:
        """Run one step of a round by itself. Its failure is logged with its name and stops no other step.

        A stop is not a failure: it ends the round, also when it came out of
        a broker call as another error. A lost execution lease is raised,
        because nothing more can be written under it.
        """
        try:
            return await step()
        except ExecutionLeaseLost:
            raise
        except Exception:
            if being_cancelled():
                raise asyncio.CancelledError from None
            logger.exception(
                "a step of the deadline keeper's round failed; the round's other step still runs",
                extra={"action": "deadline_keeper_step_failed", "step": name, "account_id": self._repo.account_id},
            )
            failed.append(name)
            return nothing_done

    async def _cancel_entries_past_their_wait(self) -> tuple[list[RestingEntry], WaitEndedRound]:
        """The first step: cancel what has not filled of every entry past its wait. Also says what rests."""
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
        return resting, entries

    async def _take_scheduled_closes(self) -> ScheduledCloseRound:
        """The second step: send every scheduled close that is due, and record each one its session ended on."""
        return await drive_scheduled_closes(
            self._repo, trade=self._trade, intake=self._intake, pricing=self._pricing, off_loop=self._off_loop,
            # A real account's pass drives every unfinished EXIT on and sends a failed close again.
            # A Dry Run has no pass on a timer, so its keeper does both.
            no_pass_on_a_timer=is_synthetic_account_id(self._repo.account_id),
        )

    def _wake_for_closes(self, closes: ScheduledCloseRound) -> int:
        """How long until a close comes due, is ready to be taken again, or its session ends: at most the idle wake.

        A close that is due and not yet taken is looked at again at the
        idle wake: what keeps it from being taken is another EXIT of its bot
        still working, which does not end by the second.
        """
        if closes.next_change_ms is None:
            return IDLE_WAKE_MS
        return min(IDLE_WAKE_MS, max(closes.next_change_ms - self._repo.clock(), 0))

    def _start_reading_option_events(self) -> None:
        """Start the read of option events when none is running. Never waited for: a deadline comes first."""
        if self._read is None or (self._option_events is not None and not self._option_events.done()):
            return
        self._option_events = asyncio.create_task(
            self._read_option_events(self._read), name="alpaca-sqlite-option-events"
        )

    async def _read_option_events(self, read: BrokerReadPort) -> OptionEventPoll | None:
        try:
            return await poll_option_events(
                self._repo, read=read, simulation=option_settlement_simulation(self._trade), off_loop=self._off_loop
            )
        except Exception:
            # Its own failure, not the round's: the next read, and the pass, book what this one did not.
            logger.exception(
                "the keeper's read of option events failed",
                extra={"action": "deadline_keeper_option_events_failed", "account_id": self._repo.account_id},
            )
            return None

    async def option_events_read(self) -> OptionEventPoll | None:
        """Wait for the read of option events the last round started, and say what it did.

        For a caller that must see it finished. ``None`` when the keeper has
        no read port, when no round has run, or when the read failed.
        """
        return None if self._option_events is None else await self._option_events

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
        reading, self._option_events = self._option_events, None
        if reading is not None:
            reading.cancel()
            with suppress(asyncio.CancelledError):
                await reading
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def last_round(self) -> None:
        """The round a closing Clerk makes: cancel and prove every resting entry, due or not (ADR 0076 decision 19).

        The keeper's own task is ended first, so no round of its own holds
        an entry's claim. Then the whole worklist is handed to the step
        every round uses, the oldest deadline first, with the reason that
        the Clerk closed. An entry an EXIT owns is not on the worklist, as
        in any round.

        The round has ``LAST_ROUND_LIMIT_MS`` and no more. What it stops in
        the middle of stays on the books as it is: asked for, or uncertain.
        The next start's stopped-run step, or its keeper's first round,
        finishes it. Nothing is raised, because a closing Clerk must still
        close its books. What each entry came to is logged by the step.
        """
        await self.stop()
        try:
            async with asyncio.timeout(LAST_ROUND_LIMIT_MS / 1000):
                await cancel_every_resting_entry(self._repo, trade=self._trade, off_loop=self._off_loop)
        except TimeoutError:
            logger.warning(
                "the closing Clerk's last round ran out of time; what is not proven is left for the next start",
                extra={"action": "deadline_keeper_last_round_out_of_time", "account_id": self._repo.account_id},
            )
        except Exception:
            # A stop of the closing itself is not caught here and ends it. Anything else must not
            # keep the books from being closed: the next start reads them and acts on what is left.
            logger.exception(
                "the closing Clerk's last round failed; what is not proven is left for the next start",
                extra={"action": "deadline_keeper_last_round_failed", "account_id": self._repo.account_id},
            )


__all__ = [
    "IDLE_WAKE_MS",
    "LAST_ROUND_LIMIT_MS",
    "UNPROVEN_RETRY_MS",
    "UNPROVEN_RETRY_WINDOW_MS",
    "DeadlineKeeper",
    "KeeperRound",
    "Nudge",
]
