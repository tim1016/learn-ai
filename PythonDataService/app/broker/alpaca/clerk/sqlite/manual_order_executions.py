"""A manual leg's exact executions, read from Alpaca's account activity (#2686).

A manual leg ends ``MANUAL_ORDER_FILLED`` only when exact executions cover
its governing quantity (:func:`manual_order_has_exact_terminal_coverage`),
and those used to arrive only as ``trade_updates`` execution slices. A fill
the stream missed -- a dropped frame, or a replacement's frame consumed as a
foreign order's before the Clerk knew the chain (#2656) -- reached the Clerk
only as the sweep's REST answer, whose cumulative counts the shares but names
no execution. The leg stayed ``in_progress`` and every bot's entry was refused
``MANUAL_ORDER_OUTSTANDING``. And a replacement's ``filled_qty`` may leave out
its original's fills (:mod:`manual_order_replacement`), so a cumulative read
from it could under-credit the chain.

So before the sweep folds a REST answer from a live manual leg's chain head
that reports executions the leg has no exact record of, it reads the
account's activity since the leg's original order was created, and records
each execution of the chain as an exact ``activity_recovery`` slice under the
broker order that executed it. Once Alpaca has posted every execution the
answer counts, the slices recorded first leave its cumulative nothing to add,
whatever a replacement's ``filled_qty`` means, and its acknowledgement ends
the leg on them. Until then its cumulative folds as it always did, and the
leg stays outstanding; should that cumulative under-credit the chain, the
chain's exact executions replace it once they cover the head's quantity
(#2786). A former member's answer -- the original Alpaca still
answers for under our client id once the chain moved on -- records nothing:
the head's own answer, which the same resolution reads next, records the
whole chain against the head's quantity.

The activity is read at most once per resolution, and refreshed once: a
later head that reports ``filled`` and is still uncovered may have filled
after the first read, which was taken for an earlier answer. Only one head of
a resolution can report ``filled``, since a filled order ends its chain.

Each execution is credited once. Alpaca's activity id embeds the execution id
the stream carries (:func:`execution_id_from_activity_id`), and the one exact
append flow dedups on it: an execution the stream recorded first is a
duplicate here, and a stream redelivery of one recorded here is a duplicate
there. One that arrives after a cumulative was already folded takes the same
coverage path a late stream frame does. That id bridge has no captured
Alpaca receipt yet, and a wrong one would credit one execution twice under
two ids. So a chain's recovered executions are recorded together or not at
all, and only while its exact total stays within the head's quantity -- no
chain can execute more. A batch that would pass it records nothing and
raises ``EXECUTION_COVERAGE_CONFLICT``.

The read walks the account's ``FILL`` activity newest first, a bounded number
of reads per resolution, and says whether it reached the start of the window
(``read_activity_evidence``). Every walk starts from the newest row again, so
an execution further back than the walk reaches is never read: a leg still
uncovered by a walk that stopped short is logged
``manual_order_executions_beyond_reach`` and stays outstanding until it is
reconciled. Walking again would spend the same reads for the same answer, so
the leg is not walked again until its head's answer changes or
:data:`BEYOND_REACH_REWALK_INTERVAL_MS` passes; each pass in between logs
``manual_order_executions_walk_deferred``. A failed read is logged, and the
resolution folds as before.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

from app.broker.alpaca.adapter import execution_id_from_activity_id
from app.broker.alpaca.clerk.sqlite.exact_execution_evidence import (
    ACTIVITY_EXACT_CONFLICT_COPY,
    ACTIVITY_OVER_ORDER_QUANTITY_CONFLICT_COPY,
    append_exact_execution_slice,
    exact_execution_coverage_conflict,
)
from app.broker.alpaca.clerk.sqlite.execution_coverage import (
    FILL_QTY_EPSILON,
    active_execution_coverage_conflicts,
)
from app.broker.alpaca.clerk.sqlite.manual_order_completion import (
    accepted_manual_leg,
    manual_order_has_exact_terminal_coverage,
)
from app.broker.alpaca.clerk.sqlite.manual_order_replacement import live_manual_effect
from app.broker.alpaca.clerk.sqlite.models import EffectOperationResource
from app.broker.alpaca.clerk.sqlite.off_loop import OffLoop
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.errors import BrokerError
from app.broker.contract.models import BrokerActivity, BrokerOrder, BrokerOrderEvent, BrokerOrderLeg, OrderSide
from app.broker.contract.ports import BrokerActivityEvidencePort

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.claimed_broker_io import ClaimedBrokerIO

logger = logging.getLogger(__name__)

_EXECUTION_ACTIVITY_TYPES = frozenset({"FILL", "PARTIAL_FILL"})

#: The one activity type the walk reads. Alpaca reports every execution,
#: partial or full, as a ``FILL`` activity, so no other row takes a read's room.
_FILL_ACTIVITY_TYPE = "FILL"

#: An activity's side, as the leg spells it. A sale from a flat or short
#: position is reported ``sell_short``; it is still the leg's sell.
_LEG_SIDE_OF_ACTIVITY = {"buy": OrderSide.BUY, "sell": OrderSide.SELL, "sell_short": OrderSide.SELL}

#: The bounded activity reads one walk of the window takes, each at most
#: three pages of 100 rows, before it stops short of the window's start.
ACTIVITY_READS_PER_WALK = 4

#: How long a leg whose executions lay beyond one walk waits, on the Clerk's
#: clock, before it is walked again for an unchanged head answer.
BEYOND_REACH_REWALK_INTERVAL_MS = 10 * 60_000


@dataclass(frozen=True)
class _ActivityWindow:
    """One walk of the account's activity since the leg's original order was created."""

    activities: tuple[BrokerActivity, ...]
    complete: bool
    reads: int
    read_for: str


@dataclass(frozen=True)
class _BeyondReach:
    """A leg's last walk stopped short of its executions: for which head answer, and when to walk again."""

    answer: tuple[str, str, float | None, float]
    rewalk_at_ms: int


#: Each account's legs whose last walk stopped short of their executions,
#: by order ref. In memory only: a restart walks each of them once more.
_BEYOND_REACH: WeakKeyDictionary[ClerkSqliteRepository, dict[str, _BeyondReach]] = WeakKeyDictionary()
_BEYOND_REACH_GUARD = threading.Lock()


def _beyond_reach(repo: ClerkSqliteRepository) -> dict[str, _BeyondReach]:
    with _BEYOND_REACH_GUARD:
        return _BEYOND_REACH.setdefault(repo, {})


def _answer_key(answer: BrokerOrder) -> tuple[str, str, float | None, float]:
    """What a head's answer says of its executions: a change in it may mean a new execution to read."""
    return (answer.order_id, answer.status.strip().lower(), answer.quantity, answer.filled_quantity)


@dataclass(frozen=True)
class RecoveredExecutions:
    """What one recording did: whether the head's quantity refused the chain's executions."""

    over_head_quantity: bool = False


@dataclass
class ManualLegExecutionRecovery:
    """One resolution's recovery of a manual leg's exact executions.

    Built for one resolution of one captured order, so the account's
    activity is walked once however many members of the chain it folds,
    and once more at most (:meth:`recover`). ``since_ms`` bounds the walk at
    the broker's creation time of the leg's original order: no execution of
    its chain is older.
    """

    repo: ClerkSqliteRepository
    broker: ClaimedBrokerIO
    read: BrokerActivityEvidencePort
    order_ref: str
    since_ms: int
    run: OffLoop
    _window: _ActivityWindow | None = field(default=None, init=False)
    _unreadable: bool = field(default=False, init=False)

    async def recover(self, answer: BrokerOrder) -> None:
        """Record the chain's executions before ``answer`` folds, when it is the head and lacks some."""
        if self._unreadable or not await self._lacking(answer) or self._deferred(answer):
            return
        window = await self._walk(answer)
        if window is None:
            return
        outcome = await self._record(answer, window)
        lacking = not outcome.over_head_quantity and await self._lacking(answer)
        if lacking and window.read_for != answer.order_id and answer.status.strip().lower() == "filled":
            self._window = None
            window = await self._walk(answer)
            if window is None:
                return
            outcome = await self._record(answer, window)
            lacking = not outcome.over_head_quantity and await self._lacking(answer)
        if not lacking or window.complete:
            _beyond_reach(self.repo).pop(self.order_ref, None)
            return
        rewalk_at_ms = self.repo.clock() + BEYOND_REACH_REWALK_INTERVAL_MS
        _beyond_reach(self.repo)[self.order_ref] = _BeyondReach(answer=_answer_key(answer), rewalk_at_ms=rewalk_at_ms)
        logger.warning(
            "A manual order's executions lie beyond the account activity one pass reads",
            extra={
                "action": "manual_order_executions_beyond_reach",
                "order_ref": self.order_ref,
                "broker_order_id": answer.order_id,
                "since_ms": self.since_ms,
                "activity_reads": window.reads,
                "activities_read": len(window.activities),
                "rewalk_at_ms": rewalk_at_ms,
            },
        )

    def _deferred(self, answer: BrokerOrder) -> bool:
        """Whether the leg's last walk stopped short for this same answer, too recently to walk again."""
        stuck = _beyond_reach(self.repo).get(self.order_ref)
        if stuck is None or stuck.answer != _answer_key(answer) or self.repo.clock() >= stuck.rewalk_at_ms:
            return False
        logger.info(
            "A manual order's executions beyond the account activity one pass reads are not walked again yet",
            extra={
                "action": "manual_order_executions_walk_deferred",
                "order_ref": self.order_ref,
                "broker_order_id": answer.order_id,
                "rewalk_at_ms": stuck.rewalk_at_ms,
            },
        )
        return True

    async def _lacking(self, answer: BrokerOrder) -> bool:
        return await self.run(lambda: head_lacks_executions(self.repo, order_ref=self.order_ref, answer=answer))

    async def _record(self, answer: BrokerOrder, window: _ActivityWindow) -> RecoveredExecutions:
        return await self.run(
            lambda: record_manual_leg_executions(
                self.repo, order_ref=self.order_ref, head=answer, fills=window.activities
            )
        )

    async def _walk(self, answer: BrokerOrder) -> _ActivityWindow | None:
        """The resolution's walk of the window, read now for ``answer`` unless one was already read."""
        if self._window is not None:
            return self._window
        activities: list[BrokerActivity] = []
        page_token: str | None = None
        complete = False
        reads = 0
        while reads < ACTIVITY_READS_PER_WALK:
            evidence = await self.broker.observe_activity_evidence(
                self.read, after_ms=self.since_ms, activity_type=_FILL_ACTIVITY_TYPE, page_token=page_token
            )
            reads += 1
            if isinstance(evidence, BrokerError):
                self._unreadable = True
                logger.warning(
                    "Alpaca's account activity could not be read for a manual order's executions",
                    extra={
                        "action": "manual_order_executions_unreadable",
                        "order_ref": self.order_ref,
                        "broker_order_id": answer.order_id,
                        "error": str(evidence),
                    },
                )
                return None
            activities.extend(evidence.activities)
            complete = evidence.history_complete
            page_token = evidence.next_page_token
            if complete or page_token is None:
                break
        self._window = _ActivityWindow(
            activities=tuple(activities), complete=complete, reads=reads, read_for=answer.order_id
        )
        return self._window


def head_lacks_executions(repo: ClerkSqliteRepository, *, order_ref: str, answer: BrokerOrder) -> bool:
    """Whether ``answer`` is the live manual leg's chain head and reports executions the leg has no exact record of.

    Either more filled shares than the leg's exact executions hold, or a
    ``filled`` head its exact executions do not cover yet -- a replacement's
    ``filled_qty`` may count only its own shares. Never for a bot order, an
    ended leg, a former member of the chain, or an answer missing a value it
    is read by: #2679's gate withholds that one.
    """
    if (
        answer.unreadable_fields
        or answer.quantity is None
        or not all(value.strip() for value in (answer.order_id, answer.status, answer.symbol, answer.side))
    ):
        return False
    row = repo.order(order_ref)
    effect = None if row is None else live_manual_effect(repo, row)
    if row is None or effect is None or row.broker_order_id not in (None, answer.order_id):
        return False
    exact_quantity, _ = repo.effective_exact_fill_totals_for_order(order_ref)
    if answer.filled_quantity - exact_quantity >= FILL_QTY_EPSILON:
        return True
    return answer.status.strip().lower() == "filled" and not manual_order_has_exact_terminal_coverage(
        repo,
        effect_operation_id=effect.effect_operation_id,
        order_ref=order_ref,
        broker_state=answer.status,
        head_quantity=answer.quantity,
    )


def record_manual_leg_executions(
    repo: ClerkSqliteRepository,
    *,
    order_ref: str,
    head: BrokerOrder,
    fills: Sequence[BrokerActivity],
) -> RecoveredExecutions:
    """Record each execution of the leg's chain in ``fills`` as an exact slice, all or none.

    An activity is the leg's when its order is ``head`` -- which the caller
    folds under this leg next -- or a member of the leg's durable
    replacement chain. Its symbol and side must be the accepted leg's, and
    its quantity, price and time readable; one that is not is logged and
    left out, never folded.

    Checked and appended under the repository's write lock, so no stream
    slice lands between the check and the appends: when the executions not
    recorded yet would take the chain's exact total past ``head``'s
    quantity, none is appended and the order's coverage conflict is raised.
    """
    with repo._write_lock:
        row = repo.order(order_ref)
        owner = None if row is None else live_manual_effect(repo, row)
        if owner is None or head.quantity is None:
            return RecoveredExecutions()
        leg = accepted_manual_leg(repo, order_ref=order_ref)
        members = repo.manual_chain_member_ids(order_ref) | {head.order_id}
        executions: list[tuple[BrokerActivity, BrokerOrderEvent]] = []
        for fill in sorted(fills, key=lambda item: (item.occurred_at_ms or 0, item.activity_id)):
            executed_on = (fill.native_order_id or "").strip()
            if fill.activity_type.strip().upper() not in _EXECUTION_ACTIVITY_TYPES or executed_on not in members:
                continue
            event = exact_execution_of_activity(fill, leg=leg)
            if event is None:
                logger.warning(
                    "An Alpaca execution of a manual order could not be read as an exact execution",
                    extra={
                        "action": "manual_order_execution_unreadable",
                        "order_ref": order_ref,
                        "activity_id": fill.activity_id,
                        "broker_order_id": executed_on,
                    },
                )
                continue
            executions.append((fill, event))
        recorded_ids = {fill["execution_id"] for fill in repo.fills_for_order(order_ref) if fill["execution_id"]}
        unrecorded = {
            event.execution_id: event.quantity
            for _, event in executions
            if event.execution_id not in recorded_ids
        }
        exact_quantity, _ = repo.effective_exact_fill_totals_for_order(order_ref)
        recovered_quantity = math.fsum(quantity for quantity in unrecorded.values() if quantity is not None)
        if exact_quantity + recovered_quantity - head.quantity >= FILL_QTY_EPSILON:
            _refuse_over_head_quantity(
                repo,
                owner=owner,
                order_ref=order_ref,
                head=head,
                executions=[item for item in executions if item[1].execution_id in unrecorded],
                exact_quantity=exact_quantity,
                recovered_quantity=recovered_quantity,
            )
            return RecoveredExecutions(over_head_quantity=True)
        for fill, event in executions:
            executed_on = (fill.native_order_id or "").strip()
            outcome = append_exact_execution_slice(
                repo,
                event=event,
                symbol=leg.symbol,
                side=leg.side.value,
                broker_order_id=executed_on,
                order_ref=order_ref,
                owner=owner,
                evidence_source="activity_recovery",
                conflict_copy=ACTIVITY_EXACT_CONFLICT_COPY,
                proof_reference=fill.activity_id,
                extra_conflict_evidence_refs=[fill.activity_id],
            )
            if outcome == "duplicate":
                continue
            logger.info(
                "A manual order's execution was recovered from Alpaca's account activity",
                extra={
                    "action": "manual_order_execution_recovered",
                    "order_ref": order_ref,
                    "execution_id": event.execution_id,
                    "broker_order_id": executed_on,
                    "outcome": outcome,
                },
            )
        return RecoveredExecutions()


def _refuse_over_head_quantity(
    repo: ClerkSqliteRepository,
    *,
    owner: EffectOperationResource,
    order_ref: str,
    head: BrokerOrder,
    executions: list[tuple[BrokerActivity, BrokerOrderEvent]],
    exact_quantity: float,
    recovered_quantity: float,
) -> None:
    """Fence a batch that would credit the chain more shares than its head asked for.

    The order's coverage conflict is raised once, naming the batch's first
    execution and every activity in it; an order already fenced by an open
    coverage conflict keeps that one.
    """
    logger.warning(
        "Executions recovered for a manual order would exceed the shares its order asked for",
        extra={
            "action": "manual_order_recovered_executions_exceed_order",
            "order_ref": order_ref,
            "broker_order_id": head.order_id,
            "head_quantity": head.quantity,
            "exact_quantity": exact_quantity,
            "recovered_quantity": recovered_quantity,
            "activity_ids": [fill.activity_id for fill, _ in executions],
        },
    )
    if not executions or active_execution_coverage_conflicts(repo._conn, order_ref=order_ref):
        return
    fill, event = executions[0]
    repo.append_transition(
        exact_execution_coverage_conflict(
            repo,
            event=event,
            broker_order_id=(fill.native_order_id or "").strip(),
            order_ref=order_ref,
            owner=owner,
            conflict_copy=ACTIVITY_OVER_ORDER_QUANTITY_CONFLICT_COPY,
            proof_reference=fill.activity_id,
            extra_evidence_refs=[item.activity_id for item, _ in executions],
        )
    )


def exact_execution_of_activity(fill: BrokerActivity, *, leg: BrokerOrderLeg) -> BrokerOrderEvent | None:
    """The activity as one exact execution of the accepted leg, or ``None`` when it cannot be one."""
    if (fill.symbol or "").strip().upper() != leg.symbol.strip().upper():
        return None
    if _LEG_SIDE_OF_ACTIVITY.get((fill.side or "").strip().lower()) != leg.side:
        return None
    if fill.quantity is None or fill.price is None or fill.occurred_at_ms is None:
        return None
    if not (math.isfinite(fill.quantity) and fill.quantity > 0 and math.isfinite(fill.price) and fill.price > 0):
        return None
    return BrokerOrderEvent(
        event_type="fill",
        occurred_at_ms=fill.occurred_at_ms,
        price=fill.price,
        quantity=fill.quantity,
        execution_id=execution_id_from_activity_id(fill.activity_id),
    )


__all__ = [
    "ACTIVITY_READS_PER_WALK",
    "BEYOND_REACH_REWALK_INTERVAL_MS",
    "ManualLegExecutionRecovery",
    "RecoveredExecutions",
    "exact_execution_of_activity",
    "head_lacks_executions",
    "record_manual_leg_executions",
]
