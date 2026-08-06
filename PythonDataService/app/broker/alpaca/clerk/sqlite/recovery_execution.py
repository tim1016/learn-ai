"""Execute presented SQLite recovery capabilities through durable custody.

This dispatcher does not author policy.  It re-runs ``recheck_recovery_action``
immediately before calling the active SQLite facade, so preview and execution
cannot drift into separate guard implementations.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from app.broker.alpaca.clerk.sqlite.commands import CommandSubmission, stop_command_resource
from app.broker.alpaca.clerk.sqlite.models import CommandResource, OrderResource
from app.broker.alpaca.clerk.sqlite.reconcile import AccountReconciliationResult
from app.broker.alpaca.clerk.sqlite.recovery_policy import (
    RecoveryActionId,
    RecoveryPolicyContext,
    recheck_recovery_action,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository


class RecoveryExecutionError(Exception):
    """A presented capability has no live mutation dispatcher."""


class ActiveSqliteRecoveryFacade(Protocol):
    @property
    def account_id(self) -> str: ...

    @property
    def repository(self) -> ClerkSqliteRepository: ...

    async def stop_strategy_run(
        self,
        *,
        strategy_instance_id: str,
        run_id: str,
        reason: str | None = None,
    ) -> CommandSubmission: ...

    async def reconcile_account(
        self,
        *,
        trigger: str,
    ) -> AccountReconciliationResult: ...

    async def cancel_verified_working_orders(
        self,
        *,
        strategy_instance_id: str | None,
        order_refs: tuple[str, ...],
    ) -> tuple[OrderResource, ...]: ...


@dataclass(frozen=True)
class RecoveryExecutionRequest:
    action_id: RecoveryActionId
    concurrency_token: str
    execution_ref: str | None
    reason: str | None


@dataclass(frozen=True)
class RecoveryExecutionResult:
    action_id: RecoveryActionId
    applied: bool
    receipt_id: str
    recorded_at_ms: int
    command: CommandResource | None = None
    reconciliation: AccountReconciliationResult | None = None
    orders: tuple[OrderResource, ...] = ()


async def execute_recovery_action(
    facade: ActiveSqliteRecoveryFacade,
    *,
    request: RecoveryExecutionRequest,
    current_context: Callable[[], Awaitable[RecoveryPolicyContext]],
) -> RecoveryExecutionResult:
    """Recheck policy, then dispatch exactly one typed in-authority action."""
    if (
        request.action_id == "stop_bot_decisions"
        and request.execution_ref is not None
    ):
        strategy_instance_id = _require_strategy_instance(await current_context())
        existing = stop_command_resource(
            facade.repository,
            account_id=facade.account_id,
            strategy_instance_id=strategy_instance_id,
            lifecycle_run_id=request.execution_ref,
        )
        if existing is not None:
            return RecoveryExecutionResult(
                action_id=request.action_id,
                applied=False,
                receipt_id=existing.command_id,
                recorded_at_ms=existing.updated_at_ms,
                command=existing,
            )

    context = await current_context()
    capability = recheck_recovery_action(
        context,
        action_id=request.action_id,
        concurrency_token=request.concurrency_token,
    )
    if request.action_id == "stop_bot_decisions":
        strategy_instance_id = _require_strategy_instance(context)
        if capability.execution_ref is None or request.execution_ref != capability.execution_ref:
            raise RecoveryExecutionError(
                "The stop target does not match the run authorized by the presented action."
            )
        submission = await facade.stop_strategy_run(
            strategy_instance_id=strategy_instance_id,
            run_id=capability.execution_ref,
            reason=request.reason,
        )
        return RecoveryExecutionResult(
            action_id=request.action_id,
            applied=submission.created,
            receipt_id=submission.command.command_id,
            recorded_at_ms=submission.command.updated_at_ms,
            command=submission.command,
        )
    if request.action_id == "reconcile_now":
        reconciliation = await facade.reconcile_account(
            trigger="OPERATOR_RECONCILE_NOW"
        )
        return RecoveryExecutionResult(
            action_id=request.action_id,
            applied=True,
            receipt_id=_require_reconciliation_receipt(reconciliation),
            recorded_at_ms=_require_reconciliation_recorded_at(reconciliation),
            reconciliation=reconciliation,
        )
    if request.action_id == "cancel_verified_working_orders":
        order_refs = tuple(
            evidence.reference.removeprefix("order:")
            for evidence in capability.evidence
            if evidence.reference.startswith("order:")
        )
        if not order_refs:
            raise RecoveryExecutionError("No exact working-order identities were authorized")
        orders = await facade.cancel_verified_working_orders(
            strategy_instance_id=context.strategy_instance_id,
            order_refs=order_refs,
        )
        return RecoveryExecutionResult(
            action_id=request.action_id,
            applied=True,
            receipt_id=orders[0].order_ref,
            recorded_at_ms=max(order.updated_at_ms for order in orders),
            orders=orders,
        )
    raise RecoveryExecutionError(
        f"{request.action_id} is navigation, preparation, or offline authority recovery; "
        "it has no direct broker mutation"
    )


def _require_strategy_instance(context: RecoveryPolicyContext) -> str:
    if context.strategy_instance_id is None:
        raise RecoveryExecutionError("Stop bot decisions requires a bot-scoped action")
    return context.strategy_instance_id


def _require_reconciliation_receipt(result: AccountReconciliationResult) -> str:
    if result.receipt_id is None:
        raise RecoveryExecutionError(
            "Operator reconciliation completed without a durable receipt identity"
        )
    return result.receipt_id


def _require_reconciliation_recorded_at(result: AccountReconciliationResult) -> int:
    if result.recorded_at_ms is None:
        raise RecoveryExecutionError(
            "Operator reconciliation completed without a durable receipt clock"
        )
    return result.recorded_at_ms
