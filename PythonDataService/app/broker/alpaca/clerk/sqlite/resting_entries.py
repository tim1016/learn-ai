"""Cancel what has not filled of every option entry whose wait has run out (ADR 0076).

An option entry is a limit order that waits at the broker. Its acceptance
records when the Clerk cancels what has not filled: ``cancel_at_ms``, the
Clerk's own clock at admission plus the owner's entry wait. This is the step
that acts on it.

It keeps no list. What is due is read from the books each time, as the
stopped-run step reads its own worklist (``stopped_run_entries``), so a
restart, a dead task or a skipped round loses nothing: the next reader finds
the same entries. Two callers run it: the deadline keeper, on time
(``deadline_keeper``), and the reconciliation pass, as its backstop.

**What is on the worklist** is one read (``reads.option_entries_with_a_deadline``):
every strategy's option entry the books show working at the broker, that no
live EXIT owns, whatever its ENTER's state. An ENTER voided as never sent
whose order the broker later shows is listed like any other: its order can
still fill. The stopped-run step reads the same predicate, with a stopped
run where this one has a deadline.

The cancel itself is the Clerk's one cancel of an owned entry
(``exit_resolution.cancel_and_prove_owned_entry``). It records that it is
asking before it asks, and then believes only a lookup of the order by its
own id: the answer to the cancel request proves nothing. So an entry reads
"cancellation requested" until the lookup shows it ended, a fill that
arrived during or after the cancel is counted, and a lookup that fails
leaves the outcome unknown.

**Each entry is handled by itself.** A few are asked about at once, each
under the operation claim of its own order, so a broker call that is slow
for one does not hold up the next. An entry whose cancel raises is logged
and left for the next run; the others go on. A cancel that keeps failing
the same way is recorded once, not once a round.

Left alone, by design: an entry before its deadline; an entry from before
entries could rest, which has no deadline; an entry whose order has ended;
and an entry a live EXIT owns, because that EXIT's own machine cancels it
and a second canceller would race it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from app.broker.alpaca.clerk.sqlite import reads
from app.broker.alpaca.clerk.sqlite.entry_outcome import entry_outcome
from app.broker.alpaca.clerk.sqlite.exit_resolution import ENTRY_WAIT_ENDED, cancel_and_prove_owned_entry
from app.broker.alpaca.clerk.sqlite.models import OrderResource
from app.broker.alpaca.clerk.sqlite.off_loop import OffLoop, run_inline
from app.broker.alpaca.clerk.sqlite.repository import (
    ClerkSqliteRepository,
    ExecutionLeaseLost,
    OperationClaimError,
)
from app.broker.contract.ports import BrokerTradePort

logger = logging.getLogger(__name__)

# How many entries are cancelled and proven at once. Each is a cancel and a lookup at the
# broker: enough that one slow answer does not hold the others, few enough to stay polite.
MAX_ENTRIES_AT_ONCE = 4


@dataclass(frozen=True, slots=True)
class RestingEntry:
    """One option entry that may still fill, and when the Clerk cancels what is left of it."""

    order: OrderResource
    cancel_at_ms: int
    # The Clerk's own clock when it admitted the entry: the clock ``cancel_at_ms`` was counted from.
    admitted_at_ms: int

    def due(self, now_ms: int) -> bool:
        """Whether the owner's wait has run out, at the Clerk's clock ``now_ms``.

        The deadline is a time on the Clerk's wall clock, and is judged on
        that same clock: no other clock survives a restart. A clock that
        reads earlier than the entry's own admission has been set back since,
        and the wait can no longer be measured on it. The entry is then due
        at once: a cancel that comes early costs a fill, and one that comes
        late leaves an order in the market past what the owner allowed. A
        clock set back by less than the time already waited delays the
        cancel by that much, and by no more than the wait itself.
        """
        return now_ms >= self.cancel_at_ms or now_ms < self.admitted_at_ms


def resting_entries(repo: ClerkSqliteRepository) -> list[RestingEntry]:
    """Every option entry that may still have to be cancelled at its deadline, the earliest deadline first."""
    with repo.write_fence() as conn:
        listed = reads.option_entries_with_a_deadline(conn)
    return [
        RestingEntry(order=entry.order, cancel_at_ms=entry.cancel_at_ms, admitted_at_ms=entry.admitted_at_ms)
        for entry in listed
    ]


def entries_past_their_wait(repo: ClerkSqliteRepository, now_ms: int) -> list[RestingEntry]:
    """The resting entries whose owner's wait has run out by ``now_ms``."""
    return [entry for entry in resting_entries(repo) if entry.due(now_ms)]


@dataclass(frozen=True, slots=True)
class WaitEndedRound:
    """What one run of the step did, by order reference."""

    # Proven: the books need nothing more from the broker to say what the entry came to.
    ended: tuple[str, ...] = ()
    # Past the deadline and not proven yet: the cancel is asked for, uncertain, raised, or its
    # operation is claimed by someone else. The next run looks again.
    still_due: tuple[str, ...] = ()


def being_cancelled() -> bool:
    """Whether the task running this has been asked to stop.

    A cancel that lands inside a broker call can surface as another error:
    the claim's renewal after the call may fail first. Whoever catches that
    error asks this, and stops instead of going on.
    """
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


async def cancel_resting_entries(
    repo: ClerkSqliteRepository,
    due: Sequence[RestingEntry],
    *,
    trade: BrokerTradePort,
    off_loop: OffLoop | None = None,
) -> WaitEndedRound:
    """Cancel and exact-prove each of ``due``, a few at once, each under its own order's claim.

    A held operation claim means another owner is acting on that order right
    now: its own POST still in flight, an EXIT, a Stop. The entry is skipped
    and the next run retries it; nothing could be cancelled meanwhile anyway.
    Broker failures are folded by ``cancel_and_prove_owned_entry`` itself
    (``ORDER_CANCEL_UNCERTAIN``), and one that tells the books nothing new is
    not written again. Anything else an entry raises is logged with the
    entry it was raised for, and the other entries are still handled. A lost
    execution lease is no one entry's failure: it is raised, as it is to
    every other step of a pass, once the entries in flight have come to rest.
    """
    if not due:
        return WaitEndedRound()
    run = off_loop if off_loop is not None else run_inline
    at_once = asyncio.Semaphore(MAX_ENTRIES_AT_ONCE)

    async def one(entry: RestingEntry) -> bool:
        async with at_once:
            return await _cancel_and_say_whether_proven(repo, entry, trade=trade, run=run)

    answers = await asyncio.gather(*(one(entry) for entry in due), return_exceptions=True)
    for answer in answers:
        if isinstance(answer, BaseException):
            # Only what is not one entry's own failure gets this far: the Clerk has lost its
            # hold on the account. Raised once every entry in flight has come to rest.
            raise answer
    proven = [answer is True for answer in answers]
    return WaitEndedRound(
        ended=tuple(entry.order.order_ref for entry, ended in zip(due, proven, strict=True) if ended),
        still_due=tuple(entry.order.order_ref for entry, ended in zip(due, proven, strict=True) if not ended),
    )


async def _cancel_and_say_whether_proven(
    repo: ClerkSqliteRepository, entry: RestingEntry, *, trade: BrokerTradePort, run: OffLoop
) -> bool:
    """Cancel one entry and look it up; whether the books then prove what it came to."""
    order_ref = entry.order.order_ref
    context = {"account_id": repo.account_id, "order_ref": order_ref, "cancel_at_ms": entry.cancel_at_ms}
    try:
        resolved = await cancel_and_prove_owned_entry(
            repo, entry_order_ref=order_ref, trade=trade, off_loop=run, reason=ENTRY_WAIT_ENDED, say_once=True
        )
        proven = (await run(lambda: entry_outcome(repo, order_ref))).proven
    except OperationClaimError:
        if being_cancelled():
            raise asyncio.CancelledError from None
        logger.info(
            "deferred the cancel of a resting option entry: its operation is claimed",
            extra={"action": "resting_entry_cancel_deferred", **context},
        )
        return False
    except ExecutionLeaseLost:
        raise
    except Exception:
        if being_cancelled():
            raise asyncio.CancelledError from None
        # One entry's failure is its own: the next run reads the same books and tries it again.
        logger.exception(
            "the cancel of a resting option entry failed; the other entries go on",
            extra={"action": "resting_entry_cancel_failed", **context},
        )
        return False
    state = (resolved.broker_state or "").lower() or None
    if not proven:
        logger.warning(
            "a resting option entry is past its wait and not proven ended; the next round looks again",
            extra={"action": "resting_entry_cancel_unconfirmed", "broker_state": state, **context},
        )
        return False
    logger.info(
        "a resting option entry reached the end of its wait and is proven ended",
        extra={"action": "resting_entry_wait_ended", "broker_state": state, **context},
    )
    return True


async def cancel_entries_past_their_wait(
    repo: ClerkSqliteRepository,
    *,
    trade: BrokerTradePort,
    off_loop: OffLoop | None = None,
) -> WaitEndedRound:
    """Cancel and exact-prove each option entry whose wait has run out, at the Clerk's own clock.

    The reconciliation pass's step. One read of the worklist, then
    :func:`cancel_resting_entries` on what is due. On books that bind no
    order to an option contract the read answers from an empty table, no
    row of the custody log is read, and nothing is written.
    """
    run = off_loop if off_loop is not None else run_inline
    due = await run(lambda: entries_past_their_wait(repo, repo.clock()))
    return await cancel_resting_entries(repo, due, trade=trade, off_loop=run)


__all__ = [
    "MAX_ENTRIES_AT_ONCE",
    "RestingEntry",
    "WaitEndedRound",
    "being_cancelled",
    "cancel_entries_past_their_wait",
    "cancel_resting_entries",
    "entries_past_their_wait",
    "resting_entries",
]
