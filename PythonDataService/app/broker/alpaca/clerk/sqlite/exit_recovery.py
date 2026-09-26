"""Explicit recovery outcomes and durable failure-budget accounting.

Decision changes belong in the custody journal. Successful-pass freshness is
replaceable operational evidence, so a weekend hold does not grow that journal.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal

from app.broker.alpaca.clerk.sqlite.facts import ExitRecoveryEvaluatedFacts
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import EXIT_NOT_FLAT_REASON_CODE
from app.services.session_authority import session_state_at_ms

DEFAULT_RECOVERY_INTERVAL_MS = 15_000


@dataclass(frozen=True)
class RecoveryResult:
    """A decided outcome; persistence never changes failure into hold."""

    outcome: Literal["failure", "hold", "accepted"]
    reason_code: str
    explanation: str
    allowed_from_ms: int | None = None


def latest_exit_recovery(
    repo: ClerkSqliteRepository, *, strategy_instance_id: str, uncertainty_id: str,
) -> ExitRecoveryEvaluatedFacts | None:
    checkpoint = repo.recovery_check(strategy_instance_id)
    row = repo.last_strategy_transition(
        strategy_instance_id=strategy_instance_id, transition_kind="EXIT_RECOVERY_EVALUATED",
    )
    if row is None:
        return None
    facts = ExitRecoveryEvaluatedFacts.from_facts_json(row["facts_json"])
    if facts.uncertainty_id != uncertainty_id:
        return None
    return recovery_with_freshness(facts, checkpoint)


def recovery_with_freshness(
    facts: ExitRecoveryEvaluatedFacts, checkpoint: Mapping[str, Any] | None,
) -> ExitRecoveryEvaluatedFacts:
    """Use same-episode pass freshness without replacing the journal's decision."""
    if checkpoint is not None and checkpoint["uncertainty_id"] == facts.uncertainty_id and (
        facts.last_checked_at_ms is None or checkpoint["completed_at_ms"] >= facts.last_checked_at_ms
    ):
        return replace(facts, last_checked_at_ms=checkpoint["last_checked_at_ms"], lease_owner=checkpoint["lease_owner"])
    return facts



def record_exit_recovery(
    repo: ClerkSqliteRepository, *, strategy_instance_id: str, uncertainty_id: str,
    result: RecoveryResult, evaluated_at_ms: int, pass_started_at_ms: int,
    interval_ms: int = DEFAULT_RECOVERY_INTERVAL_MS,
) -> ExitRecoveryEvaluatedFacts | None:
    """Commit one result only after the caller proves its complete pass succeeded."""
    episode = repo.active_uncertainty(
        scope="CUSTODY_SUBJECT", reason_code=EXIT_NOT_FLAT_REASON_CODE,
        strategy_instance_id=strategy_instance_id,
    )
    if episode is None or episode["uncertainty_id"] != uncertainty_id:
        return None
    previous = latest_exit_recovery(
        repo, strategy_instance_id=strategy_instance_id, uncertainty_id=uncertainty_id,
    )
    checkpoint = repo.recovery_check(strategy_instance_id)
    elapsed = 0 if previous is None else previous.failure_elapsed_ms
    first_failure = None if previous is None else previous.first_failure_at_ms
    if result.outcome == "accepted":
        elapsed, first_failure = 0, None
    elif result.outcome == "failure":
        if first_failure is None:
            first_failure = evaluated_at_ms
        if (
            previous is not None and previous.outcome == "failure"
            and previous.lease_owner == repo.lease_owner and previous.last_checked_at_ms is not None
            and checkpoint is not None
            and 0 <= pass_started_at_ms - checkpoint["completed_at_ms"] <= 2 * interval_ms
        ):
            prior_session = session_state_at_ms(now_ms=previous.last_checked_at_ms)
            session = session_state_at_ms(now_ms=evaluated_at_ms)
            if prior_session.phase == session.phase == "RTH" and prior_session.next_transition_ms == session.next_transition_ms:
                # The real cadence plus this observed pass bounds continuity.
                # A slow successful pass progresses; an idle/downtime gap does not.
                elapsed += min(
                    max(0, evaluated_at_ms - previous.last_checked_at_ms),
                    2 * interval_ms + max(0, evaluated_at_ms - pass_started_at_ms),
                )
    facts = ExitRecoveryEvaluatedFacts(
        uncertainty_id=uncertainty_id, outcome=result.outcome, reason_code=result.reason_code,
        last_checked_at_ms=evaluated_at_ms, first_failure_at_ms=first_failure,
        failure_elapsed_ms=elapsed, lease_owner=repo.lease_owner,
        allowed_from_ms=result.allowed_from_ms, explanation=result.explanation,
    )
    _append_changed_decision(repo, strategy_instance_id, facts, previous)
    repo.record_recovery_check(
        strategy_instance_id=strategy_instance_id, uncertainty_id=uncertainty_id, last_checked_at_ms=facts.last_checked_at_ms,
        completed_at_ms=repo.clock(), interval_ms=interval_ms,
    )
    return facts


def _append_changed_decision(
    repo: ClerkSqliteRepository, sid: str, facts: ExitRecoveryEvaluatedFacts,
    previous: ExitRecoveryEvaluatedFacts | None,
) -> None:
    if previous is not None and replace(
        facts, last_checked_at_ms=previous.last_checked_at_ms, lease_owner=previous.lease_owner,
    ) == previous:
        return
    repo.append_transition(TransitionInput(
        strategy_instance_id=sid, transition_kind="EXIT_RECOVERY_EVALUATED", custody_owner="ACCOUNT_CLERK",
        execution_authority="ACCOUNT_CLERK", operation_state="succeeded",
        clerk_observed_at_ms=repo.clock(), summary_code=facts.reason_code,
        proof_reference=facts.uncertainty_id, facts_json=facts.to_facts_json(),
    ))


def pause_exit_recovery(repo: ClerkSqliteRepository, *, reason_code: str) -> None:
    """A failed pass breaks continuity, retaining its last successful check."""
    for instance in repo.strategy_instances():
        sid = instance["strategy_instance_id"]
        episode = repo.active_uncertainty(
            scope="CUSTODY_SUBJECT", reason_code=EXIT_NOT_FLAT_REASON_CODE, strategy_instance_id=sid,
        )
        if episode is None:
            continue
        previous = latest_exit_recovery(repo, strategy_instance_id=sid, uncertainty_id=episode["uncertainty_id"])
        facts = ExitRecoveryEvaluatedFacts(
            uncertainty_id=episode["uncertainty_id"], outcome="hold", reason_code=reason_code,
            last_checked_at_ms=None if previous is None else previous.last_checked_at_ms,
            first_failure_at_ms=None if previous is None else previous.first_failure_at_ms,
            failure_elapsed_ms=0 if previous is None else previous.failure_elapsed_ms,
            lease_owner=repo.lease_owner,
        )
        _append_changed_decision(repo, sid, facts, previous)
        repo.record_recovery_check(
            strategy_instance_id=sid, uncertainty_id=facts.uncertainty_id, last_checked_at_ms=facts.last_checked_at_ms,
            completed_at_ms=repo.clock(), interval_ms=DEFAULT_RECOVERY_INTERVAL_MS,
        )
