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


class OrderProjectionReadError(RuntimeError):
    """The durable SQLite order facts cannot safely form a presentation row."""


@dataclass(frozen=True)
class ProjectedOrderDetails:
    """Immutable requested leg plus current effective filled quantity."""

    symbol: str | None
    side: str | None
    quantity: float | None
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
        "AND transition_kind IN ('ENTER_ACCEPTED', 'EXIT_REDUCING_ORDER_CREATED') "
        "ORDER BY sequence ASC",
        unique_refs,
    ).fetchall()
    legs: dict[str, tuple[str, str, float]] = {}
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
            *legs.get(order_ref, (None, None, None)),
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
        f"WHERE effect_operation_id IN ({placeholders})"
        ") owner ON owner.order_ref = o.order_ref "
        "ORDER BY o.updated_at_ms ASC, o.order_ref ASC",
        (*operation_ids, *operation_ids),
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
            filled_quantity=details[row["order_ref"]].filled_quantity,
        )
        for row in rows
    )


def _order_leg_from_facts(
    *,
    order_ref: str,
    transition_kind: str,
    facts_json: str,
) -> tuple[str, str, float]:
    try:
        facts = json.loads(facts_json)
        raw_leg = facts["leg"] if transition_kind == "ENTER_ACCEPTED" else facts
        symbol = raw_leg["symbol"]
        side = raw_leg["side"]
        quantity = raw_leg["quantity"]
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
    ):
        raise OrderProjectionReadError(
            f"SQLite order {order_ref!r} has invalid immutable leg values"
        )
    return symbol.upper(), side.lower(), float(quantity)


__all__ = [
    "OrderProjectionReadError",
    "ProjectedOrderDetails",
    "read_current_orders",
    "read_order_details",
    "read_orders_by_operation",
]
