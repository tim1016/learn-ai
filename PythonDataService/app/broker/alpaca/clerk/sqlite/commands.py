"""Purely-local operator-lifecycle commands over the SQLite spine (#1376).

Proves out the full R2 content-addressed command lifecycle end-to-end using
two commands that never touch the broker: starting and stopping a run.
Domain logic here (payload hashing, idempotency-key construction, admission)
is deliberately not in ``repository.py`` — per the Slice-2 review, the
repository owns the generic event-sourcing spine; this module owns what one
specific kind of command means. Slices 4+ (ENTER/EXIT, broker-facing
commands) get their own domain modules the same way, not more methods
bolted onto ``ClerkSqliteRepository``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.idempotency import (
    DurableConflictError,
    InvalidIdentityError,
    reject_colon,
)
from app.broker.alpaca.clerk.sqlite.repository import (
    ClerkSqliteRepository,
    CommandResource,
    ReservationConflict,
    ReservedExisting,
    ReservedNew,
    TransitionInput,
)
from app.utils.timestamps import Clock, now_ms_utc

ACTION_START = "START"
ACTION_STOP = "STOP"
INTENDED_END_STATE_ACTIVE = "ACTIVE"
INTENDED_END_STATE_STOPPED = "STOPPED"

_ALREADY_ACTIVE_REASON = "This bot already has an active run; stop it before starting a new one."
_NO_ACTIVE_RUN_REASON = "This bot has no active run to stop."

__all__ = [
    "CommandSubmission",
    "DurableConflictError",
    "InvalidIdentityError",
    "NoActiveRunError",
    "submit_start_run",
    "submit_stop_run",
]


class NoActiveRunError(Exception):
    """Stop was requested but no run is active — there is nothing to bind
    a content-addressed identity to, so no ``commands`` row is written."""

    def __init__(self, strategy_instance_id: str) -> None:
        self.strategy_instance_id = strategy_instance_id
        super().__init__(_NO_ACTIVE_RUN_REASON)


@dataclass(frozen=True)
class CommandSubmission:
    command: CommandResource
    created: bool  # False for a transport retry / genuine re-request of an existing command


def _operator_lifecycle_key(
    *,
    account_id: str,
    strategy_instance_id: str,
    lifecycle_run_id: str,
    action: str,
    intended_end_state: str,
) -> str:
    """Pinned contracts doc §3a — the operator-lifecycle natural key."""
    return f"{account_id}:{strategy_instance_id}:{lifecycle_run_id}:{action}:{intended_end_state}"


def _operator_lifecycle_hash(
    *,
    account_id: str,
    strategy_instance_id: str,
    lifecycle_run_id: str,
    action: str,
    intended_end_state: str,
    operator_reason: str | None,
) -> str:
    """R2: canonically hash action, target, account, instance, run, and any
    operator reason that changes meaning."""
    canonical = canonicalize(
        {
            "account_id": account_id,
            "strategy_instance_id": strategy_instance_id,
            "lifecycle_run_id": lifecycle_run_id,
            "action": action,
            "intended_end_state": intended_end_state,
            "operator_reason": operator_reason,
        }
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def submit_start_run(
    repo: ClerkSqliteRepository,
    *,
    account_id: str,
    strategy_instance_id: str,
    lifecycle_run_id: str,
    operator_reason: str | None = None,
    clock: Clock = now_ms_utc,
) -> CommandSubmission:
    """Start a run. Purely local — no broker contact.

    ``lifecycle_run_id`` is caller-proposed (ADR 0035 #3: "Start/Resume
    reserves its proposed run identity before committing the key") — the
    frontend mints it once per user action and resends the same value on a
    transport retry, which is what makes the retry idempotent.
    """
    reject_colon("strategy_instance_id", strategy_instance_id)
    reject_colon("lifecycle_run_id", lifecycle_run_id)

    idempotency_key = _operator_lifecycle_key(
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
        lifecycle_run_id=lifecycle_run_id,
        action=ACTION_START,
        intended_end_state=INTENDED_END_STATE_ACTIVE,
    )
    payload_hash = _operator_lifecycle_hash(
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
        lifecycle_run_id=lifecycle_run_id,
        action=ACTION_START,
        intended_end_state=INTENDED_END_STATE_ACTIVE,
        operator_reason=operator_reason,
    )
    command_id = f"cmd:{idempotency_key}"

    # reserve -> read active_run() -> decide -> append must be one atomic
    # sequence: two Starts with different lifecycle_run_ids (so no collision
    # at reserve_command) could otherwise both observe "no active run" before
    # either commits and both append RUN_STARTED, racing on
    # ux_runs_one_active_per_instance (#1376 review).
    with repo.serialized():
        outcome = repo.reserve_command(
            command_id=command_id,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            kind="operator_lifecycle",
            strategy_instance_id=strategy_instance_id,
            run_id=None,  # pre-run command: the fold creates the run, not the reservation
            action=ACTION_START,
            intended_end_state=INTENDED_END_STATE_ACTIVE,
        )
        if isinstance(outcome, ReservationConflict):
            raise DurableConflictError(outcome.command)
        if isinstance(outcome, ReservedExisting):
            return CommandSubmission(command=outcome.command, created=False)
        assert isinstance(outcome, ReservedNew)

        run_id = f"{strategy_instance_id}:{lifecycle_run_id}"
        observed_at_ms = clock()
        if repo.active_run(strategy_instance_id) is not None:
            repo.append_transition(
                TransitionInput(
                    strategy_instance_id=strategy_instance_id,
                    command_id=command_id,
                    transition_kind="COMMAND_REJECTED",
                    custody_owner="ACCOUNT_CLERK",
                    execution_authority="ACCOUNT_CLERK",
                    operation_state="rejected",
                    clerk_observed_at_ms=observed_at_ms,
                    summary_code="START_REJECTED_ALREADY_ACTIVE",
                    facts_json=canonicalize({"reason": _ALREADY_ACTIVE_REASON}),
                )
            )
        else:
            repo.append_transition(
                TransitionInput(
                    strategy_instance_id=strategy_instance_id,
                    run_id=run_id,
                    command_id=command_id,
                    transition_kind="RUN_STARTED",
                    custody_owner="ACCOUNT_CLERK",
                    execution_authority="ACCOUNT_CLERK",
                    operation_state="succeeded",
                    clerk_observed_at_ms=observed_at_ms,
                    summary_code="RUN_STARTED",
                    facts_json=canonicalize({"lifecycle_run_id": lifecycle_run_id}),
                )
            )
        command = repo.get_command(command_id)
        assert command is not None
        return CommandSubmission(command=command, created=True)


def submit_stop_run(
    repo: ClerkSqliteRepository,
    *,
    account_id: str,
    strategy_instance_id: str,
    operator_reason: str | None = None,
    clock: Clock = now_ms_utc,
) -> CommandSubmission:
    """Stop the active run. Purely local — no broker contact (this stops
    the bot's decision-making; it does not touch any existing broker
    orders, which is why it needs no effect_operation/order rows).

    Unlike Start, ``lifecycle_run_id`` is not caller-supplied: it is
    resolved from the currently active run, so repeated Stop requests for
    the same run always compute the same idempotency key without needing a
    client-generated token — idempotent by construction (ADR 0035 #3: "Stop
    binds the active run").
    """
    reject_colon("strategy_instance_id", strategy_instance_id)

    # The active-run read that resolves lifecycle_run_id, the reservation,
    # and the append must be one atomic sequence for the same reason as
    # submit_start_run: state read outside the lock can go stale before the
    # decision it drives gets appended (#1376 review).
    with repo.serialized():
        active = repo.active_run(strategy_instance_id)
        if active is None:
            raise NoActiveRunError(strategy_instance_id)

        idempotency_key = _operator_lifecycle_key(
            account_id=account_id,
            strategy_instance_id=strategy_instance_id,
            lifecycle_run_id=active.lifecycle_run_id,
            action=ACTION_STOP,
            intended_end_state=INTENDED_END_STATE_STOPPED,
        )
        payload_hash = _operator_lifecycle_hash(
            account_id=account_id,
            strategy_instance_id=strategy_instance_id,
            lifecycle_run_id=active.lifecycle_run_id,
            action=ACTION_STOP,
            intended_end_state=INTENDED_END_STATE_STOPPED,
            operator_reason=operator_reason,
        )
        command_id = f"cmd:{idempotency_key}"

        outcome = repo.reserve_command(
            command_id=command_id,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            kind="operator_lifecycle",
            strategy_instance_id=strategy_instance_id,
            run_id=active.run_id,  # the run already exists, so this FK is satisfiable now
            action=ACTION_STOP,
            intended_end_state=INTENDED_END_STATE_STOPPED,
        )
        if isinstance(outcome, ReservationConflict):
            raise DurableConflictError(outcome.command)
        if isinstance(outcome, ReservedExisting):
            return CommandSubmission(command=outcome.command, created=False)
        assert isinstance(outcome, ReservedNew)

        repo.append_transition(
            TransitionInput(
                strategy_instance_id=strategy_instance_id,
                run_id=active.run_id,
                command_id=command_id,
                transition_kind="RUN_STOPPED",
                custody_owner="ACCOUNT_CLERK",
                execution_authority="ACCOUNT_CLERK",
                operation_state="succeeded",
                clerk_observed_at_ms=clock(),
                summary_code="RUN_STOPPED",
                facts_json=canonicalize({"operator_reason": operator_reason}),
            )
        )
        command = repo.get_command(command_id)
        assert command is not None
        return CommandSubmission(command=command, created=True)
