"""Typed read snapshots over the SQLite Clerk spine.

Pure ``SELECT`` + row-to-dataclass mapping, no write path, no lock, no fold
concerns — split out of ``repository.py`` to keep that module under the
file-size ceiling as new read surfaces accumulate slice over slice.
``ClerkSqliteRepository`` methods delegate here; callers outside this package
still never see a cursor (PRD §9.2) — they go through the repository, which
happens to forward to this module for these queries.
"""

from __future__ import annotations

import sqlite3

from app.broker.alpaca.clerk.sqlite.models import (
    CommandResource,
    ControlMetaSnapshot,
    EffectOperationResource,
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
    """The order for a single-order effect operation (ENTER, #1377).

    An EXIT effect operation (#1379) owns more than one ``orders`` row (the
    cancelled entry and the reducing close) — that slice needs a different,
    order-role-aware query; this one is scoped to "exactly one order" callers.
    """
    row = conn.execute(
        "SELECT order_ref, effect_operation_id, client_order_id, broker_order_id, role, "
        "broker_state, submitted_at_ms, updated_at_ms FROM orders "
        "WHERE effect_operation_id = ?",
        (effect_operation_id,),
    ).fetchone()
    return OrderResource(**dict(row)) if row is not None else None


def position(conn: sqlite3.Connection, strategy_instance_id: str, symbol: str) -> float:
    row = conn.execute(
        "SELECT attributed_qty FROM positions WHERE strategy_instance_id = ? AND symbol = ?",
        (strategy_instance_id, symbol),
    ).fetchone()
    return row["attributed_qty"] if row is not None else 0.0


def fills_for_order(conn: sqlite3.Connection, order_ref: str) -> list[dict]:
    rows = conn.execute(
        "SELECT fill_id, order_ref, qty, price, side, is_correction, source_event_at_ms, "
        "clerk_observed_at_ms, recorded_at_ms FROM fills WHERE order_ref = ? "
        "ORDER BY recorded_at_ms ASC",
        (order_ref,),
    ).fetchall()
    return [dict(row) for row in rows]
