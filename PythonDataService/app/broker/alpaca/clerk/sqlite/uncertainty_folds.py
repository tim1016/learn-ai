"""Replayable SQLite projection folds for typed uncertainty episodes."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from app.broker.alpaca.clerk.sqlite.custody_subjects import bot_subject_id
from app.broker.alpaca.clerk.sqlite.facts import (
    FACTS_SCHEMA_VERSION,
    UncertaintyRaisedFacts,
    UncertaintyResolvedFacts,
)
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    ORDER_OUTCOME_UNKNOWN_REASON_CODE,
    OrderOutcomeUnknownCause,
    UnknownOrderIdentity,
)

logger = logging.getLogger(__name__)


def _transition_sequence(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT MAX(sequence) AS seq FROM custody_transitions").fetchone()["seq"]


def _unknown_outcome_envelope(*, cause: OrderOutcomeUnknownCause, why: str) -> UncertaintyRaisedFacts:
    return UncertaintyRaisedFacts(
        severity="error",
        blocks_new_exposure=True,
        allows_reduction=False,
        reason_code=ORDER_OUTCOME_UNKNOWN_REASON_CODE,
        headline="A broker order outcome is unknown",
        explanation=why,
        operator_impact=(
            "New exposure and new reducing orders are paused for this custody subject until "
            "the exact broker identities are recovered."
        ),
        next_step="Reconcile now; cancellation and exact-identity lookup remain available.",
        evidence_refs=[identity.order_ref for identity in cause.identities],
        cause_facts=cause.to_mapping(),
    )


def _log_unreadable_active_uncertainty(*, active: sqlite3.Row, subject_id: str, reason: str) -> None:
    logger.warning(
        "active broker-outcome uncertainty has an unreadable facts envelope",
        extra={
            "action": "unknown_outcome_envelope_unreadable",
            "uncertainty_id": active["uncertainty_id"],
            "subject_id": subject_id,
            "facts_schema_version": active["facts_schema_version"],
            "reason": reason,
        },
    )


def open_or_refresh_unknown_outcome(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """Atomically pair an UNKNOWN effect state with its fail-closed episode."""
    effect_operation_id = payload["effect_operation_id"]
    order_ref = payload["order_ref"]
    owner = conn.execute(
        "SELECT subject_id, strategy_instance_id FROM effect_operations WHERE effect_operation_id = ?",
        (effect_operation_id,),
    ).fetchone()
    if owner is None:
        raise ValueError("unknown outcome requires its durable owning effect")
    subject_id = owner["subject_id"]
    strategy_instance_id = owner["strategy_instance_id"]
    active = conn.execute(
        "SELECT uncertainty_id, facts_schema_version, facts_json FROM uncertainties "
        "WHERE scope = 'CUSTODY_SUBJECT' AND subject_id = ? AND reason_code = ? "
        "AND resolved_at_ms IS NULL",
        (subject_id, ORDER_OUTCOME_UNKNOWN_REASON_CODE),
    ).fetchone()
    identity = UnknownOrderIdentity(effect_operation_id=effect_operation_id, order_ref=order_ref)
    if active is None:
        cause = OrderOutcomeUnknownCause(identities=(identity,))
        facts = _unknown_outcome_envelope(cause=cause, why="The broker response was lost or timed out.")
        conn.execute(
            "INSERT INTO uncertainties (uncertainty_id, scope, severity, "
            "blocks_new_exposure, allows_reduction, custody_owner, subject_id, strategy_instance_id, "
            "reason_code, headline, explanation, operator_impact, next_step, observed_at_ms, "
            "resolved_at_ms, evidence_refs_json, facts_schema_version, facts_json) "
            "VALUES (?, 'CUSTODY_SUBJECT', ?, ?, ?, 'ACCOUNT_CLERK', ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
            (
                f"uncertainty:{_transition_sequence(conn)}",
                facts.severity,
                1 if facts.blocks_new_exposure else 0,
                1 if facts.allows_reduction else 0,
                subject_id,
                strategy_instance_id,
                facts.reason_code,
                facts.headline,
                facts.explanation,
                facts.operator_impact,
                facts.next_step,
                payload["recorded_at_ms"],
                canonicalize(facts.evidence_refs),
                FACTS_SCHEMA_VERSION,
                facts.to_facts_json(),
            ),
        )
        return

    # Never bless or replace an envelope from an unknown schema/shape. It
    # stays active and fail-closed until an operator-supported migration.
    if active["facts_schema_version"] != FACTS_SCHEMA_VERSION:
        _log_unreadable_active_uncertainty(
            active=active,
            subject_id=subject_id,
            reason="facts_schema_version_mismatch",
        )
        return
    try:
        prior_facts = UncertaintyRaisedFacts.from_facts_json(active["facts_json"])
        prior_cause = OrderOutcomeUnknownCause.from_mapping(prior_facts.cause_facts)
    except (TypeError, ValueError, KeyError):
        _log_unreadable_active_uncertainty(
            active=active,
            subject_id=subject_id,
            reason="facts_parse_failed",
        )
        return
    cause = OrderOutcomeUnknownCause(
        identities=tuple(
            sorted(
                {*prior_cause.identities, identity},
                key=lambda item: (item.effect_operation_id, item.order_ref),
            )
        )
    )
    facts = _unknown_outcome_envelope(cause=cause, why="One or more broker responses were lost or timed out.")
    conn.execute(
        "UPDATE uncertainties SET observed_at_ms = ?, evidence_refs_json = ?, facts_json = ? "
        "WHERE scope = 'CUSTODY_SUBJECT' AND subject_id = ? AND reason_code = ? "
        "AND resolved_at_ms IS NULL",
        (
            payload["recorded_at_ms"],
            canonicalize(facts.evidence_refs),
            facts.to_facts_json(),
            subject_id,
            ORDER_OUTCOME_UNKNOWN_REASON_CODE,
        ),
    )


@dataclass(frozen=True)
class UnknownEvidenceDisposition:
    exact_identity_proven: bool
    effect_still_unknown: bool
    episode_still_active: bool


def resolve_unknown_outcome_if_proven(conn: sqlite3.Connection, payload: dict[str, Any]) -> UnknownEvidenceDisposition:
    """Fold exact proof and report both effect-local and episode-wide state."""
    owner = conn.execute(
        "SELECT subject_id FROM effect_operations WHERE effect_operation_id = ?",
        (payload["effect_operation_id"],),
    ).fetchone()
    if owner is None:
        raise ValueError("unknown outcome resolution requires its durable owning effect")
    subject_id = owner["subject_id"]
    active = conn.execute(
        "SELECT uncertainty_id, facts_schema_version, facts_json FROM uncertainties "
        "WHERE scope = 'CUSTODY_SUBJECT' AND subject_id = ? AND reason_code = ? "
        "AND resolved_at_ms IS NULL",
        (subject_id, ORDER_OUTCOME_UNKNOWN_REASON_CODE),
    ).fetchone()
    if active is None:
        return UnknownEvidenceDisposition(False, False, False)
    if active["facts_schema_version"] != FACTS_SCHEMA_VERSION:
        _log_unreadable_active_uncertainty(
            active=active,
            subject_id=subject_id,
            reason="facts_schema_version_mismatch",
        )
        return UnknownEvidenceDisposition(False, True, True)
    try:
        facts = UncertaintyRaisedFacts.from_facts_json(active["facts_json"])
        cause = OrderOutcomeUnknownCause.from_mapping(facts.cause_facts)
    except (TypeError, ValueError, KeyError):
        _log_unreadable_active_uncertainty(
            active=active,
            subject_id=subject_id,
            reason="facts_parse_failed",
        )
        return UnknownEvidenceDisposition(False, True, True)
    proven = UnknownOrderIdentity(
        effect_operation_id=payload["effect_operation_id"],
        order_ref=payload["order_ref"],
    )
    if proven not in cause.identities:
        effect_still_unknown = any(
            identity.effect_operation_id == proven.effect_operation_id for identity in cause.identities
        )
        return UnknownEvidenceDisposition(False, effect_still_unknown, True)
    remaining = tuple(identity for identity in cause.identities if identity != proven)
    if remaining:
        remaining_facts = _unknown_outcome_envelope(
            cause=OrderOutcomeUnknownCause(identities=remaining),
            why="One or more broker responses remain unknown.",
        )
        conn.execute(
            "UPDATE uncertainties SET observed_at_ms = ?, evidence_refs_json = ?, "
            "facts_json = ? WHERE uncertainty_id = ? AND resolved_at_ms IS NULL",
            (
                payload["recorded_at_ms"],
                canonicalize(remaining_facts.evidence_refs),
                remaining_facts.to_facts_json(),
                active["uncertainty_id"],
            ),
        )
        effect_still_unknown = any(identity.effect_operation_id == proven.effect_operation_id for identity in remaining)
        return UnknownEvidenceDisposition(True, effect_still_unknown, True)
    conn.execute(
        "UPDATE uncertainties SET resolved_at_ms = ? WHERE uncertainty_id = ? AND resolved_at_ms IS NULL",
        (payload["recorded_at_ms"], active["uncertainty_id"]),
    )
    return UnknownEvidenceDisposition(True, False, False)


def fold_uncertainty_raised(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """Open one database-unique, versioned R5 uncertainty episode."""
    facts = UncertaintyRaisedFacts.from_facts_json(payload["facts_json"])
    scope = "CUSTODY_SUBJECT" if payload["strategy_instance_id"] is not None else "ACCOUNT_CLERK"
    conn.execute(
        "INSERT INTO uncertainties (uncertainty_id, scope, severity, blocks_new_exposure, "
        "allows_reduction, custody_owner, subject_id, strategy_instance_id, reason_code, headline, "
        "explanation, operator_impact, next_step, observed_at_ms, resolved_at_ms, "
        "evidence_refs_json, facts_schema_version, facts_json) "
        "VALUES (?, ?, ?, ?, ?, 'ACCOUNT_CLERK', ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
        (
            f"uncertainty:{_transition_sequence(conn)}",
            scope,
            facts.severity,
            1 if facts.blocks_new_exposure else 0,
            1 if facts.allows_reduction else 0,
            (
                None
                if payload["strategy_instance_id"] is None
                else bot_subject_id(payload["strategy_instance_id"])
            ),
            payload["strategy_instance_id"],
            facts.reason_code,
            facts.headline,
            facts.explanation,
            facts.operator_impact,
            facts.next_step,
            payload["recorded_at_ms"],
            canonicalize(facts.evidence_refs),
            payload["facts_schema_version"],
            payload["facts_json"],
        ),
    )


def fold_uncertainty_refreshed(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    facts = UncertaintyRaisedFacts.from_facts_json(payload["facts_json"])
    scope = "CUSTODY_SUBJECT" if payload["strategy_instance_id"] is not None else "ACCOUNT_CLERK"
    conn.execute(
        "UPDATE uncertainties SET severity = ?, blocks_new_exposure = ?, allows_reduction = ?, "
        "headline = ?, explanation = ?, operator_impact = ?, next_step = ?, observed_at_ms = ?, "
        "evidence_refs_json = ?, facts_schema_version = ?, facts_json = ? "
        "WHERE scope = ? AND reason_code = ? AND strategy_instance_id IS ? "
        "AND resolved_at_ms IS NULL",
        (
            facts.severity,
            1 if facts.blocks_new_exposure else 0,
            1 if facts.allows_reduction else 0,
            facts.headline,
            facts.explanation,
            facts.operator_impact,
            facts.next_step,
            payload["recorded_at_ms"],
            canonicalize(facts.evidence_refs),
            payload["facts_schema_version"],
            payload["facts_json"],
            scope,
            facts.reason_code,
            payload["strategy_instance_id"],
        ),
    )


def fold_uncertainty_resolved(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """Close one active episode; an already-resolved row is a no-op."""
    facts = UncertaintyResolvedFacts.from_facts_json(payload["facts_json"])
    conn.execute(
        "UPDATE uncertainties SET resolved_at_ms = ? WHERE uncertainty_id = ? AND resolved_at_ms IS NULL",
        (payload["recorded_at_ms"], facts.uncertainty_id),
    )


__all__ = [
    "fold_uncertainty_raised",
    "fold_uncertainty_refreshed",
    "fold_uncertainty_resolved",
    "open_or_refresh_unknown_outcome",
    "resolve_unknown_outcome_if_proven",
]
