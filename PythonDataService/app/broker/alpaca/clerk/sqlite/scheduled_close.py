"""Close an option position when the close it owes comes due, whatever its run is doing (ADR 0076).

Two closes are the Clerk's own to make. A position holding a contract that
expires today is closed ahead of the session's close: 45 minutes before it,
unless the owner set another lead. A structure with a sold leg is closed
before every session's close it is held through: 10 minutes before it. Both
times are counted back from the calendar's own session close
(``app.options.deadlines``), so a half day moves them and a holiday has none.

**The close is owed by what is held.** Nothing is created when an entry
fills and nothing is stored to be lost. Each time, the books are read: every
option contract any bot holds (``reads.held_option_contracts``), the entry
that opened it, the order terms kept with that entry, and the calendar. So an
owner's Stop, a dead runner and a restart change no holding and cancel no
close. A position the strategy does not know it holds is closed like any
other: a part of an entry that filled after the strategy dropped it, a fill
on an entry already told as failed, what a run that ended left behind.

**The positions say first whether anything can be owed.** A close is owed on
a held contract's expiry day, and every session by a holding with a sold
contract. So a bot whose contracts are all bought and all expire on a later
day owes nothing and missed nothing, and none of its entries is read: it
costs the same few steps however long its history is.

**It is one more exit.** A close that is due is accepted as a recovery EXIT
under ``SCHEDULED_CLOSE_DECISION_PREFIX``, keyed on the session's date and
the entry, so asking again drives the same EXIT and never makes a second.
Its acceptance records which close it is and when it was due. It is accepted
while the bot's run is ACTIVE, in one locked run that first asks: is the
close still owed, its own session not yet closed at the Clerk's clock now,
is the contract still held, is no other EXIT of the bot working, and has
the Clerk not stopped exiting this bot by itself? A deciding program's EXIT
is accepted under the same lock, so when the two meet only one is accepted
and one closing order is made. The EXIT machine then does what it does for
every option close: a market order in the regular session, held on a halt
of the underlying, never sent for a contract past its last session.

**Who runs it.** The deadline keeper, on time, and the account's
reconciliation pass, as its backstop (:func:`drive_scheduled_closes`). Once
a close is taken it is an EXIT like any other, and the pass's own recovery
of unfinished operations drives it on: it sends it when a halt lifts, and
looks its order up until it has ended. A Dry Run has no pass on a timer, so
there the keeper drives on a close that was taken and has not finished.

**A close is not an outcome.** A session can end with the contract still
held: a halt that did not lift, a refusal, nothing bid, the Clerk down. That
is recorded as ``SCHEDULED_CLOSE_MISSED``, a notice on that bot alone. It
holds the bot's new entries, allows every reduction, names each contract and
what can now happen to it, and ends when the bot holds none of them. This
module raises it, rewrites it when what it names changes, and ends it, from
the same read of the books.

**While the Clerk has stopped exiting a bot by itself** (``EXIT_STUCK``) it
accepts no scheduled close for it: it cannot know whether its last order
traded. The close reads as left to the owner, and is recorded missed.

**A failed close is tried again every 30 seconds while its own session is
open.** The rule is asked of the books (:func:`close_being_retried`): the
contract is still held, it owes a close in the session open now, and the
Clerk took that close. Which order of the bot failed last does not come
into it. On a real account the stuck-EXIT watchdog sends the close again,
on the pass. It neither counts it out nor gives the bot up in that session,
and what was tried under the rule is not counted in a later session
(``exit_watchdog``). A Dry Run has no pass on a timer, so there the
keeper's own step takes the failed close again. Whichever takes a retry
asks, in the locked run that takes it, whether the wait has passed since
the bot's newest exit (:func:`inside_the_retry_wait`).

**A stop loses no close and sends none twice.** A close cut by a stop after
it was accepted stays accepted, and the next start drives it as it drives
every unfinished EXIT. One cut inside its send is looked up by its order's
own id before anything is sent again.

**A Clerk that has begun to close takes no close.** It says so first, under
the lock a close is accepted under (``repo.closing``), and its keeper and
its reconciliation loop both go on until the shutdown stops them. So the
run that would accept a close asks, and so does the watchdog's retry of a
failed one. A close is accepted before the Clerk began to close, and is
then finished or left for the next start, or it is not taken. The keeper's
last round takes none either.

Not here: reading an expiry, an exercise or an assignment and taking the
contract off the books. After a miss on an expired contract nothing is sent
and the bot stays held under its notice.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.broker.alpaca.clerk.account_authority import simulated_account_named
from app.broker.alpaca.clerk.account_money import quantity_text
from app.broker.alpaca.clerk.recovery_reduction import RecoveryPricing
from app.broker.alpaca.clerk.sqlite import reads
from app.broker.alpaca.clerk.sqlite.ended_run_close import Holding, decision_id_under, holding_of_entry
from app.broker.alpaca.clerk.sqlite.exit import (
    accept_recovery_exit,
    exit_effect_operation_id,
    newest_reducible_entry,
)
from app.broker.alpaca.clerk.sqlite.exit_resolution import (
    SCHEDULED_CLOSE_DECISION_PREFIX,
    expired_option_close,
    resolve_exit,
)
from app.broker.alpaca.clerk.sqlite.facts import ExitAcceptedFacts, ScheduledCloseFacts, UncertaintyResolvedFacts
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.models import EffectOperationResource, ExitSubmission, TransitionInput
from app.broker.alpaca.clerk.sqlite.off_loop import OffLoop, run_inline
from app.broker.alpaca.clerk.sqlite.option_admission import ENTRY_CLOSE_TERMS
from app.broker.alpaca.clerk.sqlite.option_entry import option_entry_of_order
from app.broker.alpaca.clerk.sqlite.reads import FAILED_EFFECT_STATES
from app.broker.alpaca.clerk.sqlite.repository import (
    ClerkSqliteRepository,
    ExecutionLeaseLost,
    OperationClaimError,
)
from app.broker.alpaca.clerk.sqlite.resting_entries import being_cancelled
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    AdmissionBlockedError,
    automatic_exit_stopped,
    raise_uncertainty,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    SCHEDULED_CLOSE_MISSED_REASON_CODE,
    MissedScheduledClose,
    ScheduledCloseMissedCause,
)
from app.broker.contract.ports import BrokerTradePort
from app.options.contract import OptionContract
from app.options.deadlines import CloseKind, ScheduledClose, close_missed_at, close_owed_at, past_last_session
from app.utils.et_words import et_clock_words, et_when_words
from app.utils.session_anchors import et_date_at_ms

logger = logging.getLogger(__name__)

# How many closes are accepted and driven at once. Each is a send or a lookup at the
# broker: one slow answer must not hold up another bot's close.
MAX_CLOSES_AT_ONCE = 4

# A close taken more than this long after it was due is told as late: the Clerk was down or held.
LATE_AFTER_MS = 60_000

# How soon a failed scheduled close is ready to be sent again, while the session it was due in is open.
SCHEDULED_CLOSE_RETRY_AFTER_MS = 30_000

_MISSED_HEADLINE = "A scheduled close was missed: the position is still open"
_ENTRIES_HELD = "New entries are on hold for this bot. No other bot is held."


@dataclass(frozen=True, slots=True)
class HeldContract:
    """One option contract a bot holds, the entry that opened it, and what the Clerk owes for it at one instant."""

    strategy_instance_id: str
    symbol: str
    # Contracts held, signed: a bought position is above zero.
    quantity: float
    entry_order_ref: str
    # The close owed in the session of the instant's own New York date, while that session has not ended.
    owed: ScheduledClose | None
    # A close a session ended on with the contract still held.
    missed: ScheduledClose | None
    # Where the owed close stands, by the four states an ended run's close has. None when none is owed.
    holding: Holding | None
    # The effect of a scheduled close of this bot that was accepted and has not finished: today's,
    # or an earlier session's still held for an open.
    unfinished: str | None
    # The Clerk has stopped exiting this bot's position by itself.
    left_to_owner: bool
    # The contract's last session has ended: no order can close it.
    expired: bool
    # When the bot's newest exit was taken, for an owed close that failed. None for any other.
    last_exit_taken_at_ms: int | None = None

    def to_be_taken(self, now_ms: int) -> bool:
        """Whether the close owed today is due, not yet taken by any EXIT, and the Clerk's to take."""
        return (
            self.owed is not None
            and self.owed.due(now_ms)
            and self.holding is not None
            and self.holding.state == "owed"
            and not self.left_to_owner
        )

    @property
    def again_at_ms(self) -> int | None:
        """When an owed close that failed is ready to be taken again, or None when none failed.

        Counted from the bot's newest exit, whoever made it: a try is never
        closer than the retry wait to the exit before it.
        """
        if self.owed is None or self.holding is None or self.holding.state != "failed":
            return None
        return (self.last_exit_taken_at_ms or self.owed.due_ms) + SCHEDULED_CLOSE_RETRY_AFTER_MS

    def to_be_taken_again(self, now_ms: int) -> bool:
        """Whether the close owed today failed and is ready to be taken again.

        Asked only where nothing else retries a failed close: on an account
        with no reconciliation pass on a timer. A close is owed only until
        its session's close, so none is taken again after it.
        """
        return (
            self.again_at_ms is not None
            and self.again_at_ms <= now_ms
            and self.unfinished is None
            and not self.left_to_owner
        )


@dataclass(frozen=True, slots=True)
class HeldContracts:
    """One read of every option contract held on the account."""

    # Each contract of every bot that can owe a close today or have missed one. A bot whose
    # contracts are all bought and all expire on a later day is not read and is not here.
    held: tuple[HeldContract, ...] = ()
    # Every bot holding an option contract, read or not.
    holders: frozenset[str] = frozenset()
    # Bots holding a contract whose entry could not be read. Nothing is concluded about them.
    unread: frozenset[str] = frozenset()
    # Bots with an open missed-close notice, held or not.
    noticed: tuple[str, ...] = ()


def decision_prefix(session_open_ms: int) -> str:
    """The namespace every scheduled close of one session is decided under: the prefix and the session's date."""
    return f"{SCHEDULED_CLOSE_DECISION_PREFIX}{et_date_at_ms(session_open_ms):%Y%m%d}-"


def held_contracts(
    repo: ClerkSqliteRepository, now_ms: int, *, strategy_instance_id: str | None = None
) -> HeldContracts:
    """Every option contract that can owe a close, with what it owes at the Clerk's clock ``now_ms``.

    One read of the positions that are not zero. Books that never traded an
    option stop there: an empty table, and no row of the custody log.

    Then each bot's positions say, by themselves, whether anything can be
    owed (:func:`_a_close_can_be_owed`). Only when one can are the entries
    behind them read. So a bot holding a contract that is not due today
    costs the same few steps however long its history is.

    One bot's unreadable books are logged with their stack and named in
    ``unread``. The others are still read: a pass that raised here would
    fail every later pass on the account.
    """
    with repo.write_fence() as conn:
        positions = reads.held_option_contracts(conn)
        noticed = reads.strategies_under_active_uncertainty(conn, SCHEDULED_CLOSE_MISSED_REASON_CODE)
    of_bot: dict[str, list[reads.HeldOptionContract]] = {}
    for position in positions:
        if strategy_instance_id is not None and position.strategy_instance_id != strategy_instance_id:
            continue
        if position_quantity_is_nonzero(position.quantity):
            of_bot.setdefault(position.strategy_instance_id, []).append(position)
    held: list[HeldContract] = []
    unread: set[str] = set()
    for own in of_bot.values():
        if not _a_close_can_be_owed(own, now_ms):
            continue
        for position in own:
            try:
                held.append(_held_contract(repo, position, now_ms))
            except (LookupError, ValueError, AssertionError):
                # What an entry's own facts raise when they are missing or cannot be read back.
                unread.add(position.strategy_instance_id)
                logger.exception(
                    "a held option contract's entry could not be read; no close is worked out for it",
                    extra={
                        "action": "scheduled_close_holding_unreadable",
                        "account_id": repo.account_id,
                        "strategy_instance_id": position.strategy_instance_id,
                        "symbol": position.symbol,
                    },
                )
    return HeldContracts(
        held=tuple(held), holders=frozenset(of_bot), unread=frozenset(unread), noticed=tuple(noticed)
    )


def _a_close_can_be_owed(own: Sequence[reads.HeldOptionContract], now_ms: int) -> bool:
    """Whether what one bot holds can owe a close today or have missed one, told from its positions alone.

    A close is owed on a held contract's expiry day, and every session by a
    structure with a sold leg. One is missed once a contract is past its
    last session, or a sold leg was held through a session's close. So a
    bot whose contracts are all bought and all expire on a later day owes
    nothing and missed nothing: none of its entries needs reading.
    """
    today = et_date_at_ms(now_ms)
    for position in own:
        if position.quantity < 0:
            return True
        try:
            expiry = OptionContract.from_occ_symbol(position.symbol).expiry_date
        except ValueError:
            # A symbol that names no contract: the read of its entry says so, with its stack.
            return True
        if expiry <= today or past_last_session(expiry, now_ms):
            return True
    return False


def _held_contract(repo: ClerkSqliteRepository, position: reads.HeldOptionContract, now_ms: int) -> HeldContract:
    sid, symbol = position.strategy_instance_id, position.symbol
    target = newest_reducible_entry(repo, sid, symbol)
    entry = None if target is None else option_entry_of_order(repo, target.order_ref)
    if target is None or entry is None:
        # A contract is held only through an entry order of its own bot in that contract.
        raise LookupError(f"{sid} holds {symbol} and no option entry of its own opened it")
    # The entry's own terms. One accepted before entries kept theirs is held to the starting ones.
    terms = ENTRY_CLOSE_TERMS if entry.terms is None else entry.terms.close_terms
    owed = close_owed_at(entry.structure, terms, at_ms=now_ms)
    missed = close_missed_at(entry.structure, terms, held_since_ms=entry.admitted_at_ms, at_ms=now_ms)
    concerns_the_clerk = owed is not None or missed is not None
    holding = (
        None if owed is None
        else holding_of_entry(repo, sid, symbol, target, decision_prefix=decision_prefix(owed.session_open_ms))
    )
    newest_exit = (
        repo.last_strategy_transition(strategy_instance_id=sid, transition_kind="EXIT_ACCEPTED")
        if holding is not None and holding.state == "failed"
        else None
    )
    return HeldContract(
        strategy_instance_id=sid,
        symbol=symbol,
        quantity=position.quantity,
        entry_order_ref=target.order_ref,
        owed=owed,
        missed=missed,
        holding=holding,
        unfinished=_unfinished_close(repo, sid) if concerns_the_clerk else None,
        left_to_owner=concerns_the_clerk and automatic_exit_stopped(repo, sid),
        expired=past_last_session(OptionContract.from_occ_symbol(symbol).expiry_date, now_ms),
        last_exit_taken_at_ms=None if newest_exit is None else newest_exit["clerk_observed_at_ms"],
    )


def _is_a_scheduled_close(effect: EffectOperationResource) -> bool:
    return effect.command_id.startswith(f"cmd:{effect.strategy_instance_id}:{SCHEDULED_CLOSE_DECISION_PREFIX}")


def _unfinished_close(repo: ClerkSqliteRepository, strategy_instance_id: str) -> str | None:
    """The bot's working EXIT when it is a scheduled close, of today's session or an earlier one."""
    active = repo.active_exit_for_strategy(strategy_instance_id)
    return active.effect_operation_id if active is not None and _is_a_scheduled_close(active) else None


@dataclass(frozen=True, slots=True)
class ScheduledCloseRound:
    """What one run of the step did."""

    # Closes accepted in this run, by their EXIT's effect.
    taken: tuple[str, ...] = ()
    # EXITs driven in this run, the ones just taken included.
    driven: tuple[str, ...] = ()
    # Closes left for the next run: their operation is claimed, or a hold refuses reductions.
    deferred: tuple[str, ...] = ()
    # Each bot and contract a missed-close notice stands for after this run.
    missed: tuple[tuple[str, str], ...] = ()
    # The next instant something changes without anyone acting: a close comes due, or its session ends.
    next_change_ms: int | None = None


async def drive_scheduled_closes(
    repo: ClerkSqliteRepository,
    *,
    trade: BrokerTradePort,
    intake: ReentrantAsyncLock,
    pricing: RecoveryPricing,
    off_loop: OffLoop | None = None,
    no_pass_on_a_timer: bool = False,
) -> ScheduledCloseRound:
    """Send every scheduled close that is due, and record each one its session ended on.

    The deadline keeper's step and the reconciliation pass's. One read of
    what is held, then each contract by itself, a few at once: one that is
    slow or raises does not hold up the others. On books holding no option
    contract and no missed-close notice it reads that once, takes no lock
    and writes nothing.

    ``no_pass_on_a_timer`` is said by the keeper of an account nothing else
    looks at on a timer: a Dry Run's. There the step also does the two
    things a pass does on a real account. It drives on a close that was
    taken and has not finished. And it takes a failed close again, once
    the retry wait has passed since the bot's newest exit, while the
    close's own session is open. The pass asks for neither: its recovery of
    operations has driven every unfinished EXIT earlier in the same pass,
    and its stuck-EXIT watchdog sends a failed close again.

    A held operation claim means another owner is driving that EXIT right
    now, and a refused reduction means a hold stands on the account: the
    close is left for the next run. A lost execution lease is no one
    contract's failure and is raised once the closes in flight have come to
    rest.

    A stop can cut the step anywhere. A close cut after it was accepted and
    before its order was sent stays accepted on the books, and the next
    start drives it as it drives every unfinished EXIT. One cut inside its
    send is looked up by the order's own id before anything is sent again.
    So a stop loses no close and sends none twice.
    """
    run = off_loop if off_loop is not None else run_inline
    now_ms = repo.clock()
    read = await run(lambda: held_contracts(repo, now_ms))
    at_once = asyncio.Semaphore(MAX_CLOSES_AT_ONCE)

    async def one(item: HeldContract) -> _Done | None:
        async with at_once:
            return await _take_or_drive(repo, item, trade=trade, intake=intake, pricing=pricing, run=run)

    # One EXIT closes one bot's position: two contracts of one bot point at one unfinished close.
    worked: dict[str, HeldContract] = {}
    for item in read.held:
        if item.unfinished is not None:
            if no_pass_on_a_timer:
                worked.setdefault(item.unfinished, item)
        elif item.to_be_taken(now_ms) or (no_pass_on_a_timer and item.to_be_taken_again(now_ms)):
            worked.setdefault(f"{item.strategy_instance_id}:{item.symbol}", item)
    answers = await asyncio.gather(*(one(item) for item in worked.values()), return_exceptions=True)
    for answer in answers:
        if isinstance(answer, BaseException):
            raise answer
    done = [answer for answer in answers if answer is not None]
    missed: tuple[tuple[str, str], ...] = ()
    # The lock is taken only when a notice may have to be written or ended.
    if read.noticed or any(item.missed is not None for item in read.held):
        missed = await intake.off_loop(_record_missed_closes, repo, read, now_ms)
    return ScheduledCloseRound(
        taken=tuple(result.effect_operation_id for result in done if result.taken),
        driven=tuple(result.effect_operation_id for result in done if result.driven),
        deferred=tuple(result.effect_operation_id for result in done if not result.driven),
        missed=missed,
        next_change_ms=_next_change_ms(read.held, now_ms, with_retries=no_pass_on_a_timer),
    )


def _next_change_ms(held: Sequence[HeldContract], now_ms: int, *, with_retries: bool = False) -> int | None:
    """When a close next comes due or the session it is owed in ends: always later than ``now_ms``.

    ``with_retries`` also counts the instant a failed close is ready to be taken again.
    """
    changes: list[int] = []
    for item in held:
        if item.owed is None:
            continue
        changes.append(item.owed.due_ms if now_ms < item.owed.due_ms else item.owed.session_close_ms)
        if with_retries and item.again_at_ms is not None and now_ms < item.again_at_ms:
            changes.append(item.again_at_ms)
    return min(changes, default=None)


@dataclass(frozen=True, slots=True)
class _Done:
    effect_operation_id: str
    taken: bool
    driven: bool


async def _take_or_drive(
    repo: ClerkSqliteRepository,
    item: HeldContract,
    *,
    trade: BrokerTradePort,
    intake: ReentrantAsyncLock,
    pricing: RecoveryPricing,
    run: OffLoop,
) -> _Done | None:
    """Accept one due close and drive it, or drive on one already accepted. ``None`` when it is no longer owed."""
    context: dict[str, Any] = {
        "account_id": repo.account_id,
        "strategy_instance_id": item.strategy_instance_id,
        "symbol": item.symbol,
    }
    effect_operation_id = item.unfinished
    taken = False
    try:
        if effect_operation_id is None:
            assert item.owed is not None and item.holding is not None
            accepted = await intake.off_loop(_accept_while_still_owed, repo, item)
            if accepted is None or accepted.effect_operation_id is None:
                return None
            effect_operation_id, taken = accepted.effect_operation_id, accepted.created
            if taken:
                logger.warning(
                    "took a scheduled close of an option position",
                    extra={
                        "action": "scheduled_close_taken",
                        "effect_operation_id": effect_operation_id,
                        "command_id": accepted.command.command_id,
                        "again": item.holding.state == "failed",
                        "kind": item.owed.kind.value,
                        "due_ms": item.owed.due_ms,
                        "late_by_ms": max(repo.clock() - item.owed.due_ms, 0),
                        **context,
                    },
                )
        # A close of the Clerk's own is no operator's Flatten: it is sent with no account check of its own.
        await resolve_exit(
            repo, effect_operation_id=effect_operation_id, trade=trade, pricing=pricing, read=None, off_loop=run
        )
    except (OperationClaimError, AdmissionBlockedError) as held_up:
        if being_cancelled():
            raise asyncio.CancelledError from None
        logger.info(
            "deferred a scheduled close: its operation is claimed, or a hold refuses reductions",
            extra={
                "action": "scheduled_close_deferred",
                "effect_operation_id": effect_operation_id,
                "why": str(held_up),
                **context,
            },
        )
        return None if effect_operation_id is None else _Done(effect_operation_id, taken=taken, driven=False)
    except ExecutionLeaseLost:
        raise
    except Exception:
        if being_cancelled():
            raise asyncio.CancelledError from None
        # One contract's failure is its own: the next run reads the same books and tries it again.
        logger.exception(
            "a scheduled close failed; the other closes go on",
            extra={"action": "scheduled_close_failed", "effect_operation_id": effect_operation_id, **context},
        )
        return None if effect_operation_id is None else _Done(effect_operation_id, taken=taken, driven=False)
    return _Done(effect_operation_id, taken=taken, driven=True)


def _accept_while_still_owed(repo: ClerkSqliteRepository, item: HeldContract) -> ExitSubmission | None:
    """Accept one scheduled close if it is still the Clerk's to take; one locked run, under intake.

    Asked again here, in the run that accepts: the contract is still held,
    no EXIT of the bot is working, and the Clerk has not stopped exiting the
    bot by itself. A deciding program's EXIT is accepted under the same
    lock, so of two that meet only the first is accepted, and the second
    finds an EXIT working. ``None`` writes nothing.

    A Clerk that has begun to close takes none. It says so under this same
    lock, so a close is accepted before that or not at all.

    The close is asked of the Clerk's clock again too. It was read as due,
    and its turn can come late: behind four other closes a slow broker has
    not answered, or behind this lock. A close is owed only until its own
    session closes (``ScheduledClose.due``). Once that has come it is not
    accepted, in that session's name or any other's. The next run reads the
    contract as held past its close and records the close as missed, and a
    holding with a sold leg owes the next session's close at its own time.

    A close that failed is taken again as a decision of its own, numbered
    after the first. It is asked one thing more: has the retry wait passed
    since the bot's newest exit, as the books show it now?
    """
    assert item.owed is not None and item.holding is not None
    sid = item.strategy_instance_id
    if repo.closing:
        return None
    if not item.owed.due(repo.clock()):
        return None
    if not position_quantity_is_nonzero(repo.position(sid, item.symbol)):
        return None
    if repo.active_exit_for_strategy(sid) is not None:
        return None
    if automatic_exit_stopped(repo, sid):
        return None
    decision_id = item.holding.close.decision_id
    if item.holding.state == "failed":
        if inside_the_retry_wait(repo, sid):
            return None
        decision_id = _next_try_of(repo, sid, decision_id)
    return accept_recovery_exit(
        repo,
        account_id=repo.account_id,
        strategy_instance_id=sid,
        decision_id=decision_id,
        entry_order_ref=item.holding.close.entry_order_ref,
        scheduled_close=ScheduledCloseFacts(
            kind=item.owed.kind.value, due_ms=item.owed.due_ms, session_open_ms=item.owed.session_open_ms
        ),
    )


def _next_try_of(repo: ClerkSqliteRepository, strategy_instance_id: str, first_decision_id: str) -> str:
    """The decision a failed close is taken again under: the first one's id and the next number not yet used."""
    tries = 1
    while repo.get_command(f"cmd:{strategy_instance_id}:{first_decision_id}-again-{tries}") is not None:
        tries += 1
    return f"{first_decision_id}-again-{tries}"


def inside_the_retry_wait(repo: ClerkSqliteRepository, strategy_instance_id: str) -> bool:
    """Whether the bot's newest exit was taken less than the retry wait ago, as the books show it now.

    Asked in the locked run that would send a failed scheduled close
    again, by whichever takes that try: the keeper's step, or the
    stuck-EXIT watchdog under the close's own rule. So such a try is
    never closer than the wait to the exit before it, whoever made that
    one.
    """
    newest = repo.last_strategy_transition(strategy_instance_id=strategy_instance_id, transition_kind="EXIT_ACCEPTED")
    return newest is not None and repo.clock() < newest["clerk_observed_at_ms"] + SCHEDULED_CLOSE_RETRY_AFTER_MS


def close_being_retried(
    repo: ClerkSqliteRepository, strategy_instance_id: str, symbol: str, now_ms: int
) -> ScheduledCloseFacts | None:
    """The scheduled close of one bot's holding that the Clerk took in the session open at ``now_ms``, or None.

    The one question behind a scheduled close's own retry rule, asked of
    the books and of nothing else: is the contract still held, does it owe
    a close in the session that is open now, and did the Clerk accept that
    close? Which order of the bot failed last does not come into it. So
    another exit of the bot that fails after the close leaves the rule
    standing. The answer is the close as its own acceptance recorded it.

    None before that session opens and from its close on, for a holding
    that owes no close today, for one whose close was never taken, and for
    one whose entry cannot be read.
    """
    for item in held_contracts(repo, now_ms, strategy_instance_id=strategy_instance_id).held:
        if item.symbol != symbol.upper() or item.owed is None or now_ms < item.owed.session_open_ms:
            continue
        first = _first_acceptance(
            repo, strategy_instance_id, session_open_ms=item.owed.session_open_ms, entry_order_ref=item.entry_order_ref
        )
        return None if first is None else ExitAcceptedFacts.from_facts_json(first["facts_json"]).the_scheduled_close
    return None


# ── a close the session ended on ─────────────────────────────────────────


def _record_missed_closes(repo: ClerkSqliteRepository, read: HeldContracts, now_ms: int) -> tuple[tuple[str, str], ...]:
    """Raise, rewrite or end each bot's missed-close notice from the books; one locked run, under intake.

    A contract is named only while it is still held, read again here. A
    bot's notice is ended once it names nothing, and never for a bot whose
    books could not be read. The words hold no clock value, so a later run
    that finds the same contracts writes nothing.
    """
    missed: dict[str, list[tuple[HeldContract, float]]] = {}
    for item in read.held:
        if item.missed is None:
            continue
        quantity = repo.position(item.strategy_instance_id, item.symbol)
        if position_quantity_is_nonzero(quantity):
            missed.setdefault(item.strategy_instance_id, []).append((item, quantity))
    stands: list[tuple[str, str]] = []
    for sid, contracts in sorted(missed.items()):
        try:
            outcome = raise_uncertainty(repo, strategy_instance_id=sid, **_missed_notice(repo, contracts, now_ms))
        except ExecutionLeaseLost:
            raise
        except Exception:
            # One bot's notice is its own: the other bots' are still written, and the next run tries this one again.
            logger.exception(
                "a missed-close notice could not be written; the other bots' notices go on",
                extra={"action": "scheduled_close_missed_unwritten", "account_id": repo.account_id,
                       "strategy_instance_id": sid},
            )
            continue
        stands.extend((sid, item.symbol) for item, _quantity in contracts)
        if outcome != "unchanged":
            logger.error(
                "a session ended with an option contract still held that the Clerk owed a close of",
                extra={
                    "action": "scheduled_close_missed",
                    "account_id": repo.account_id,
                    "strategy_instance_id": sid,
                    "symbols": [item.symbol for item, _quantity in contracts],
                    "notice": outcome,
                },
            )
    for sid in read.noticed:
        if sid in missed or sid in read.unread:
            continue
        try:
            ended = _end_missed_notice(repo, sid)
        except ExecutionLeaseLost:
            raise
        except Exception:
            logger.exception(
                "a missed-close notice could not be ended; the other bots' notices go on",
                extra={"action": "scheduled_close_missed_unended", "account_id": repo.account_id,
                       "strategy_instance_id": sid},
            )
            continue
        if ended:
            logger.info(
                "a missed-close notice ended: its bot holds none of the contracts it named",
                extra={"action": "scheduled_close_missed_ended", "account_id": repo.account_id,
                       "strategy_instance_id": sid},
            )
    return tuple(stands)


def _end_missed_notice(repo: ClerkSqliteRepository, strategy_instance_id: str) -> bool:
    def build_transition(uncertainty_id: str) -> TransitionInput:
        facts = UncertaintyResolvedFacts(
            uncertainty_id=uncertainty_id,
            resolution_kind="ATTRIBUTED_FLAT_PROVEN",
            evidence_refs=["attributed_positions"],
        )
        return TransitionInput(
            strategy_instance_id=strategy_instance_id,
            transition_kind="UNCERTAINTY_RESOLVED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=repo.clock(),
            summary_code="SCHEDULED_CLOSE_MISSED_RESOLVED",
            facts_json=facts.to_facts_json(),
        )

    return repo.resolve_uncertainty_if_active(
        scope="CUSTODY_SUBJECT",
        reason_code=SCHEDULED_CLOSE_MISSED_REASON_CODE,
        strategy_instance_id=strategy_instance_id,
        build_transition=build_transition,
    )


def _missed_notice(
    repo: ClerkSqliteRepository, contracts: Sequence[tuple[HeldContract, float]], now_ms: int
) -> dict[str, Any]:
    """The notice of one bot's missed closes: what ``raise_uncertainty`` is handed."""
    simulated = simulated_account_named(repo.account_id)
    ordered = sorted(contracts, key=lambda held: held[0].symbol)
    closes = []
    for item, quantity in ordered:
        assert item.missed is not None
        closes.append(
            MissedScheduledClose(
                symbol=item.symbol,
                attributed_qty=quantity,
                kind=item.missed.kind.value,
                due_ms=item.missed.due_ms,
                session_open_ms=item.missed.session_open_ms,
            )
        )
    return {
        "reason_code": SCHEDULED_CLOSE_MISSED_REASON_CODE,
        "headline": _MISSED_HEADLINE,
        "explanation": " ".join(_missed_words(item, quantity, simulated=simulated) for item, quantity in ordered),
        "operator_impact": (
            f"{_ENTRIES_HELD} No order can close an expired contract."
            if all(item.expired for item, _quantity in ordered)
            else f"{_ENTRIES_HELD} Closing what it holds stays allowed."
        ),
        # Said once each, in the contracts' own order.
        "next_step": " ".join(
            dict.fromkeys(_missed_next_step(item, simulated=simulated, now_ms=now_ms) for item, _quantity in ordered)
        ),
        "evidence_refs": tuple(item.entry_order_ref for item, _quantity in ordered),
        "cause_facts": ScheduledCloseMissedCause(closes=tuple(closes)).to_mapping(),
        "severity": "error",
    }


def _why_owed(kind: CloseKind) -> str:
    return "its contract expired that day" if kind is CloseKind.EXPIRY_DAY else "a sold option is not held overnight"


def _missed_words(item: HeldContract, quantity: float, *, simulated: str | None) -> str:
    """What was to be closed, by when and why, that it is still held, and what can now happen to it."""
    assert item.missed is not None
    # The due time is written as of itself, so the words never change with the clock.
    due = et_when_words(item.missed.due_ms, now_ms=item.missed.due_ms)
    said = [
        f"The Clerk was to close {quantity_text(abs(quantity))} {item.symbol} by {due}: {_why_owed(item.missed.kind)}. "
        "That session ended with it still held."
    ]
    if item.left_to_owner:
        said.append("The Clerk has stopped exiting this bot's position by itself and sends no close for it.")
    said.append(_what_can_happen(quantity, expired=item.expired, simulated=simulated))
    return " ".join(said)


def _what_can_happen(quantity: float, *, expired: bool, simulated: str | None) -> str:
    if simulated is not None:
        gone = "The contract has expired, so nothing can close it. " if expired else ""
        return f"{gone}This is a {simulated}: nothing real is held."
    if expired and quantity > 0:
        return (
            "The contract has expired, so no order can close it. If it ended in the money Alpaca exercises it "
            "and the account trades the shares at the strike. Alpaca may instead have sold it in the hour "
            "before the close. If it ended out of the money it is worth nothing."
        )
    if expired:
        return (
            "The contract has expired, so no order can close it. If it ended in the money it is assigned and "
            "the account trades the shares at the strike."
        )
    if quantity < 0:
        return (
            "A sold option held past the close can be assigned overnight, and the account then trades the "
            "shares at the strike."
        )
    return "The contract still trades."


def _missed_next_step(item: HeldContract, *, simulated: str | None, now_ms: int) -> str:
    if item.expired:
        expired = expired_option_close(item.symbol, now_ms)
        settled = (
            "No order can close an expired contract, so there is nothing to send." if expired is None
            else expired.next_step
        )
        if simulated is not None:
            return settled
        # Never a write-off: an expired contract is not offered one, whatever notice stands (ADR 0076 decision 22).
        return (
            f"{settled} Look at the account at Alpaca for an exercise, an assignment or a sale. An expired "
            "contract is not written off. If no record of its end comes, the notice that it is not settled "
            "says what can be done."
        )
    if item.left_to_owner:
        return (
            "Stop the bot and flatten the position from its page once the regular session is open: the Clerk "
            "sends nothing for this bot by itself."
        )
    # The next session counts on from the ordinary tries made before the close was taken (ADR 0076).
    return (
        "The Clerk sends a close of it in the next regular session. There is one case where it does not: its "
        "own retries of this bot's exit had already run out before it took this close. Then it sends nothing, "
        "and stops exiting the bot by itself when that session opens. Either way you can stop the bot and "
        "flatten the position from its page once that session is open."
    )


# ── one read for the screens ─────────────────────────────────────────────


class CloseStanding(StrEnum):
    """Where a scheduled close stands. Flat is said only of a close whose EXIT proved the bot holds nothing."""

    SCHEDULED = "scheduled"
    SENT = "sent"
    PARTLY_CLOSED = "partly_closed"
    FAILED = "failed"
    FLAT = "flat"
    MISSED = "missed"


@dataclass(frozen=True, slots=True)
class ScheduledCloseState:
    """One scheduled close of a bot, as its owner is told it."""

    strategy_instance_id: str
    contract: str
    # Contracts still held, signed. Zero once flat.
    held: float
    standing: CloseStanding
    kind: CloseKind
    due_ms: int
    # The session the close was owed in, named by its open.
    session_open_ms: int
    # When the Clerk took the close, on its own clock. None while it has taken none.
    taken_at_ms: int | None
    # How long after it was due the close was taken. None while it has taken none.
    late_by_ms: int | None
    # The Clerk has stopped exiting this bot by itself: the close is the owner's.
    left_to_owner: bool
    # In plain words, written here and shown as written.
    sentence: str

    @property
    def late(self) -> bool:
        """Whether the close was taken late: more than a minute after it was due."""
        return self.late_by_ms is not None and self.late_by_ms > LATE_AFTER_MS


def scheduled_close_state(
    repo: ClerkSqliteRepository, strategy_instance_id: str, *, now_ms: int | None = None
) -> tuple[ScheduledCloseState, ...]:
    """Each scheduled close one bot has to its name now: one for every contract it holds that owes or missed one.

    With nothing held, the close that left the bot flat is told for the rest
    of its own day. An empty answer says no close is scheduled: the bot
    holds no option contract that owes one today and missed none.
    """
    now = repo.clock() if now_ms is None else now_ms
    read = held_contracts(repo, now, strategy_instance_id=strategy_instance_id)
    states = tuple(
        _state_of(repo, item, now) for item in read.held if item.owed is not None or item.missed is not None
    )
    if states or strategy_instance_id in read.holders:
        return states
    finished = _the_close_that_left_it_flat(repo, strategy_instance_id, now)
    return () if finished is None else (finished,)


def _first_acceptance(
    repo: ClerkSqliteRepository, strategy_instance_id: str, *, session_open_ms: int, entry_order_ref: str
) -> dict | None:
    """The acceptance of one session's own scheduled close of what one entry opened; None when none was taken.

    A re-drive of that close is another EXIT, taken later. It is not when the close was taken.
    """
    return repo.first_effect_transition(
        effect_operation_id=exit_effect_operation_id(
            strategy_instance_id=strategy_instance_id,
            decision_id=decision_id_under(decision_prefix(session_open_ms), entry_order_ref),
        ),
        transition_kind="EXIT_ACCEPTED",
    )


def _newest_attempt(repo: ClerkSqliteRepository, strategy_instance_id: str, session_open_ms: int) -> dict | None:
    """The bot's newest EXIT acceptance when it is that session's scheduled close, first try or re-drive."""
    newest = repo.last_strategy_transition(strategy_instance_id=strategy_instance_id, transition_kind="EXIT_ACCEPTED")
    if newest is None:
        return None
    scheduled = ExitAcceptedFacts.from_facts_json(newest["facts_json"]).the_scheduled_close
    return newest if scheduled is not None and scheduled.session_open_ms == session_open_ms else None


def _state_of(repo: ClerkSqliteRepository, item: HeldContract, now_ms: int) -> ScheduledCloseState:
    close = item.missed if item.missed is not None else item.owed
    assert close is not None
    first = _first_acceptance(
        repo, item.strategy_instance_id, session_open_ms=close.session_open_ms, entry_order_ref=item.entry_order_ref
    )
    taken_at_ms = None if first is None else first["clerk_observed_at_ms"]
    held = f"{quantity_text(abs(item.quantity))} {item.symbol}"
    if item.missed is not None:
        standing = CloseStanding.MISSED
        sentence = _missed_words(item, item.quantity, simulated=simulated_account_named(repo.account_id))
    else:
        attempt = _newest_attempt(repo, item.strategy_instance_id, close.session_open_ms)
        if attempt is not None:
            standing = _attempt_standing(repo, attempt)
        elif item.holding is not None and item.holding.state == "failed":
            # The close was sent and did not go through, and the bot's newest exit is another one.
            standing = CloseStanding.FAILED
        else:
            standing = CloseStanding.SCHEDULED
        another_exit = item.unfinished is None and repo.active_exit_for_strategy(item.strategy_instance_id) is not None
        sentence = _standing_words(item, close, standing, held=held, now_ms=now_ms, another_exit=another_exit)
    return ScheduledCloseState(
        strategy_instance_id=item.strategy_instance_id,
        contract=item.symbol,
        held=item.quantity,
        standing=standing,
        kind=close.kind,
        due_ms=close.due_ms,
        session_open_ms=close.session_open_ms,
        taken_at_ms=taken_at_ms,
        late_by_ms=None if taken_at_ms is None else max(taken_at_ms - close.due_ms, 0),
        left_to_owner=item.left_to_owner,
        sentence=sentence,
    )


def _attempt_standing(repo: ClerkSqliteRepository, attempt: dict) -> CloseStanding:
    """Where the newest try of a close stands, while its contract is still held."""
    effect = repo.effect_operation(attempt["effect_operation_id"])
    assert effect is not None
    if effect.state in FAILED_EFFECT_STATES:
        return CloseStanding.FAILED
    closing = next(
        (order for order in repo.orders_for_effect_operation(effect.effect_operation_id) if order.role == "REDUCING"),
        None,
    )
    if closing is None or not repo.has_order_transition(
        order_ref=closing.order_ref, transition_kind="ORDER_SUBMIT_REQUESTED"
    ):
        # Taken and not sent yet: held for a halt, a hold on the account, or the entry's own cancel.
        return CloseStanding.SCHEDULED
    filled, _cost = repo.effective_fill_totals_for_order(closing.order_ref)
    return CloseStanding.PARTLY_CLOSED if position_quantity_is_nonzero(filled) else CloseStanding.SENT


def _standing_words(
    item: HeldContract, close: ScheduledClose, standing: CloseStanding, *, held: str, now_ms: int, another_exit: bool
) -> str:
    """One sentence for a close its session has not ended on. ``another_exit`` says an exit of the bot that is
    no scheduled close is working now."""
    due = f"{et_clock_words(close.due_ms)} ET"
    ends = f"{et_clock_words(close.session_close_ms)} ET"
    why = "its contract expires today" if close.kind is CloseKind.EXPIRY_DAY else "a sold option is not held overnight"
    if standing is CloseStanding.SENT:
        return f"The close of {held} was sent as a market order. Nothing of it has filled yet."
    if standing is CloseStanding.PARTLY_CLOSED:
        return f"The close has filled in part. {held} is still held."
    if standing is CloseStanding.FAILED:
        failed = f"The close did not go through and {held} is still held."
        if item.left_to_owner:
            return (
                f"{failed} The Clerk has stopped exiting this bot's position by itself. Close it yourself from "
                "the bot's page."
            )
        if another_exit:
            return f"{failed} Another exit of this bot is closing it now."
        return f"{failed} The Clerk tries it again about every 30 seconds until the session's close at {ends}."
    if not close.due(now_ms):
        return f"The Clerk closes {held} at {due}: {why}."
    if item.left_to_owner:
        return (
            f"The close of {held} was due at {due} and was not sent: the Clerk has stopped exiting this bot's "
            "position by itself. Close it yourself from the bot's page."
        )
    if item.unfinished is not None:
        return f"The close of {held} was due at {due}. The Clerk took it and holds it: it has not gone out yet."
    if item.holding is not None and item.holding.state == "other_exit":
        return f"The close of {held} was due at {due}. Another exit of this bot is closing it, so no second order is sent."
    return f"The close of {held} was due at {due}. The Clerk sends it now."


def _the_close_that_left_it_flat(
    repo: ClerkSqliteRepository, strategy_instance_id: str, now_ms: int
) -> ScheduledCloseState | None:
    """Today's scheduled close of a bot that now holds nothing, when it is what proved the bot flat."""
    newest = repo.last_strategy_transition(strategy_instance_id=strategy_instance_id, transition_kind="EXIT_ACCEPTED")
    if newest is None:
        return None
    facts = ExitAcceptedFacts.from_facts_json(newest["facts_json"])
    scheduled = facts.the_scheduled_close
    effect = repo.effect_operation(newest["effect_operation_id"])
    if (
        scheduled is None
        or facts.option_contract is None
        or effect is None
        or effect.state != "succeeded"
        or et_date_at_ms(scheduled.session_open_ms) != et_date_at_ms(now_ms)
    ):
        return None
    own = _first_acceptance(
        repo, strategy_instance_id, session_open_ms=scheduled.session_open_ms, entry_order_ref=facts.entry_order_ref
    )
    taken_at_ms = (newest if own is None else own)["clerk_observed_at_ms"]
    late_by_ms = max(taken_at_ms - scheduled.due_ms, 0)
    lateness = f", {round(late_by_ms / 60_000)} minutes late" if late_by_ms > LATE_AFTER_MS else ""
    return ScheduledCloseState(
        strategy_instance_id=strategy_instance_id,
        contract=facts.option_contract,
        held=0.0,
        standing=CloseStanding.FLAT,
        kind=CloseKind(scheduled.kind),
        due_ms=scheduled.due_ms,
        session_open_ms=scheduled.session_open_ms,
        taken_at_ms=taken_at_ms,
        late_by_ms=late_by_ms,
        left_to_owner=False,
        sentence=(
            f"The close was taken at {et_clock_words(taken_at_ms)} ET{lateness}. "
            f"The bot holds none of {facts.option_contract}."
        ),
    )


__all__ = [
    "LATE_AFTER_MS",
    "MAX_CLOSES_AT_ONCE",
    "CloseStanding",
    "HeldContract",
    "HeldContracts",
    "ScheduledCloseRound",
    "ScheduledCloseState",
    "decision_prefix",
    "drive_scheduled_closes",
    "held_contracts",
    "scheduled_close_state",
]
