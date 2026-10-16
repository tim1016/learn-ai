"""Purely local Stop and Retire commands over the SQLite Clerk spine.

These commands build a transition under the repository's write lock. Historical
``RUN_STARTED`` facts remain replayable, but new runs use budgeted Deploy.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass

from app.broker.alpaca.clerk.sqlite.facts import (
    RunStoppedFacts,
    StrategyInstanceRetiredFacts,
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
    "ACTION_STOP",
    "CommandSubmission",
    "DurableConflictError",
    "InvalidIdentityError",
    "NoActiveRunError",
    "UnknownStrategyInstanceError",
    "run_stop_reason",
    "stop_command_resource",
    "submit_retire_strategy_instance",
    "submit_stop_run",
]

ACTION_STOP = "STOP"
ACTION_RETIRE = "RETIRE"
INTENDED_END_STATE_STOPPED = "STOPPED"
INTENDED_END_STATE_RETIRED = "RETIRED"


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
    """ADR 0035's binding annex §3a — the operator-lifecycle natural key."""
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
    """Return the key, payload hash, and command ID for a lifecycle command."""
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

    ``lifecycle_run_id`` is caller-supplied; it is
    **not** resolved from the currently active run. A prior revision of this
    function did resolve it, which made a lost Stop response unrecoverable:
    a retry could no longer find an active run to resolve a key against,
    since the first attempt had already stopped it.
    Because the identity is caller-supplied, the existing-command
    lookup inside ``commit_first_transition`` runs *before* this function
    re-reads the active run, so a lost-response retry replays the completed
    Stop even though the run it targeted is no longer active.

    A run is stopped once, whatever the reason: a second STOP of the same run
    under another reason -- the owner's Stop landing in the minute the Clerk
    stopped the run at its scheduled end (#2607) -- is the STOP already
    committed, returned as it was, never a conflict.
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
        # Check existence under the same write lock that commits the transition.
        require_strategy_instance(repo, strategy_instance_id)
        active = require_active_run(repo, strategy_instance_id, lifecycle_run_id)
        # Valued under the same lock as the append: the release is exactly
        # the custody this transition ends (#2555).
        release = repo.release_at_stop(run_id=active.run_id)
        facts = RunStoppedFacts(
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            kind="operator_lifecycle",
            action=ACTION_STOP,
            intended_end_state=INTENDED_END_STATE_STOPPED,
            lifecycle_run_id=lifecycle_run_id,
            operator_reason=operator_reason,
            released_cents=None if release is None else release.released_cents,
            held_cents=None if release is None else release.held_cents,
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

    outcome = repo.commit_first_transition(
        command_id=command_id,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        build_transition=build_transition,
    )
    if isinstance(outcome, CommandExistingConflict | CommandExistingSame):
        # The key names this run's STOP alone; only its reason can differ.
        return CommandSubmission(command=outcome.command, created=False)
    assert isinstance(outcome, CommandCreated)
    return CommandSubmission(command=outcome.command, created=True)


def submit_retire_strategy_instance(
    repo: ClerkSqliteRepository,
    *,
    account_id: str,
    strategy_instance_id: str,
    retired_at_ms: int,
    operator_reason: str | None = None,
    clock: Clock = now_ms_utc,
) -> CommandSubmission:
    """Retire a registration in the authority. Purely local — no broker contact.

    Retirement is the fact that makes a registration permanently inadmissible:
    ``run_admission`` refuses ``BOT_RETIRED``, and the V2 catalog derives its
    phase from ``strategy_instances.retired_at_ms``. Both read SQLite, so
    SQLite is where retirement has to be written — the file lifecycle record
    is a projection of this, never the other way round.

    Unlike Stop the identity carries no ``lifecycle_run_id``:
    retirement is instance-scoped and happens exactly once, when the guards in
    ``evaluate_archive`` have already proved there is no active run. The
    key therefore names the instance alone, which is also what makes a
    lost-response retry replay the original retirement rather than mint a
    second one at a later instant.
    """
    reject_colon("strategy_instance_id", strategy_instance_id)

    idempotency_key = f"{account_id}:{strategy_instance_id}:{ACTION_RETIRE}"
    payload_hash = hashlib.sha256(
        canonicalize(
            {
                "account_id": account_id,
                "action": ACTION_RETIRE,
                "intended_end_state": INTENDED_END_STATE_RETIRED,
                "operator_reason": operator_reason,
                "strategy_instance_id": strategy_instance_id,
            }
        ).encode("utf-8")
    ).hexdigest()
    command_id = f"cmd:{idempotency_key}"

    def build_transition() -> TransitionInput:
        # Check existence under the same write lock that commits retirement.
        require_strategy_instance(repo, strategy_instance_id)
        facts = StrategyInstanceRetiredFacts(
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            kind="operator_lifecycle",
            action=ACTION_RETIRE,
            intended_end_state=INTENDED_END_STATE_RETIRED,
            retired_at_ms=retired_at_ms,
            operator_reason=operator_reason,
        )
        return TransitionInput(
            strategy_instance_id=strategy_instance_id,
            command_id=command_id,
            transition_kind="STRATEGY_INSTANCE_RETIRED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=clock(),
            summary_code="STRATEGY_INSTANCE_RETIRED",
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


def run_stop_reason(
    repo: ClerkSqliteRepository,
    *,
    strategy_instance_id: str,
    lifecycle_run_id: str,
) -> str | None:
    """The ``operator_reason`` this run's STOP committed under, or ``None``.

    A run is stopped once, under its first STOP's reason (:func:`submit_stop_run`),
    and an instance's runs stop in turn, so its latest ``RUN_STOPPED`` answers
    for its latest stopped run. ``None`` when that is another run, when the run
    has not stopped, or when its STOP carried no reason.
    """
    stopped = repo.last_strategy_transition(
        strategy_instance_id=strategy_instance_id, transition_kind="RUN_STOPPED"
    )
    if stopped is None:
        return None
    facts = RunStoppedFacts.from_facts_json(stopped["facts_json"])
    return facts.operator_reason if facts.lifecycle_run_id == lifecycle_run_id else None
