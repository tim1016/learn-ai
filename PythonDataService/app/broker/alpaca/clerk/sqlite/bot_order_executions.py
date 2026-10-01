"""A bot order's executions the stream never delivered, read from Alpaca's account activity (#2787).

A bot order's fill reaches the Clerk as an exact execution slice on the
``trade_updates`` stream. When that frame is lost, the sweep's REST answer
credits the order's shares as a cumulative that names no execution: the
account's fill coverage stays incomplete -- no fee can be attributed to an
execution nobody named -- and a budgeted account refuses every bot entry.
Nothing replaced that cumulative. The stream does not redeliver a lost frame,
the sweep stops resolving an order once it ends, and the operator's recovery
(#2305) needs a coverage conflict that names the execution.

The account's fee evidence retains Alpaca's ``FILL`` activity. So after each
fee-evidence read, every bot order still holding a REST cumulative records
the executions the retained activity names for it as exact
``activity_recovery`` slices, through :mod:`activity_executions` with the
order's quantity as its cap. They go through the one exact append flow,
whose coverage proof supersedes the cumulative they account for; a stream
frame that arrives later is a duplicate there. A manual leg is #2686's
(:mod:`manual_order_executions`) and an outside order is not the Clerk's:
neither is touched. Nor is an order with a coverage conflict open, whose
quarantined evidence the operator settles, nor activity rows whose recorded
copies disagree: the fee projection already refuses on them.

A batch is recorded only once it accounts for exactly the shares the order's
fills hold, so an execution Alpaca has not posted yet leaves everything as it
was, and a later read that retains it picks it up. An order whose accepted
instruction cannot be read is logged once per process and left as it was;
the other orders are still recovered.

An activity that contradicts an execution the order records raises the
coverage conflict once (#2791); the order then leaves the held set, its
quarantined evidence the operator's to settle.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from app.broker.alpaca.clerk.sqlite.activity_executions import BOT_ORDER, first_report, record_activity_executions
from app.broker.alpaca.clerk.sqlite.execution_coverage import active_execution_coverage_conflicts
from app.broker.alpaca.clerk.sqlite.fee_evidence import retained_activities
from app.broker.alpaca.clerk.sqlite.order_projection import OrderProjectionReadError, read_order_details
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerActivity, BrokerOrderLeg

logger = logging.getLogger(__name__)


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
    check and its appends.
    """
    with repo._write_lock:
        held = _bot_orders_holding_a_cumulative(repo._conn)
        if not held:
            return False
        retained = retained_activities(repo._conn)
        executed_on: dict[str, list[BrokerActivity]] = defaultdict(list)
        for activity in retained.unique.values():
            if activity.activity_id not in retained.conflicts:
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
    """Record one order's retained executions it does not account for yet; whether custody grew."""
    if not fills:
        return False
    leg = _accepted_leg(repo, order=order)
    if leg is None:
        return False
    owner = repo.active_exit_for_order(order.order_ref) or repo.effect_operation(order.effect_operation_id)
    if owner is None:
        raise RuntimeError(f"SQLite order {order.order_ref!r} has no owning effect operation")
    return record_activity_executions(
        repo,
        subject=BOT_ORDER,
        order_ref=order.order_ref,
        owner=owner,
        leg=leg,
        broker_order_id=order.broker_order_id,
        member_ids={order.broker_order_id},
        quantity_cap=leg.quantity,
        require_total=True,
        fills=fills,
    ).grew


def _accepted_leg(repo: ClerkSqliteRepository, *, order: _HeldCumulative) -> BrokerOrderLeg | None:
    """The order's accepted instruction, or ``None`` when it cannot be read, logged once per process."""
    try:
        details = read_order_details(repo._conn, (order.order_ref,))[order.order_ref]
    except OrderProjectionReadError as error:
        reason = str(error)
    else:
        if details.symbol is not None and details.side is not None and details.quantity is not None:
            return BrokerOrderLeg(symbol=details.symbol, side=details.side.lower(), quantity=details.quantity)
        reason = "the accepted instruction names no symbol, side or quantity"
    action = "bot_order_instruction_unreadable"
    if first_report(repo, (action, order.order_ref, reason)):
        logger.warning(
            "A bot order's executions cannot be recovered without its accepted instruction",
            extra={"action": action, "order_ref": order.order_ref, "error": reason},
        )
    return None


__all__ = ["record_bot_order_executions"]
