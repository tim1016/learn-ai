"""Explicit Options bot recovery keeps one deployment's lifetime money.

Formula: restored free-cash claim = max(0, the canonical deployment free balance).
Reference: ADR 0079 (explicit Options resume) and ADR 0059 (deployment budgets).
Canonical implementation: ``clerk/budgets.py`` and ``sqlite/budget_projection.py``;
  this module checks and records their figures, never resets capital or P&L.
Validated against: tests/broker/alpaca/clerk/test_options_resume.py.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app.broker.alpaca.clerk.budgets import BudgetUnavailable
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeGate
from app.broker.alpaca.clerk.money import cents_spendable
from app.broker.alpaca.clerk.sealed_ledger import canonical_sha256
from app.broker.alpaca.clerk.sqlite.commands import CommandSubmission
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.idempotency import DurableConflictError, reject_colon
from app.broker.alpaca.clerk.sqlite.models import (
    CommandCreated,
    CommandExistingConflict,
    CommandExistingSame,
    TransitionInput,
)
from app.broker.alpaca.clerk.sqlite.risk_admission import require_current_risk_admission
from app.broker.alpaca.clerk.sqlite.uncertainty import require_admission

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
    from app.services.bot_binding_repository import BrokerBotBinding


@dataclass(frozen=True)
class OptionsRunResumedFacts:
    idempotency_key: str
    payload_hash: str
    lifecycle_run_id: str
    prior_run_id: str
    configuration_hash: str
    entries_enabled: bool
    restored_free_cents: int
    risk_revision: int | None
    actor: str
    request_fingerprint: str

    def to_facts_json(self) -> str:
        return canonicalize(asdict(self))

    @classmethod
    def from_facts_json(cls, facts_json: str) -> OptionsRunResumedFacts:
        return cls(**json.loads(facts_json))


@dataclass(frozen=True)
class ResumeSpendingSnapshot:
    restored_free_cents: int
    available_cents: int
    risk_revision: int
    observed_at_ms: int


def resume_spending_snapshot(
    repo: ClerkSqliteRepository, binding: BrokerBotBinding, *, envelope: LiveEnvelopeGate | None,
) -> ResumeSpendingSnapshot:
    """The same current-risk and remaining-free calculation for review and commit."""
    if envelope is None or binding.options_bot is None:
        raise BudgetUnavailable("Fresh account evidence is required to restore entry spending.")
    sid = binding.strategy_instance_id
    budget = repo.deployment_budget(sid)
    if budget is None:
        raise BudgetUnavailable("The original Options deployment budget is unavailable.")
    require_admission(repo, strategy_instance_id=sid, session_leg_id=binding.options_bot.setup.legs[0].leg_id)
    observation = require_current_risk_admission(repo, envelope=envelope, now_ms=repo.clock())
    policy = repo.account_risk_policy()
    revision = 0 if policy is None else policy.revision
    if budget["world"] != "synthetic" and (policy is None or observation.risk_revision != revision):
        raise BudgetUnavailable("Account risk evidence changed; review Resume again.")
    projection = repo.account_budget(
        cash=observation.cash_available_usd, seen_before_ms=observation.fills_seen_before_ms,
        modelled_fees_seen_before_ms=observation.modelled_fees_seen_before_ms,
    )
    own = next((item for item in projection.deployments if item.strategy_instance_id == sid), None)
    if own is None or own.active:
        raise BudgetUnavailable("The stopped deployment's lifetime budget cannot be restored.")
    restored = max(0, cents_spendable(own.free))
    if Decimal(restored).scaleb(-2) > projection.available:
        raise BudgetUnavailable("The account cannot reserve this bot's remaining free balance. Resume without entry spending to manage held positions.")
    return ResumeSpendingSnapshot(restored, max(0, cents_spendable(projection.available)), revision, observation.observed_at_ms)


def submit_options_resume(
    repo: ClerkSqliteRepository, binding: BrokerBotBinding, *, envelope: LiveEnvelopeGate | None,
) -> CommandSubmission:
    """Reserve only existing free balance, or explicitly activate reductions alone."""
    from app.services.bot_carryover import configuration_hash

    reject_colon("strategy_instance_id", binding.strategy_instance_id)
    reject_colon("lifecycle_run_id", binding.run_id)
    sid = binding.strategy_instance_id
    if binding.options_bot is None:
        raise BudgetUnavailable("Explicit recovery applies only to an Options bot.")
    budget = repo.deployment_budget(sid)
    prior = repo.latest_run(sid)
    if budget is None or prior is None:
        raise BudgetUnavailable("This Options bot has no original deployment to resume.")
    consent = binding.budget_consent
    if consent is not None and (consent.committed_cents != budget["committed_cents"] or consent.world != budget["world"]):
        raise BudgetUnavailable("Resume keeps this deployment's original budget and custody world.")
    identity = f"{repo.account_id}:{sid}:{binding.run_id}:OPTIONS_RESUME"
    draft = {
        "idempotency_key": identity, "lifecycle_run_id": binding.run_id,
        "configuration_hash": configuration_hash(binding), "entries_enabled": consent is not None,
        "risk_revision": None if consent is None else consent.risk_revision,
        "actor": "owner" if consent is None else consent.actor,
        "request_fingerprint": "manage_existing_positions" if consent is None else consent.request_fingerprint,
    }
    payload_hash = canonical_sha256(draft)

    def build() -> TransitionInput:
        current = repo.latest_run(sid)
        if current is None or repo.active_run(sid) is not None or current.lifecycle_run_id == binding.run_id:
            raise BudgetUnavailable("Resume needs an interrupted, stopped run and a fresh run identity.")
        if repo.budget_authority_version() != 2 or budget["configuration_hash"] != draft["configuration_hash"]:
            raise BudgetUnavailable("The original Options configuration or budget authority changed.")
        registered = repo.bot_config(sid)
        if registered is None or registered.config_hash != draft["configuration_hash"]:
            raise BudgetUnavailable("Resume must use the custody-sealed Options configuration.")
        restored = 0
        if consent is not None:
            spending = resume_spending_snapshot(repo, binding, envelope=envelope)
            if spending.risk_revision != consent.risk_revision:
                raise BudgetUnavailable("Account risk evidence changed; review Resume again.")
            restored = spending.restored_free_cents
        facts = OptionsRunResumedFacts(
            **draft, payload_hash=payload_hash, prior_run_id=current.run_id, restored_free_cents=restored,
        )
        return TransitionInput(
            strategy_instance_id=sid, run_id=f"{sid}:{binding.run_id}", command_id=f"cmd:{identity}",
            transition_kind="OPTIONS_RUN_RESUMED", custody_owner="ACCOUNT_CLERK", execution_authority="ACCOUNT_CLERK",
            operation_state="accepted", clerk_observed_at_ms=repo.clock(), summary_code="OPTIONS_RUN_RESUMED",
            facts_json=facts.to_facts_json(),
        )

    outcome = repo.commit_first_transition(
        command_id=f"cmd:{identity}", idempotency_key=identity, payload_hash=payload_hash, build_transition=build,
    )
    if isinstance(outcome, CommandExistingConflict):
        raise DurableConflictError(outcome.command)
    if isinstance(outcome, CommandExistingSame):
        return CommandSubmission(outcome.command, False)
    assert isinstance(outcome, CommandCreated)
    return CommandSubmission(outcome.command, True)


def fold_options_run_resumed(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    from app.broker.alpaca.clerk.sqlite.folds import _insert_command_row

    facts = OptionsRunResumedFacts.from_facts_json(payload["facts_json"])
    sid = payload["strategy_instance_id"]
    config = conn.execute("SELECT config_json FROM bot_config WHERE strategy_instance_id=?", (sid,)).fetchone()
    if config is None or json.loads(config[0]).get("options_bot") is None:
        raise ValueError("Only a sealed Options bot can resume")
    prior = conn.execute("SELECT state FROM runs WHERE run_id=? AND strategy_instance_id=?", (facts.prior_run_id, sid)).fetchone()
    budget = conn.execute("SELECT * FROM deployment_budgets WHERE strategy_instance_id=?", (sid,)).fetchone()
    if prior is None or prior[0] != "STOPPED" or budget is None or budget["configuration_hash"] != facts.configuration_hash:
        raise ValueError("Options resume must preserve its stopped deployment's identity")
    if conn.execute("SELECT 1 FROM runs WHERE strategy_instance_id=? AND state='ACTIVE'", (sid,)).fetchone():
        raise ValueError("Options resume cannot replace an active run")
    if (budget["active_run_id"] or budget["run_id"]) != facts.prior_run_id:
        raise ValueError("Options resume must continue its latest stopped run")
    if payload["run_id"] != f"{sid}:{facts.lifecycle_run_id}":
        raise ValueError("Options resume must own a fresh lifecycle run")
    conn.execute(
        "INSERT INTO runs(run_id,strategy_instance_id,lifecycle_run_id,state,started_at_ms) VALUES(?,?,?,'ACTIVE',?)",
        (payload["run_id"], sid, facts.lifecycle_run_id, payload["recorded_at_ms"]),
    )
    _insert_command_row(
        conn, command_id=payload["command_id"], authority_generation=payload["authority_generation"],
        idempotency_key=facts.idempotency_key, payload_hash=facts.payload_hash,
        kind="operator_lifecycle", subject_id=f"bot:{sid}", strategy_instance_id=sid, run_id=payload["run_id"],
        action="OPTIONS_RESUME", intended_end_state="ACTIVE", state="accepted", recorded_at_ms=payload["recorded_at_ms"],
    )
    conn.execute(
        "UPDATE deployment_budgets SET active_run_id=?,active_resume_command_id=?,options_entries_enabled=? "
        + (",released_at_ms=NULL,released_cents=NULL,held_cents=NULL " if facts.entries_enabled else "")
        + "WHERE strategy_instance_id=?",
        (payload["run_id"], payload["command_id"], int(facts.entries_enabled), sid),
    )


def fold_options_run_launched(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    from app.broker.alpaca.clerk.sqlite.folds import _attach_command_receipt

    budget = conn.execute("SELECT active_run_id,active_resume_command_id FROM deployment_budgets WHERE strategy_instance_id=?", (payload["strategy_instance_id"],)).fetchone()
    if budget is None or budget[0] != payload["run_id"] or budget[1] != payload["command_id"]:
        raise ValueError("Options launch must match its recorded resume")
    conn.execute("UPDATE commands SET state='succeeded',updated_at_ms=? WHERE command_id=?", (payload["recorded_at_ms"], payload["command_id"]))
    _attach_command_receipt(conn, command_id=payload["command_id"], terminal_state="succeeded", payload=payload)
