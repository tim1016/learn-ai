"""Execute presented SQLite recovery capabilities through durable custody.

This dispatcher does not author policy.  It re-runs ``recheck_recovery_action``
immediately before calling the active SQLite facade, so preview and execution
cannot drift into separate guard implementations.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from app.broker.alpaca.clerk.program_leg import LegRefusal
from app.broker.alpaca.clerk.recovery_reduction import ConfirmedRecoveryLimit
from app.broker.alpaca.clerk.sqlite.commands import CommandSubmission, stop_command_resource
from app.broker.alpaca.clerk.sqlite.models import CommandResource, OrderResource, RunResource
from app.broker.alpaca.clerk.sqlite.projection_models import SafeFlattenPlan
from app.broker.alpaca.clerk.sqlite.reconcile import AccountReconciliationResult
from app.broker.alpaca.clerk.sqlite.recovery_policy import (
    RecoveryActionId,
    RecoveryPolicyContext,
    recheck_recovery_action,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.residue_discharge import (
    ResidueDischargeReceipt,
    ResidueDischargeRefused,
)
from app.broker.alpaca.clerk.sqlite.safe_flatten_execution import (
    SafeFlattenExecutionError,
    SafeFlattenResult,
)
from app.broker.contract.errors import BrokerError

logger = logging.getLogger(__name__)

# The process stop's reason when the operator gave `stop_bot_decisions` none.
_RECOVERY_STOP_REASON = "sqlite_recovery_stop_bot_decisions"


class RecoveryExecutionError(Exception):
    """A presented capability has no live mutation dispatcher, or refused to run.

    ``refusal`` is the typed reason when the reduction's shape refused it
    (#2007): its code and, for a clock refusal, when it becomes possible.
    """

    def __init__(self, message: str, *, refusal: LegRefusal | None = None) -> None:
        super().__init__(message)
        self.refusal = refusal


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
        confirmed_limit: ConfirmedRecoveryLimit | None = None,
    ) -> SafeFlattenResult: ...

    async def discharge_attributed_residue(
        self,
        *,
        strategy_instance_id: str,
        symbol: str,
        reason: str | None,
    ) -> ResidueDischargeReceipt: ...


@dataclass(frozen=True)
class RecoveryExecutionRequest:
    action_id: RecoveryActionId
    concurrency_token: str
    execution_ref: str | None
    reason: str | None
    # The operator's confirmed extended-hours limit for ``execute_safe_flatten``
    # (#2007); ``None`` everywhere else and inside the regular session.
    confirmed_limit: ConfirmedRecoveryLimit | None = None


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
            # The durable STOP already committed on a prior attempt, so the
            # capability presented for it is spent. A retry still redoes the
            # whole Stop, replaying that STOP: an earlier attempt could have
            # failed after committing the STOP but before the in-process task
            # stopped, leaving it free to keep consuming bars.
            return await _stop_bot_decisions(
                facade, request, strategy_instance_id=strategy_instance_id, lifecycle_run_id=request.execution_ref
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
        return await _stop_bot_decisions(
            facade, request, strategy_instance_id=strategy_instance_id, lifecycle_run_id=capability.execution_ref
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
                confirmed_limit=request.confirmed_limit,
            )
        except SafeFlattenExecutionError as exc:
            # A rejected reduction (or any other executability failure) surfaces
            # as an honest error, never a silent applied=True while exposure
            # remains (Codex review 2026-08-25 P1).
            raise RecoveryExecutionError(str(exc), refusal=exc.refusal) from exc
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
    if request.action_id == "discharge_attributed_residue":
        strategy_instance_id = _require_strategy_instance(context)
        if capability.execution_ref is None or request.execution_ref != capability.execution_ref:
            raise RecoveryExecutionError(
                "The residue does not match the symbol authorized by the presented action."
            )
        try:
            receipt = await facade.discharge_attributed_residue(
                strategy_instance_id=strategy_instance_id,
                symbol=capability.execution_ref,
                reason=request.reason,
            )
        except ResidueDischargeRefused as exc:
            raise RecoveryExecutionError(str(exc)) from exc
        except BrokerError as exc:
            raise RecoveryExecutionError(
                f"The broker could not be read, so nothing was discharged: {exc}"
            ) from exc
        return RecoveryExecutionResult(
            action_id=request.action_id,
            applied=True,
            receipt_id=f"transition:{receipt.sequence}",
            recorded_at_ms=receipt.recorded_at_ms,
        )
    raise RecoveryExecutionError(
        f"{request.action_id} is navigation, preparation, or offline authority recovery; "
        "it has no direct broker mutation"
    )


async def _stop_bot_decisions(
    facade: ActiveSqliteRecoveryFacade,
    request: RecoveryExecutionRequest,
    *,
    strategy_instance_id: str,
    lifecycle_run_id: str,
) -> RecoveryExecutionResult:
    """The panel's Stop: an operator's Stop of the run."""
    submission = await operator_stop_run(
        facade,
        strategy_instance_id=strategy_instance_id,
        lifecycle_run_id=lifecycle_run_id,
        operator_reason=request.reason,
        updated_by="operator_recovery",
        reason=request.reason or _RECOVERY_STOP_REASON,
    )
    return RecoveryExecutionResult(
        action_id=request.action_id,
        applied=submission.created,
        receipt_id=submission.command.command_id,
        recorded_at_ms=submission.command.updated_at_ms,
        command=submission.command,
    )


async def operator_stop_run(
    facade: ActiveSqliteRecoveryFacade,
    *,
    strategy_instance_id: str,
    lifecycle_run_id: str,
    operator_reason: str | None,
    updated_by: str,
    reason: str,
) -> CommandSubmission:
    """An operator's Stop of run ``lifecycle_run_id``: cancel the bot's end, commit the STOP, stop the process.

    The one sequence the panel's Stop (``stop_bot_decisions``) and the raw
    ``runs/stop`` route (#2664) run. Its STOP, recording ``operator_reason``,
    commits through the account authority's facade (``stop_strategy_run``):
    under its intake, and keyed with the account the authority stores, as
    every other STOP of the run is -- the sweep's, a restart's, the Clerk's at
    the end. Never with a route's spelling of the account, which a route admits
    in lowercase and, under Shadow, as the plain live account: such a key
    misses the run's STOP. ``reason`` is the one the process stop records. The
    owner's Stop sells nothing at the end time (#2607): the end is cancelled
    before the STOP commits, so nothing that runs between the STOP and the
    process stop -- the runner's end watch, a Clerk pass -- reads it as still
    to be carried out, and the process stop records the rest of the Stop, its
    STOPPED intent. Should the STOP fail, the bot runs on with no end: the end
    is the owner's, and they asked to Stop -- they are told the Stop failed,
    and the bot runs until they Stop it again. Nothing restores the end: a
    restored SELL end would sell what the owner meant to keep.

    Only a Stop of the bot's current run -- its ACTIVE run, else its latest --
    is an operator's Stop of the bot: whether that run is live, died in a
    crash the sweep or a restart then stopped, or was stopped by the Clerk at
    its end, the end is cancelled and the process stopped. A run is stopped
    once, under its first STOP's reason, so a retry -- after a lost response,
    or after the process stop failed -- finds the STOP already committed, and
    the Stop is redone whole; each step is idempotent. A Stop naming an
    earlier run (a retry landing after a redeploy) or a run the bot never had
    touches neither end nor process: its STOP alone is replayed or refused. A
    sale the Clerk already accepted for the end is that sale's EXIT's, and no
    Stop calls it off (#2666).
    """
    current = await asyncio.to_thread(_current_run, facade.repository, strategy_instance_id)
    stops_the_bot = current is not None and current.lifecycle_run_id == lifecycle_run_id
    if stops_the_bot:
        await _cancel_bot_end(strategy_instance_id, lifecycle_run_id, updated_by=updated_by)
    submission = await facade.stop_strategy_run(
        strategy_instance_id=strategy_instance_id, run_id=lifecycle_run_id, reason=operator_reason,
    )
    if stops_the_bot:
        await _quiesce_bot_process(strategy_instance_id, lifecycle_run_id, updated_by=updated_by, reason=reason)
    return submission


def _current_run(repo: ClerkSqliteRepository, strategy_instance_id: str) -> RunResource | None:
    """The bot's current run: its ACTIVE run, else its latest.

    ``latest_run`` alone is not enough: runs started in the same millisecond
    tie-break on ``run_id``, which need not name the ACTIVE one.
    """
    return repo.active_run(strategy_instance_id) or repo.latest_run(strategy_instance_id)


async def _cancel_bot_end(strategy_instance_id: str, lifecycle_run_id: str, *, updated_by: str) -> None:
    """The Stop's first step: cancel the bot's scheduled end, before its STOP commits (#2607).

    Through the registry that keeps the end (``BotTaskRegistry.cancel_end``),
    whether or not it runs the bot's process. A process with no registry
    installs no end schedule either (``scheduled_end.install_bot_end_schedule``),
    so no end is carried out here; the Stop goes on, and that is said -- the
    Clerk's routes and the runner are installed together, so a Stop that
    finds no runner is a process set up wrong.
    """
    from app.services.bot_runner import get_bot_task_registry

    registry = get_bot_task_registry()
    if registry is None:
        logger.error(
            "A Stop found no bot runner in this process, so it cancelled no end. This process installs no end "
            "schedule and carries out no end; the Stop went on",
            extra={"action": "bot_end_cancel_no_runner", "strategy_instance_id": strategy_instance_id},
        )
        return
    await registry.cancel_end(strategy_instance_id, lifecycle_run_id=lifecycle_run_id, updated_by=updated_by)


async def _quiesce_bot_process(
    strategy_instance_id: str, lifecycle_run_id: str, *, updated_by: str, reason: str
) -> None:
    """The Stop's last step: stop the in-process bot task after its durable SQLite STOP commits.

    The Clerk-side STOP alone leaves a running `BotTaskRegistry` task free to
    keep consuming bars and calling `execute_for_instance` — the process only
    reacts to `DesiredState`, which the STOP never touched. Route through the
    registry's own serialized stop boundary so the durable intent and process
    termination land together, matching the Button Rule's cancel + reap
    contract (`BotTaskRegistry.stop`).

    A bot with no live task of this run in this process (already stopped,
    running in a different process, never started here, or running a later
    run) is not an error: the durable SQLite STOP is already the authority
    evidence in that case.
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
            lifecycle_run_id=lifecycle_run_id,
            updated_by=updated_by,
            reason=reason,
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
