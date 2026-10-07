"""An option entry beside the broker's option buying power: two readings, one record, one notice (ADR 0076 decision 26).

Around an option entry on a real account the Clerk reads the account's
option buying power twice. The first reading is made just before the entry
is judged, beside the read of the contract's listing and outside the intake
lock. The second is made once the books prove what the entry came to. The
fall between them is set beside what the bot set aside, and what that comes
to is recorded once on the ENTER's own timeline.

A change in an account-wide figure is not what one trade cost. So a pair of
readings is judged only when nothing else can explain a difference: both
state a figure in whole cents, the first was fresh when the order was sent,
no cash moved, no other order of the account was working or changed, and
the account's own setting says the broker has been seen to move this figure
by an order's cost. Every other pair is ``unknown``, with the one reason
that was met first (``NotComparedReason``). The readings, the fall and what
was set aside are recorded all the same.

Only one result does anything: a fall of more than the bot set aside,
across a clean pair, judged. It raises a notice of that bot's own that
holds its new entries and none of its exits until the owner has reviewed
it. No other bot is touched.

This module costs an entry nothing. A reading that fails or is slow is
recorded as that, and the entry goes on. An account whose bots trade shares
makes no reading and writes no record. A simulation makes none either: it
has no broker figure to read.

Formula: premium paid = sum over the entry order's fills in effect of
  contracts * price a share * the contract's multiplier, exact, and used
  only when it is whole cents. The comparison itself is
  ``app.options.buying_power_check`` and nothing is computed twice.
Reference: #2913, "Comparing with Alpaca"; ADR 0076 decision 26.
Canonical implementation: ``app/options/buying_power_check.py`` (the rows),
  ``app/broker/alpaca/clerk/money.py`` (``notional``); this file for which
  pair of readings may be compared.
Validated against: tests/broker/alpaca/clerk/sqlite/test_entry_buying_power.py.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from app.broker.alpaca.clerk.live_envelope import OBSERVATION_MAX_AGE_MS
from app.broker.alpaca.clerk.money import dollars, money_context, notional
from app.broker.alpaca.clerk.sqlite.day_pnl import CASH_TRANSFER_ACTIVITY_FILTER
from app.broker.alpaca.clerk.sqlite.economic_projection import effective_fill_records
from app.broker.alpaca.clerk.sqlite.entry_outcome import EntryOutcome, EntryStanding, entry_outcome
from app.broker.alpaca.clerk.sqlite.facts import (
    FACTS_SCHEMA_VERSION,
    EntryBuyingPowerComparedFacts,
    UncertaintyRaisedFacts,
    UncertaintyResolvedFacts,
)
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.option_entry import OptionEntryFacts, option_entry_of_order
from app.broker.alpaca.clerk.sqlite.reads import CANCELLABLE_ENTRY_BROKER_STATES
from app.broker.alpaca.clerk.sqlite.uncertainty import raise_uncertainty
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    OPTION_BUYING_POWER_UNDER_RESERVED_REASON_CODE,
    BuyingPowerUnderReservedCause,
    UnderReservedEntry,
)
from app.options.buying_power_check import (
    BuyingPowerCheckResult,
    judge_fall,
    judge_part_fill,
    requirement_for_filled_cents,
    set_aside_for_filled_cents,
    whole_cents,
)
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
    from app.broker.contract.models import BrokerActivity
    from app.broker.contract.options import BrokerOptionAccountSnapshot
    from app.broker.contract.ports import BrokerReadPort

logger = logging.getLogger(__name__)

COMPARED_KIND = "ENTRY_BUYING_POWER_COMPARED"

# The most the first reading may take. It is made beside the read of the contract's listing, which
# has three seconds of its own, so the two together never take longer than that read may alone.
FIRST_READING_BOUND_MS = 1_500
# The most the second reading may take. Nothing waits on it.
SECOND_READING_BOUND_MS = 5_000
# How old the first reading may be when the order is sent: an account reading's own staleness.
FIRST_READING_MAX_AGE_MS = OBSERVATION_MAX_AGE_MS
# How long a pair of readings waits for a pass of the account that reached its verdict after the
# second one. The sweep publishes a pass about every fifteen seconds; one this late has stopped.
PASS_AFTER_SECOND_READING_BOUND_MS = 60_000
# How long past its own deadline an entry is watched for its outcome to be proven.
OUTCOME_PROOF_BOUND_MS = 600_000
# One page of the account's cash transfers. A full page cannot prove there was no more.
_TRANSFER_PAGE = 100

REVIEWED_SUMMARY_CODE = "OPTION_BUYING_POWER_UNDER_RESERVED_REVIEWED"
_OPERATOR_REF_PREFIX = "operator:"


class NotComparedReason(StrEnum):
    """Why one entry's comparison is ``unknown``. A closed set; the first that is met is the one recorded."""

    # The account's setting has not said the broker moves this figure by an order's cost.
    JUDGMENT_NOT_SWITCHED_ON = "JUDGMENT_NOT_SWITCHED_ON"
    # The read before the order failed or did not answer in time.
    FIRST_READING_FAILED = "FIRST_READING_FAILED"
    # It answered, and stated no option buying power.
    FIRST_READING_WITHOUT_FIGURE = "FIRST_READING_WITHOUT_FIGURE"
    # It was older than an account reading may be when the order was sent.
    FIRST_READING_STALE = "FIRST_READING_STALE"
    SECOND_READING_FAILED = "SECOND_READING_FAILED"
    SECOND_READING_WITHOUT_FIGURE = "SECOND_READING_WITHOUT_FIGURE"
    # A reading carries a fraction of a cent.
    READING_NOT_WHOLE_CENTS = "READING_NOT_WHOLE_CENTS"
    # The fills on the books do not come to whole cents: an average, not the executions.
    PREMIUM_NOT_WHOLE_CENTS = "PREMIUM_NOT_WHOLE_CENTS"
    # Another order of the account was working or changed between the first reading and the judgment.
    OTHER_ORDER_MOVED = "OTHER_ORDER_MOVED"
    # A cash transfer reached the account between the readings.
    CASH_MOVED = "CASH_MOVED"
    # Whether one did could not be read.
    CASH_MOVEMENT_UNREAD = "CASH_MOVEMENT_UNREAD"
    # No pass of the account reached its verdict after the second reading in time.
    ACCOUNT_NOT_CHECKED_AFTER = "ACCOUNT_NOT_CHECKED_AFTER"
    # Nothing filled, so there was no cost to compare.
    ENTRY_BOUGHT_NOTHING = "ENTRY_BOUGHT_NOTHING"
    # It filled in part and the rest ended. Whether the broker had given back what it held for the
    # unfilled part is not verified, and the fall is inside everything the entry set aside.
    UNFILLED_PART_RELEASE_NOT_VERIFIED = "UNFILLED_PART_RELEASE_NOT_VERIFIED"
    # The entry's outcome was not proven in time.
    OUTCOME_NOT_PROVEN = "OUTCOME_NOT_PROVEN"
    # The Clerk restarted before the second reading, and the first was lost with it.
    CLERK_RESTARTED = "CLERK_RESTARTED"
    # Served only, never recorded: the bot's account is a simulation and has no broker figure.
    SIMULATION = "SIMULATION"
    # Served only, never recorded: the entry is on the books and its comparison has not been made yet.
    NOT_COMPARED_YET = "NOT_COMPARED_YET"


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountReading:
    """One read of the account's option buying power, with the Clerk's clock as it began and ended."""

    began_ms: int
    ended_ms: int
    # The newest record on the custody log as the read began: what was written after it came later.
    began_after_sequence: int
    # Dollars, exact as the broker wrote them. None when the read failed or stated none.
    buying_power: Decimal | None
    # Whether the read raised or did not answer inside its bound.
    failed: bool
    # The ids of the day's cash transfers as this reading saw them. None when they could not be read.
    cash_transfers: frozenset[str] | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ReadingBegun:
    """One reading of the account, started and not yet waited for."""

    began_ms: int
    began_after_sequence: int
    transfers_from_ms: int
    account: asyncio.Future[BrokerOptionAccountSnapshot]
    transfers: asyncio.Future[list[BrokerActivity]]

    def cancel(self) -> None:
        """Stop both reads: the reading is no longer wanted."""
        self.account.cancel()
        self.transfers.cancel()


async def _answer_of[T](read: asyncio.Future[T]) -> T | Exception:
    """What one read answered, or what it raised. A cancelled wait is not caught."""
    try:
        return await read
    except Exception as exc:
        # Handed back to be logged and recorded by the caller, never dropped.
        return exc


@dataclass(slots=True)
class _Watched:
    """One sent option entry between its first reading and its record."""

    order_ref: str
    first: AccountReading | None
    # The Clerk's clock as the order was handed to the broker's port. None for one never handed over.
    sent_at_ms: int | None
    # The start of the New York day the cash transfers are listed from, the same for both readings.
    transfers_from_ms: int
    second: AccountReading | None = None


class EntryBuyingPowerWatch:
    """The readings a real account's option entries are compared on, and the round that compares them.

    One per Clerk facade. ``read_first`` and ``sent`` are called on the
    entry's own path and never raise for a broker's sake. ``round`` is the
    Clerk's own step, run after a pass of the account: it takes second
    readings, judges, records, and raises the notice.

    The first reading is held in memory only. Entries a restart left with
    none are recorded as that by the first round of the new process.
    """

    def __init__(
        self, *, repo: ClerkSqliteRepository, read: BrokerReadPort, intake: ReentrantAsyncLock,
        judged: Callable[[], bool],
    ) -> None:
        self._repo = repo
        self._read = read
        self._intake = intake
        self._judged = judged
        self._first: dict[tuple[str, str], tuple[AccountReading, int]] = {}
        self._watched: dict[str, _Watched] = {}
        self._restart_recorded = False
        # One round at a time: two would each take a second reading of the same entry.
        self._one_round = asyncio.Lock()

    # ── on the entry's own path ──────────────────────────────────────────

    def begin_first(self) -> ReadingBegun:
        """Start the reading of the account that an entry is compared from, and hand it back still running.

        The caller goes on with its own read of the broker meanwhile, so
        the reading adds no step of its own to an entry.
        """
        return self._begin_reading(
            bound_ms=FIRST_READING_BOUND_MS, transfers_from_ms=et_midnight_ms(et_date_at_ms(self._repo.clock()))
        )

    async def finish_first(self, key: tuple[str, str], begun: ReadingBegun) -> None:
        """Wait for the reading ``begun`` and keep it for the entry ``key`` (its bot and its decision).

        Bounded by the reading's own bound, and never raises for the
        broker's sake: a read that fails is kept as a reading that failed.
        """
        self._first[key] = (await self._finish_reading(begun), begun.transfers_from_ms)

    def sent(self, key: tuple[str, str], *, order_ref: str | None, sent_at_ms: int | None) -> None:
        """The entry ``key`` was accepted as ``order_ref`` and handed to the broker at ``sent_at_ms``: watch it.

        ``sent_at_ms`` is ``None`` for an order the Clerk took and then did
        not hand over; it is watched all the same, so its record says it
        bought nothing. With no order at all the reading is dropped. An
        order already watched keeps the reading it was first watched with.
        """
        held = self._first.pop(key, None)
        if order_ref is None or order_ref in self._watched:
            return
        first, transfers_from_ms = (
            held if held is not None else (None, et_midnight_ms(et_date_at_ms(self._repo.clock())))
        )
        self._watched[order_ref] = _Watched(
            order_ref=order_ref, first=first, sent_at_ms=sent_at_ms, transfers_from_ms=transfers_from_ms
        )

    def forget(self, key: tuple[str, str]) -> None:
        """Drop the reading held for an entry that was not taken."""
        self._first.pop(key, None)

    @property
    def watching(self) -> tuple[str, ...]:
        """The entries whose comparison has not been recorded yet, by order reference."""
        return tuple(self._watched)

    @property
    def has_work(self) -> bool:
        """Whether a round would have anything to do: an entry is watched, or this process has not yet
        looked for entries a restart left."""
        return bool(self._watched) or not self._restart_recorded

    # ── the Clerk's own round ────────────────────────────────────────────

    async def round(self, *, last_pass_at_ms: int | None) -> tuple[str, ...]:
        """One step for every watched entry; the order references a comparison was recorded for.

        ``last_pass_at_ms`` is when the newest pass of the account that
        reached a verdict reached it, or ``None`` when none has.
        """
        recorded: list[str] = []
        async with self._one_round:
            if not self._restart_recorded and not self._first:
                # Not while an entry is between its first reading and its send: it is not watched yet.
                recorded.extend(await self._record_entries_a_restart_left())
                self._restart_recorded = True
            for watched in tuple(self._watched.values()):
                if await self._step(watched, last_pass_at_ms=last_pass_at_ms):
                    recorded.append(watched.order_ref)
                    self._watched.pop(watched.order_ref, None)
        return tuple(recorded)

    async def _step(self, watched: _Watched, *, last_pass_at_ms: int | None) -> bool:
        """One entry's next step. True once its comparison is on the books."""
        outcome = entry_outcome(self._repo, watched.order_ref)
        now_ms = self._repo.clock()
        first = watched.first
        if watched.second is None:
            if not outcome.proven:
                watched_until_ms = max(outcome.cancel_at_ms or 0, watched.sent_at_ms or 0) + OUTCOME_PROOF_BOUND_MS
                if now_ms <= watched_until_ms:
                    return False
                return await self._record(watched, reason=NotComparedReason.OUTCOME_NOT_PROVEN)
            if (
                outcome.standing is EntryStanding.NOT_SENT
                or first is None
                or first.failed
                or first.buying_power is None
            ):
                # Nothing a second reading could be compared with.
                return await self._record(watched, reason=None)
            watched.second = await self._finish_reading(
                self._begin_reading(bound_ms=SECOND_READING_BOUND_MS, transfers_from_ms=watched.transfers_from_ms)
            )
            if watched.second.failed or watched.second.buying_power is None:
                return await self._record(watched, reason=None)
            return False
        if last_pass_at_ms is None or last_pass_at_ms <= watched.second.ended_ms:
            if now_ms - watched.second.ended_ms <= PASS_AFTER_SECOND_READING_BOUND_MS:
                return False
            return await self._record(watched, reason=NotComparedReason.ACCOUNT_NOT_CHECKED_AFTER)
        return await self._record(watched, reason=None)

    def _begin_reading(self, *, bound_ms: int, transfers_from_ms: int) -> ReadingBegun:
        """Start one reading of the account: both reads at once, each inside ``bound_ms``.

        Each read is begun here and now, before this returns, and goes on
        by itself from its first wait on the broker. So a caller that reads
        the broker for something else meanwhile is not held back by a turn
        of the event loop, and a port that answers at once has answered by
        the time this returns.
        """
        loop = asyncio.get_running_loop()
        began_ms = self._repo.clock()
        return ReadingBegun(
            began_ms=began_ms,
            began_after_sequence=self._repo.last_custody_sequence(),
            transfers_from_ms=transfers_from_ms,
            account=asyncio.Task(
                asyncio.wait_for(self._read.get_option_account(), timeout=bound_ms / 1000),
                loop=loop, eager_start=True,
            ),
            transfers=asyncio.Task(
                asyncio.wait_for(
                    self._read.list_activities(
                        after_ms=transfers_from_ms, limit=_TRANSFER_PAGE, activity_type=CASH_TRANSFER_ACTIVITY_FILTER
                    ),
                    timeout=bound_ms / 1000,
                ),
                loop=loop, eager_start=True,
            ),
        )

    async def _finish_reading(self, begun: ReadingBegun) -> AccountReading:
        """Wait for a reading that was begun, and say what it read. Never raises for the broker's sake."""
        try:
            account = await _answer_of(begun.account)
            transfers = await _answer_of(begun.transfers)
        except BaseException:
            begun.cancel()
            raise
        ended_ms = self._repo.clock()
        if isinstance(account, Exception):
            # Whatever the read raised, a timeout among it: it is evidence the Clerk could not have,
            # never a reason to stop an entry.
            logger.warning(
                "the account's option buying power could not be read",
                extra={"action": "option_buying_power_read_failed", "account_id": self._repo.account_id,
                       "error": f"{type(account).__name__}: {account}"},
            )
        if isinstance(transfers, Exception):
            logger.warning(
                "the account's cash transfers could not be read beside its option buying power",
                extra={"action": "option_buying_power_transfers_unread", "account_id": self._repo.account_id,
                       "error": f"{type(transfers).__name__}: {transfers}"},
            )
        listed = None if isinstance(transfers, Exception) or len(transfers) >= _TRANSFER_PAGE else transfers
        return AccountReading(
            began_ms=begun.began_ms,
            ended_ms=ended_ms,
            began_after_sequence=begun.began_after_sequence,
            buying_power=None if isinstance(account, Exception) else account.options_buying_power,
            failed=isinstance(account, Exception),
            cash_transfers=None if listed is None else frozenset(activity.activity_id for activity in listed),
        )

    async def _record_entries_a_restart_left(self) -> list[str]:
        """Record, for each option entry with no comparison that this process is not watching, that none can be made."""
        with self._repo.write_fence() as conn:
            rows = conn.execute(
                "SELECT o.order_ref FROM order_contracts bound "
                "CROSS JOIN orders o ON o.order_ref = bound.order_ref "
                "WHERE o.role = 'ENTRY' AND NOT EXISTS ("
                "SELECT 1 FROM custody_transitions t WHERE t.effect_operation_id = o.effect_operation_id "
                "AND t.transition_kind = ?) ORDER BY o.submitted_at_ms ASC, o.order_ref ASC",
                (COMPARED_KIND,),
            ).fetchall()
        recorded: list[str] = []
        for row in rows:
            order_ref = row["order_ref"]
            if order_ref in self._watched or option_entry_of_order(self._repo, order_ref) is None:
                continue
            left = _Watched(order_ref=order_ref, first=None, sent_at_ms=None, transfers_from_ms=0)
            if await self._record(left, reason=NotComparedReason.CLERK_RESTARTED):
                recorded.append(order_ref)
        return recorded

    async def _record(self, watched: _Watched, *, reason: NotComparedReason | None) -> bool:
        """Judge one entry and write its record, and its notice when it holds. True once the record is on the books."""
        async with self._intake:
            with self._repo.write_fence() as conn:
                order = self._repo.order(watched.order_ref)
                entry = option_entry_of_order(self._repo, watched.order_ref)
                if order is None or entry is None:
                    raise ValueError(f"{watched.order_ref!r} is not an entry in an option contract")
                effect = self._repo.effect_operation(order.effect_operation_id)
                assert effect is not None
                if self._repo.first_effect_transition(
                    effect_operation_id=effect.effect_operation_id, transition_kind=COMPARED_KIND
                ) is not None:
                    return True
                outcome = entry_outcome(self._repo, watched.order_ref)
                now_ms = self._repo.clock()
                facts = _compare(
                    conn, account_id=self._repo.account_id, strategy_instance_id=effect.strategy_instance_id,
                    watched=watched, entry=entry, outcome=outcome, effect_operation_id=effect.effect_operation_id,
                    broker_order_id=order.broker_order_id, forced=reason, judged=self._judged(), now_ms=now_ms,
                )
                if facts.holds:
                    # The notice first: were the Clerk to stop between the two writes, the bot is held.
                    # It names what the fall was held to: everything the entry set aside.
                    assert facts.fall_cents is not None
                    _raise_under_reserved(
                        self._repo, strategy_instance_id=effect.strategy_instance_id or "",
                        entry=UnderReservedEntry(
                            order_ref=watched.order_ref, fall_cents=facts.fall_cents,
                            set_aside_cents=facts.commitment_cents,
                        ),
                    )
                self._repo.append_transition(
                    TransitionInput(
                        strategy_instance_id=effect.strategy_instance_id,
                        run_id=effect.run_id,
                        command_id=effect.command_id,
                        effect_operation_id=effect.effect_operation_id,
                        transition_kind=COMPARED_KIND,
                        custody_owner="ACCOUNT_CLERK",
                        execution_authority="ACCOUNT_CLERK",
                        operation_state=effect.state,
                        clerk_observed_at_ms=now_ms,
                        summary_code=facts.reason_code or facts.result.upper(),
                        facts_schema_version=FACTS_SCHEMA_VERSION,
                        facts_json=facts.to_facts_json(),
                    )
                )
        logger.log(
            logging.ERROR if facts.holds else logging.INFO,
            "an option entry was compared with the broker's option buying power",
            extra={
                "action": "option_entry_buying_power_compared",
                "account_id": self._repo.account_id,
                "strategy_instance_id": effect.strategy_instance_id,
                "order_ref": watched.order_ref,
                "result": facts.result,
                "reason_code": facts.reason_code,
                "fall_cents": facts.fall_cents,
                "set_aside_for_filled_cents": facts.set_aside_for_filled_cents,
                "holds": facts.holds,
            },
        )
        return True


def _compare(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    strategy_instance_id: str | None,
    watched: _Watched,
    entry: OptionEntryFacts,
    outcome: EntryOutcome,
    effect_operation_id: str,
    broker_order_id: str | None,
    forced: NotComparedReason | None,
    judged: bool,
    now_ms: int,
) -> EntryBuyingPowerComparedFacts:
    """What one entry's comparison comes to at ``now_ms``, as its record. Reads the books; writes nothing."""
    first, second = watched.first, watched.second
    contracts_asked = entry.order.quantity
    contracts_filled = outcome.contracts_filled.get(entry.contract_terms.symbol, 0)
    first_cents = None if first is None or first.buying_power is None else whole_cents(first.buying_power)
    second_cents = None if second is None or second.buying_power is None else whole_cents(second.buying_power)
    fall_cents = None if first_cents is None or second_cents is None else first_cents - second_cents
    premium_cents = _premium_paid_cents(
        conn, account_id=account_id, strategy_instance_id=strategy_instance_id, order_ref=watched.order_ref
    )
    filled_within_what_was_asked = 0 <= contracts_filled <= contracts_asked and outcome.units_filled <= entry.units
    set_aside_cents = (
        set_aside_for_filled_cents(
            commitment_cents=entry.money.entry_commitment_cents, contracts_asked=contracts_asked,
            contracts_filled=contracts_filled,
        )
        if filled_within_what_was_asked
        else None
    )
    cost_cents = (
        None
        if premium_cents is None or not filled_within_what_was_asked
        else premium_cents + requirement_for_filled_cents(
            requirement_cents=entry.money.broker_requirement_cents, units_asked=entry.units,
            units_filled=outcome.units_filled,
        )
    )
    reason = forced or _why_not_compared(
        conn, watched=watched, outcome=outcome, contracts_filled=contracts_filled, first_cents=first_cents,
        second_cents=second_cents, cost_cents=cost_cents, set_aside_cents=set_aside_cents,
        effect_operation_id=effect_operation_id, broker_order_id=broker_order_id,
    )
    result = BuyingPowerCheckResult.UNKNOWN
    if reason is None:
        # A clean pair. What it comes to is the arithmetic's to say, and whether that is acted on the setting's.
        assert first_cents is not None and second_cents is not None
        assert cost_cents is not None and set_aside_cents is not None
        part_fill = (
            judge_part_fill(
                first_cents=first_cents, second_cents=second_cents,
                commitment_cents=entry.money.entry_commitment_cents,
            )
            if contracts_filled < contracts_asked
            else None
        )
        if part_fill is BuyingPowerCheckResult.UNKNOWN:
            reason = NotComparedReason.UNFILLED_PART_RELEASE_NOT_VERIFIED
        elif not judged:
            reason = NotComparedReason.JUDGMENT_NOT_SWITCHED_ON
        else:
            result = part_fill or judge_fall(
                first_cents=first_cents, second_cents=second_cents, cost_of_filled_cents=cost_cents,
                set_aside_for_filled_cents=set_aside_cents,
            )
    return EntryBuyingPowerComparedFacts(
        result=result.value,
        reason_code=None if reason is None else reason.value,
        contracts_asked=contracts_asked,
        contracts_filled=contracts_filled,
        commitment_cents=entry.money.entry_commitment_cents,
        set_aside_for_filled_cents=set_aside_cents,
        premium_paid_cents=premium_cents,
        cost_of_filled_cents=cost_cents,
        first_began_ms=None if first is None else first.began_ms,
        first_ended_ms=None if first is None else first.ended_ms,
        first_buying_power=None if first is None or first.buying_power is None else str(first.buying_power),
        sent_at_ms=watched.sent_at_ms,
        second_began_ms=None if second is None else second.began_ms,
        second_ended_ms=None if second is None else second.ended_ms,
        second_buying_power=None if second is None or second.buying_power is None else str(second.buying_power),
        fall_cents=fall_cents,
        compared_at_ms=now_ms,
        holds=result is BuyingPowerCheckResult.UNDER_RESERVED_UNEXPLAINED,
    )


def _why_not_compared(
    conn: sqlite3.Connection,
    *,
    watched: _Watched,
    outcome: EntryOutcome,
    contracts_filled: int,
    first_cents: int | None,
    second_cents: int | None,
    cost_cents: int | None,
    set_aside_cents: int | None,
    effect_operation_id: str,
    broker_order_id: str | None,
) -> NotComparedReason | None:
    """The first reason this pair of readings is not a clean pair, or ``None`` when it is one.

    What a clean pair comes to, and whether the account's setting has it
    judged, is said after this (``_compare``).
    """
    first, second = watched.first, watched.second
    if first is None or first.failed:
        return NotComparedReason.FIRST_READING_FAILED
    if first.buying_power is None:
        return NotComparedReason.FIRST_READING_WITHOUT_FIGURE
    if not outcome.proven or set_aside_cents is None:
        return NotComparedReason.OUTCOME_NOT_PROVEN
    if contracts_filled == 0:
        return NotComparedReason.ENTRY_BOUGHT_NOTHING
    if second is None or second.failed:
        return NotComparedReason.SECOND_READING_FAILED
    if second.buying_power is None:
        return NotComparedReason.SECOND_READING_WITHOUT_FIGURE
    if first_cents is None or second_cents is None:
        return NotComparedReason.READING_NOT_WHOLE_CENTS
    if cost_cents is None:
        return NotComparedReason.PREMIUM_NOT_WHOLE_CENTS
    if watched.sent_at_ms is None or not 0 <= watched.sent_at_ms - first.ended_ms <= FIRST_READING_MAX_AGE_MS:
        return NotComparedReason.FIRST_READING_STALE
    if first.cash_transfers is None or second.cash_transfers is None:
        return NotComparedReason.CASH_MOVEMENT_UNREAD
    if first.cash_transfers != second.cash_transfers:
        return NotComparedReason.CASH_MOVED
    if _another_order_moved(
        conn, order_ref=watched.order_ref, effect_operation_id=effect_operation_id, broker_order_id=broker_order_id,
        after_sequence=first.began_after_sequence,
    ):
        return NotComparedReason.OTHER_ORDER_MOVED
    return None


def _premium_paid_cents(
    conn: sqlite3.Connection, *, account_id: str, strategy_instance_id: str | None, order_ref: str
) -> int | None:
    """What the order's fills in effect cost, in whole cents; ``None`` when they do not come to whole cents."""
    records = effective_fill_records(
        conn, account_id=account_id,
        strategy_instance_ids=None if strategy_instance_id is None else (strategy_instance_id,),
    )
    with money_context():
        paid = sum(
            (
                notional(record.quantity, record.fill_price, multiplier=record.multiplier)
                for record in records
                if record.order_ref == order_ref and record.multiplier is not None
            ),
            Decimal(0),
        )
    return whole_cents(paid)


def _another_order_moved(
    conn: sqlite3.Connection, *, order_ref: str, effect_operation_id: str, broker_order_id: str | None,
    after_sequence: int,
) -> bool:
    """Whether any other order of the account is working, or was recorded about after ``after_sequence``.

    ``after_sequence`` is the newest record on the custody log as the first
    reading began, so what is asked is everything the Clerk has learned of
    another order from then to now, the moment of judgment. It is asked by
    the log's own order and not by a clock: two records of one millisecond
    are still one before the other. A record about another order is one
    that names another effect, another order, or another broker order:
    another bot's entry or exit, this bot's own exit, a manual order, an
    order from outside the Clerk as it was seen. The Clerk knows when it
    saw an outside order, never when that order traded, which is why
    nothing here ends at the second reading.
    """
    about_another = conn.execute(
        "SELECT 1 FROM custody_transitions WHERE sequence > ? AND ("
        "(effect_operation_id IS NOT NULL AND effect_operation_id != ?) "
        "OR (order_ref IS NOT NULL AND order_ref != ?) "
        "OR (broker_order_id IS NOT NULL AND broker_order_id != ?)) LIMIT 1",
        (after_sequence, effect_operation_id, order_ref, broker_order_id or ""),
    ).fetchone()
    if about_another is not None:
        return True
    working = sorted(CANCELLABLE_ENTRY_BROKER_STATES)
    still_working = conn.execute(
        "SELECT 1 FROM orders o JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id "
        f"WHERE o.order_ref != ? AND (LOWER(o.broker_state) IN ({', '.join('?' for _ in working)}) "
        "OR (o.broker_state IS NULL AND e.state NOT IN ('succeeded', 'failed', 'rejected'))) LIMIT 1",
        (order_ref, *working),
    ).fetchone()
    return still_working is not None


# ── the notice, and the owner's review of it ─────────────────────────────


def under_reserved_cause(
    repo: ClerkSqliteRepository, *, strategy_instance_id: str
) -> BuyingPowerUnderReservedCause | None:
    """The entries the bot's open notice names, or ``None`` when it has none open."""
    episode = repo.active_uncertainty(
        scope="CUSTODY_SUBJECT", reason_code=OPTION_BUYING_POWER_UNDER_RESERVED_REASON_CODE,
        strategy_instance_id=strategy_instance_id,
    )
    if episode is None:
        return None
    return BuyingPowerUnderReservedCause.from_mapping(
        UncertaintyRaisedFacts.from_facts_json(episode["facts_json"]).cause_facts
    )


def _raise_under_reserved(
    repo: ClerkSqliteRepository, *, strategy_instance_id: str, entry: UnderReservedEntry
) -> str:
    """Raise the bot's notice for ``entry``, or widen the one it has open. Read, widen, write: under the intake lock."""
    open_cause = under_reserved_cause(repo, strategy_instance_id=strategy_instance_id)
    cause = (
        BuyingPowerUnderReservedCause(entries=(entry,)) if open_cause is None else open_cause.with_entry(entry)
    )
    named = " ".join(
        f"Across entry {item.order_ref} Alpaca's option buying power fell ${dollars(item.fall_cents)}. "
        f"The bot had set aside ${dollars(item.set_aside_cents)} for the whole entry."
        for item in cause.entries
    )
    return raise_uncertainty(
        repo,
        strategy_instance_id=strategy_instance_id,
        reason_code=OPTION_BUYING_POWER_UNDER_RESERVED_REASON_CODE,
        headline="Alpaca took more than this bot set aside for an entry",
        explanation=f"{named} No other order of this account moved and no cash was transferred in between.",
        operator_impact="This bot will open nothing new until you have reviewed this. It can still close what it holds.",
        next_step=(
            "Look at this account's activity at Alpaca around the entry, then review this notice to let the "
            "bot enter again."
        ),
        evidence_refs=tuple(item.order_ref for item in cause.entries),
        cause_facts=cause.to_mapping(),
        severity="error",
    )


def review_under_reserved_hold(repo: ClerkSqliteRepository, *, strategy_instance_id: str, operator: str) -> bool:
    """The owner has reviewed the bot's notice: end it, under their name. False when none was open.

    The only way the notice ends. The review is its resolution on the
    custody log; the comparisons it named stay as they were recorded. The
    caller holds the Clerk's intake lock.
    """
    if not operator or len(operator) > 64:
        raise ValueError("operator must be between 1 and 64 characters")

    def build_transition(uncertainty_id: str) -> TransitionInput:
        facts = UncertaintyResolvedFacts(
            uncertainty_id=uncertainty_id,
            resolution_kind="OPERATOR_ACKNOWLEDGED",
            evidence_refs=[f"{_OPERATOR_REF_PREFIX}{operator}"],
        )
        return TransitionInput(
            strategy_instance_id=strategy_instance_id,
            transition_kind="UNCERTAINTY_RESOLVED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=repo.clock(),
            summary_code=REVIEWED_SUMMARY_CODE,
            facts_json=facts.to_facts_json(),
        )

    return repo.resolve_uncertainty_if_active(
        scope="CUSTODY_SUBJECT",
        reason_code=OPTION_BUYING_POWER_UNDER_RESERVED_REASON_CODE,
        strategy_instance_id=strategy_instance_id,
        build_transition=build_transition,
    )


__all__ = [
    "COMPARED_KIND",
    "FIRST_READING_BOUND_MS",
    "FIRST_READING_MAX_AGE_MS",
    "OUTCOME_PROOF_BOUND_MS",
    "PASS_AFTER_SECOND_READING_BOUND_MS",
    "REVIEWED_SUMMARY_CODE",
    "SECOND_READING_BOUND_MS",
    "AccountReading",
    "EntryBuyingPowerWatch",
    "NotComparedReason",
    "ReadingBegun",
    "review_under_reserved_hold",
    "under_reserved_cause",
]
