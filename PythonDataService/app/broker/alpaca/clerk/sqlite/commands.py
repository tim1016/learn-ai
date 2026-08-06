"""Purely-local operator-lifecycle commands over the SQLite spine (#1376,
repaired by the corrective foundation slice).

Proves out the full R2 content-addressed command lifecycle end-to-end using
two commands that never touch the broker: starting and stopping a run.
Domain logic here (payload hashing, idempotency-key construction, admission)
is deliberately not in ``repository.py`` — per the Slice-2 review, the
repository owns the generic event-sourcing spine; this module owns what one
specific kind of command means. Slices 4+ (ENTER/EXIT, broker-facing
commands) get their own domain modules the same way, not more methods
bolted onto ``ClerkSqliteRepository``.

Corrective foundation slice: both Start and Stop now build a
:class:`~app.broker.alpaca.clerk.sqlite.models.TransitionInput` and commit it
through :meth:`ClerkSqliteRepository.commit_first_transition` — there is no
longer a separate ``reserve_command()`` step or a public ``serialized()``
lock for this module to compose. Stop also now takes a caller-supplied
``lifecycle_run_id`` instead of resolving it from the currently active run
(open-pr-review-2026-08-05.md P2 "Stop retry loses the active-run identity";
see the pinned contract's §3a for the full rationale).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass

from app.broker.alpaca.clerk.sqlite.facts import (
    CommandRejectedFacts,
    RunStartedFacts,
    RunStoppedFacts,
)
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.idempotency import (
    DurableConflictError,
    InvalidIdentityError,
    NoActiveRunError,
    UnknownStrategyInstanceError,
    reject_colon,
    require_active_run,
    require_strategy_instance,
)
from app.broker.alpaca.clerk.sqlite.models import (
    CommandCreated,
    CommandExistingConflict,
    CommandExistingSame,
    CommandResource,
    TransitionInput,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.utils.timestamps import Clock, now_ms_utc

__all__ = [
    "ACTION_START",
    "ACTION_STOP",
    "CommandSubmission",
    "DurableConflictError",
    "InvalidIdentityError",
    "NoActiveRunError",
    "UnknownStrategyInstanceError",
    "stop_command_resource",
    "submit_start_run",
    "submit_stop_run",
]

ACTION_START = "START"
ACTION_STOP = "STOP"
INTENDED_END_STATE_ACTIVE = "ACTIVE"
INTENDED_END_STATE_STOPPED = "STOPPED"

_ALREADY_ACTIVE_REASON = "This bot already has an active run; stop it before starting a new one."


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


def _lifecycle_identity(
    *,
    account_id: str,
    strategy_instance_id: str,
    lifecycle_run_id: str,
    action: str,
    intended_end_state: str,
    operator_reason: str | None,
) -> tuple[str, str, str]:
    """Shared by Start and Stop: the (idempotency_key, payload_hash,
    command_id) triple every operator-lifecycle command commits under."""
    idempotency_key = _operator_lifecycle_key(
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
        lifecycle_run_id=lifecycle_run_id,
        action=action,
        intended_end_state=intended_end_state,
    )
    payload_hash = _operator_lifecycle_hash(
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
        lifecycle_run_id=lifecycle_run_id,
        action=action,
        intended_end_state=intended_end_state,
        operator_reason=operator_reason,
    )
    return idempotency_key, payload_hash, f"cmd:{idempotency_key}"


def _commit(
    repo: ClerkSqliteRepository,
    *,
    command_id: str,
    idempotency_key: str,
    payload_hash: str,
    build_transition: Callable[[], TransitionInput],
) -> CommandSubmission:
    outcome = repo.commit_first_transition(
        command_id=command_id,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        build_transition=build_transition,
    )
    if isinstance(outcome, CommandExistingConflict):
        raise DurableConflictError(outcome.command)
    if isinstance(outcome, CommandExistingSame):
        return CommandSubmission(command=outcome.command, created=False)
    assert isinstance(outcome, CommandCreated)
    return CommandSubmission(command=outcome.command, created=True)


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

    idempotency_key, payload_hash, command_id = _lifecycle_identity(
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
        lifecycle_run_id=lifecycle_run_id,
        action=ACTION_START,
        intended_end_state=INTENDED_END_STATE_ACTIVE,
        operator_reason=operator_reason,
    )

    def build_transition() -> TransitionInput:
        # Reading strategy_instance()/active_run() here — not before the
        # idempotency lookup above — is what makes this thread-safe: both
        # only run once commit_first_transition's write lock is held, and
        # only for a fresh key (a retry never re-reads either). Checking
        # existence before acquiring that lock (like reject_colon() above)
        # would race unprotected reads against concurrent callers on the
        # same shared connection.
        require_strategy_instance(repo, strategy_instance_id)
        observed_at_ms = clock()
        if repo.active_run(strategy_instance_id) is not None:
            facts = CommandRejectedFacts(
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                kind="operator_lifecycle",
                action=ACTION_START,
                intended_end_state=INTENDED_END_STATE_ACTIVE,
                reason_code="START_REJECTED_ALREADY_ACTIVE",
                operator_reason=operator_reason,
            )
            return TransitionInput(
                strategy_instance_id=strategy_instance_id,
                command_id=command_id,
                transition_kind="COMMAND_REJECTED",
                custody_owner="ACCOUNT_CLERK",
                execution_authority="ACCOUNT_CLERK",
                operation_state="rejected",
                clerk_observed_at_ms=observed_at_ms,
                summary_code="START_REJECTED_ALREADY_ACTIVE",
                facts_json=facts.to_facts_json(),
            )
        run_id = f"{strategy_instance_id}:{lifecycle_run_id}"
        facts = RunStartedFacts(
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            kind="operator_lifecycle",
            action=ACTION_START,
            intended_end_state=INTENDED_END_STATE_ACTIVE,
            lifecycle_run_id=lifecycle_run_id,
            operator_reason=operator_reason,
        )
        return TransitionInput(
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
            command_id=command_id,
            transition_kind="RUN_STARTED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=observed_at_ms,
            summary_code="RUN_STARTED",
            facts_json=facts.to_facts_json(),
        )

    return _commit(
        repo,
        command_id=command_id,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        build_transition=build_transition,
    )


def submit_stop_run(
    repo: ClerkSqliteRepository,
    *,
    account_id: str,
    strategy_instance_id: str,
    lifecycle_run_id: str,
    operator_reason: str | None = None,
    clock: Clock = now_ms_utc,
) -> CommandSubmission:
    """Stop the run identified by ``lifecycle_run_id``. Purely local — no
    broker contact (this stops the bot's decision-making; it does not touch
    any existing broker orders, which is why it needs no
    effect_operation/order rows).

    ``lifecycle_run_id`` is caller-supplied, exactly like Start — it is
    **not** resolved from the currently active run. A prior revision of this
    function did resolve it, which made a lost Stop response unrecoverable:
    a retry could no longer find an active run to resolve a key against,
    since the first attempt had already stopped it
    (open-pr-review-2026-08-05.md P2 "Stop retry loses the active-run
    identity"). Because the identity is caller-supplied, the existing-command
    lookup inside ``commit_first_transition`` runs *before* this function
    re-reads the active run, so a lost-response retry replays the completed
    Stop even though the run it targeted is no longer active.
    """
    reject_colon("strategy_instance_id", strategy_instance_id)
    reject_colon("lifecycle_run_id", lifecycle_run_id)

    idempotency_key, payload_hash, command_id = _lifecycle_identity(
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
        lifecycle_run_id=lifecycle_run_id,
        action=ACTION_STOP,
        intended_end_state=INTENDED_END_STATE_STOPPED,
        operator_reason=operator_reason,
    )

    def build_transition() -> TransitionInput:
        # See submit_start_run's build_transition for why this existence
        # check runs here, under the lock, rather than before it.
        require_strategy_instance(repo, strategy_instance_id)
        active = require_active_run(repo, strategy_instance_id, lifecycle_run_id)
        facts = RunStoppedFacts(
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            kind="operator_lifecycle",
            action=ACTION_STOP,
            intended_end_state=INTENDED_END_STATE_STOPPED,
            lifecycle_run_id=lifecycle_run_id,
            operator_reason=operator_reason,
        )
        return TransitionInput(
            strategy_instance_id=strategy_instance_id,
            run_id=active.run_id,
            command_id=command_id,
            transition_kind="RUN_STOPPED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=clock(),
            summary_code="RUN_STOPPED",
            facts_json=facts.to_facts_json(),
        )

    return _commit(
        repo,
        command_id=command_id,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        build_transition=build_transition,
    )


def stop_command_resource(
    repo: ClerkSqliteRepository,
    *,
    account_id: str,
    strategy_instance_id: str,
    lifecycle_run_id: str,
) -> CommandResource | None:
    """Return the durable STOP resource for a lost-response transport retry."""
    idempotency_key = _operator_lifecycle_key(
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
        lifecycle_run_id=lifecycle_run_id,
        action=ACTION_STOP,
        intended_end_state=INTENDED_END_STATE_STOPPED,
    )
    return repo.get_command(f"cmd:{idempotency_key}")
