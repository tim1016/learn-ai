"""Seed a pre-budget Start fact when a test exercises historical Clerk replay."""

from __future__ import annotations

from collections.abc import Callable

from app.broker.alpaca.clerk.sqlite.commands import _lifecycle_identity
from app.broker.alpaca.clerk.sqlite.facts import RunStartedFacts
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository


def seed_historical_run_started(
    repo: ClerkSqliteRepository,
    *,
    account_id: str,
    strategy_instance_id: str,
    lifecycle_run_id: str,
    operator_reason: str | None = None,
    clock: Callable[[], int] | None = None,
) -> str:
    """Write one old RUN_STARTED fact so migration/replay tests retain real old-state input."""
    if account_id != repo.account_id or repo.budget_authority_version() != 1:
        raise ValueError("historical Start seed requires this account before budget cutover")
    if repo.strategy_instance(strategy_instance_id) is None:
        raise ValueError("historical Start seed requires a registered strategy instance")
    if clock is not None and clock is not repo.clock:
        raise ValueError("historical Start seed requires the repository clock")
    if repo.active_run(strategy_instance_id) is not None:
        raise ValueError("historical Start seed requires no active run")
    key, payload_hash, command_id = _lifecycle_identity(
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
        lifecycle_run_id=lifecycle_run_id,
        action="START",
        intended_end_state="ACTIVE",
        operator_reason=operator_reason,
    )
    run_id = f"{strategy_instance_id}:{lifecycle_run_id}"
    facts = RunStartedFacts(
        idempotency_key=key,
        payload_hash=payload_hash,
        kind="operator_lifecycle",
        action="START",
        intended_end_state="ACTIVE",
        lifecycle_run_id=lifecycle_run_id,
        operator_reason=operator_reason,
    )
    repo.append_transition(
        TransitionInput(
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
            command_id=command_id,
            transition_kind="RUN_STARTED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=repo.clock(),
            summary_code="RUN_STARTED",
            facts_json=facts.to_facts_json(),
        )
    )
    return run_id
