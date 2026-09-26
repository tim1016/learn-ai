"""Project recovery from the Clerk's last evaluation, never from a promised retry."""

from __future__ import annotations

import sqlite3

from app.broker.alpaca.clerk.sqlite.exit_recovery import RECOVERY_OBSERVATION_MAX_GAP_MS
from app.broker.alpaca.clerk.sqlite.facts import ExitRecoveryEvaluatedFacts
from app.broker.alpaca.clerk.sqlite.projection_models import RecoveryStatus


def read_recovery_status(
    conn: sqlite3.Connection, *, strategy_instance_id: str, uncertainty_id: str,
    now_ms: int, stopped: bool, working: bool,
) -> RecoveryStatus:
    # The immutable episode id names its original raise sequence. Refreshes
    # can change observed_at_ms, but cannot change this starting instant.
    prefix, _, sequence = uncertainty_id.partition(":")
    raised = None if prefix != "uncertainty" or not sequence.isdecimal() else conn.execute(
        "SELECT clerk_observed_at_ms FROM custody_transitions WHERE sequence = ? "
        "AND transition_kind = 'UNCERTAINTY_RAISED' AND strategy_instance_id = ?",
        (int(sequence), strategy_instance_id),
    ).fetchone()
    since = None if raised is None else raised["clerk_observed_at_ms"]
    row = conn.execute(
        "SELECT facts_json FROM custody_transitions WHERE strategy_instance_id = ? "
        "AND transition_kind = 'EXIT_RECOVERY_EVALUATED' ORDER BY sequence DESC LIMIT 1",
        (strategy_instance_id,),
    ).fetchone()
    last_checked = None

    def status(kind, reason_code, explanation, allowed_from_ms=None):
        return RecoveryStatus(kind, reason_code, explanation, last_checked, since, allowed_from_ms)

    unknown = "Recovery status is unknown until the Clerk completes a fresh check."
    if row is None:
        return status("unknown", "RECOVERY_NOT_CHECKED", unknown)
    facts = ExitRecoveryEvaluatedFacts.from_facts_json(row["facts_json"])
    owner = conn.execute("SELECT execution_lease_owner FROM control_meta").fetchone()[0]
    if facts.uncertainty_id != uncertainty_id or facts.lease_owner != owner:
        return status("unknown", "RECOVERY_NOT_CHECKED", unknown)
    last_checked = facts.last_checked_at_ms
    if stopped:
        return status("stuck", "EXIT_STUCK", "Automatic recovery stopped after repeated regular-session failures.")
    if facts.reason_code == "BROKER_SNAPSHOT_STALE":
        return status("broker_unreachable", facts.reason_code, "The Clerk could not reach the broker; recovery is paused.")
    if working:
        return status("working", "OWN_EXIT_WORKING", "An exit is in progress; the Clerk is waiting for its outcome.")
    if last_checked is None or not 0 <= now_ms - last_checked <= RECOVERY_OBSERVATION_MAX_GAP_MS:
        return status("unknown", "RECOVERY_CHECK_STALE", unknown)
    if facts.allowed_from_ms is not None:
        if facts.allowed_from_ms > now_ms:
            return status("allowed_from", facts.reason_code, facts.explanation, facts.allowed_from_ms)
        return status("allowed_now", "RECOVERY_ALLOWED_NOW", "Recovery is allowed now; the Clerk will check before sending.")
    if facts.outcome == "accepted":
        return status("allowed_now", "RECOVERY_ALLOWED_NOW", "The last recovery attempt was accepted.")
    return status("on_hold", facts.reason_code, facts.explanation or "Recovery is on hold pending the next Clerk check.")
