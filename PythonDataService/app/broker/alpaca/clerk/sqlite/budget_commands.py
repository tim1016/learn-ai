"""Durable one-Deploy consent over the existing command/mirror coordinator."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from decimal import Decimal

from app.broker.alpaca.clerk.budgets import BudgetUnavailable
from app.broker.alpaca.clerk.funding import EntryNeed, deploy_funding_answer
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeGate
from app.broker.alpaca.clerk.money import MoneyInputError
from app.broker.alpaca.clerk.sealed_ledger import canonical_sha256
from app.broker.alpaca.clerk.sqlite.budget_facts import DeployCommittedFacts
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
from app.broker.alpaca.clerk.sqlite.risk_admission import require_current_risk_admission
from app.broker.alpaca.clerk.sqlite.uncertainty import require_admission
from app.schemas.account_authority import AuthorityKind, account_authority_agrees


def deployment_command_id(account_id: str, strategy_instance_id: str) -> str:
    """One immutable deployment identity is one durable command, including retries."""
    return f"cmd:{account_id}:{strategy_instance_id}:DEPLOY"


def _require_budget_authority(repo: ClerkSqliteRepository) -> None:
    if repo.budget_authority_version() != 2:
        raise BudgetUnavailable("Switch this account to budgets in Settings before Deploy.")


def _require_funded(
    repo: ClerkSqliteRepository, *, strategy_instance_id: str, world: AuthorityKind, committed_cents: int,
    risk_revision: int, envelope: LiveEnvelopeGate, minimum_position_cost: Decimal,
) -> None:
    """The account's admission, risk and funding rules for one budget; none needs the bot registered."""
    require_admission(repo, strategy_instance_id=strategy_instance_id)
    # One instant judges the reading's freshness and the budget by it, so a reading admitted here is
    # never called stale a moment later.
    admitted_at_ms = repo.clock()
    observation = require_current_risk_admission(repo, envelope=envelope, now_ms=admitted_at_ms)
    if observation is None:
        raise BudgetUnavailable("Wait for a fresh cash observation, then review Deploy again.")
    policy = repo.account_risk_policy()
    effective_revision = 0 if policy is None else policy.revision
    if effective_revision != risk_revision:
        raise BudgetUnavailable("Account risk limits changed; review Deploy again.")
    if world != "synthetic" and (policy is None or observation.risk_revision != policy.revision):
        raise BudgetUnavailable("Set a daily loss limit in Settings and wait for current risk evidence.")
    try:
        projection = repo.account_budget(
            cash=observation.cash_available_usd, seen_before_ms=observation.fills_seen_before_ms,
            modelled_fees_seen_before_ms=observation.modelled_fees_seen_before_ms,
        )
        need = None if minimum_position_cost <= 0 else EntryNeed.for_shares(
            requirement=minimum_position_cost, fee_provision_cents=0,
        )
        answer = deploy_funding_answer(
            projection, amount_cents=committed_cents, need=need, broker_buying_power=None,
            observed_at_ms=observation.observed_at_ms, now_ms=admitted_at_ms,
        )
        # A priced need passes only on a verdict; with none, only a refusal the account itself makes stops it.
        if answer.admits is False or (need is not None and answer.admits is None):
            raise BudgetUnavailable(answer.sentence)
    except MoneyInputError as exc:
        raise BudgetUnavailable(str(exc)) from exc


def submit_budgeted_deploy(
    repo: ClerkSqliteRepository, *, strategy_instance_id: str, lifecycle_run_id: str,
    world: AuthorityKind, committed_cents: int, configuration_hash: str, exit_terms_hash: str,
    risk_revision: int, actor: str, envelope: LiveEnvelopeGate, minimum_position_cost: Decimal,
    request_fingerprint: str = "",
    registration_json: str | None = None,
) -> CommandSubmission:
    """Reserve one budget and run atomically; launch outcome follows separately.

    The process registry can launch only a newly created command. A retry
    returns the same durable state and never reactivates a stopped run.
    Startup failure and boot recovery append the normal RUN_STOPPED release.
    The initial ACTIVE run is custody permission held by the launch's owner,
    not a claim that the process has started; commands remains accepted until
    DEPLOY_LAUNCHED proves that outcome.

    The budget is judged by ``funding.deploy_funding_answer``, the one rule the
    Deploy review reads (#3099): it must cover ``minimum_position_cost`` (zero
    for a bot whose entries are priced when their contracts are picked, so none
    is asked here) and fit what the account has unreserved.
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
        request_fingerprint=request_fingerprint, registration_json=registration_json,
    )
    # Registration is part of the custody transition, but the command identity
    # stays the same when a transport retry sees the newly registered bot.
    command_payload = asdict(draft)
    command_payload.pop("registration_json")
    payload_hash = hashlib.sha256(canonicalize({**command_payload, "account_id": repo.account_id}).encode()).hexdigest()
    facts = DeployCommittedFacts(**{**asdict(draft), "payload_hash": payload_hash})
    command_id = deployment_command_id(repo.account_id, strategy_instance_id)

    def build_transition() -> TransitionInput:
        _require_budget_authority(repo)
        instance = repo.strategy_instance(strategy_instance_id)
        if registration_json is None:
            require_strategy_instance(repo, strategy_instance_id)
            assert instance is not None
            if instance["config_hash"] != configuration_hash:
                raise BudgetUnavailable("The strategy configuration changed; review Deploy again.")
            terms = repo.exit_terms(strategy_instance_id)
            if terms is None or canonical_sha256(terms.model_dump(mode="json")) != exit_terms_hash:
                raise BudgetUnavailable("The reviewed exit terms do not match this deployment.")
        else:
            if instance is not None:
                raise BudgetUnavailable("The deployment identity is already registered. Recover its stored result.")
            registration = json.loads(registration_json)
            if registration["config_hash"] != configuration_hash:
                raise BudgetUnavailable("The strategy configuration changed; review Deploy again.")
            from app.schemas.exit_terms import ExitTerms

            terms = ExitTerms.model_validate(registration["exit_terms"])
            if canonical_sha256(terms.model_dump(mode="json")) != exit_terms_hash:
                raise BudgetUnavailable("The reviewed exit terms do not match this deployment.")
        if repo.latest_run(strategy_instance_id) is not None:
            raise BudgetUnavailable("Trading again requires a fresh deployment identity.")
        _require_funded(
            repo, strategy_instance_id=strategy_instance_id, world=world, committed_cents=committed_cents,
            risk_revision=risk_revision, envelope=envelope, minimum_position_cost=minimum_position_cost,
        )
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
