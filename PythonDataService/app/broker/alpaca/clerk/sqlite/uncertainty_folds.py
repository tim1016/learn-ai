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
    STREAM_HEALTH_HOLD_REASON_CODE,
    UNEXPLAINED_ORDER_HOLD_REASON_CODE,
    OrderOutcomeUnknownCause,
    StreamHealthHoldCause,
    UnexplainedOrderCause,
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


def account_hold_envelope(
    *, reason_code: str, evidence_refs: list[str]
) -> UncertaintyRaisedFacts:
    """The R5 envelope for one former ``holds`` cause (ADR 0048 Decision 2).

    A hold carried no envelope — only a reason code and evidence refs — so
    the copy that describes it has to be authored somewhere, and this is the
    one place. The wording deliberately matches the panel's closed operator
    vocabulary for the same two codes; the panel keeps rendering its own
    copy, so the two are peers, not a chain, and neither layer imports the
    other. Every hold blocks entries account-wide and authorizes no
    reduction, which is exactly what the registered policy declares.

    Raises ``KeyError`` for any other reason code: a hold cause reaching here
    unregistered would otherwise be described as a generic uncertainty and
    lose the account-wide fence it exists to hold.
    """
    cause: dict[str, Any]
    if reason_code == UNEXPLAINED_ORDER_HOLD_REASON_CODE:
        cause = UnexplainedOrderCause(broker_order_ids=tuple(evidence_refs)).to_mapping()
        headline = "An order this account did not submit is unreviewed"
        explanation = (
            "One or more broker orders were seen that this account's journal cannot "
            "explain. Until each is reviewed, the Clerk cannot prove that new exposure "
            "would be attributable."
        )
        operator_impact = "New submits are paused account-wide."
        next_step = "Review each unexplained order, then acknowledge it to release the hold."
    elif reason_code == STREAM_HEALTH_HOLD_REASON_CODE:
        cause = StreamHealthHoldCause(channels=tuple(evidence_refs)).to_mapping()
        headline = "A Clerk channel is unhealthy"
        explanation = (
            "A market-data or execution channel this account depends on is not "
            "delivering. The Clerk cannot prove it would see the outcome of a new "
            "order, so it does not authorize one."
        )
        operator_impact = "New submits are paused account-wide."
        next_step = "Restore the named channel; the hold releases on its own once it recovers."
    else:
        raise KeyError(f"{reason_code!r} is not a registered account-hold cause")
    return UncertaintyRaisedFacts(
        severity="error",
        blocks_new_exposure=True,
        allows_reduction=False,
        reason_code=reason_code,
        headline=headline,
        explanation=explanation,
        operator_impact=operator_impact,
        next_step=next_step,
        evidence_refs=sorted(evidence_refs),
        cause_facts=cause,
    )


ACCOUNT_HOLD_EPISODE_INSERT = (
    "INSERT INTO uncertainties (uncertainty_id, scope, severity, blocks_new_exposure, "
    "allows_reduction, custody_owner, subject_id, strategy_instance_id, reason_code, "
    "headline, explanation, operator_impact, next_step, observed_at_ms, resolved_at_ms, "
    "evidence_refs_json, facts_schema_version, facts_json) "
    "VALUES (?, 'ACCOUNT_CLERK', ?, ?, ?, 'ACCOUNT_CLERK', NULL, NULL, ?, ?, ?, ?, ?, ?, ?, "
    "?, ?, ?)"
)


def insert_account_hold_episode(
    conn: sqlite3.Connection,
    *,
    uncertainty_id: str,
    reason_code: str,
    evidence_refs: list[str],
    observed_at_ms: int,
    resolved_at_ms: int | None,
) -> None:
    """Write one account-hold episode row (ADR 0048 Decision 2).

    The single INSERT behind all three ways an episode reaches
    ``uncertainties``: opened inside the fold of a foreign order, replayed
    from a pre-v12 mirror, or carried across by the v11-to-v12 backfill.
    Those three differ only in identity and stamps, so the column list —
    which has to stay in lockstep with the schema — is written once rather
    than three times, where a later column could be added to two of them.

    A duplicate ``uncertainty_id`` raises rather than being absorbed. Every
    caller either mints an id from the transition sequence or carries one
    that was already unique, so a collision is a bug, and the unique
    constraint names the offending row at the statement that caused it.
    """
    facts = account_hold_envelope(reason_code=reason_code, evidence_refs=evidence_refs)
    conn.execute(
        ACCOUNT_HOLD_EPISODE_INSERT,
        (
            uncertainty_id,
            facts.severity,
            1 if facts.blocks_new_exposure else 0,
            1 if facts.allows_reduction else 0,
            facts.reason_code,
            facts.headline,
            facts.explanation,
            facts.operator_impact,
            facts.next_step,
            observed_at_ms,
            resolved_at_ms,
            canonicalize(facts.evidence_refs),
            FACTS_SCHEMA_VERSION,
            facts.to_facts_json(),
        ),
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


def _uncertainty_custody(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
) -> tuple[str, str | None, str | None]:
    """Resolve every effect-bound uncertainty through its durable owner."""
    effect_operation_id = payload.get("effect_operation_id")
    if effect_operation_id is not None:
        owner = conn.execute(
            "SELECT subject_id, strategy_instance_id FROM effect_operations "
            "WHERE effect_operation_id = ?",
            (effect_operation_id,),
        ).fetchone()
        if owner is None:
            raise ValueError("effect-bound uncertainty requires its durable owning effect")
        return "CUSTODY_SUBJECT", owner["subject_id"], owner["strategy_instance_id"]
    strategy_instance_id = payload["strategy_instance_id"]
    if strategy_instance_id is not None:
        return "CUSTODY_SUBJECT", bot_subject_id(strategy_instance_id), strategy_instance_id
    return "ACCOUNT_CLERK", None, None


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
    scope, subject_id, strategy_instance_id = _uncertainty_custody(conn, payload)
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
            subject_id,
            strategy_instance_id,
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
    scope, subject_id, _strategy_instance_id = _uncertainty_custody(conn, payload)
    conn.execute(
        "UPDATE uncertainties SET severity = ?, blocks_new_exposure = ?, allows_reduction = ?, "
        "headline = ?, explanation = ?, operator_impact = ?, next_step = ?, observed_at_ms = ?, "
        "evidence_refs_json = ?, facts_schema_version = ?, facts_json = ? "
        "WHERE scope = ? AND reason_code = ? AND subject_id IS ? "
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
            subject_id,
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
