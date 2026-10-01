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

So before the sweep folds a REST answer from a live manual leg's chain that
reports executions the leg has no exact record of, it reads the account's
``FILL`` activities -- at most once per resolution -- and records each
execution of the chain as an exact ``activity_recovery`` slice under the
broker order that executed it. Recorded first, they leave the answer's
cumulative nothing to add, whatever a replacement's ``filled_qty`` means, and
the answer's acknowledgement ends the leg on them.

Each execution is credited once. Alpaca's activity id embeds the execution id
the stream carries (:func:`execution_id_from_activity_id`), and the one exact
append flow dedups on it: an execution the stream recorded first is a
duplicate here, and a stream redelivery of one recorded here is a duplicate
there. One that arrives after a cumulative was already folded takes the same
coverage path a late stream frame does.

The read is the broker's bounded newest-first walk. An execution older than
its reach, or not yet posted, is simply absent; the sweep resolves a live
manual leg on every pass until it ends, so a later pass finds it. A failed
read is logged, and the resolution folds as before.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.broker.alpaca.adapter import execution_id_from_activity_id
from app.broker.alpaca.clerk.sqlite.exact_execution_evidence import (
    ACTIVITY_EXACT_CONFLICT_COPY,
    append_exact_execution_slice,
)
from app.broker.alpaca.clerk.sqlite.execution_coverage import FILL_QTY_EPSILON
from app.broker.alpaca.clerk.sqlite.manual_order_completion import (
    manual_order_has_exact_terminal_coverage,
)
from app.broker.alpaca.clerk.sqlite.manual_order_replacement import live_manual_effect
from app.broker.alpaca.clerk.sqlite.off_loop import OffLoop
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.errors import BrokerError
from app.broker.contract.models import BrokerActivity, BrokerOrder, BrokerOrderEvent
from app.broker.contract.ports import BrokerReadPort

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.claimed_broker_io import ClaimedBrokerIO

logger = logging.getLogger(__name__)

_EXECUTION_ACTIVITY_TYPES = frozenset({"FILL", "PARTIAL_FILL"})

#: An activity's side, as the leg spells it. A sale from a flat or short
#: position is reported ``sell_short``; it is still the leg's sell.
_LEG_SIDE_OF_ACTIVITY = {"buy": "BUY", "sell": "SELL", "sell_short": "SELL"}


@dataclass
class ManualLegExecutionRecovery:
    """One resolution's recovery of a manual leg's exact executions.

    Built for one resolution of one captured order, so the account's
    ``FILL`` activities are read at most once however many members of the
    chain it folds. ``since_ms`` bounds the read at the broker's creation
    time of the leg's original order: no execution of its chain is older.
    """

    repo: ClerkSqliteRepository
    broker: ClaimedBrokerIO
    read: BrokerReadPort
    order_ref: str
    since_ms: int
    run: OffLoop
    _fills: tuple[BrokerActivity, ...] | None = field(default=None, init=False)
    _unreadable: bool = field(default=False, init=False)

    async def before_fold(self, answer: BrokerOrder) -> None:
        """Record the chain's executions before ``answer`` folds, when it reports ones the leg lacks."""
        if self._unreadable:
            return
        lacking = await self.run(
            lambda: reports_executions_the_leg_lacks(self.repo, order_ref=self.order_ref, answer=answer)
        )
        if not lacking:
            return
        fills = await self._fill_activities()
        if fills is None:
            return
        await self.run(
            lambda: record_manual_leg_executions(
                self.repo, order_ref=self.order_ref, answer=answer, fills=fills
            )
        )

    async def _fill_activities(self) -> tuple[BrokerActivity, ...] | None:
        if self._fills is None:
            observed = await self.broker.observe_fill_activities(self.read, after_ms=self.since_ms)
            if isinstance(observed, BrokerError):
                self._unreadable = True
                logger.warning(
                    "Alpaca's account activity could not be read for a manual order's executions",
                    extra={
                        "action": "manual_order_executions_unreadable",
                        "order_ref": self.order_ref,
                        "error": str(observed),
                    },
                )
                return None
            self._fills = tuple(observed)
        return self._fills


def reports_executions_the_leg_lacks(
    repo: ClerkSqliteRepository, *, order_ref: str, answer: BrokerOrder
) -> bool:
    """Whether ``answer`` reports executions the live manual leg has no exact record of.

    Either more filled shares than the leg's exact executions hold, or a
    ``filled`` head its exact executions do not cover yet -- a replacement's
    ``filled_qty`` may count only its own shares. Never for a bot order, an
    ended leg, or an answer missing a value it is read by: #2679's gate
    withholds that one.
    """
    if answer.unreadable_fields or not all(
        value.strip() for value in (answer.order_id, answer.status, answer.symbol, answer.side)
    ):
        return False
    row = repo.order(order_ref)
    effect = None if row is None else live_manual_effect(repo, row)
    if effect is None:
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
    answer: BrokerOrder,
    fills: Sequence[BrokerActivity],
) -> int:
    """Record each execution of the leg's chain in ``fills`` as an exact slice; return how many were new.

    An activity is the leg's when its order is ``answer`` itself -- which the
    caller folds under this leg next -- or a member of the leg's durable
    replacement chain. A member linked by an earlier answer's fold is found
    on the next answer, so a chain discovered within one resolution is
    recorded hop by hop. Its symbol and side must be the leg's and its
    quantity, price and time readable; one that is not is logged and left
    out, never folded.
    """
    row = repo.order(order_ref)
    owner = None if row is None else live_manual_effect(repo, row)
    if owner is None:
        return 0
    recorded = 0
    for fill in sorted(fills, key=lambda item: (item.occurred_at_ms or 0, item.activity_id)):
        executed_on = (fill.native_order_id or "").strip()
        if (
            fill.activity_type.strip().upper() not in _EXECUTION_ACTIVITY_TYPES
            or not executed_on
            or (executed_on != answer.order_id and repo.manual_chain_order_ref(executed_on) != order_ref)
        ):
            continue
        event = _exact_execution(fill, symbol=answer.symbol, side=answer.side)
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
        outcome = append_exact_execution_slice(
            repo,
            event=event,
            symbol=answer.symbol,
            side=answer.side,
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
        recorded += 1
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
    return recorded


def _exact_execution(fill: BrokerActivity, *, symbol: str, side: str) -> BrokerOrderEvent | None:
    """The activity as one exact execution of the leg, or ``None`` when it cannot be one."""
    if (fill.symbol or "").strip().upper() != symbol.strip().upper():
        return None
    if _LEG_SIDE_OF_ACTIVITY.get((fill.side or "").strip().lower()) != side.strip().upper():
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
    "ManualLegExecutionRecovery",
    "record_manual_leg_executions",
    "reports_executions_the_leg_lacks",
]
