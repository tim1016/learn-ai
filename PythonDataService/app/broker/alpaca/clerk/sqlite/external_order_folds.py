"""Replayable SQLite projection folds for foreign broker-order observations."""

from __future__ import annotations

import json
import math
import sqlite3
from typing import Any

from app.broker.alpaca.clerk.sqlite import reads
from app.broker.alpaca.clerk.sqlite.facts import (
    FACTS_SCHEMA_VERSION,
    ExternalOrderAcknowledgedFacts,
    ExternalOrderObservedFacts,
)
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    UNEXPLAINED_ORDER_HOLD_REASON_CODE,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_folds import account_hold_envelope


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
    active = reads.active_hold(
        conn, scope="ACCOUNT_CLERK", reason_code=UNEXPLAINED_ORDER_HOLD_REASON_CODE
    )
    if active is None:
        _insert_unexplained_order_hold(
            conn,
            evidence_refs=[facts.broker_order_id],
            recorded_at_ms=payload["recorded_at_ms"],
        )
        return
    current_refs = _hold_evidence_refs(active["evidence_refs_json"])
    merged = sorted({*current_refs, facts.broker_order_id})
    if merged != current_refs:
        _update_unexplained_order_hold(conn, hold_id=active["hold_id"], evidence_refs=merged)


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
    active = reads.active_hold(
        conn, scope="ACCOUNT_CLERK", reason_code=UNEXPLAINED_ORDER_HOLD_REASON_CODE
    )
    if active is None:
        return
    current_refs = _hold_evidence_refs(active["evidence_refs_json"])
    # `external_orders` is retained as audit evidence, not a list of currently
    # active causes. The live hold's evidence is the authoritative scope.
    remaining = sorted(set(current_refs) - {acknowledged.broker_order_id})
    if remaining:
        _update_unexplained_order_hold(conn, hold_id=active["hold_id"], evidence_refs=remaining)
        return
    _resolve_unexplained_order_hold(
        conn, hold_id=active["hold_id"], recorded_at_ms=payload["recorded_at_ms"]
    )


# ── The unexplained-order hold, as an uncertainty episode (ADR 0048 D2) ──────
# These folds maintain the hold *inside the same atomic transition* that makes
# a foreign order durable, which is why they write the row directly rather
# than going through ``uncertainty.raise_account_hold``: that path appends its
# own transition, and a fold may not append while it is being folded. The
# envelope still comes from ``account_hold_envelope``, so this hold reads
# identically to one the reconciliation sweep raises.


def _insert_unexplained_order_hold(
    conn: sqlite3.Connection, *, evidence_refs: list[str], recorded_at_ms: int
) -> None:
    facts = account_hold_envelope(
        reason_code=UNEXPLAINED_ORDER_HOLD_REASON_CODE, evidence_refs=evidence_refs
    )
    conn.execute(
        "INSERT INTO uncertainties (uncertainty_id, scope, severity, blocks_new_exposure, "
        "allows_reduction, custody_owner, subject_id, strategy_instance_id, reason_code, "
        "headline, explanation, operator_impact, next_step, observed_at_ms, resolved_at_ms, "
        "evidence_refs_json, facts_schema_version, facts_json) "
        "VALUES (?, 'ACCOUNT_CLERK', ?, 1, 0, 'ACCOUNT_CLERK', NULL, NULL, ?, ?, ?, ?, ?, ?, "
        "NULL, ?, ?, ?)",
        (
            f"hold:{_transition_sequence(conn)}",
            facts.severity,
            facts.reason_code,
            facts.headline,
            facts.explanation,
            facts.operator_impact,
            facts.next_step,
            recorded_at_ms,
            canonicalize(facts.evidence_refs),
            FACTS_SCHEMA_VERSION,
            facts.to_facts_json(),
        ),
    )


def _update_unexplained_order_hold(
    conn: sqlite3.Connection, *, hold_id: str, evidence_refs: list[str]
) -> None:
    """Re-state the episode's evidence without moving ``observed_at_ms``.

    The set of unreviewed orders changed; when the hold was first observed did
    not. Advancing the observation stamp here would make an unchanged outage
    look freshly detected on every acknowledgement.
    """
    facts = account_hold_envelope(
        reason_code=UNEXPLAINED_ORDER_HOLD_REASON_CODE, evidence_refs=evidence_refs
    )
    conn.execute(
        "UPDATE uncertainties SET evidence_refs_json = ?, facts_json = ? "
        "WHERE uncertainty_id = ?",
        (canonicalize(facts.evidence_refs), facts.to_facts_json(), hold_id),
    )


def _resolve_unexplained_order_hold(
    conn: sqlite3.Connection, *, hold_id: str, recorded_at_ms: int
) -> None:
    facts = account_hold_envelope(
        reason_code=UNEXPLAINED_ORDER_HOLD_REASON_CODE, evidence_refs=[]
    )
    conn.execute(
        "UPDATE uncertainties SET resolved_at_ms = ?, evidence_refs_json = ?, facts_json = ? "
        "WHERE uncertainty_id = ? AND resolved_at_ms IS NULL",
        (recorded_at_ms, canonicalize(facts.evidence_refs), facts.to_facts_json(), hold_id),
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
