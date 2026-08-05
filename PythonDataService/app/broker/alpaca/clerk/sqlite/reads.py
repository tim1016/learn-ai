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


def orders_for_effect_operation(
    conn: sqlite3.Connection, effect_operation_id: str
) -> list[OrderResource]:
    """Every order this effect operation currently owns (#1379) — 0 or 1 for
    ENTER, and up to 2 for EXIT (the reassigned entry, role ``ENTRY``, and
    the newly-created reducing/close order, role ``REDUCING``). Ordered by
    ``updated_at_ms`` so the entry (reassigned first) sorts before a later-
    created reducing order in the common case."""
    rows = conn.execute(
        "SELECT order_ref, effect_operation_id, client_order_id, broker_order_id, role, "
        "broker_state, submitted_at_ms, updated_at_ms FROM orders "
        "WHERE effect_operation_id = ? ORDER BY updated_at_ms ASC",
        (effect_operation_id,),
    ).fetchall()
    return [OrderResource(**dict(row)) for row in rows]


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
        "SELECT symbol, SUM(attributed_qty) AS qty FROM positions GROUP BY symbol"
    ).fetchall()
    return {row["symbol"]: row["qty"] for row in rows}


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
    "observed_at_ms, resolved_at_ms, evidence_refs_json"
)


def active_uncertainty(
    conn: sqlite3.Connection, *, scope: str, reason_code: str, strategy_instance_id: str | None
) -> dict | None:
    """The current ``ACTIVE`` (``resolved_at_ms IS NULL``) uncertainty for
    this ``(scope, reason_code, strategy_instance_id)``, if any — the
    idempotency check before raising a new one (#1380). ``strategy_instance_id``
    is part of the key (unlike ``active_hold``, which never needs it since
    every hold raised so far is ``ACCOUNT_CLERK``-scoped): two different bots'
    ``BOT``-scoped uncertainties sharing the same ``reason_code`` must never
    be confused for one another."""
    row = conn.execute(
        f"SELECT {_UNCERTAINTY_COLUMNS} FROM uncertainties "
        "WHERE scope = ? AND reason_code = ? AND strategy_instance_id IS ? "
        "AND resolved_at_ms IS NULL ORDER BY observed_at_ms DESC LIMIT 1",
        (scope, reason_code, strategy_instance_id),
    ).fetchone()
    return dict(row) if row is not None else None


def active_uncertainties_for_admission(
    conn: sqlite3.Connection, *, strategy_instance_id: str
) -> list[dict]:
    """Every currently-``ACTIVE`` uncertainty relevant to admission for one
    bot (#1380): every ``ACCOUNT_CLERK``-scoped uncertainty (blocks every
    bot) plus this specific bot's own ``BOT``-scoped ones — never another
    bot's ``BOT``-scoped uncertainty."""
    rows = conn.execute(
        f"SELECT {_UNCERTAINTY_COLUMNS} FROM uncertainties "
        "WHERE resolved_at_ms IS NULL AND (scope = 'ACCOUNT_CLERK' OR strategy_instance_id = ?)",
        (strategy_instance_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def active_holds_for_admission(conn: sqlite3.Connection, *, strategy_instance_id: str) -> list[dict]:
    """Every currently-``ACTIVE`` hold relevant to admission for one bot
    (#1380) — the same account-wide-or-this-bot shape as
    :func:`active_uncertainties_for_admission`, so :func:`admit_new_exposure`
    can fold both mechanisms behind one admission surface."""
    rows = conn.execute(
        "SELECT hold_id, scope, strategy_instance_id, reason_code, state, opened_at_ms, "
        "resolved_at_ms, evidence_refs_json FROM holds "
        "WHERE state = 'ACTIVE' AND (scope = 'ACCOUNT_CLERK' OR strategy_instance_id = ?)",
        (strategy_instance_id,),
    ).fetchall()
    return [dict(row) for row in rows]
