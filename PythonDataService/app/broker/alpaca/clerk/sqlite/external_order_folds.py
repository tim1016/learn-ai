"""Replayable SQLite projection folds for foreign broker-order observations."""

from __future__ import annotations

import json
import math
import sqlite3
from typing import Any

from app.broker.alpaca.clerk.sqlite import reads
from app.broker.alpaca.clerk.sqlite.facts import (
    ExternalOrderAcknowledgedFacts,
    ExternalOrderObservedFacts,
)
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize


def _transition_sequence(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT MAX(sequence) AS seq FROM custody_transitions").fetchone()["seq"]


def fold_external_order_observed(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """Materialize foreign broker evidence without ever entering bot economics.

    The same atomic fold both retains the external observation and ensures its
    individual broker identity is present in the account-wide unexplained
    order hold.  A crash therefore cannot leave a newly durable external
    order able to admit fresh bot exposure before a second transition runs.
    """
    facts = ExternalOrderObservedFacts.from_facts_json(payload["facts_json"])
    if (
        not facts.external_order_id
        or not facts.broker_order_id
        or not facts.client_order_id
        or not facts.symbol
    ):
        raise ValueError("external order identity and symbol fields must be non-empty")
    if facts.side not in {"BUY", "SELL"}:
        raise ValueError("external order side must be BUY or SELL")
    if not math.isfinite(facts.qty) or facts.qty < 0:
        raise ValueError("external order quantity must be finite and non-negative")
    if not facts.order_type:
        raise ValueError("external order type must be non-empty")
    for price_name, price in (
        ("limit", facts.limit_price),
        ("stop", facts.stop_price),
        ("filled average", facts.filled_avg_price),
    ):
        if price is not None and not math.isfinite(price):
            raise ValueError(f"external order {price_name} price must be finite when supplied")
    if facts.observed_at_ms < 0:
        raise ValueError("external order observed_at_ms must be non-negative")
    if not facts.evidence_refs or not all(facts.evidence_refs):
        raise ValueError("external order evidence refs must be non-empty strings")
    evidence_refs = sorted(set(facts.evidence_refs))
    conn.execute(
        "INSERT INTO external_orders (external_order_id, broker_order_id, client_order_id, symbol, "
        "side, qty, order_type, limit_price, stop_price, filled_avg_price, observed_at_ms, "
        "acknowledged_at_ms, ack_operator, evidence_refs_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?) "
        "ON CONFLICT(broker_order_id) DO UPDATE SET "
        "client_order_id = excluded.client_order_id, symbol = excluded.symbol, "
        "side = excluded.side, qty = excluded.qty, order_type = excluded.order_type, "
        "limit_price = excluded.limit_price, stop_price = excluded.stop_price, "
        "filled_avg_price = excluded.filled_avg_price, "
        "observed_at_ms = excluded.observed_at_ms, evidence_refs_json = excluded.evidence_refs_json",
        (
            facts.external_order_id,
            facts.broker_order_id,
            facts.client_order_id,
            facts.symbol,
            facts.side,
            facts.qty,
            facts.order_type,
            facts.limit_price,
            facts.stop_price,
            facts.filled_avg_price,
            facts.observed_at_ms,
            canonicalize(evidence_refs),
        ),
    )
    observed = reads.external_order_by_broker_order_id(conn, facts.broker_order_id)
    assert observed is not None
    if observed.acknowledged_at_ms is not None:
        return
    active = reads.active_hold(conn, scope="ACCOUNT_CLERK", reason_code="UNEXPLAINED_ORDER")
    if active is None:
        conn.execute(
            "INSERT INTO holds (hold_id, scope, strategy_instance_id, reason_code, state, "
            "opened_at_ms, resolved_at_ms, evidence_refs_json) "
            "VALUES (?, 'ACCOUNT_CLERK', NULL, 'UNEXPLAINED_ORDER', 'ACTIVE', ?, NULL, ?)",
            (
                f"hold:{_transition_sequence(conn)}",
                payload["recorded_at_ms"],
                canonicalize([facts.broker_order_id]),
            ),
        )
        return
    current_refs = _hold_evidence_refs(active["evidence_refs_json"])
    merged = sorted({*current_refs, facts.broker_order_id})
    if merged != current_refs:
        conn.execute(
            "UPDATE holds SET evidence_refs_json = ? WHERE hold_id = ?",
            (canonicalize(merged), active["hold_id"]),
        )


def fold_external_order_acknowledged(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """Durably record one operator review and remove only its hold cause."""
    facts = ExternalOrderAcknowledgedFacts.from_facts_json(payload["facts_json"])
    if not facts.external_order_id or not facts.ack_operator:
        raise ValueError("external-order acknowledgement requires an order and operator")
    cursor = conn.execute(
        "UPDATE external_orders SET acknowledged_at_ms = ?, ack_operator = ? "
        "WHERE external_order_id = ? AND acknowledged_at_ms IS NULL",
        (payload["recorded_at_ms"], facts.ack_operator, facts.external_order_id),
    )
    if cursor.rowcount == 0:
        return
    acknowledged = reads.external_order(conn, facts.external_order_id)
    assert acknowledged is not None
    active = reads.active_hold(conn, scope="ACCOUNT_CLERK", reason_code="UNEXPLAINED_ORDER")
    if active is None:
        return
    current_refs = _hold_evidence_refs(active["evidence_refs_json"])
    # `external_orders` is retained as audit evidence, not a list of currently
    # active causes. The live hold's evidence is the authoritative scope.
    remaining = sorted(set(current_refs) - {acknowledged.broker_order_id})
    if remaining:
        conn.execute(
            "UPDATE holds SET evidence_refs_json = ? WHERE hold_id = ?",
            (canonicalize(remaining), active["hold_id"]),
        )
        return
    conn.execute(
        "UPDATE holds SET state = 'RESOLVED', resolved_at_ms = ?, evidence_refs_json = ? "
        "WHERE hold_id = ? AND state = 'ACTIVE'",
        (payload["recorded_at_ms"], canonicalize([]), active["hold_id"]),
    )


def _hold_evidence_refs(evidence_refs_json: str) -> list[str]:
    values = json.loads(evidence_refs_json)
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError("account hold evidence_refs_json must be a string list")
    return sorted(set(values))


__all__ = [
    "fold_external_order_acknowledged",
    "fold_external_order_observed",
]
