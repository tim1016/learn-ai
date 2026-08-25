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
from app.broker.alpaca.clerk.sqlite.projection_models import SafeFlattenPlan
from app.broker.alpaca.clerk.sqlite.reconcile import AccountReconciliationResult
from app.broker.alpaca.clerk.sqlite.recovery_policy import (
    RecoveryActionId,
    RecoveryPolicyContext,
    recheck_recovery_action,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.safe_flatten_execution import (
    SafeFlattenExecutionError,
    SafeFlattenResult,
)


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

    async def execute_safe_flatten(
        self,
        *,
        plan: SafeFlattenPlan,
        reason: str | None = None,
    ) -> SafeFlattenResult: ...


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
            # The durable STOP already committed on a prior attempt. A retry
            # must still re-drive local quiescence: an earlier attempt could
            # have failed after committing the STOP but before the in-process
            # task stopped, leaving it free to keep consuming bars.
            # `_quiesce_bot_process` is idempotent for an already-stopped task.
            await _quiesce_bot_process(strategy_instance_id, reason=request.reason)
            return RecoveryExecutionResult(
                action_id=request.action_id,
                applied=False,
                receipt_id=existing.command_id,
                recorded_at_ms=existing.updated_at_ms,
                command=existing,
            )

    context = await current_context()
    if request.action_id == "resolve_execution_coverage" and request.execution_ref is not None:
        receipt = facade.repository.execution_coverage_resolution_receipt(
            request.execution_ref
        )
        if receipt is not None:
            return RecoveryExecutionResult(
                action_id=request.action_id,
                applied=receipt.applied,
                receipt_id=receipt.receipt_id,
                recorded_at_ms=receipt.recorded_at_ms,
            )
    capability = recheck_recovery_action(
        context,
        action_id=request.action_id,
        concurrency_token=request.concurrency_token,
    )
    if request.action_id == "resolve_execution_coverage":
        if capability.execution_ref is None or request.execution_ref != capability.execution_ref:
            raise RecoveryExecutionError(
                "The selected coverage conflict does not match the presented Clerk action."
            )
        receipt = facade.repository.resolve_execution_coverage_conflict(
            uncertainty_id=capability.execution_ref,
            expected_authority_generation=context.authority_generation,
            expected_db_identity_token=context.db_identity_token,
            expected_control_revision=context.control_revision,
        )
        return RecoveryExecutionResult(
            action_id=request.action_id,
            applied=receipt.applied,
            receipt_id=receipt.receipt_id,
            recorded_at_ms=receipt.recorded_at_ms,
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
        await _quiesce_bot_process(strategy_instance_id, reason=request.reason)
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
    if request.action_id == "execute_safe_flatten":
        if capability.reduction_plan is None:
            raise RecoveryExecutionError(
                "The presented flatten action carries no prepared reduction plan."
            )
        try:
            result = await facade.execute_safe_flatten(
                plan=capability.reduction_plan,
                reason=request.reason,
            )
        except SafeFlattenExecutionError as exc:
            # A rejected reduction (or any other executability failure) surfaces
            # as an honest error, never a silent applied=True while exposure
            # remains (Codex review 2026-08-25 P1).
            raise RecoveryExecutionError(str(exc)) from exc
        # Every captured recovery EXIT is a durably committed reduction: the
        # reducing order is either already at the broker (``orders``) or the
        # reconciliation sweep re-drives it. A transient deferral therefore
        # reports an accepted receipt, not an effect-free rejection (P1). The
        # receipt anchors to a real order ref when one exists, else the durable
        # effect id.
        receipt_id = (
            result.orders[0].order_ref
            if result.orders
            else result.accepted_effect_operation_ids[0]
        )
        return RecoveryExecutionResult(
            action_id=request.action_id,
            applied=True,
            receipt_id=receipt_id,
            recorded_at_ms=result.recorded_at_ms,
            orders=result.orders,
        )
    raise RecoveryExecutionError(
        f"{request.action_id} is navigation, preparation, or offline authority recovery; "
        "it has no direct broker mutation"
    )


async def _quiesce_bot_process(strategy_instance_id: str, *, reason: str | None) -> None:
    """Stop the in-process bot task after its durable SQLite STOP commits.

    `stop_bot_decisions` durably records the Clerk-side STOP first (above),
    but that alone leaves a running `BotTaskRegistry` task free to keep
    consuming bars and calling `execute_for_instance` — the process only
    reacts to `DesiredState`, which this command never touched. Route
    through the registry's own serialized stop boundary so the durable
    intent and process termination land together, matching the Button
    Rule's cancel + reap contract (`BotTaskRegistry.stop`).

    A bot with no live task in this process (already stopped, running in a
    different process, or never started here) is not an error: the durable
    SQLite STOP above is already the authority evidence in that case.
    """
    from app.services.bot_runner import get_bot_task_registry
    from app.services.bot_runner_errors import UnknownBotError

    registry = get_bot_task_registry()
    if registry is None:
        return
    try:
        await registry.stop_after_durable_clerk_stop(
            "alpaca",
            strategy_instance_id,
            updated_by="operator_recovery",
            reason=reason or "sqlite_recovery_stop_bot_decisions",
        )
    except UnknownBotError:
        return


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
