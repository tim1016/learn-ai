"""A bot order's executions the stream never delivered, read from Alpaca's account activity (#2787).

A bot order's fill reaches the Clerk as an exact execution slice on the
``trade_updates`` stream. When that frame is lost, the sweep's REST answer
credits the order's shares as a cumulative that names no execution: the
account's fill coverage stays incomplete -- no fee can be attributed to an
execution nobody named -- and a budgeted account refuses every bot entry.
Nothing replaced that cumulative. The stream does not redeliver a lost frame,
the sweep stops resolving an order once it ends, and the operator's recovery
(#2305) needs a coverage conflict that names the execution.

The account's fee evidence retains Alpaca's ``FILL`` activity, and each row's
id embeds the execution id the stream carries
(:func:`execution_id_from_activity_id`). So after each fee-evidence read,
every bot order still holding a REST cumulative records the executions the
retained activity names for it, in its symbol and on its side, as exact
``activity_recovery`` slices. They go through the one exact append flow,
whose coverage proof supersedes the cumulative they account for; a stream
frame that arrives later is a duplicate there, as one the stream recorded
first is here. A manual leg is #2686's (:mod:`manual_order_executions`) and
an outside order is not the Clerk's: neither is touched. Nor is an order with
a coverage conflict open, whose quarantined evidence the operator settles.

Each execution is credited once. The id bridge has no captured Alpaca
receipt yet, and a wrong one would credit an execution twice under two ids.
So, under #2686's guard, an order's executions are recorded together or not
at all, and only while its exact total stays within its quantity, which no
order can execute past: a batch past it records nothing and raises
``EXECUTION_COVERAGE_CONFLICT``. A batch is recorded only once it accounts
for exactly the shares the order's fills hold, so an execution Alpaca has
not posted yet leaves everything as it was, and a later read that retains it
picks it up.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from app.broker.alpaca.clerk.sqlite.exact_execution_evidence import (
    ACTIVITY_EXACT_CONFLICT_COPY,
    ExactExecutionConflictCopy,
    append_exact_execution_slice,
    exact_execution_coverage_conflict,
)
from app.broker.alpaca.clerk.sqlite.execution_coverage import (
    FILL_QTY_EPSILON,
    active_execution_coverage_conflicts,
)
from app.broker.alpaca.clerk.sqlite.fee_evidence import retained_activities
from app.broker.alpaca.clerk.sqlite.manual_order_executions import exact_execution_of_activity
from app.broker.alpaca.clerk.sqlite.models import EffectOperationResource
from app.broker.alpaca.clerk.sqlite.order_projection import read_order_details
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerActivity, BrokerOrderEvent, BrokerOrderLeg

logger = logging.getLogger(__name__)

_EXECUTION_ACTIVITY_TYPES = frozenset({"FILL", "PARTIAL_FILL"})

#: This recovery's own refusal: no execution is held aside, so the copy sends
#: the operator to Alpaca rather than to a reconcile.
BOT_ACTIVITY_OVER_ORDER_QUANTITY_CONFLICT_COPY = ExactExecutionConflictCopy(
    headline="Alpaca's fill history reports more shares than the order asked for",
    explanation=(
        "Alpaca's fill history reports more shares for this bot's order than it "
        "asked for, so nothing was credited."
    ),
    operator_impact="New exposure is blocked while this conflict stands.",
    next_step="Check the order at Alpaca.",
)


@dataclass(frozen=True)
class _HeldCumulative:
    """A bot order Alpaca acknowledged whose fills still include a REST cumulative."""

    order_ref: str
    effect_operation_id: str
    broker_order_id: str


def record_bot_order_executions(repo: ClerkSqliteRepository) -> bool:
    """Record the retained executions of every bot order still holding a REST cumulative.

    Returns whether custody grew. Checked and appended under the
    repository's write lock, so no stream slice lands between an order's
    check and its appends. Activity rows whose recorded copies disagree are
    left out: the fee projection already refuses on them.
    """
    with repo._write_lock:
        held = _bot_orders_holding_a_cumulative(repo._conn)
        if not held:
            return False
        retained = retained_activities(repo._conn)
        executed_on: dict[str, list[BrokerActivity]] = defaultdict(list)
        for activity in retained.unique.values():
            if (
                activity.activity_id not in retained.conflicts
                and activity.activity_type.strip().upper() in _EXECUTION_ACTIVITY_TYPES
            ):
                executed_on[(activity.native_order_id or "").strip()].append(activity)
        grew = False
        for order in held:
            grew = _record_order(repo, order=order, fills=executed_on.get(order.broker_order_id, ())) or grew
        return grew


def _bot_orders_holding_a_cumulative(conn: sqlite3.Connection) -> tuple[_HeldCumulative, ...]:
    rows = conn.execute(
        "SELECT o.order_ref, o.effect_operation_id, o.broker_order_id FROM orders o "
        "JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id "
        "WHERE o.role IN ('ENTRY', 'REDUCING') AND o.broker_order_id IS NOT NULL "
        "AND e.strategy_instance_id IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM fills f WHERE f.order_ref = o.order_ref "
        "AND f.evidence_source = 'cumulative_recovery') "
        "ORDER BY o.order_ref"
    ).fetchall()
    return tuple(
        _HeldCumulative(
            order_ref=row["order_ref"],
            effect_operation_id=row["effect_operation_id"],
            broker_order_id=row["broker_order_id"],
        )
        for row in rows
        if not active_execution_coverage_conflicts(conn, order_ref=row["order_ref"])
    )


def _record_order(repo: ClerkSqliteRepository, *, order: _HeldCumulative, fills: Sequence[BrokerActivity]) -> bool:
    """Record one order's retained executions it has no exact record of, all or none."""
    if not fills:
        return False
    details = read_order_details(repo._conn, (order.order_ref,))[order.order_ref]
    if details.symbol is None or details.side is None or details.quantity is None:
        logger.warning(
            "A bot order's executions cannot be recovered without its accepted instruction",
            extra={"action": "bot_order_instruction_unreadable", "order_ref": order.order_ref},
        )
        return False
    leg = BrokerOrderLeg(symbol=details.symbol, side=details.side.lower(), quantity=details.quantity)
    owner = repo.active_exit_for_order(order.order_ref) or repo.effect_operation(order.effect_operation_id)
    if owner is None:
        raise RuntimeError(f"SQLite order {order.order_ref!r} has no owning effect operation")
    executions: list[tuple[BrokerActivity, BrokerOrderEvent]] = []
    for fill in sorted(fills, key=lambda item: (item.occurred_at_ms or 0, item.activity_id)):
        event = exact_execution_of_activity(fill, leg=leg)
        if event is None:
            logger.warning(
                "An Alpaca execution of a bot order could not be read as an exact execution",
                extra={
                    "action": "bot_order_execution_unreadable",
                    "order_ref": order.order_ref,
                    "activity_id": fill.activity_id,
                    "broker_order_id": order.broker_order_id,
                },
            )
            continue
        executions.append((fill, event))
    recorded_ids = {row["execution_id"] for row in repo.fills_for_order(order.order_ref) if row["execution_id"]}
    unrecorded = [(fill, event) for fill, event in executions if event.execution_id not in recorded_ids]
    if not unrecorded:
        return False
    exact_quantity, _ = repo.effective_exact_fill_totals_for_order(order.order_ref)
    filled_quantity, _ = repo.effective_fill_totals_for_order(order.order_ref)
    recovered_quantity = math.fsum(
        {event.execution_id: event.quantity or 0.0 for _, event in unrecorded}.values()
    )
    if exact_quantity + recovered_quantity - details.quantity >= FILL_QTY_EPSILON:
        _refuse_over_order_quantity(
            repo,
            order=order,
            owner=owner,
            order_quantity=details.quantity,
            executions=unrecorded,
            exact_quantity=exact_quantity,
            recovered_quantity=recovered_quantity,
        )
        return True
    if abs(exact_quantity + recovered_quantity - filled_quantity) >= FILL_QTY_EPSILON:
        return False
    for fill, event in unrecorded:
        outcome = append_exact_execution_slice(
            repo,
            event=event,
            symbol=leg.symbol,
            side=leg.side.value,
            broker_order_id=order.broker_order_id,
            order_ref=order.order_ref,
            owner=owner,
            evidence_source="activity_recovery",
            conflict_copy=ACTIVITY_EXACT_CONFLICT_COPY,
            proof_reference=fill.activity_id,
            extra_conflict_evidence_refs=[fill.activity_id],
        )
        logger.info(
            "A bot order's execution was recovered from Alpaca's account activity",
            extra={
                "action": "bot_order_execution_recovered",
                "order_ref": order.order_ref,
                "execution_id": event.execution_id,
                "broker_order_id": order.broker_order_id,
                "outcome": outcome,
            },
        )
    return True


def _refuse_over_order_quantity(
    repo: ClerkSqliteRepository,
    *,
    order: _HeldCumulative,
    owner: EffectOperationResource,
    order_quantity: float,
    executions: Sequence[tuple[BrokerActivity, BrokerOrderEvent]],
    exact_quantity: float,
    recovered_quantity: float,
) -> None:
    """Fence a batch that would credit the order more shares than it asked for.

    The order's coverage conflict names the batch's first execution and every
    activity in it. While it is open, no later read recovers the order again.
    """
    logger.warning(
        "Executions recovered for a bot order would exceed the shares it asked for",
        extra={
            "action": "bot_order_recovered_executions_exceed_order",
            "order_ref": order.order_ref,
            "broker_order_id": order.broker_order_id,
            "order_quantity": order_quantity,
            "exact_quantity": exact_quantity,
            "recovered_quantity": recovered_quantity,
            "activity_ids": [fill.activity_id for fill, _ in executions],
        },
    )
    fill, event = executions[0]
    repo.append_transition(
        exact_execution_coverage_conflict(
            repo,
            event=event,
            broker_order_id=order.broker_order_id,
            order_ref=order.order_ref,
            owner=owner,
            conflict_copy=BOT_ACTIVITY_OVER_ORDER_QUANTITY_CONFLICT_COPY,
            proof_reference=fill.activity_id,
            extra_evidence_refs=[item.activity_id for item, _ in executions],
        )
    )


__all__ = [
    "BOT_ACTIVITY_OVER_ORDER_QUANTITY_CONFLICT_COPY",
    "record_bot_order_executions",
]
