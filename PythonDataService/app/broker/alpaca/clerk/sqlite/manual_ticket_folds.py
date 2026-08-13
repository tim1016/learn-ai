"""Replayable v9 folds for custody subjects and manual-ticket reservations.

These folds create no broker effect.  They only make a trusted manual subject
and its immutable ticket/leg identities durable, so the later submission slice
has a SQLite-owned resource to advance rather than a browser-side draft.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from app.broker.alpaca.clerk.sqlite.facts import (
    CustodySubjectRegisteredFacts,
    ManualTicketReservedFacts,
    ManualTicketStateFacts,
    validate_custody_subject_registered_facts,
    validate_manual_ticket_reserved_facts,
    validate_manual_ticket_state_facts,
)


def _require_manual_outer_identity_is_null(payload: dict[str, Any]) -> None:
    if payload["strategy_instance_id"] is not None or payload["run_id"] is not None:
        raise ValueError("manual custody transition cannot impersonate a strategy instance or run")


def fold_custody_subject_registered(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """Create one immutable non-bot custody subject or prove replay identity."""
    facts = CustodySubjectRegisteredFacts.from_facts_json(payload["facts_json"])
    validate_custody_subject_registered_facts(facts)
    _require_manual_outer_identity_is_null(payload)
    existing = conn.execute(
        "SELECT kind, strategy_instance_id, operator_id FROM custody_subjects WHERE subject_id = ?",
        (facts.subject_id,),
    ).fetchone()
    expected = (facts.kind, facts.strategy_instance_id, facts.operator_id)
    if existing is not None:
        if tuple(existing) != expected:
            raise ValueError("custody subject identity conflicts with prior durable registration")
        return
    conn.execute(
        "INSERT INTO custody_subjects "
        "(subject_id, kind, strategy_instance_id, operator_id, created_at_ms) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            facts.subject_id,
            facts.kind,
            facts.strategy_instance_id,
            facts.operator_id,
            payload["recorded_at_ms"],
        ),
    )


def fold_manual_ticket_reserved(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """Reserve one complete immutable manual ticket without broker eligibility."""
    facts = ManualTicketReservedFacts.from_facts_json(payload["facts_json"])
    validate_manual_ticket_reserved_facts(facts)
    _require_manual_outer_identity_is_null(payload)
    subject = conn.execute(
        "SELECT kind, operator_id FROM custody_subjects WHERE subject_id = ?",
        (facts.subject_id,),
    ).fetchone()
    if subject is None or tuple(subject) != ("MANUAL_OPERATOR", facts.operator_id):
        raise ValueError("manual ticket requires its registered trusted manual operator subject")
    existing = conn.execute(
        "SELECT subject_id, operator_id, instruction_hash FROM manual_order_tickets WHERE ticket_id = ?",
        (facts.ticket_id,),
    ).fetchone()
    expected = (facts.subject_id, facts.operator_id, facts.instruction_hash)
    if existing is not None:
        if tuple(existing) != expected:
            raise ValueError("manual ticket identity conflicts with prior immutable reservation")
        persisted_legs = tuple(
            conn.execute(
                "SELECT leg_id, subject_id, instruction_hash FROM manual_order_legs "
                "WHERE ticket_id = ? ORDER BY leg_id",
                (facts.ticket_id,),
            )
        )
        expected_legs = tuple(
            (leg.leg_id, facts.subject_id, leg.instruction_hash)
            for leg in sorted(facts.legs, key=lambda leg: leg.leg_id)
        )
        if persisted_legs != expected_legs:
            raise ValueError("manual ticket legs conflict with prior immutable reservation")
        return
    conn.execute(
        "INSERT INTO manual_order_tickets "
        "(ticket_id, subject_id, operator_id, instruction_hash, state, created_at_ms, updated_at_ms) "
        "VALUES (?, ?, ?, ?, 'RESERVED', ?, ?)",
        (
            facts.ticket_id,
            facts.subject_id,
            facts.operator_id,
            facts.instruction_hash,
            payload["recorded_at_ms"],
            payload["recorded_at_ms"],
        ),
    )
    conn.executemany(
        "INSERT INTO manual_order_legs "
        "(ticket_id, leg_id, subject_id, instruction_hash, command_id, effect_operation_id, "
        "order_ref, state, created_at_ms, updated_at_ms) "
        "VALUES (?, ?, ?, ?, NULL, NULL, NULL, 'RESERVED', ?, ?)",
        (
            (
                facts.ticket_id,
                leg.leg_id,
                facts.subject_id,
                leg.instruction_hash,
                payload["recorded_at_ms"],
                payload["recorded_at_ms"],
            )
            for leg in facts.legs
        ),
    )


def fold_manual_ticket_state(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """Record a closed manual-ticket state without creating a broker side effect."""
    facts = ManualTicketStateFacts.from_facts_json(payload["facts_json"])
    validate_manual_ticket_state_facts(facts)
    _require_manual_outer_identity_is_null(payload)
    updated = conn.execute(
        "UPDATE manual_order_tickets SET state = ?, updated_at_ms = ? "
        "WHERE ticket_id = ? AND subject_id = ?",
        (facts.state, payload["recorded_at_ms"], facts.ticket_id, facts.subject_id),
    )
    if updated.rowcount != 1:
        raise ValueError("manual ticket state requires its durable ticket and custody subject")
