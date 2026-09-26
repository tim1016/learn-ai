"""Durable exit-recovery observations, independent of the uncertainty's age gate.

Only adjacent failure observations in the same regular session and writer
generation spend the failure-time budget. Gaps longer than two normal sweep
intervals are unobserved time, not proof that a failure kept happening.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal

from app.broker.alpaca.clerk.sqlite.facts import ExitRecoveryEvaluatedFacts
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import EXIT_NOT_FLAT_REASON_CODE
from app.services.session_authority import session_state_at_ms

RECOVERY_OBSERVATION_MAX_GAP_MS = 30_000
"""Observation continuity bound: two normal 15-second sweep intervals.

A slower or stalled sweep may still recover orders, but cannot retrospectively
spend an escalation budget over time it did not observe.
"""


@dataclass
class RecoveryEvaluationBatch:
    repository: ClerkSqliteRepository
    observations: dict[str, ExitRecoveryEvaluatedFacts] = field(default_factory=dict)
    escalations: list[Callable[[], Awaitable[None]]] = field(default_factory=list)

    def commit(self) -> None:
        for sid, facts in self.observations.items():
            episode = self.repository.active_uncertainty(
                scope="CUSTODY_SUBJECT", reason_code=EXIT_NOT_FLAT_REASON_CODE, strategy_instance_id=sid,
            )
            if episode is not None and episode["uncertainty_id"] == facts.uncertainty_id:
                _append_observation(self.repository, sid, facts)


_EVALUATION_BATCH: ContextVar[RecoveryEvaluationBatch | None] = ContextVar("exit_recovery_batch", default=None)


@contextmanager
def collect_recovery_evaluations(repo: ClerkSqliteRepository) -> Iterator[RecoveryEvaluationBatch]:
    """Only a pass with fresh final broker evidence commits checks or escalation.

    Broker commands retain their own durable facts throughout. This buffer
    contains only recovery observations, so an incomplete pass cannot spend
    failure time or make its last successful check appear newer.
    """
    batch = RecoveryEvaluationBatch(repo)
    token = _EVALUATION_BATCH.set(batch)
    try:
        yield batch
    finally:
        _EVALUATION_BATCH.reset(token)


def defer_recovery_escalation(repo: ClerkSqliteRepository, escalate: Callable[[], Awaitable[None]]) -> bool:
    batch = _EVALUATION_BATCH.get()
    if batch is None or batch.repository is not repo:
        return False
    batch.escalations.append(escalate)
    return True


def latest_exit_recovery(
    repo: ClerkSqliteRepository, *, strategy_instance_id: str, uncertainty_id: str,
) -> ExitRecoveryEvaluatedFacts | None:
    row = repo.last_strategy_transition(
        strategy_instance_id=strategy_instance_id, transition_kind="EXIT_RECOVERY_EVALUATED",
    )
    if row is None:
        return None
    facts = ExitRecoveryEvaluatedFacts.from_facts_json(row["facts_json"])
    return facts if facts.uncertainty_id == uncertainty_id else None


def observe_exit_recovery(
    repo: ClerkSqliteRepository, *, strategy_instance_id: str, uncertainty_id: str,
    outcome: Literal["failure", "hold", "accepted"], reason_code: str,
    checked: bool = True, allowed_from_ms: int | None = None, explanation: str = "",
) -> ExitRecoveryEvaluatedFacts | None:
    """Record under intake; a failed pass pauses without inventing a watchdog check."""
    episode = repo.active_uncertainty(
        scope="CUSTODY_SUBJECT", reason_code=EXIT_NOT_FLAT_REASON_CODE,
        strategy_instance_id=strategy_instance_id,
    )
    if episode is None or episode["uncertainty_id"] != uncertainty_id:
        return None
    now_ms = repo.clock()
    session = session_state_at_ms(now_ms=now_ms)
    if outcome == "failure" and session.phase != "RTH":
        outcome = "hold"
    previous = latest_exit_recovery(
        repo, strategy_instance_id=strategy_instance_id, uncertainty_id=uncertainty_id,
    )
    batch = _EVALUATION_BATCH.get()
    if batch is not None and batch.repository is repo:
        if checked:
            pending = batch.observations.get(strategy_instance_id)
            if pending is not None and pending.uncertainty_id == uncertainty_id:
                previous = pending
        else:
            batch.observations.pop(strategy_instance_id, None)
    elapsed = 0 if previous is None else previous.failure_elapsed_ms
    first_failure = None if previous is None else previous.first_failure_at_ms
    if outcome == "accepted":
        elapsed, first_failure = 0, None
    elif outcome == "failure":
        if first_failure is None:
            first_failure = now_ms
        if (
            previous is not None and previous.outcome == "failure"
            and previous.lease_owner == repo.lease_owner and previous.last_checked_at_ms is not None
        ):
            delta = now_ms - previous.last_checked_at_ms
            if 0 <= delta <= RECOVERY_OBSERVATION_MAX_GAP_MS:
                prior_session = session_state_at_ms(now_ms=previous.last_checked_at_ms)
                if prior_session.phase == "RTH" and prior_session.next_transition_ms == session.next_transition_ms:
                    elapsed += delta
    facts = ExitRecoveryEvaluatedFacts(
        uncertainty_id=uncertainty_id, outcome=outcome, reason_code=reason_code,
        last_checked_at_ms=now_ms if checked else (None if previous is None else previous.last_checked_at_ms),
        first_failure_at_ms=first_failure,
        failure_elapsed_ms=elapsed, lease_owner=repo.lease_owner,
        allowed_from_ms=allowed_from_ms, explanation=explanation,
    )
    if facts != previous:
        if checked and batch is not None and batch.repository is repo:
            batch.observations[strategy_instance_id] = facts
        else:
            _append_observation(repo, strategy_instance_id, facts)
    return facts


def _append_observation(repo: ClerkSqliteRepository, sid: str, facts: ExitRecoveryEvaluatedFacts) -> None:
    repo.append_transition(TransitionInput(
        strategy_instance_id=sid,
        transition_kind="EXIT_RECOVERY_EVALUATED", custody_owner="ACCOUNT_CLERK",
        execution_authority="ACCOUNT_CLERK", operation_state="succeeded",
        clerk_observed_at_ms=repo.clock(), summary_code=facts.reason_code,
        proof_reference=facts.uncertainty_id, facts_json=facts.to_facts_json(),
    ))


def pause_exit_recovery(repo: ClerkSqliteRepository, *, reason_code: str) -> None:
    """An unsuccessful pass breaks observation continuity without spending time."""
    for instance in repo.strategy_instances():
        sid = instance["strategy_instance_id"]
        episode = repo.active_uncertainty(
            scope="CUSTODY_SUBJECT", reason_code=EXIT_NOT_FLAT_REASON_CODE,
            strategy_instance_id=sid,
        )
        if episode is not None:
            observe_exit_recovery(
                repo, strategy_instance_id=sid, uncertainty_id=episode["uncertainty_id"],
                outcome="hold", reason_code=reason_code, checked=False,
            )
