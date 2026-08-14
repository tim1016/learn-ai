"""Typed read snapshots over the SQLite Clerk spine.

Pure ``SELECT`` + row-to-dataclass mapping, no write path, no lock, no fold
concerns — split out of ``repository.py`` to keep that module under the
file-size ceiling as new read surfaces accumulate slice over slice.
``ClerkSqliteRepository`` methods delegate here; callers outside this package
still never see a cursor (PRD §9.2) — they go through the repository, which
happens to forward to this module for these queries.
"""

from __future__ import annotations

import json
import sqlite3

from app.broker.alpaca.clerk.sqlite.models import (
    BotConfigResource,
    CommandResource,
    ControlMetaSnapshot,
    DecisionReceiptResource,
    EffectOperationResource,
    ExternalOrderResource,
    ManualOrderCancellationResource,
    ManualOrderLegResource,
    ManualOrderTicketResource,
    OrderResource,
)

_COMMAND_COLUMNS: tuple[str, ...] = (
    "command_id",
    "idempotency_key",
    "payload_hash",
    "kind",
    "strategy_instance_id",
    "run_id",
    "action",
    "intended_end_state",
    "state",
    "effect_operation_id",
    "receipt_id",
    "created_at_ms",
    "updated_at_ms",
)

_DECISION_RECEIPT_COLUMNS: tuple[str, ...] = (
    "strategy_instance_id",
    "seq",
    "outcome",
    "symbol",
    "intent_id",
    "order_ref",
    "observed_at_ms",
    "facts_json",
)

_EXTERNAL_ORDER_COLUMNS: tuple[str, ...] = (
    "external_order_id",
    "broker_order_id",
    "client_order_id",
    "symbol",
    "side",
    "qty",
    "order_type",
    "limit_price",
    "stop_price",
    "filled_avg_price",
    "observed_at_ms",
    "acknowledged_at_ms",
    "ack_operator",
    "evidence_refs_json",
)

_EXTERNAL_ORDER_SELECT = (
    ", ".join(f"eo.{column}" for column in _EXTERNAL_ORDER_COLUMNS)
    + ", (SELECT MIN(ct.sequence) FROM custody_transitions ct "
    "WHERE ct.broker_order_id = eo.broker_order_id "
    "AND ct.transition_kind = 'EXTERNAL_ORDER_OBSERVED') AS observation_sequence"
    + ", (SELECT MAX(ct.sequence) FROM custody_transitions ct "
    "WHERE ct.broker_order_id = eo.broker_order_id "
    "AND ct.transition_kind = 'EXTERNAL_ORDER_ACKNOWLEDGED') AS acknowledgement_sequence"
    + ", (SELECT ct.recorded_at_ms FROM custody_transitions ct "
    "WHERE ct.broker_order_id = eo.broker_order_id "
    "AND ct.transition_kind = 'EXTERNAL_ORDER_OBSERVED' "
    "ORDER BY ct.sequence ASC LIMIT 1) AS observation_recorded_at_ms"
    + ", (SELECT ct.recorded_at_ms FROM custody_transitions ct "
    "WHERE ct.broker_order_id = eo.broker_order_id "
    "AND ct.transition_kind = 'EXTERNAL_ORDER_ACKNOWLEDGED' "
    "ORDER BY ct.sequence DESC LIMIT 1) AS acknowledgement_recorded_at_ms"
)


def _row_to_command_resource(row: sqlite3.Row) -> CommandResource:
    return CommandResource(**{column: row[column] for column in _COMMAND_COLUMNS})


def control_meta_snapshot(conn: sqlite3.Connection) -> ControlMetaSnapshot:
    row = conn.execute(
        "SELECT schema_version, account_id, db_identity_token, authority_generation, "
        "control_revision, created_at_ms, last_open_at_ms FROM control_meta WHERE id = 1"
    ).fetchone()
    return ControlMetaSnapshot(**dict(row))


def strategy_instances(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT strategy_instance_id, symbol, config_hash, created_at_ms, retired_at_ms "
        "FROM strategy_instances ORDER BY created_at_ms ASC"
    ).fetchall()
    return [dict(row) for row in rows]


def strategy_instance(conn: sqlite3.Connection, strategy_instance_id: str) -> dict | None:
    row = conn.execute(
        "SELECT strategy_instance_id, symbol, config_hash, created_at_ms, retired_at_ms "
        "FROM strategy_instances WHERE strategy_instance_id = ?",
        (strategy_instance_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def bot_config(conn: sqlite3.Connection, strategy_instance_id: str) -> BotConfigResource | None:
    row = conn.execute(
        "SELECT strategy_instance_id, strategy_key, display_name, config_json, config_hash, created_at_ms "
        "FROM bot_config WHERE strategy_instance_id = ?",
        (strategy_instance_id,),
    ).fetchone()
    return BotConfigResource(**dict(row)) if row is not None else None


def decision_receipt_tail(
    conn: sqlite3.Connection,
    *,
    strategy_instance_id: str,
    limit: int,
) -> list[DecisionReceiptResource]:
    rows = conn.execute(
        f"SELECT {', '.join(_DECISION_RECEIPT_COLUMNS)} FROM decision_receipts "
        "WHERE strategy_instance_id = ? ORDER BY seq DESC LIMIT ?",
        (strategy_instance_id, limit),
    ).fetchall()
    return [DecisionReceiptResource(**dict(row)) for row in reversed(rows)]


def decision_receipts_by_transaction(
    conn: sqlite3.Connection,
    *,
    strategy_instance_id: str,
    transaction_ref: str,
    limit: int,
) -> list[DecisionReceiptResource]:
    rows = conn.execute(
        f"SELECT {', '.join(_DECISION_RECEIPT_COLUMNS)} FROM decision_receipts "
        "WHERE strategy_instance_id = ? AND (intent_id = ? OR order_ref = ?) "
        "ORDER BY seq DESC LIMIT ?",
        (strategy_instance_id, transaction_ref, transaction_ref, limit),
    ).fetchall()
    return [DecisionReceiptResource(**dict(row)) for row in reversed(rows)]


def _external_order_resource(row: sqlite3.Row) -> ExternalOrderResource:
    values = {column: row[column] for column in _EXTERNAL_ORDER_COLUMNS}
    evidence_refs = json.loads(values.pop("evidence_refs_json"))
    if not isinstance(evidence_refs, list) or not all(isinstance(item, str) for item in evidence_refs):
        raise ValueError("external order evidence_refs_json must be a string list")
    return ExternalOrderResource(
        **values,
        evidence_refs=tuple(evidence_refs),
        observation_sequence=row["observation_sequence"],
        acknowledgement_sequence=row["acknowledgement_sequence"],
        observation_recorded_at_ms=row["observation_recorded_at_ms"],
        acknowledgement_recorded_at_ms=row["acknowledgement_recorded_at_ms"],
    )


def external_order(
    conn: sqlite3.Connection,
    external_order_id: str,
) -> ExternalOrderResource | None:
    row = conn.execute(
        f"SELECT {_EXTERNAL_ORDER_SELECT} FROM external_orders eo "
        "WHERE external_order_id = ?",
        (external_order_id,),
    ).fetchone()
    return _external_order_resource(row) if row is not None else None


def external_order_by_broker_order_id(
    conn: sqlite3.Connection,
    broker_order_id: str,
) -> ExternalOrderResource | None:
    row = conn.execute(
        f"SELECT {_EXTERNAL_ORDER_SELECT} FROM external_orders eo "
        "WHERE broker_order_id = ?",
        (broker_order_id,),
    ).fetchone()
    return _external_order_resource(row) if row is not None else None


def external_orders(conn: sqlite3.Connection) -> list[ExternalOrderResource]:
    rows = conn.execute(
        f"SELECT {_EXTERNAL_ORDER_SELECT} FROM external_orders eo "
        "ORDER BY eo.observed_at_ms DESC, eo.external_order_id DESC"
    ).fetchall()
    return [_external_order_resource(row) for row in rows]


def external_order_page(
    conn: sqlite3.Connection,
    *,
    observation_sequence_before: int | None,
    external_order_id_before: str | None,
    lifecycle_state: str | None,
    limit: int,
) -> list[ExternalOrderResource]:
    if (observation_sequence_before is None) != (external_order_id_before is None):
        raise ValueError("external-order cursor must include both keyset fields")
    lifecycle_predicate = {
        None: "",
        "review_required": "eo.acknowledged_at_ms IS NULL",
        "reviewed": "eo.acknowledged_at_ms IS NOT NULL",
    }.get(lifecycle_state)
    if lifecycle_predicate is None:
        raise ValueError("external-order lifecycle_state is invalid")
    params: tuple[object, ...]
    where_clauses: list[str] = []
    observation_sequence = (
        "(SELECT MIN(ct.sequence) FROM custody_transitions ct "
        "WHERE ct.broker_order_id = eo.broker_order_id "
        "AND ct.transition_kind = 'EXTERNAL_ORDER_OBSERVED')"
    )
    if observation_sequence_before is None:
        params = (limit,)
    else:
        where_clauses.append(
            f"({observation_sequence} < ? OR ({observation_sequence} = ? "
            "AND eo.external_order_id < ?))"
        )
        params = (
            observation_sequence_before,
            observation_sequence_before,
            external_order_id_before,
            limit,
        )
    if lifecycle_predicate:
        where_clauses.append(lifecycle_predicate)
    where = f"WHERE {' AND '.join(where_clauses)} " if where_clauses else ""
    rows = conn.execute(
        f"SELECT {_EXTERNAL_ORDER_SELECT} FROM external_orders eo {where}"
        f"ORDER BY {observation_sequence} DESC, eo.external_order_id DESC LIMIT ?",
        params,
    ).fetchall()
    return [_external_order_resource(row) for row in rows]


def command(conn: sqlite3.Connection, command_id: str) -> CommandResource | None:
    row = conn.execute(
        f"SELECT {', '.join(_COMMAND_COLUMNS)} FROM commands WHERE command_id = ?",
        (command_id,),
    ).fetchone()
    return _row_to_command_resource(row) if row is not None else None


def command_by_idempotency_key(
    conn: sqlite3.Connection, *, authority_generation: int, idempotency_key: str
) -> CommandResource | None:
    row = conn.execute(
        f"SELECT {', '.join(_COMMAND_COLUMNS)} FROM commands "
        "WHERE authority_generation = ? AND idempotency_key = ?",
        (authority_generation, idempotency_key),
    ).fetchone()
    return _row_to_command_resource(row) if row is not None else None


def effect_operation(
    conn: sqlite3.Connection, effect_operation_id: str
) -> EffectOperationResource | None:
    row = conn.execute(
        "SELECT effect_operation_id, authority_generation, idempotency_key, command_id, "
        "strategy_instance_id, run_id, kind, state, custody_owner, created_at_ms, "
        "updated_at_ms, terminal_receipt_id FROM effect_operations "
        "WHERE effect_operation_id = ?",
        (effect_operation_id,),
    ).fetchone()
    return EffectOperationResource(**dict(row)) if row is not None else None


def order(conn: sqlite3.Connection, order_ref: str) -> OrderResource | None:
    row = conn.execute(
        "SELECT order_ref, effect_operation_id, client_order_id, broker_order_id, role, "
        "broker_state, submitted_at_ms, updated_at_ms FROM orders WHERE order_ref = ?",
        (order_ref,),
    ).fetchone()
    return OrderResource(**dict(row)) if row is not None else None


def order_for_effect_operation(
    conn: sqlite3.Connection, effect_operation_id: str
) -> OrderResource | None:
    """The order originally created by a single-order effect (ENTER)."""
    row = conn.execute(
        "SELECT order_ref, effect_operation_id, client_order_id, broker_order_id, role, "
        "broker_state, submitted_at_ms, updated_at_ms FROM orders "
        "WHERE effect_operation_id = ?",
        (effect_operation_id,),
    ).fetchone()
    return OrderResource(**dict(row)) if row is not None else None


def manual_order_ticket(conn: sqlite3.Connection, ticket_id: str) -> ManualOrderTicketResource | None:
    """Return one manual ticket and its bounded, durable leg resources."""
    ticket = conn.execute(
        "SELECT ticket_id, subject_id, operator_id, instruction_hash, state, created_at_ms, updated_at_ms "
        "FROM manual_order_tickets WHERE ticket_id = ?",
        (ticket_id,),
    ).fetchone()
    if ticket is None:
        return None
    legs = conn.execute(
        "SELECT ticket_id, leg_id, subject_id, instruction_hash, command_id, effect_operation_id, "
        "order_ref, state, created_at_ms, updated_at_ms FROM manual_order_legs "
        "WHERE ticket_id = ? ORDER BY leg_id ASC",
        (ticket_id,),
    ).fetchall()
    return ManualOrderTicketResource(
        **dict(ticket),
        legs=tuple(ManualOrderLegResource(**dict(leg)) for leg in legs),
    )


def manual_order_cancellation(
    conn: sqlite3.Connection,
    *,
    order_ref: str,
) -> ManualOrderCancellationResource | None:
    """Return the one durable cancellation owned by a manual order reference."""
    row = conn.execute(
        "SELECT order_ref, subject_id, cancel_request_id, command_id, effect_operation_id, "
        "state, created_at_ms, updated_at_ms FROM manual_order_cancellations WHERE order_ref = ?",
        (order_ref,),
    ).fetchone()
    return ManualOrderCancellationResource(**dict(row)) if row is not None else None


def manual_order_leg_for_order_ref(
    conn: sqlite3.Connection,
    *,
    order_ref: str,
) -> ManualOrderLegResource | None:
    """Return the immutable ticket leg that owns one manual order reference."""
    row = conn.execute(
        "SELECT ticket_id, leg_id, subject_id, instruction_hash, command_id, effect_operation_id, "
        "order_ref, state, created_at_ms, updated_at_ms FROM manual_order_legs WHERE order_ref = ?",
        (order_ref,),
    ).fetchone()
    return ManualOrderLegResource(**dict(row)) if row is not None else None


def manual_order_cancellation_for_effect(
    conn: sqlite3.Connection,
    *,
    effect_operation_id: str,
) -> ManualOrderCancellationResource | None:
    """Resolve a recovery effect back to its immutable manual target order."""
    row = conn.execute(
        "SELECT order_ref, subject_id, cancel_request_id, command_id, effect_operation_id, "
        "state, created_at_ms, updated_at_ms FROM manual_order_cancellations "
        "WHERE effect_operation_id = ?",
        (effect_operation_id,),
    ).fetchone()
    return ManualOrderCancellationResource(**dict(row)) if row is not None else None


def custody_subject(conn: sqlite3.Connection, subject_id: str) -> dict | None:
    row = conn.execute(
        "SELECT subject_id, kind, strategy_instance_id, operator_id, created_at_ms "
        "FROM custody_subjects WHERE subject_id = ?",
        (subject_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def orders_for_effect_operation(
    conn: sqlite3.Connection, effect_operation_id: str
) -> list[OrderResource]:
    """Every order linked to this operation, without changing order origin."""
    rows = conn.execute(
        "SELECT o.order_ref, o.effect_operation_id, o.client_order_id, o.broker_order_id, o.role, "
        "o.broker_state, o.submitted_at_ms, o.updated_at_ms FROM orders o "
        "JOIN operation_order_links l USING (order_ref) "
        "WHERE l.effect_operation_id = ? ORDER BY l.linked_at_ms ASC, o.order_ref ASC",
        (effect_operation_id,),
    ).fetchall()
    return [OrderResource(**dict(row)) for row in rows]


def all_order_refs(conn: sqlite3.Connection) -> frozenset[str]:
    """Every immutable broker identity captured by this authority."""
    rows = conn.execute("SELECT order_ref FROM orders").fetchall()
    return frozenset(row["order_ref"] for row in rows)


def entry_orders_for_strategy(
    conn: sqlite3.Connection, strategy_instance_id: str
) -> list[OrderResource]:
    """Every entry order whose immutable origin belongs to one strategy."""
    rows = conn.execute(
        "SELECT o.order_ref, o.effect_operation_id, o.client_order_id, o.broker_order_id, "
        "o.role, o.broker_state, o.submitted_at_ms, o.updated_at_ms FROM orders o "
        "JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id "
        "WHERE o.role = 'ENTRY' AND e.strategy_instance_id = ? "
        "ORDER BY o.updated_at_ms ASC, o.order_ref ASC",
        (strategy_instance_id,),
    ).fetchall()
    return [OrderResource(**dict(row)) for row in rows]


def orders_for_strategy(
    conn: sqlite3.Connection, strategy_instance_id: str
) -> list[OrderResource]:
    """Every order (ENTRY and REDUCING alike) belonging to one strategy.

    Unlike :func:`entry_orders_for_strategy`, this is not role-filtered — the
    custody-proof surface needs a live EXIT's REDUCING child counted as
    working/unresolved exposure too, not just its cancelled ENTRY siblings.
    """
    rows = conn.execute(
        "SELECT o.order_ref, o.effect_operation_id, o.client_order_id, o.broker_order_id, "
        "o.role, o.broker_state, o.submitted_at_ms, o.updated_at_ms FROM orders o "
        "JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id "
        "WHERE e.strategy_instance_id = ? "
        "ORDER BY o.updated_at_ms ASC, o.order_ref ASC",
        (strategy_instance_id,),
    ).fetchall()
    return [OrderResource(**dict(row)) for row in rows]


def active_exit_for_order(conn: sqlite3.Connection, order_ref: str) -> EffectOperationResource | None:
    """The nonterminal EXIT currently linked to an entry, if any."""
    row = conn.execute(
        "SELECT e.effect_operation_id, e.authority_generation, e.idempotency_key, e.command_id, "
        "e.strategy_instance_id, e.run_id, e.kind, e.state, e.custody_owner, e.created_at_ms, "
        "e.updated_at_ms, e.terminal_receipt_id FROM effect_operations e "
        "JOIN operation_order_links l ON l.effect_operation_id = e.effect_operation_id "
        "WHERE l.order_ref = ? AND e.kind = 'EXIT' "
        "AND e.state NOT IN ('succeeded','failed','rejected') "
        "ORDER BY e.created_at_ms DESC LIMIT 1",
        (order_ref,),
    ).fetchone()
    return EffectOperationResource(**dict(row)) if row is not None else None


def active_exit_for_strategy(
    conn: sqlite3.Connection, strategy_instance_id: str
) -> EffectOperationResource | None:
    """The strategy's live EXIT fence against concurrently admitted ENTERs."""
    row = conn.execute(
        "SELECT effect_operation_id, authority_generation, idempotency_key, command_id, "
        "strategy_instance_id, run_id, kind, state, custody_owner, created_at_ms, "
        "updated_at_ms, terminal_receipt_id FROM effect_operations "
        "WHERE strategy_instance_id = ? AND kind = 'EXIT' "
        "AND state NOT IN ('succeeded','failed','rejected') "
        "ORDER BY created_at_ms DESC LIMIT 1",
        (strategy_instance_id,),
    ).fetchone()
    return EffectOperationResource(**dict(row)) if row is not None else None


def reconcilable_effect_operations(conn: sqlite3.Connection) -> list[EffectOperationResource]:
    """Distinct nonterminal broker-facing operations requiring fresh evidence."""
    rows = conn.execute(
        "SELECT DISTINCT e.effect_operation_id, e.authority_generation, e.idempotency_key, "
        "e.command_id, e.strategy_instance_id, e.run_id, e.kind, e.state, e.custody_owner, "
        "e.created_at_ms, e.updated_at_ms, e.terminal_receipt_id "
        "FROM effect_operations e LEFT JOIN operation_order_links l "
        "ON l.effect_operation_id = e.effect_operation_id LEFT JOIN orders o "
        "ON o.order_ref = l.order_ref WHERE e.kind IN ('ENTER','EXIT','MANUAL_ORDER','CANCEL') "
        "AND e.state NOT IN ('succeeded','failed','rejected') "
        "AND (e.state IN ('accepted','unknown') OR e.kind IN ('EXIT','CANCEL') "
        "OR o.broker_state IS NULL OR lower(o.broker_state) NOT IN "
        "('filled','canceled','expired','rejected','replaced')) "
        "ORDER BY e.created_at_ms ASC"
    ).fetchall()
    return [EffectOperationResource(**dict(row)) for row in rows]


def position(conn: sqlite3.Connection, strategy_instance_id: str, symbol: str) -> float:
    row = conn.execute(
        "SELECT attributed_qty FROM positions WHERE strategy_instance_id = ? AND symbol = ?",
        (strategy_instance_id, symbol.upper()),
    ).fetchone()
    return row["attributed_qty"] if row is not None else 0.0


def fills_for_order(conn: sqlite3.Connection, order_ref: str) -> list[dict]:
    rows = conn.execute(
        "SELECT fill_id, order_ref, qty, price, side, is_correction, execution_id, evidence_source, "
        "event_kind, superseded_execution_ref, fee, fee_fidelity, source_event_at_ms, "
        "clerk_observed_at_ms, recorded_at_ms FROM fills WHERE order_ref = ? "
        "ORDER BY recorded_at_ms ASC",
        (order_ref,),
    ).fetchall()
    return [dict(row) for row in rows]


def execution_exists(conn: sqlite3.Connection, execution_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM fills WHERE execution_id = ? LIMIT 1", (execution_id,)
    ).fetchone()
    return row is not None


def cumulative_recovery_fill_exists_for_order(conn: sqlite3.Connection, order_ref: str) -> bool:
    """Whether aggregate recovery has already covered part of one order.

    Recovery evidence has no execution identity, so a later exact websocket
    slice cannot be safely merged into the same order without proving its
    coverage relationship to the aggregate total.
    """
    row = conn.execute(
        "SELECT 1 FROM fills WHERE order_ref = ? AND evidence_source = 'cumulative_recovery' LIMIT 1",
        (order_ref,),
    ).fetchone()
    return row is not None


def cumulative_recovery_fill_ids_for_order(conn: sqlite3.Connection, order_ref: str) -> list[str]:
    """Return the immutable fold identities covered by aggregate recovery."""
    rows = conn.execute(
        "SELECT fill_id FROM fills WHERE order_ref = ? AND evidence_source = 'cumulative_recovery' "
        "ORDER BY fill_id",
        (order_ref,),
    ).fetchall()
    return [str(row["fill_id"]) for row in rows]


def correction_uncertainty_exists(conn: sqlite3.Connection, execution_id: str) -> bool:
    """Whether an invalid correction already durably raised its uncertainty.

    Invalid corrections intentionally do not create a ``fills`` execution
    row, so their broker execution identity lives in the typed uncertainty
    cause. Parsing the small closed transition subset avoids relying on an
    optional SQLite JSON extension while preserving crash/restart dedup.
    """
    rows = conn.execute(
        "SELECT facts_json FROM custody_transitions WHERE transition_kind = 'UNCERTAINTY_RAISED'"
    ).fetchall()
    for row in rows:
        facts = json.loads(row["facts_json"])
        if facts.get("cause_facts", {}).get("execution_id") == execution_id:
            return True
    return False


def effective_execution_slice(conn: sqlite3.Connection, execution_id: str) -> dict | None:
    """Return one currently effective execution plus its immutable custody owner.

    Corrections are append-only replacement rows. A row is effective only
    while no later row names its execution identity as ``superseded``.
    """
    row = conn.execute(
        "SELECT f.execution_id, f.order_ref, f.qty, f.price, f.side, f.evidence_source, f.fee, "
        "f.fee_fidelity, e.subject_id, e.strategy_instance_id, "
        "COALESCE(s.symbol, json_extract(manual_acceptance.facts_json, '$.leg.symbol')) AS symbol "
        "FROM fills f JOIN orders o ON o.order_ref = f.order_ref "
        "JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id "
        "LEFT JOIN strategy_instances s ON s.strategy_instance_id = e.strategy_instance_id "
        "LEFT JOIN custody_transitions manual_acceptance ON manual_acceptance.sequence = ("
        "SELECT MIN(acceptance.sequence) FROM custody_transitions acceptance "
        "WHERE acceptance.order_ref = o.order_ref "
        "AND acceptance.effect_operation_id = e.effect_operation_id "
        "AND acceptance.transition_kind = 'MANUAL_ORDER_ACCEPTED') "
        "WHERE f.execution_id = ? "
        "AND NOT EXISTS (SELECT 1 FROM fills successor "
        "WHERE successor.superseded_execution_ref = f.execution_id)",
        (execution_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _effective_fill_totals_for_order(
    conn: sqlite3.Connection,
    order_ref: str,
    *,
    evidence_sources: tuple[str, ...] | None = None,
) -> tuple[float, float]:
    """Canonical effective-fill aggregation with an optional evidence filter."""
    source_predicate = ""
    parameters: tuple[object, ...] = (order_ref,)
    if evidence_sources is not None:
        placeholders = ", ".join("?" for _ in evidence_sources)
        source_predicate = f"AND f.evidence_source IN ({placeholders}) "
        parameters += evidence_sources
    row = conn.execute(
        "SELECT COALESCE(SUM(f.qty), 0) AS qty, COALESCE(SUM(f.qty * f.price), 0) AS cost "
        "FROM fills f WHERE f.order_ref = ? "
        + source_predicate
        + "AND NOT EXISTS (SELECT 1 FROM fills successor "
        "WHERE successor.superseded_execution_ref = f.execution_id)",
        parameters,
    ).fetchone()
    return float(row["qty"]), float(row["cost"])


def effective_fill_totals_for_order(conn: sqlite3.Connection, order_ref: str) -> tuple[float, float]:
    """Return the quantity and cost of the order's current effective leaves.

    Formula: ``effective_qty = SUM(qty)`` and ``effective_cost = SUM(qty * price)``
    for fills with no successor naming their ``execution_id`` as superseded.
    Reference: PRD #1441 S1.2 execution corrections.
    Canonical implementation: this query, reused by cumulative recovery.
    Validated against: ``test_cumulative_recovery_fill_is_explicitly_tagged``.
    """
    return _effective_fill_totals_for_order(conn, order_ref)


def effective_exact_fill_totals_for_order(conn: sqlite3.Connection, order_ref: str) -> tuple[float, float]:
    """Return current exact-execution quantity and cost for one order.

    Aggregate REST recovery is deliberately excluded: it can establish
    exposure and an acknowledgement, but only a broker-issued execution ID
    may complete a manual ticket.
    """
    return _effective_fill_totals_for_order(
        conn,
        order_ref,
        evidence_sources=("websocket", "activity_recovery"),
    )


def uncertain_orders(conn: sqlite3.Connection) -> list[OrderResource]:
    """Every order whose effect operation is still ``unknown`` (#1378) — the
    reconciliation sweep's own worklist. Joins rather than filtering
    ``orders`` directly: "uncertain" is a property of the effect operation
    (R4), not of a column on ``orders`` itself."""
    rows = conn.execute(
        "SELECT o.order_ref, o.effect_operation_id, o.client_order_id, o.broker_order_id, "
        "o.role, o.broker_state, o.submitted_at_ms, o.updated_at_ms FROM orders o "
        "JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id "
        "WHERE e.state = 'unknown' ORDER BY o.updated_at_ms ASC"
    ).fetchall()
    return [OrderResource(**dict(row)) for row in rows]


def attributed_positions_by_symbol(conn: sqlite3.Connection) -> dict[str, float]:
    """Account-wide Clerk-attributed exposure per symbol (#1378) — the sum of
    every bot's namespace-attributed ``positions`` row, for comparison
    against the broker's own account-wide position snapshot. Never nets
    against the raw broker position; this is our side of that comparison."""
    rows = conn.execute(
        "SELECT UPPER(symbol) AS symbol, SUM(attributed_qty) AS qty "
        "FROM positions GROUP BY UPPER(symbol)"
    ).fetchall()
    return {row["symbol"]: row["qty"] for row in rows}


def attributed_positions_for_strategy(
    conn: sqlite3.Connection, strategy_instance_id: str
) -> dict[str, float]:
    rows = conn.execute(
        "SELECT UPPER(symbol) AS symbol, SUM(attributed_qty) AS qty FROM positions "
        "WHERE strategy_instance_id = ? GROUP BY UPPER(symbol)",
        (strategy_instance_id,),
    ).fetchall()
    return {row["symbol"]: row["qty"] for row in rows}


def attributed_positions_for_subject(conn: sqlite3.Connection, subject_id: str) -> dict[str, float]:
    """Return one custody subject's position projection without bot aliases."""
    rows = conn.execute(
        "SELECT UPPER(symbol) AS symbol, SUM(attributed_qty) AS qty FROM positions "
        "WHERE subject_id = ? GROUP BY UPPER(symbol)",
        (subject_id,),
    ).fetchall()
    return {row["symbol"]: row["qty"] for row in rows}


def manual_reduction_available_quantity(
    conn: sqlite3.Connection,
    *,
    subject_id: str,
    symbol: str,
) -> float:
    """Return manual-owned long quantity not already reserved for a sell.

    Formula: ``max(0, folded_manual_long - nonterminal_manual_sell_qty)``.
    Reference: docs/prds/2026-08-13-sqlite-clerk-manual-orders.md §8.
    Canonical implementation: this file.
    Validated against: tests/broker/alpaca/clerk/sqlite/test_manual_orders.py::
      test_manual_sell_reserves_only_its_subject_long_position.

    The position projection includes exact fills and corrections.  Each
    accepted, working, or unknown manual sell remains reserved until its own
    effect becomes terminal, so concurrent confirmations cannot oversell the
    custody subject while a broker response is pending.
    """
    normalized_symbol = symbol.upper()
    position = conn.execute(
        "SELECT attributed_qty FROM positions WHERE subject_id = ? AND symbol = ?",
        (subject_id, normalized_symbol),
    ).fetchone()
    long_quantity = max(0.0, float(position["attributed_qty"]) if position is not None else 0.0)
    reservation = conn.execute(
        "SELECT COALESCE(SUM(CAST(json_extract(acceptance.facts_json, '$.leg.quantity') AS REAL)), 0) "
        "AS qty FROM effect_operations effect "
        "JOIN custody_transitions acceptance "
        "ON acceptance.effect_operation_id = effect.effect_operation_id "
        "AND acceptance.transition_kind = 'MANUAL_ORDER_ACCEPTED' "
        "WHERE effect.kind = 'MANUAL_ORDER' AND effect.subject_id = ? "
        "AND effect.state NOT IN ('succeeded', 'failed', 'rejected') "
        "AND UPPER(json_extract(acceptance.facts_json, '$.leg.symbol')) = ? "
        "AND UPPER(json_extract(acceptance.facts_json, '$.leg.side')) = 'SELL'",
        (subject_id, normalized_symbol),
    ).fetchone()
    reserved_quantity = float(reservation["qty"])
    return max(0.0, long_quantity - reserved_quantity)


def has_nonterminal_manual_order(conn: sqlite3.Connection) -> bool:
    """Whether any manual order leaves account-wide new exposure unsafe."""
    row = conn.execute(
        "SELECT 1 FROM effect_operations WHERE kind = 'MANUAL_ORDER' "
        "AND state NOT IN ('succeeded', 'failed', 'rejected') LIMIT 1"
    ).fetchone()
    return row is not None


def active_hold(conn: sqlite3.Connection, *, scope: str, reason_code: str) -> dict | None:
    """The current ``ACTIVE`` hold for this ``(scope, reason_code)``, if any —
    reconciliation's idempotency check before raising a new one (#1378)."""
    row = conn.execute(
        "SELECT hold_id, scope, strategy_instance_id, reason_code, state, opened_at_ms, "
        "resolved_at_ms, evidence_refs_json FROM holds "
        "WHERE scope = ? AND reason_code = ? AND state = 'ACTIVE' "
        "ORDER BY opened_at_ms DESC LIMIT 1",
        (scope, reason_code),
    ).fetchone()
    return dict(row) if row is not None else None


_UNCERTAINTY_COLUMNS = (
    "uncertainty_id, scope, severity, blocks_new_exposure, allows_reduction, custody_owner, "
    "strategy_instance_id, reason_code, headline, explanation, operator_impact, next_step, "
    "observed_at_ms, resolved_at_ms, evidence_refs_json, facts_schema_version, facts_json"
)


def active_uncertainty(
    conn: sqlite3.Connection, *, scope: str, reason_code: str, strategy_instance_id: str | None
) -> dict | None:
    """The current ``ACTIVE`` (``resolved_at_ms IS NULL``) uncertainty for
    this ``(scope, reason_code, strategy_instance_id)``, if any — the
    idempotency check before raising a new one (#1380). ``strategy_instance_id``
    is part of the key (unlike ``active_hold``, which never needs it since
    every hold raised so far is ``ACCOUNT_CLERK``-scoped): two different bots'
    ``CUSTODY_SUBJECT``-scoped uncertainties sharing the same ``reason_code`` must never
    be confused for one another."""
    row = conn.execute(
        f"SELECT {_UNCERTAINTY_COLUMNS} FROM uncertainties "
        "WHERE scope = ? AND reason_code = ? AND strategy_instance_id IS ? "
        "AND resolved_at_ms IS NULL ORDER BY observed_at_ms DESC LIMIT 1",
        (scope, reason_code, strategy_instance_id),
    ).fetchone()
    return dict(row) if row is not None else None


def active_uncertainties_for_admission(
    conn: sqlite3.Connection,
    *,
    strategy_instance_id: str | None = None,
    subject_id: str | None = None,
) -> list[dict]:
    """Return account-wide plus exactly one custody subject's uncertainty."""
    if (strategy_instance_id is None) == (subject_id is None):
        raise ValueError("admission reads require exactly one subject identity")
    rows = conn.execute(
        f"SELECT {_UNCERTAINTY_COLUMNS} FROM uncertainties "
        "WHERE resolved_at_ms IS NULL AND (scope = 'ACCOUNT_CLERK' "
        "OR (strategy_instance_id IS NOT NULL AND strategy_instance_id = ?) "
        "OR (subject_id IS NOT NULL AND subject_id = ?))",
        (strategy_instance_id, subject_id),
    ).fetchall()
    return [dict(row) for row in rows]


def active_holds_for_admission(
    conn: sqlite3.Connection,
    *,
    strategy_instance_id: str | None = None,
    subject_id: str | None = None,
) -> list[dict]:
    """Return account-wide plus exactly one custody subject's active holds."""
    if (strategy_instance_id is None) == (subject_id is None):
        raise ValueError("admission reads require exactly one subject identity")
    rows = conn.execute(
        "SELECT hold_id, scope, subject_id, strategy_instance_id, reason_code, state, opened_at_ms, "
        "resolved_at_ms, evidence_refs_json FROM holds "
        "WHERE state = 'ACTIVE' AND (scope = 'ACCOUNT_CLERK' "
        "OR (strategy_instance_id IS NOT NULL AND strategy_instance_id = ?) "
        "OR (subject_id IS NOT NULL AND subject_id = ?))",
        (strategy_instance_id, subject_id),
    ).fetchall()
    return [dict(row) for row in rows]
