"""Durable one-Deploy consent over the existing command/mirror coordinator."""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from decimal import Decimal

from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeGate
from app.broker.alpaca.clerk.money import MoneyInputError, cents_required, money_context
from app.broker.alpaca.clerk.sealed_ledger import canonical_sha256
from app.broker.alpaca.clerk.sqlite.budget_facts import DeployCommittedFacts
from app.broker.alpaca.clerk.sqlite.budget_projection import BudgetUnavailable
from app.broker.alpaca.clerk.sqlite.commands import CommandSubmission
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.idempotency import DurableConflictError, reject_colon, require_strategy_instance
from app.broker.alpaca.clerk.sqlite.models import (
    CommandCreated,
    CommandExistingConflict,
    CommandExistingSame,
    TransitionInput,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import require_admission
from app.schemas.account_authority import AuthorityKind, account_authority_agrees


def deployment_command_id(account_id: str, strategy_instance_id: str) -> str:
    """One immutable deployment identity is one durable command, including retries."""
    return f"cmd:{account_id}:{strategy_instance_id}:DEPLOY"


def submit_budgeted_deploy(
    repo: ClerkSqliteRepository, *, strategy_instance_id: str, lifecycle_run_id: str,
    world: AuthorityKind, committed_cents: int, configuration_hash: str, exit_terms_hash: str,
    risk_revision: int, actor: str, envelope: LiveEnvelopeGate, minimum_position_cost: Decimal,
    request_fingerprint: str = "",
) -> CommandSubmission:
    """Reserve one budget and run atomically; launch outcome follows separately.

    The process registry can launch only a newly created command. A retry
    returns the same durable state and never reactivates a stopped run.
    Startup failure and boot recovery append the normal RUN_STOPPED release.
    The initial ACTIVE run is custody permission held by the launch's owner,
    not a claim that the process has started; commands remains accepted until
    DEPLOY_LAUNCHED proves that outcome.
    """
    reject_colon("strategy_instance_id", strategy_instance_id)
    reject_colon("lifecycle_run_id", lifecycle_run_id)
    if not account_authority_agrees(repo.account_id, world):
        raise BudgetUnavailable("The reviewed account and custody world do not agree.")
    idempotency_key = f"{repo.account_id}:{strategy_instance_id}:DEPLOY"
    # Validate immutable facts before hashing, including whole-cent integer.
    draft = DeployCommittedFacts(
        idempotency_key=idempotency_key, payload_hash="", lifecycle_run_id=lifecycle_run_id,
        world=world, committed_cents=committed_cents, configuration_hash=configuration_hash,
        exit_terms_hash=exit_terms_hash, risk_revision=risk_revision, actor=actor,
        request_fingerprint=request_fingerprint,
    )
    payload_hash = hashlib.sha256(canonicalize({**asdict(draft), "account_id": repo.account_id}).encode()).hexdigest()
    facts = DeployCommittedFacts(**{**asdict(draft), "payload_hash": payload_hash})
    command_id = deployment_command_id(repo.account_id, strategy_instance_id)

    def build_transition() -> TransitionInput:
        require_strategy_instance(repo, strategy_instance_id)
        instance = repo.strategy_instance(strategy_instance_id)
        assert instance is not None
        if instance["config_hash"] != configuration_hash:
            raise BudgetUnavailable("The strategy configuration changed; review Deploy again.")
        terms = repo.exit_terms(strategy_instance_id)
        if terms is None or canonical_sha256(terms.model_dump(mode="json")) != exit_terms_hash:
            raise BudgetUnavailable("The reviewed exit terms do not match this deployment.")
        if repo.latest_run(strategy_instance_id) is not None:
            raise BudgetUnavailable("Trading again requires a fresh deployment identity.")
        require_admission(repo, strategy_instance_id=strategy_instance_id)
        observation = envelope.fresh_observation(repo.clock())
        if observation is None:
            raise BudgetUnavailable("Wait for a fresh cash observation, then review Deploy again.")
        policy = repo.account_risk_policy()
        effective_revision = 0 if policy is None else policy.revision
        if effective_revision != risk_revision:
            raise BudgetUnavailable("Account risk limits changed; review Deploy again.")
        if world != "synthetic" and (policy is None or observation.risk_revision != policy.revision):
            raise BudgetUnavailable("Apply account risk limits in Configuration and wait for current risk evidence.")
        try:
            projection = repo.account_budget(
                cash=observation.cash_available_usd, seen_before_ms=observation.fills_seen_before_ms,
            )
            with money_context():
                amount = Decimal(committed_cents) / 100
                if amount < minimum_position_cost:
                    raise BudgetUnavailable(f"One estimated position needs at least {cents_required(minimum_position_cost)} cents including fees.")
                if amount > projection.available:
                    raise BudgetUnavailable(f"The requested budget exceeds the {projection.unreserved_cents} unreserved cents.")
        except MoneyInputError as exc:
            raise BudgetUnavailable(str(exc)) from exc
        return TransitionInput(
            strategy_instance_id=strategy_instance_id,
            run_id=f"{strategy_instance_id}:{lifecycle_run_id}", command_id=command_id,
            transition_kind="DEPLOY_COMMITTED", custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK", operation_state="accepted",
            clerk_observed_at_ms=repo.clock(), summary_code="DEPLOY_COMMITTED",
            facts_json=facts.to_facts_json(),
        )

    outcome = repo.commit_first_transition(
        command_id=command_id, idempotency_key=idempotency_key,
        payload_hash=payload_hash, build_transition=build_transition,
    )
    if isinstance(outcome, CommandExistingConflict):
        raise DurableConflictError(outcome.command)
    if isinstance(outcome, CommandExistingSame):
        return CommandSubmission(command=outcome.command, created=False)
    assert isinstance(outcome, CommandCreated)
    return CommandSubmission(command=outcome.command, created=True)
