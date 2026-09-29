"""SQLite order-leg presentation projections.

The ``orders`` fold owns broker identity and lifecycle state. Its immutable
requested leg lives in the original SQLite custody fact so mirror rebuild can
reproduce it without extending the hash-participating transition shape. This
module reads that single provenance fact plus current effective fill leaves;
it never contacts a broker or reads the legacy journal.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from app.broker.alpaca.clerk.sqlite.projection_models import ProjectedOrder
from app.broker.contract.models import BrokerPosition, OrderSide

ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES = frozenset(
    {"filled", "canceled", "expired", "rejected", "replaced"}
)

# An order that can never fill further, over ``orders o`` joined to its
# ``effect_operations e``: the broker ended it, or its effect is terminal while
# nothing says the broker ever knew it -- no broker identity (set only by an
# acknowledgement) and no fill (a fill can be recorded before its order's
# acknowledgement). A refused, failed or proven-absent order is the common
# case (#2553); an ``unknown`` effect is not terminal. Never NULL, so it
# composes under NOT. The one definition every "has this order ended?" read
# composes; the same three facts ``order_evidence.order_never_reached_broker``
# reads.
ORDER_ENDED_SQL = (
    "(LOWER(COALESCE(o.broker_state, '')) IN ('canceled','expired','rejected','replaced') "
    "OR (e.state IN ('failed','rejected') AND o.broker_order_id IS NULL "
    "AND NOT EXISTS (SELECT 1 FROM fills ended_fill WHERE ended_fill.order_ref = o.order_ref)))"
)

# An order that may still fill: neither filled nor ended (:data:`ORDER_ENDED_SQL`).
# One still being sent, or whose outcome is unknown, is open -- it may be
# working at the broker.
ORDER_OPEN_SQL = f"(LOWER(COALESCE(o.broker_state, '')) <> 'filled' AND NOT {ORDER_ENDED_SQL})"


def signed_broker_position_quantity(position: BrokerPosition) -> float:
    """Normalize Alpaca's absolute quantity and side to a signed quantity."""

    quantity = abs(position.quantity)
    return -quantity if position.side.lower() == "short" else quantity


class OrderProjectionReadError(RuntimeError):
    """The durable SQLite order facts cannot safely form a presentation row."""


@dataclass(frozen=True)
class ProjectedOrderDetails:
    """Immutable requested leg plus current effective filled quantity."""

    symbol: str | None
    side: str | None
    quantity: float | None
    order_type: str | None
    limit_price: float | None
    time_in_force: str | None
    filled_quantity: float


def read_order_details(
    conn: sqlite3.Connection,
    order_refs: Sequence[str],
) -> dict[str, ProjectedOrderDetails]:
    """Read presentation details from one caller-owned SQLite transaction.

    The only log lookup is a direct read of each order's immutable creation
    fact. Current execution quantity comes from effective fill leaves, so a
    correction replaces its predecessor instead of being double-counted.
    """
    unique_refs = tuple(dict.fromkeys(order_refs))
    if not unique_refs:
        return {}
    placeholders = ",".join("?" for _ in unique_refs)
    fact_rows = conn.execute(
        "SELECT order_ref, transition_kind, facts_json FROM custody_transitions "
        f"WHERE order_ref IN ({placeholders}) "
        "AND transition_kind IN "
        "('ENTER_ACCEPTED', 'EXIT_REDUCING_ORDER_CREATED', 'MANUAL_ORDER_ACCEPTED') "
        "ORDER BY sequence ASC",
        unique_refs,
    ).fetchall()
    legs: dict[
        str,
        tuple[str, str, float, str | None, float | None, str | None],
    ] = {}
    for row in fact_rows:
        candidate = _order_leg_from_facts(
            order_ref=row["order_ref"],
            transition_kind=row["transition_kind"],
            facts_json=row["facts_json"],
        )
        prior = legs.setdefault(row["order_ref"], candidate)
        if prior != candidate:
            raise OrderProjectionReadError(
                f"SQLite order {row['order_ref']!r} has contradictory immutable leg facts"
            )
    fill_rows = conn.execute(
        "SELECT f.order_ref, SUM(f.qty) AS filled_quantity FROM fills f "
        f"WHERE f.order_ref IN ({placeholders}) AND NOT EXISTS ("
        "SELECT 1 FROM fills successor "
        "WHERE successor.superseded_execution_ref = f.execution_id) "
        "GROUP BY f.order_ref",
        unique_refs,
    ).fetchall()
    filled_quantities = {
        row["order_ref"]: float(row["filled_quantity"])
        for row in fill_rows
    }
    return {
        order_ref: ProjectedOrderDetails(
            *legs.get(order_ref, (None, None, None, None, None, None)),
            filled_quantities.get(order_ref, 0.0),
        )
        for order_ref in unique_refs
    }


def read_orders_by_operation(
    conn: sqlite3.Connection,
    operation_ids: tuple[str, ...],
) -> dict[str, tuple[ProjectedOrder, ...]]:
    """Return SQLite-owned orders linked to each requested operation."""
    if not operation_ids:
        return {}
    placeholders = ",".join("?" for _ in operation_ids)
    rows = conn.execute(
        "SELECT owner.effect_operation_id AS owner_effect_operation_id, o.order_ref, "
        "o.client_order_id, o.broker_order_id, o.role, o.broker_state, o.submitted_at_ms, "
        "o.updated_at_ms FROM orders o JOIN ("
        "SELECT effect_operation_id, order_ref FROM operation_order_links "
        f"WHERE effect_operation_id IN ({placeholders}) UNION "
        "SELECT effect_operation_id, order_ref FROM orders "
        f"WHERE effect_operation_id IN ({placeholders}) UNION "
        "SELECT effect_operation_id, order_ref FROM manual_order_cancellations "
        f"WHERE effect_operation_id IN ({placeholders})"
        ") owner ON owner.order_ref = o.order_ref "
        "ORDER BY o.updated_at_ms ASC, o.order_ref ASC",
        (*operation_ids, *operation_ids, *operation_ids),
    ).fetchall()
    details = read_order_details(conn, tuple(row["order_ref"] for row in rows))
    grouped: dict[str, list[ProjectedOrder]] = {
        operation_id: [] for operation_id in operation_ids
    }
    for row in rows:
        detail = details[row["order_ref"]]
        grouped[row["owner_effect_operation_id"]].append(
            ProjectedOrder(
                order_ref=row["order_ref"],
                client_order_id=row["client_order_id"],
                broker_order_id=row["broker_order_id"],
                role=row["role"],
                broker_state=row["broker_state"],
                submitted_at_ms=row["submitted_at_ms"],
                updated_at_ms=row["updated_at_ms"],
                symbol=detail.symbol,
                side=detail.side,
                quantity=detail.quantity,
                order_type=detail.order_type,
                limit_price=detail.limit_price,
                time_in_force=detail.time_in_force,
                filled_quantity=detail.filled_quantity,
            )
        )
    return {key: tuple(value) for key, value in grouped.items()}


def read_current_orders(
    conn: sqlite3.Connection,
    strategy_instance_id: str | None,
) -> tuple[ProjectedOrder, ...]:
    """Return every materialized SQLite order in the requested custody scope."""
    if strategy_instance_id is None:
        where, params = "", ()
    else:
        where, params = "WHERE e.strategy_instance_id = ?", (strategy_instance_id,)
    rows = conn.execute(
        "SELECT o.order_ref, o.client_order_id, o.broker_order_id, o.role, "
        "o.broker_state, o.submitted_at_ms, o.updated_at_ms FROM orders o "
        "JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id "
        f"{where} ORDER BY o.updated_at_ms ASC, o.order_ref ASC",
        params,
    ).fetchall()
    return _projected_orders(conn, rows)


def read_open_opposite_side_orders(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    side: OrderSide,
) -> tuple[ProjectedOrder, ...]:
    """The account's orders still open on ``symbol`` on the other side from ``side``.

    Alpaca refuses a new order while an opposite-side order on the same
    symbol is open in the account (its wash-trade protection,
    https://docs.alpaca.markets/us/docs/user-protection), whoever placed it.
    So this reads every custody subject's orders -- each bot's entries and
    exits and every manual ticket's -- from the Clerk's own records, never a
    broker read. Open is :data:`ORDER_OPEN_SQL`: an order still being sent, or
    whose outcome is unknown, counts; one that provably never reached the
    broker does not.
    """
    rows = conn.execute(
        "SELECT o.order_ref, o.client_order_id, o.broker_order_id, o.role, "
        "o.broker_state, o.submitted_at_ms, o.updated_at_ms FROM orders o "
        "JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id "
        f"WHERE {ORDER_OPEN_SQL} "
        "ORDER BY o.updated_at_ms ASC, o.order_ref ASC",
    ).fetchall()
    other_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
    return tuple(
        order
        for order in _projected_orders(conn, rows)
        if order.symbol == symbol.upper() and order.side == other_side.value
    )


def _projected_orders(
    conn: sqlite3.Connection,
    rows: Sequence[sqlite3.Row],
) -> tuple[ProjectedOrder, ...]:
    """Each ``orders`` row joined with its immutable leg and effective fills."""
    details = read_order_details(conn, tuple(row["order_ref"] for row in rows))
    return tuple(
        ProjectedOrder(**dict(row), **asdict(details[row["order_ref"]]))
        for row in rows
    )


def _order_leg_from_facts(
    *,
    order_ref: str,
    transition_kind: str,
    facts_json: str,
) -> tuple[str, str, float, str | None, float | None, str | None]:
    try:
        facts = json.loads(facts_json)
        is_reducing_exit = transition_kind == "EXIT_REDUCING_ORDER_CREATED"
        raw_leg = facts if is_reducing_exit else facts["leg"]
        symbol = raw_leg["symbol"]
        side = raw_leg["side"]
        quantity = raw_leg["quantity"]
        order_type = None if is_reducing_exit else raw_leg["order_type"]
        limit_price = None if is_reducing_exit else raw_leg.get("limit_price")
        time_in_force = None if is_reducing_exit else raw_leg["time_in_force"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise OrderProjectionReadError(
            f"SQLite order {order_ref!r} has malformed {transition_kind} facts"
        ) from exc
    if (
        not isinstance(symbol, str)
        or not symbol
        or not isinstance(side, str)
        or side.lower() not in {"buy", "sell"}
        or isinstance(quantity, bool)
        or not isinstance(quantity, (int, float))
        or not math.isfinite(quantity)
        or quantity <= 0
        or (
            not is_reducing_exit
            and (
                not isinstance(order_type, str)
                or order_type.lower() not in {"market", "limit"}
            )
        )
        or (
            limit_price is not None
            and (
                isinstance(limit_price, bool)
                or not isinstance(limit_price, (int, float))
                or not math.isfinite(limit_price)
                or limit_price <= 0
            )
        )
        or (
            not is_reducing_exit
            and (
                not isinstance(time_in_force, str)
                or time_in_force.lower() not in {"day", "gtc"}
            )
        )
    ):
        raise OrderProjectionReadError(
            f"SQLite order {order_ref!r} has invalid immutable leg values"
        )
    return (
        symbol.upper(),
        side.lower(),
        float(quantity),
        None if order_type is None else order_type.lower(),
        None if limit_price is None else float(limit_price),
        None if time_in_force is None else time_in_force.lower(),
    )


__all__ = [
    "ORDER_ENDED_SQL",
    "ORDER_OPEN_SQL",
    "OrderProjectionReadError",
    "ProjectedOrderDetails",
    "read_current_orders",
    "read_open_opposite_side_orders",
    "read_order_details",
    "read_orders_by_operation",
]
