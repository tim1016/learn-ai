"""Accept and drive a cancel-first, attributed EXIT operation."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
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
    OrderResource,
    TransitionInput,
)
from app.broker.alpaca.clerk.sqlite.order_evidence import (
    entry_never_accepted_durably,
    entry_order_symbol,
)
from app.broker.alpaca.clerk.sqlite.repository import (
    ClerkSqliteRepository,
    OperationClaimError,
)
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    AdmissionBlockedError,
    RefusalClass,
    classify_admission_refusal,
)
from app.broker.contract.ports import BrokerTradePort

logger = logging.getLogger(__name__)

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


def _exit_cancellable_entries(
    repo: ClerkSqliteRepository,
    *,
    strategy_instance_id: str,
    symbol: str,
    target_order_ref: str,
) -> list[OrderResource]:
    """The entries one EXIT must cancel-prove — narrower than every entry.

    ``repo.entry_orders_for_strategy`` stays whole for its other consumers
    (safe flatten, runtime recovery, the stuck-EXIT watchdog): they need full
    custody evidence, and narrowing that shared read would hide orders they
    exist to find. Cancel-prove planning wants only orders that could still be
    working — a sibling already proven never to have reached the broker holds
    no exposure and can never be cancelled, so enumerating it just adds a dead
    order for every pass to prove. The EXIT's own target is kept whatever its
    state: an EXIT with no linked entry has nothing to resolve, and the
    never-accepted branch in ``exit_resolution`` resolves that target correctly.
    """
    return [
        candidate
        for candidate in repo.entry_orders_for_strategy(strategy_instance_id)
        if entry_order_symbol(repo, candidate.order_ref) == symbol
        and (
            candidate.order_ref == target_order_ref
            or not entry_never_accepted_durably(repo, candidate)
        )
    ]


def _accept_exit_capture(
    repo: ClerkSqliteRepository,
    *,
    account_id: str,
    strategy_instance_id: str,
    decision_id: str,
    entry_order_ref: str,
    resolve_run_id: Callable[[OrderResource], str],
    decision_receipt: AtomicDecisionReceipt | None,
) -> ExitSubmission:
    """Capture one EXIT and every same-strategy/symbol entry before contact.

    The run identity is supplied by ``resolve_run_id``: a strategy decision
    binds to the currently ACTIVE run (``accept_exit``); a recovery EXIT binds
    to the run recorded on the targeted entry's effect operation
    (``accept_recovery_exit``), which is why crash/stop-held exposure can be
    driven to flat after ``runtime.recover()`` retired every active run.
    """
    reject_colon("strategy_instance_id", strategy_instance_id)
    reject_colon("decision_id", decision_id)
    idempotency_key, payload_hash, command_id, effect_idempotency_key = _exit_identity(
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
        decision_id=decision_id,
        entry_order_ref=entry_order_ref,
    )

    def build_transition() -> TransitionInput:
        require_strategy_instance(repo, strategy_instance_id)
        target = require_owned_entry_order(
            repo,
            strategy_instance_id=strategy_instance_id,
            entry_order_ref=entry_order_ref,
        )
        run_id = resolve_run_id(target)
        symbol = entry_order_symbol(repo, target.order_ref)
        entry_order_refs: list[str] = []
        for candidate in _exit_cancellable_entries(
            repo,
            strategy_instance_id=strategy_instance_id,
            symbol=symbol,
            target_order_ref=target.order_ref,
        ):
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
            run_id=run_id,
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
    reject_colon("lifecycle_run_id", lifecycle_run_id)

    def resolve_run_id(_target: OrderResource) -> str:
        return require_active_run(repo, strategy_instance_id, lifecycle_run_id).run_id

    return _accept_exit_capture(
        repo,
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
        decision_id=decision_id,
        entry_order_ref=entry_order_ref,
        resolve_run_id=resolve_run_id,
        decision_receipt=decision_receipt,
    )


class RecoveryRunActiveError(Exception):
    """A recovery EXIT that forbids live runs found one at capture time.

    Raised inside the ``build_transition`` closure — i.e. under the
    repository write lock — so a Resume landing between recovery-policy
    recheck and EXIT capture fails closed instead of racing the flatten.
    """

    def __init__(self, strategy_instance_id: str) -> None:
        self.strategy_instance_id = strategy_instance_id
        super().__init__(
            f"strategy instance {strategy_instance_id!r} re-activated a run "
            "before the recovery EXIT was captured"
        )


def accept_recovery_exit(
    repo: ClerkSqliteRepository,
    *,
    account_id: str,
    strategy_instance_id: str,
    decision_id: str,
    entry_order_ref: str,
    forbid_active_run: bool = False,
) -> ExitSubmission:
    """Capture one reduction-only recovery EXIT without the active-run fence.

    ``accept_exit`` requires the caller's ``lifecycle_run_id`` to be the
    currently ACTIVE run (``require_active_run``) — correct for strategy
    decisions, and exactly why crash/stop-held exposure (F18) and a stuck
    EXIT could never be re-driven: after a crash, ``runtime.recover()``
    retires every active run. A recovery EXIT is anchored to the *exposure*,
    not to a live run: its ``run_id`` is the run recorded on the targeted
    entry's effect operation. Admission is owned by the caller (the
    SafeFlattenPlan recheck gates, or the stuck-EXIT watchdog policy) plus
    the downstream ``require_capability(Capability.REDUCE, …)`` in
    ``exit_resolution.py``, which only authorizes movement toward zero.

    ``forbid_active_run=True`` (the safe-flatten executor) additionally
    re-asserts *inside the capture transaction* that no run is ACTIVE —
    recovery-policy already refused presentation with ``RUN_STILL_ACTIVE``,
    but a Resume (approved-carryover resumes are legitimate while exposure
    is held) can land between recheck and capture; the closure runs under
    the repository write lock, so this check cannot race a registration
    commit. The watchdog re-drive keeps the default ``False``: a stuck EXIT
    on a running bot is re-drivable by design.

    Decision-id namespaces: ``recovery-flatten-<hex16>``,
    ``exit-redrive-<episode-hex12>-<n>`` (both colon-free; the idempotency
    key is ``(strategy_instance_id, decision_id)`` only — see
    ``_exit_identity`` — so each namespace must be unique per intent).
    """

    def resolve_run_id(target: OrderResource) -> str:
        if forbid_active_run and repo.active_run(strategy_instance_id) is not None:
            raise RecoveryRunActiveError(strategy_instance_id)
        origin = repo.effect_operation(target.effect_operation_id)
        assert origin is not None, "an owned ENTRY order always has an effect operation"
        assert origin.run_id is not None, "an owned ENTRY effect always records its run"
        return origin.run_id

    return _accept_exit_capture(
        repo,
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
        decision_id=decision_id,
        entry_order_ref=entry_order_ref,
        resolve_run_id=resolve_run_id,
        decision_receipt=None,
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
    """Drive a previously accepted EXIT outside the intake decision segment.

    A TRANSIENT admission refusal (see ``classify_admission_refusal``) is not
    an error for a durably accepted EXIT: the effect stays non-terminal, so
    ``reconcilable_effect_operations`` keeps selecting it and the 15 s sweep
    re-drives it once the refusal self-heals. Returning the accepted snapshot
    here is the F19 fix — the caller (runner or panel) sees an honest
    "accepted, await reconciliation" receipt instead of a crash. TERMINAL
    refusals still raise.
    """
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
    except AdmissionBlockedError as exc:
        if classify_admission_refusal(exc.decision.reason_code) is not RefusalClass.TRANSIENT:
            raise
        logger.warning(
            "Deferred a transient Clerk refusal on an accepted EXIT; the sweep re-drives it",
            extra={
                "action": "exit_transient_refusal_deferred",
                "effect_operation_id": accepted.effect_operation_id,
                "reason_code": exc.decision.reason_code,
            },
        )
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
