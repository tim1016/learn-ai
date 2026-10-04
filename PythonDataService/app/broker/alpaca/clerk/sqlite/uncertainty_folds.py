"""Replayable SQLite projection folds for typed uncertainty episodes."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.broker.alpaca.clerk.money import recorded_dollars
from app.broker.alpaca.clerk.sqlite.custody_subjects import bot_subject_id
from app.broker.alpaca.clerk.sqlite.facts import (
    FACTS_SCHEMA_VERSION,
    OrderSubmitUncertainFacts,
    UncertaintyRaisedFacts,
    UncertaintyResolvedFacts,
    UnknownOutcomeNotice,
)
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
    ORDER_OUTCOME_UNKNOWN_REASON_CODE,
    STREAM_HEALTH_HOLD_REASON_CODE,
    UNEXPLAINED_ORDER_HOLD_REASON_CODE,
    LossHoldCause,
    OrderOutcomeUnknownCause,
    StreamHealthHoldCause,
    UnexplainedOrderCause,
    UnknownOrderIdentity,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_policies import reason_policy

logger = logging.getLogger(__name__)

LOST_ANSWER_NOTICE = UnknownOutcomeNotice(
    explanation="The broker response was lost or timed out.",
    next_step="Reconcile now; cancellation and exact-identity lookup remain available.",
)
"""What the unknown-outcome episode says when it opens on a fold that records no notice: an answer was lost.

A fold that records none leaves an open episode's words as they are, so one
that must put them back to these after an earlier notice records this as its
own (#2854).
"""


def _transition_sequence(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT MAX(sequence) AS seq FROM custody_transitions").fetchone()["seq"]


def _unknown_outcome_envelope(
    *, cause: OrderOutcomeUnknownCause, why: str, notice: UnknownOutcomeNotice | None = None
) -> UncertaintyRaisedFacts:
    """The unknown-outcome episode's envelope.

    ``why`` and the lost-answer next step are what it says unless the fold
    being applied recorded a ``notice`` of its own (#2854): the broker did
    answer, so "lost or timed out" would be false.
    """
    return UncertaintyRaisedFacts(
        severity="error",
        blocks_new_exposure=True,
        allows_reduction=False,
        reason_code=ORDER_OUTCOME_UNKNOWN_REASON_CODE,
        headline="A broker order outcome is unknown",
        explanation=why if notice is None else notice.explanation,
        operator_impact=(
            "New exposure and new reducing orders are on hold for the bot or ticket that "
            "placed this order until its exact broker identities are recovered."
        ),
        next_step=LOST_ANSWER_NOTICE.next_step if notice is None else notice.next_step,
        evidence_refs=[identity.order_ref for identity in cause.identities],
        cause_facts=cause.to_mapping(),
    )


def account_hold_envelope(
    *,
    reason_code: str,
    evidence_refs: list[str],
    cause_facts: Mapping[str, Any] | None = None,
) -> UncertaintyRaisedFacts:
    """The R5 envelope for one account-hold cause (ADR 0048 Decision 2).

    A hold carried no envelope — only a reason code and evidence refs — so
    the copy that describes it has to be authored somewhere, and this is the
    one place. The wording deliberately matches the panel's closed operator
    vocabulary for the same codes; the panel keeps rendering its own copy, so
    the two are peers, not a chain, and neither layer imports the other.

    The two v12 causes derive their whole cause payload from
    ``evidence_refs``, and passing ``cause_facts`` for either is refused
    rather than ignored — a caller that believed it had stamped a cause which
    was never stored is the failure this module exists to prevent. The ADR
    0059 D4 loss hold is the opposite case: the breach it was raised on is
    numbers, not evidence lines, so its caller supplies ``cause_facts`` and
    this function refuses to describe the hold without them.

    ``blocks_new_exposure`` and ``allows_reduction`` are read from the
    registered policy, which is the authority on what an episode permits
    (ADR 0048 Decision 1). They are not this envelope's to invent: the one
    caller that stores them verbatim, :func:`insert_account_hold_episode`,
    would otherwise be free to write a row contradicting the policy the
    admission path reads.

    Raises ``KeyError`` for any other reason code: a hold cause reaching here
    unregistered would otherwise be described as a generic uncertainty and
    lose the account-wide fence it exists to hold.
    """
    if cause_facts is not None and reason_code in (
        UNEXPLAINED_ORDER_HOLD_REASON_CODE,
        STREAM_HEALTH_HOLD_REASON_CODE,
    ):
        raise ValueError(
            f"{reason_code!r} derives its whole cause from its evidence lines; "
            "cause_facts would be silently dropped"
        )
    cause: dict[str, Any]
    if reason_code == UNEXPLAINED_ORDER_HOLD_REASON_CODE:
        cause = UnexplainedOrderCause(broker_order_ids=tuple(evidence_refs)).to_mapping()
        headline = "An order this account did not submit is unreviewed"
        explanation = (
            "One or more broker orders were seen that this account's records cannot "
            "explain. Until each is reviewed, the Clerk cannot prove that new exposure "
            "would be attributable."
        )
        operator_impact = "The account is on hold: new submits are refused until each order is reviewed."
        next_step = "Review each unexplained order, then acknowledge it to release the hold."
    elif reason_code == STREAM_HEALTH_HOLD_REASON_CODE:
        cause = StreamHealthHoldCause(channels=tuple(evidence_refs)).to_mapping()
        headline = "A Clerk channel is unhealthy"
        explanation = (
            "A market-data or execution channel this account depends on is not "
            "delivering. The Clerk cannot prove it would see the outcome of a new "
            "order, so it does not authorize one."
        )
        operator_impact = "The account is on hold: new submits are refused until the channel recovers."
        next_step = "Restore the named channel; the hold releases on its own once it recovers."
    elif reason_code == LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE:
        if cause_facts is None:
            raise ValueError("the loss hold needs cause_facts: the breach it was raised on")
        loss = LossHoldCause.from_mapping(dict(cause_facts))
        cause = loss.to_mapping()
        headline = "The account is in loss hold"
        # Each sealed float is normalized on its own through the money
        # boundary, so the owner reads the cents the record carries (#2612).
        # The sealed ``LossHoldCause`` bytes above are untouched: only this
        # explanation's rendering changed, and it is written once, at the
        # raise — rows already in the chain keep their recorded text.
        explanation = (
            "Account day P&L — current equity minus prior regular-session-close "
            "equity, net of deposits and withdrawals after that close — reached "
            f"{recorded_dollars(loss.day_pnl_usd)} USD against a loss limit of "
            f"{recorded_dollars(loss.loss_limit_usd)} USD. Every ENTER on the account is refused; "
            "every EXIT still runs, so each program keeps managing its own position."
        )
        operator_impact = "New entries are held account-wide; exits are unaffected."
        next_step = (
            "In Settings, use Clear hold after recovery. In a new session, "
            "the same action first proves previous obligations resolved and both loss "
            "limits safe. Session rollover never clears a hold automatically."
        )
    else:
        raise KeyError(f"{reason_code!r} is not a registered account-hold cause")
    policy = reason_policy(reason_code)
    if policy is None:
        # Only registered codes reach here today (the branches above name
        # three), but the two facts below are the policy's to state, not this
        # envelope's to invent -- so an unregistered one refuses rather than
        # being described with defaults.
        raise KeyError(f"{reason_code!r} has no registered reason policy")
    return UncertaintyRaisedFacts(
        severity="error",
        blocks_new_exposure=policy.blocks_new_exposure,
        allows_reduction=policy.allows_reduction,
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
    """Atomically pair an UNKNOWN effect state with its fail-closed episode.

    The episode says a broker response was lost or timed out, unless the
    fold being applied records a notice of its own
    (``OrderSubmitUncertainFacts.notice``, #2854). That notice becomes what
    the open episode tells the operator -- its explanation and next step --
    whether this fold opens the episode or refreshes it. A fold that records
    none leaves an open episode's words as they are: each pass that finds the
    order still absent refreshes the episode, and what the broker last
    answered stays true until another answer or the order's proof replaces
    it. When another order's proof takes that order out of the episode, the
    words are read back from the orders that remain
    (:func:`resolve_unknown_outcome_if_proven`).
    """
    notice = OrderSubmitUncertainFacts.from_facts_json(payload["facts_json"]).notice
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
        facts = _unknown_outcome_envelope(
            cause=cause, why=LOST_ANSWER_NOTICE.explanation, notice=notice
        )
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
    facts = _unknown_outcome_envelope(
        cause=cause, why="One or more broker responses were lost or timed out.", notice=notice
    )
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
    if notice is not None:
        conn.execute(
            "UPDATE uncertainties SET explanation = ?, next_step = ? WHERE uncertainty_id = ? "
            "AND resolved_at_ms IS NULL",
            (facts.explanation, facts.next_step, active["uncertainty_id"]),
        )


def _newest_standing_notice(
    conn: sqlite3.Connection, identities: tuple[UnknownOrderIdentity, ...]
) -> UnknownOutcomeNotice | None:
    """The newest notice recorded for any of these still-unknown orders, or ``None`` when none has one (#2854).

    A notice is recorded once, on the row of the broker's answer; the passes
    that then find the order still unknown record none. So when another
    order leaves the episode, what is still true of these is read back from
    their own rows: an order held because the broker answered under another
    client order id is still told as held.

    A notice describes a send the broker never acknowledged. Once the broker
    has acknowledged the order it no longer does, even if the order's outcome
    is unknown again, so an acknowledged order's notices are not read.

    Each read walks one order's index from its newest row back to its newest
    notice. Only rows already in the ledger are read, in sequence order, so a
    store replayed from its mirror reads the same notice.
    """
    newest: sqlite3.Row | None = None
    for identity in identities:
        row = conn.execute(
            "SELECT t.sequence, t.facts_json FROM custody_transitions t "
            "JOIN orders o ON o.order_ref = t.order_ref "
            "WHERE t.order_ref = ? AND t.effect_operation_id = ? "
            "AND t.transition_kind = 'ORDER_SUBMIT_UNCERTAIN' "
            "AND json_extract(t.facts_json, '$.notice') IS NOT NULL "
            "AND o.broker_order_id IS NULL "
            "ORDER BY t.sequence DESC LIMIT 1",
            (identity.order_ref, identity.effect_operation_id),
        ).fetchone()
        if row is not None and (newest is None or row["sequence"] > newest["sequence"]):
            newest = row
    if newest is None:
        return None
    return OrderSubmitUncertainFacts.from_facts_json(newest["facts_json"]).notice


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
        # The episode's words are written again (#2854): the notice they came
        # from may be about the order just proven. They are the newest notice
        # recorded for an order that is still unknown, or this envelope's own
        # when none of those orders has one.
        remaining_facts = _unknown_outcome_envelope(
            cause=OrderOutcomeUnknownCause(identities=remaining),
            why="One or more broker responses remain unknown.",
            notice=_newest_standing_notice(conn, remaining),
        )
        conn.execute(
            "UPDATE uncertainties SET observed_at_ms = ?, evidence_refs_json = ?, "
            "facts_json = ?, explanation = ?, next_step = ? "
            "WHERE uncertainty_id = ? AND resolved_at_ms IS NULL",
            (
                payload["recorded_at_ms"],
                canonicalize(remaining_facts.evidence_refs),
                remaining_facts.to_facts_json(),
                remaining_facts.explanation,
                remaining_facts.next_step,
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


def fold_uncertainty_raised(
    conn: sqlite3.Connection, payload: dict[str, Any], *, beside_another: bool = False
) -> None:
    """Open one database-unique, versioned R5 uncertainty episode.

    The episode is named for the transition being folded,
    ``uncertainty:<sequence>``. One transition names one episode so; the
    second episode of a fold that opens two (``beside_another``, #2854) is
    named for its reason too, ``uncertainty:<sequence>:<reason code>``.
    """
    facts = UncertaintyRaisedFacts.from_facts_json(payload["facts_json"])
    scope, subject_id, strategy_instance_id = _uncertainty_custody(conn, payload)
    uncertainty_id = f"uncertainty:{_transition_sequence(conn)}"
    if beside_another:
        uncertainty_id = f"{uncertainty_id}:{facts.reason_code}"
    conn.execute(
        "INSERT INTO uncertainties (uncertainty_id, scope, severity, blocks_new_exposure, "
        "allows_reduction, custody_owner, subject_id, strategy_instance_id, reason_code, headline, "
        "explanation, operator_impact, next_step, observed_at_ms, resolved_at_ms, "
        "evidence_refs_json, facts_schema_version, facts_json) "
        "VALUES (?, ?, ?, ?, ?, 'ACCOUNT_CLERK', ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
        (
            uncertainty_id,
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


def open_or_refresh_episode(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    facts: UncertaintyRaisedFacts,
    *,
    beside_another: bool = False,
) -> None:
    """Open the episode ``facts`` describes inside another transition's own fold, or refresh the open one (#2854).

    For a transition that ends an operation and must tell the operator so in
    the same write: the notice cannot then be missing from a ledger that
    holds the ending. The episode is opened and refreshed exactly as
    ``UNCERTAINTY_RAISED`` and ``UNCERTAINTY_REFRESHED`` do it, with ``facts``
    as its envelope; one opened here takes its id from the transition being
    folded, as every episode does. A fold that opens two episodes says so
    for its second (``beside_another``), which is named for its reason as
    well (:func:`fold_uncertainty_raised`).
    """
    envelope = {
        **payload,
        "facts_schema_version": FACTS_SCHEMA_VERSION,
        "facts_json": facts.to_facts_json(),
    }
    scope, subject_id, _strategy_instance_id = _uncertainty_custody(conn, envelope)
    active = conn.execute(
        "SELECT 1 FROM uncertainties WHERE scope = ? AND reason_code = ? AND subject_id IS ? "
        "AND resolved_at_ms IS NULL",
        (scope, facts.reason_code, subject_id),
    ).fetchone()
    if active is None:
        fold_uncertainty_raised(conn, envelope, beside_another=beside_another)
    else:
        fold_uncertainty_refreshed(conn, envelope)


def fold_uncertainty_resolved(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """Close one active episode; an already-resolved row is a no-op."""
    facts = UncertaintyResolvedFacts.from_facts_json(payload["facts_json"])
    conn.execute(
        "UPDATE uncertainties SET resolved_at_ms = ? WHERE uncertainty_id = ? AND resolved_at_ms IS NULL",
        (payload["recorded_at_ms"], facts.uncertainty_id),
    )


__all__ = [
    "LOST_ANSWER_NOTICE",
    "fold_uncertainty_raised",
    "fold_uncertainty_refreshed",
    "fold_uncertainty_resolved",
    "open_or_refresh_episode",
    "open_or_refresh_unknown_outcome",
    "resolve_unknown_outcome_if_proven",
]
