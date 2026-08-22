"""Accept and drive a cancel-first, attributed EXIT operation."""

from __future__ import annotations

import hashlib
from dataclasses import replace

from app.broker.alpaca.clerk.sqlite.decision_receipts import AtomicDecisionReceipt
from app.broker.alpaca.clerk.sqlite.exit_resolution import resolve_exit
from app.broker.alpaca.clerk.sqlite.facts import ExitAcceptedFacts
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.idempotency import (
    DurableConflictError,
    reject_colon,
    require_active_run,
    require_owned_entry_order,
    require_strategy_instance,
)
from app.broker.alpaca.clerk.sqlite.models import (
    CommandCreated,
    CommandExistingConflict,
    CommandExistingSame,
    ExitSubmission,
    TransitionInput,
)
from app.broker.alpaca.clerk.sqlite.order_evidence import entry_order_symbol
from app.broker.alpaca.clerk.sqlite.repository import (
    ClerkSqliteRepository,
    OperationClaimError,
)
from app.broker.contract.ports import BrokerTradePort

ACTION_EXIT = "EXIT"


def _exit_identity(
    *, account_id: str, strategy_instance_id: str, decision_id: str, entry_order_ref: str
) -> tuple[str, str, str, str]:
    idempotency_key = f"{strategy_instance_id}:{decision_id}"
    command_id = f"cmd:{idempotency_key}"
    effect_idempotency_key = f"exit:{idempotency_key}"
    payload_hash = hashlib.sha256(
        canonicalize(
            {
                "account_id": account_id,
                "strategy_instance_id": strategy_instance_id,
                "decision_id": decision_id,
                "action": ACTION_EXIT,
                "entry_order_ref": entry_order_ref,
            }
        ).encode("utf-8")
    ).hexdigest()
    return idempotency_key, payload_hash, command_id, effect_idempotency_key


def accept_exit(
    repo: ClerkSqliteRepository,
    *,
    account_id: str,
    strategy_instance_id: str,
    decision_id: str,
    lifecycle_run_id: str,
    entry_order_ref: str,
    decision_receipt: AtomicDecisionReceipt | None = None,
) -> ExitSubmission:
    """Capture one EXIT and every same-strategy/symbol entry before contact."""
    reject_colon("strategy_instance_id", strategy_instance_id)
    reject_colon("decision_id", decision_id)
    reject_colon("lifecycle_run_id", lifecycle_run_id)
    idempotency_key, payload_hash, command_id, effect_idempotency_key = _exit_identity(
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
        decision_id=decision_id,
        entry_order_ref=entry_order_ref,
    )

    def build_transition() -> TransitionInput:
        require_strategy_instance(repo, strategy_instance_id)
        active = require_active_run(repo, strategy_instance_id, lifecycle_run_id)
        target = require_owned_entry_order(
            repo,
            strategy_instance_id=strategy_instance_id,
            entry_order_ref=entry_order_ref,
        )
        symbol = entry_order_symbol(repo, target.order_ref)
        entry_order_refs: list[str] = []
        for candidate in repo.entry_orders_for_strategy(strategy_instance_id):
            if entry_order_symbol(repo, candidate.order_ref) != symbol:
                continue
            require_owned_entry_order(
                repo,
                strategy_instance_id=strategy_instance_id,
                entry_order_ref=candidate.order_ref,
            )
            entry_order_refs.append(candidate.order_ref)

        effect_operation_id = f"effect:{idempotency_key}"
        facts = ExitAcceptedFacts(
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            kind="strategy_decision",
            action=ACTION_EXIT,
            intended_end_state=None,
            effect_idempotency_key=effect_idempotency_key,
            effect_kind="EXIT",
            decision_id=decision_id,
            entry_order_ref=entry_order_ref,
            entry_order_refs=entry_order_refs,
        )
        return TransitionInput(
            strategy_instance_id=strategy_instance_id,
            run_id=active.run_id,
            command_id=command_id,
            effect_operation_id=effect_operation_id,
            order_ref=entry_order_ref,
            transition_kind="EXIT_ACCEPTED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="accepted",
            clerk_observed_at_ms=repo.clock(),
            summary_code="EXIT_ACCEPTED",
            facts_json=facts.to_facts_json(),
        )

    outcome = repo.commit_first_transition(
        command_id=command_id,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        build_transition=build_transition,
        decision_receipt=decision_receipt,
    )
    if isinstance(outcome, CommandExistingConflict):
        raise DurableConflictError(outcome.command)
    if isinstance(outcome, CommandExistingSame):
        return ExitSubmission(
            command=outcome.command,
            effect_operation_id=outcome.command.effect_operation_id,
            entry_order_ref=entry_order_ref,
            reducing_order_ref=_reducing_order_ref(repo, outcome.command.effect_operation_id),
            created=False,
        )
    assert isinstance(outcome, CommandCreated)
    return ExitSubmission(
        command=outcome.command,
        effect_operation_id=outcome.command.effect_operation_id,
        entry_order_ref=entry_order_ref,
        reducing_order_ref=None,
        created=True,
    )


async def submit_exit(
    repo: ClerkSqliteRepository,
    *,
    account_id: str,
    strategy_instance_id: str,
    decision_id: str,
    lifecycle_run_id: str,
    entry_order_ref: str,
    trade: BrokerTradePort,
    decision_receipt: AtomicDecisionReceipt | None = None,
) -> ExitSubmission:
    accepted = accept_exit(
        repo,
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
        decision_id=decision_id,
        lifecycle_run_id=lifecycle_run_id,
        entry_order_ref=entry_order_ref,
        decision_receipt=decision_receipt,
    )
    return await resolve_accepted_exit(repo, accepted=accepted, trade=trade)


async def resolve_accepted_exit(
    repo: ClerkSqliteRepository,
    *,
    accepted: ExitSubmission,
    trade: BrokerTradePort,
) -> ExitSubmission:
    """Drive a previously accepted EXIT outside the intake decision segment."""
    assert accepted.effect_operation_id is not None
    try:
        resolved = await resolve_exit(
            repo,
            effect_operation_id=accepted.effect_operation_id,
            trade=trade,
        )
    except OperationClaimError:
        if accepted.created:
            raise
        # A concurrent attempt already owns the broker-contact claim for this
        # exact durable EXIT. A transport retry returns the existing snapshot;
        # it must not turn idempotency into a 500 or contact the broker again.
        return accepted
    return replace(resolved, created=accepted.created)


def _reducing_order_ref(
    repo: ClerkSqliteRepository, effect_operation_id: str | None
) -> str | None:
    if effect_operation_id is None:
        return None
    reducing = next(
        (
            order
            for order in repo.orders_for_effect_operation(effect_operation_id)
            if order.role == "REDUCING"
        ),
        None,
    )
    return reducing.order_ref if reducing is not None else None


__all__ = ["ExitSubmission", "accept_exit", "resolve_accepted_exit", "resolve_exit", "submit_exit"]
