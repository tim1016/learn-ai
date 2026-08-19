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
from dataclasses import dataclass

from app.broker.alpaca.clerk.sqlite.projection_models import ProjectedOrder
from app.broker.contract.models import BrokerPosition

ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES = frozenset(
    {"filled", "canceled", "expired", "rejected", "replaced"}
)


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
    details = read_order_details(conn, tuple(row["order_ref"] for row in rows))
    return tuple(
        ProjectedOrder(
            **dict(row),
            symbol=details[row["order_ref"]].symbol,
            side=details[row["order_ref"]].side,
            quantity=details[row["order_ref"]].quantity,
            order_type=details[row["order_ref"]].order_type,
            limit_price=details[row["order_ref"]].limit_price,
            time_in_force=details[row["order_ref"]].time_in_force,
            filled_quantity=details[row["order_ref"]].filled_quantity,
        )
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
    "OrderProjectionReadError",
    "ProjectedOrderDetails",
    "read_current_orders",
    "read_order_details",
    "read_orders_by_operation",
]
