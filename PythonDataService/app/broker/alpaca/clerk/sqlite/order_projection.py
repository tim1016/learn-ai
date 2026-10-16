"""SQLite order-leg presentation projections.

The ``orders`` fold owns broker identity and lifecycle state. Its immutable
requested leg lives in the original SQLite custody fact so mirror rebuild can
reproduce it without extending the hash-participating transition shape. This
module reads that single provenance fact plus current effective fill leaves;
it never contacts a broker or reads the legacy journal.

One option order of several legs is a parent row and a row for each leg
(``option_order_legs``, ADR 0076). The parent's line is marked and names no
symbol and no side, so no read here can take it for an order in a real
symbol. A leg's line is its own contract, side and contracts.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Collection, Sequence
from dataclasses import asdict, dataclass

from app.broker.alpaca.clerk.sqlite.option_order_legs import MULTI_LEG_ORDER_CLASS
from app.broker.alpaca.clerk.sqlite.projection_models import ProjectedOrder
from app.broker.contract.models import BrokerPosition, OrderSide

ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES = frozenset(
    {"filled", "canceled", "expired", "rejected", "replaced"}
)

UNFILLED_TERMINAL_STATES = frozenset({"canceled", "expired", "rejected"})
"""Terminal broker states that, with no recorded execution, are proven
unfilled (ADR 0059 D5.4) — distinct from ``filled``/``replaced``, whose
terminal snapshot can truthfully precede its execution slice on the
websocket.

Lives in this leaf module rather than in any one domain module because each
reads it: EXIT falls through to ``EXIT_NOT_FLAT`` on it (R12), ENTER folds
``ENTER_UNFILLED`` on it (#2006, the same ruling mirrored), and a manual
order's unfilled remainder ends on it (#2647), whose owner copy
(``manual_order_completion``) is checked against it at import.
"""

ENDED_BROKER_STATES = ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES - {"filled"}
"""Terminal broker states after which an order can never fill further.

Every terminal state but ``filled``: ``UNFILLED_TERMINAL_STATES`` plus
``replaced``, an order the broker replaced, which fills no further under its
own id. Derived from the one terminal set so the SQL below cannot spell a
second list that drifts from it.
"""


def sql_string_list(values: Collection[str]) -> str:
    """``values`` as the body of an SQL ``IN (...)``, sorted so the text is stable.

    For this module's fixed broker-state vocabularies only -- never for input,
    which is always bound as a parameter.
    """
    return ",".join(f"'{value}'" for value in sorted(values))


# Broker contact belongs to the parent and every leg: identity,
# acknowledgement, or fill on any of them prevents an absence proof.
ORDER_HAS_BROKER_EVIDENCE_SQL = (
    "(o.broker_order_id IS NOT NULL "
    "OR EXISTS (SELECT 1 FROM fills witnessed_fill WHERE witnessed_fill.order_ref = o.order_ref) "
    "OR EXISTS (SELECT 1 FROM custody_transitions witnessed_ack WHERE witnessed_ack.order_ref = o.order_ref "
    "AND witnessed_ack.transition_kind = 'ORDER_SUBMIT_ACKED') "
    "OR EXISTS (SELECT 1 FROM option_order_legs witnessed_leg "
    "JOIN orders witnessed_order ON witnessed_order.order_ref = witnessed_leg.order_ref "
    "WHERE witnessed_leg.parent_order_ref = o.order_ref AND (witnessed_order.broker_order_id IS NOT NULL "
    "OR EXISTS (SELECT 1 FROM fills leg_fill WHERE leg_fill.order_ref = witnessed_order.order_ref) "
    "OR EXISTS (SELECT 1 FROM custody_transitions leg_ack WHERE leg_ack.order_ref = witnessed_order.order_ref "
    "AND leg_ack.transition_kind = 'ORDER_SUBMIT_ACKED'))))"
)

# An order that can never fill further, over ``orders o`` joined to its
# ``effect_operations e``: the broker ended it, or its effect is terminal
# with no broker contact. Refused, failed and proven-absent orders share
# this definition (#2553); an unknown effect is not terminal. Never NULL.
ORDER_ENDED_SQL = (
    f"(LOWER(COALESCE(o.broker_state, '')) IN ({sql_string_list(ENDED_BROKER_STATES)}) "
    f"OR (e.state IN ('failed','rejected') AND NOT {ORDER_HAS_BROKER_EVIDENCE_SQL}))"
)

# An order that may still fill: neither filled nor ended (:data:`ORDER_ENDED_SQL`).
# One still being sent, or whose outcome is unknown, is open -- it may be
# working at the broker.
ORDER_OPEN_SQL = f"(LOWER(COALESCE(o.broker_state, '')) <> 'filled' AND NOT {ORDER_ENDED_SQL})"


def order_has_broker_evidence(conn: sqlite3.Connection, order_ref: str) -> bool:
    """A parent or any child has broker identity, acknowledgement or fills (ADR 0076)."""
    row = conn.execute(
        f"SELECT {ORDER_HAS_BROKER_EVIDENCE_SQL} FROM orders o WHERE o.order_ref = ?", (order_ref,),
    ).fetchone()
    return row is not None and bool(row[0])


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


# An order's line as it is read back: symbol, side, quantity, order type, limit, time in force.
# The symbol and the side are None only for the parent of an option order of several legs.
_OrderLine = tuple[str | None, str | None, float, str | None, float | None, str | None]


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
    legs: dict[str, _OrderLine] = {}
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
    unanswered = tuple(order_ref for order_ref in unique_refs if order_ref not in legs)
    if unanswered:
        legs.update(_lines_of_legs(conn, unanswered))
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
        "o.client_order_id, o.broker_order_id, o.role, o.broker_state, "
        f"o.updated_at_ms, {ORDER_OPEN_SQL} AS may_fill FROM orders o "
        "JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id JOIN ("
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
                updated_at_ms=row["updated_at_ms"],
                may_fill=bool(row["may_fill"]),
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
        f"o.broker_state, o.updated_at_ms, {ORDER_OPEN_SQL} AS may_fill FROM orders o "
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
        "o.broker_state, o.updated_at_ms FROM orders o "
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
    projected = []
    for row in rows:
        values = dict(row)
        if "may_fill" in values:
            values["may_fill"] = bool(values["may_fill"])
        projected.append(ProjectedOrder(**values, **asdict(details[row["order_ref"]])))
    return tuple(projected)


def _lines_of_legs(conn: sqlite3.Connection, order_refs: Sequence[str]) -> dict[str, _OrderLine]:
    """The order line of each leg of an option order of several legs among ``order_refs`` (ADR 0076).

    A leg's row has no creation fact of its own: its parent's creates it.
    Its line is read from the books' own record of the leg: its contract,
    its side, the contracts asked of it and its parent's kind of order. It
    states no limit: the limit is the parent's, a net price for the whole
    structure. So every reader of open orders sees each leg working in its
    own contract. Asked only for the orders no creation fact answered, so
    books that never took such an order make no read here.
    """
    placeholders = ",".join("?" for _ in order_refs)
    rows = conn.execute(
        "SELECT leg.order_ref, leg.symbol, leg.side, leg.contracts, parent_fact.transition_kind, "
        "parent_fact.facts_json FROM option_order_legs leg "
        "JOIN custody_transitions parent_fact ON parent_fact.order_ref = leg.parent_order_ref "
        "AND parent_fact.transition_kind IN ('ENTER_ACCEPTED', 'EXIT_REDUCING_ORDER_CREATED') "
        f"WHERE leg.order_ref IN ({placeholders}) ORDER BY parent_fact.sequence ASC",
        tuple(order_refs),
    ).fetchall()
    lines: dict[str, _OrderLine] = {}
    for row in rows:
        _symbol, _side, _units, order_type, _limit, time_in_force = _order_leg_from_facts(
            order_ref=row["order_ref"], transition_kind=row["transition_kind"], facts_json=row["facts_json"]
        )
        lines.setdefault(
            row["order_ref"],
            (row["symbol"], row["side"], float(row["contracts"]), order_type, None, time_in_force),
        )
    return lines


def _multi_leg_parent_line(*, order_ref: str, raw_leg: dict[str, object]) -> _OrderLine:
    """A parent's own order line, read back: no symbol, no side, whole structures, no limit.

    Held to exactly that. A marked line that names a symbol, a side or a
    limit is not a parent's, and is refused rather than read as an order in
    that symbol.
    """
    quantity, order_type, time_in_force = raw_leg.get("quantity"), raw_leg.get("order_type"), raw_leg.get("time_in_force")
    if (
        raw_leg.get("symbol") is not None
        or raw_leg.get("side") is not None
        or raw_leg.get("limit_price") is not None
        or isinstance(quantity, bool)
        or not isinstance(quantity, (int, float))
        or not math.isfinite(quantity)
        or quantity <= 0
        or not float(quantity).is_integer()
        or not isinstance(order_type, str)
        or order_type.lower() not in {"market", "limit"}
        or (time_in_force is not None and (not isinstance(time_in_force, str) or time_in_force.lower() != "day"))
    ):
        raise OrderProjectionReadError(f"SQLite order {order_ref!r} has invalid immutable multi-leg order values")
    return (None, None, float(quantity), order_type.lower(), None, "day")


def _order_leg_from_facts(
    *,
    order_ref: str,
    transition_kind: str,
    facts_json: str,
) -> _OrderLine:
    try:
        facts = json.loads(facts_json)
        is_reducing_exit = transition_kind == "EXIT_REDUCING_ORDER_CREATED"
        raw_leg = facts if is_reducing_exit else facts["leg"]
        if is_reducing_exit and isinstance(facts.get("option_order"), dict) and "legs" in facts["option_order"]:
            from app.broker.alpaca.clerk.sqlite.option_order_legs import multi_leg_parent_line

            raw_leg = multi_leg_parent_line(
                units=facts["option_order"]["quantity"], order_type=facts["option_order"]["order_type"],
            )
        if raw_leg.get("order_class") == MULTI_LEG_ORDER_CLASS:
            # One order of several legs (ADR 0076): the parent names no symbol and no side of its own.
            return _multi_leg_parent_line(order_ref=order_ref, raw_leg=raw_leg)
        symbol = raw_leg["symbol"]
        side = raw_leg["side"]
        quantity = raw_leg["quantity"]
        order_type = None if is_reducing_exit else raw_leg["order_type"]
        limit_price = None if is_reducing_exit else raw_leg.get("limit_price")
        time_in_force = None if is_reducing_exit else raw_leg["time_in_force"]
    except (KeyError, TypeError, AttributeError, json.JSONDecodeError) as exc:
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
    "ENDED_BROKER_STATES",
    "ORDER_ENDED_SQL",
    "ORDER_OPEN_SQL",
    "OrderProjectionReadError",
    "ProjectedOrderDetails",
    "read_current_orders",
    "read_open_opposite_side_orders",
    "read_order_details",
    "read_orders_by_operation",
    "sql_string_list",
]
