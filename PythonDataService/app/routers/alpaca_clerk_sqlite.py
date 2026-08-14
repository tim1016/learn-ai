"""Control and read endpoints for the activation-selected SQLite Alpaca Clerk.

Every handler resolves only the boot-selected, activation-verified authority.
Legacy accounts receive a typed conflict; activated authorities that failed
startup expose fail-closed account recovery state without installing a broker
mutation capability.

Every repository call is dispatched via ``asyncio.to_thread`` — the
repository is synchronous blocking I/O (SQLite + fsync), and calling it
directly from an ``async def`` handler would stall the FastAPI event loop
for every other in-flight request for as long as the write takes
(open-pr-review-2026-08-05.md P2 "Synchronous SQLite/fsync blocks the
FastAPI event loop").
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TypeVar

from fastapi import APIRouter, HTTPException, Query

from app.broker.alpaca.clerk.active_authority import get_active_clerk_runtime
from app.broker.alpaca.clerk.sqlite.commands import (
    DurableConflictError,
    InvalidIdentityError,
    NoActiveRunError,
    UnknownStrategyInstanceError,
    submit_start_run,
    submit_stop_run,
)
from app.broker.alpaca.clerk.sqlite.historical_execution_recovery import (
    HistoricalExecutionRecoveryPlan,
    HistoricalExecutionRecoveryRefused,
)
from app.broker.alpaca.clerk.sqlite.projections import (
    InvalidTimelineCursor,
    ProjectionReadError,
    SqliteClerkProjectionReader,
)
from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
from app.broker.alpaca.clerk.sqlite.recovery_execution import (
    RecoveryExecutionError,
    RecoveryExecutionRequest,
    execute_recovery_action,
)
from app.broker.alpaca.clerk.sqlite.recovery_policy import (
    RecoveryActionUnavailableError,
    StaleRecoveryTokenError,
    recheck_recovery_action,
)
from app.broker.alpaca.clerk.sqlite.repository import (
    ClerkSqliteRepository,
    ExecutionLeaseLost,
    RepositoryPoisoned,
)
from app.broker.alpaca.clerk.sqlite.repository_execution_coverage_api import (
    ExecutionCoverageResolutionUnavailable,
)
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.contract.errors import BrokerError, UnknownBrokerError
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort
from app.broker.contract.registry import get_broker_registry
from app.schemas.alpaca_clerk_sqlite import (
    ClerkProjectionResponse,
    CommandResponse,
    HistoricalExecutionRecoveryConfirmRequest,
    HistoricalExecutionRecoveryPlanResponse,
    HistoricalExecutionRecoveryPrepareRequest,
    HistoricalExecutionRecoveryReceiptResponse,
    ReconciliationResponse,
    RecoveryActionCheckRequest,
    RecoveryActionCheckResponse,
    RecoveryActionExecuteRequest,
    RecoveryActionExecuteResponse,
    RecoveryCapabilityResponse,
    StartRunRequest,
    StopRunRequest,
    TimelinePageResponse,
)
from app.services.sqlite_clerk_compat import failed_sqlite_projection

router = APIRouter(prefix="/api/alpaca-clerk-sqlite", tags=["alpaca-clerk-sqlite"])
ReadResult = TypeVar("ReadResult")
_MAX_TIMELINE_CURSOR_LENGTH = 4_096
_HISTORICAL_EXECUTION_RECOVERY_RESPONSES = {
    404: {"description": "The selected SQLite authority or bot is unavailable."},
    409: {"description": "The signed plan or exact-evidence proof is no longer safe to apply."},
    503: {"description": "SQLite projection or Alpaca evidence is temporarily unavailable."},
}


async def _repo(account_id: str) -> ClerkSqliteRepository:
    """Resolve only the boot-selected, activation-verified SQLite authority."""
    return _active_sqlite_facade(account_id).repository


def _active_sqlite_facade(account_id: str) -> SqliteAlpacaClerkFacade:
    """Return the one boot-selected SQLite facade or fail closed."""
    runtime = get_active_clerk_runtime()
    if runtime is None:
        raise HTTPException(
            status_code=503,
            detail={
                "reason": "active_clerk_not_started",
                "message": "The active Alpaca Clerk has not completed boot selection.",
            },
        )
    if runtime.authority_kind == "legacy":
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "sqlite_authority_inactive",
                "message": "This account is still governed by the legacy Clerk authority.",
            },
        )
    if runtime.authority_kind != "sqlite" or not isinstance(
        runtime.clerk, SqliteAlpacaClerkFacade
    ):
        failure = runtime.startup_failure
        raise HTTPException(
            status_code=503,
            detail={
                "reason": (
                    failure.reason_code.lower()
                    if failure is not None
                    else "sqlite_authority_unavailable"
                ),
                "message": (
                    failure.recovery
                    if failure is not None
                    else "No verified SQLite Clerk authority is installed."
                ),
            },
        )
    if runtime.clerk.account_id != account_id:
        raise HTTPException(
            status_code=404,
            detail={"reason": "sqlite_account_not_active"},
        )
    return runtime.clerk


def _conflict_response(exc: DurableConflictError) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "reason": "durable_conflict",
            "message": (
                "A command with this identity was already accepted with a different payload."
            ),
            "existing_command": CommandResponse.from_resource(exc.command).model_dump(),
        },
    )


def _unavailable_response(exc: ExecutionLeaseLost | RepositoryPoisoned) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={"reason": "sqlite_authority_unavailable", "message": str(exc)},
    )


def _unknown_bot_response(exc: UnknownStrategyInstanceError) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "reason": "unknown_strategy_instance",
            "message": f"No bot {exc.strategy_instance_id!r} is registered on this account.",
        },
    )


def _read_projection(  # noqa: UP047 - Python 3.11 runtime; PEP 695 needs 3.12.
    repo: ClerkSqliteRepository,
    read: Callable[[SqliteClerkProjectionReader], ReadResult],
) -> ReadResult:
    reader = SqliteClerkProjectionReader.from_repository(repo)
    try:
        return read(reader)
    finally:
        reader.close()


def _projection_read_error(exc: ProjectionReadError) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "reason": "sqlite_projection_unavailable",
            "message": str(exc),
        },
    )


def _historical_recovery_refusal(exc: HistoricalExecutionRecoveryRefused) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "reason": exc.reason.lower(),
            "message": exc.message,
            "next_step": exc.next_step,
        },
    )


@router.post(
    "/accounts/{account_id}/bots/{strategy_instance_id}/runs/start",
    response_model=CommandResponse,
    status_code=202,
)
async def start_run(
    account_id: str, strategy_instance_id: str, body: StartRunRequest
) -> CommandResponse:
    """Reserve and admit a Start command. Idempotent on
    ``(account_id, strategy_instance_id, lifecycle_run_id)`` — the frontend
    mints ``lifecycle_run_id`` once and resends the same value on retry."""
    repo = await _repo(account_id)
    try:
        submission = await asyncio.to_thread(
            submit_start_run,
            repo,
            account_id=account_id,
            strategy_instance_id=strategy_instance_id,
            lifecycle_run_id=body.lifecycle_run_id,
            operator_reason=body.operator_reason,
        )
    except DurableConflictError as exc:
        raise _conflict_response(exc) from exc
    except InvalidIdentityError as exc:
        raise HTTPException(
            status_code=400, detail={"reason": "invalid_identity", "message": str(exc)}
        ) from exc
    except UnknownStrategyInstanceError as exc:
        raise _unknown_bot_response(exc) from exc
    except (ExecutionLeaseLost, RepositoryPoisoned) as exc:
        raise _unavailable_response(exc) from exc
    return CommandResponse.from_resource(submission.command)


@router.post(
    "/accounts/{account_id}/bots/{strategy_instance_id}/runs/stop",
    response_model=CommandResponse,
    status_code=202,
)
async def stop_run(
    account_id: str, strategy_instance_id: str, body: StopRunRequest
) -> CommandResponse:
    """Reserve and admit a Stop command for ``body.lifecycle_run_id`` —
    caller-supplied, exactly like Start (corrective foundation slice)."""
    repo = await _repo(account_id)
    try:
        submission = await asyncio.to_thread(
            submit_stop_run,
            repo,
            account_id=account_id,
            strategy_instance_id=strategy_instance_id,
            lifecycle_run_id=body.lifecycle_run_id,
            operator_reason=body.operator_reason,
        )
    except NoActiveRunError as exc:
        raise HTTPException(
            status_code=404,
            detail={"reason": "no_active_run", "message": str(exc)},
        ) from exc
    except DurableConflictError as exc:
        raise _conflict_response(exc) from exc
    except InvalidIdentityError as exc:
        raise HTTPException(
            status_code=400, detail={"reason": "invalid_identity", "message": str(exc)}
        ) from exc
    except UnknownStrategyInstanceError as exc:
        raise _unknown_bot_response(exc) from exc
    except (ExecutionLeaseLost, RepositoryPoisoned) as exc:
        raise _unavailable_response(exc) from exc
    return CommandResponse.from_resource(submission.command)


@router.get(
    "/accounts/{account_id}/commands/{command_id}",
    response_model=CommandResponse,
)
async def get_command(account_id: str, command_id: str) -> CommandResponse:
    repo = await _repo(account_id)
    resource = await asyncio.to_thread(repo.get_command, command_id)
    if resource is None:
        raise HTTPException(status_code=404, detail={"reason": "command_not_found"})
    return CommandResponse.from_resource(resource)


@router.get(
    "/accounts/{account_id}/snapshot",
    response_model=ClerkProjectionResponse,
)
async def get_account_snapshot(account_id: str) -> ClerkProjectionResponse:
    """Return the account-wide fold projection; never replay transition history."""
    failed = failed_sqlite_projection(
        account_id=account_id,
        strategy_instance_id=None,
    )
    if failed is not None:
        return ClerkProjectionResponse.from_projection(failed)
    repo = await _repo(account_id)
    try:
        projection = await asyncio.to_thread(
            _read_projection,
            repo,
            lambda reader: reader.account_snapshot(),
        )
    except ProjectionReadError as exc:
        raise _projection_read_error(exc) from exc
    return ClerkProjectionResponse.from_projection(projection)


@router.get(
    "/accounts/{account_id}/bots/{strategy_instance_id}/snapshot",
    response_model=ClerkProjectionResponse,
)
async def get_bot_snapshot(
    account_id: str,
    strategy_instance_id: str,
) -> ClerkProjectionResponse:
    """Return one bot's fold projection plus account-wide fail-closed facts."""
    failed = failed_sqlite_projection(
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
    )
    if failed is not None:
        return ClerkProjectionResponse.from_projection(failed)
    repo = await _repo(account_id)
    try:
        projection = await asyncio.to_thread(
            _read_projection,
            repo,
            lambda reader: reader.bot_snapshot(strategy_instance_id),
        )
    except ProjectionReadError as exc:
        raise _projection_read_error(exc) from exc
    if projection is None:
        raise HTTPException(
            status_code=404,
            detail={"reason": "unknown_strategy_instance"},
        )
    return ClerkProjectionResponse.from_projection(projection)


async def _timeline(
    account_id: str,
    *,
    strategy_instance_id: str | None,
    order_ref: str | None,
    effect_operation_id: str | None,
    uncertainty_id: str | None,
    execution_id: str | None,
    transition_kind: str | None,
    sequence: int | None,
    cursor: str | None,
    page_size: int,
) -> TimelinePageResponse:
    repo = await _repo(account_id)
    try:
        page = await asyncio.to_thread(
            _read_projection,
            repo,
            lambda reader: reader.timeline_page(
                strategy_instance_id=strategy_instance_id,
                order_ref=order_ref,
                effect_operation_id=effect_operation_id,
                uncertainty_id=uncertainty_id,
                execution_id=execution_id,
                transition_kind=transition_kind,
                sequence=sequence,
                cursor=cursor,
                page_size=page_size,
            ),
        )
    except InvalidTimelineCursor as exc:
        raise HTTPException(
            status_code=400,
            detail={"reason": "invalid_timeline_cursor", "message": str(exc)},
        ) from exc
    except ProjectionReadError as exc:
        raise _projection_read_error(exc) from exc
    return TimelinePageResponse.from_page(page)


@router.get(
    "/accounts/{account_id}/timeline",
    response_model=TimelinePageResponse,
)
async def get_account_timeline(
    account_id: str,
    cursor: str | None = Query(default=None, max_length=_MAX_TIMELINE_CURSOR_LENGTH),
    page_size: int = Query(default=25, ge=1, le=100),
    strategy_instance_id: str | None = Query(default=None, min_length=1, max_length=256),
    order_ref: str | None = Query(default=None, min_length=1, max_length=512),
    effect_operation_id: str | None = Query(default=None, min_length=1, max_length=512),
    uncertainty_id: str | None = Query(default=None, min_length=1, max_length=256),
    execution_id: str | None = Query(default=None, min_length=1, max_length=256),
    transition_kind: str | None = Query(default=None, min_length=1, max_length=128),
    sequence: int | None = Query(default=None, ge=1),
) -> TimelinePageResponse:
    return await _timeline(
        account_id,
        strategy_instance_id=strategy_instance_id,
        order_ref=order_ref,
        effect_operation_id=effect_operation_id,
        uncertainty_id=uncertainty_id,
        execution_id=execution_id,
        transition_kind=transition_kind,
        sequence=sequence,
        cursor=cursor,
        page_size=page_size,
    )


@router.get(
    "/accounts/{account_id}/bots/{strategy_instance_id}/timeline",
    response_model=TimelinePageResponse,
)
async def get_bot_timeline(
    account_id: str,
    strategy_instance_id: str,
    cursor: str | None = Query(default=None, max_length=_MAX_TIMELINE_CURSOR_LENGTH),
    page_size: int = Query(default=25, ge=1, le=100),
    order_ref: str | None = Query(default=None, min_length=1, max_length=512),
    effect_operation_id: str | None = Query(default=None, min_length=1, max_length=512),
    uncertainty_id: str | None = Query(default=None, min_length=1, max_length=256),
    execution_id: str | None = Query(default=None, min_length=1, max_length=256),
    transition_kind: str | None = Query(default=None, min_length=1, max_length=128),
    sequence: int | None = Query(default=None, ge=1),
) -> TimelinePageResponse:
    return await _timeline(
        account_id,
        strategy_instance_id=strategy_instance_id,
        order_ref=order_ref,
        effect_operation_id=effect_operation_id,
        uncertainty_id=uncertainty_id,
        execution_id=execution_id,
        transition_kind=transition_kind,
        sequence=sequence,
        cursor=cursor,
        page_size=page_size,
    )


async def _check_recovery_action(
    account_id: str,
    *,
    strategy_instance_id: str | None,
    body: RecoveryActionCheckRequest,
) -> RecoveryActionCheckResponse:
    """Re-evaluate the exact presentation policy without performing an effect.

    Mutation executors call the same ``recheck_recovery_action`` immediately
    before their durable command/effect path.  This transport seam lets the
    existing confirmation component refresh evidence before confirmation.
    """
    repo = await _repo(account_id)
    try:
        context = await asyncio.to_thread(
            _read_projection,
            repo,
            lambda reader: reader.recovery_context(
                strategy_instance_id=strategy_instance_id,
            ),
        )
    except ProjectionReadError as exc:
        raise _projection_read_error(exc) from exc
    if context is None:
        raise HTTPException(
            status_code=404,
            detail={"reason": "unknown_strategy_instance"},
        )
    try:
        capability = recheck_recovery_action(
            context,
            action_id=body.action_id,
            concurrency_token=body.concurrency_token,
        )
    except StaleRecoveryTokenError as exc:
        raise HTTPException(
            status_code=409,
            detail={"reason": "stale_action_token", "message": str(exc)},
        ) from exc
    except RecoveryActionUnavailableError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "recovery_action_unavailable",
                "message": str(exc),
                "capability": RecoveryCapabilityResponse.model_validate(
                    exc.capability
                ).model_dump(mode="json"),
            },
        ) from exc
    return RecoveryActionCheckResponse(
        capability=RecoveryCapabilityResponse.model_validate(capability)
    )


@router.post(
    "/accounts/{account_id}/recovery-actions/check",
    response_model=RecoveryActionCheckResponse,
)
async def check_account_recovery_action(
    account_id: str,
    body: RecoveryActionCheckRequest,
) -> RecoveryActionCheckResponse:
    return await _check_recovery_action(
        account_id,
        strategy_instance_id=None,
        body=body,
    )


@router.post(
    "/accounts/{account_id}/bots/{strategy_instance_id}/recovery-actions/check",
    response_model=RecoveryActionCheckResponse,
)
async def check_bot_recovery_action(
    account_id: str,
    strategy_instance_id: str,
    body: RecoveryActionCheckRequest,
) -> RecoveryActionCheckResponse:
    return await _check_recovery_action(
        account_id,
        strategy_instance_id=strategy_instance_id,
        body=body,
    )


@router.post(
    "/accounts/{account_id}/bots/{strategy_instance_id}/historical-execution-recovery/prepare",
    response_model=HistoricalExecutionRecoveryPlanResponse,
    responses=_HISTORICAL_EXECUTION_RECOVERY_RESPONSES,
)
async def prepare_bot_historical_execution_recovery(
    account_id: str,
    strategy_instance_id: str,
    body: HistoricalExecutionRecoveryPrepareRequest,
) -> HistoricalExecutionRecoveryPlanResponse:
    """Read one exact Alpaca paper activity and bind it into a no-write plan."""
    facade = _active_sqlite_facade(account_id)
    try:
        context = await asyncio.to_thread(
            _read_projection,
            facade.repository,
            lambda reader: reader.recovery_context(
                strategy_instance_id=strategy_instance_id,
            ),
        )
        if context is None:
            raise HTTPException(
                status_code=404,
                detail={"reason": "unknown_strategy_instance"},
            )
        plan = await facade.prepare_historical_execution_recovery(
            context=context,
            concurrency_token=body.concurrency_token,
        )
    except StaleRecoveryTokenError as exc:
        raise HTTPException(
            status_code=409,
            detail={"reason": "stale_action_token", "message": str(exc)},
        ) from exc
    except RecoveryActionUnavailableError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "recovery_action_unavailable",
                "message": str(exc),
                "capability": RecoveryCapabilityResponse.model_validate(
                    exc.capability
                ).model_dump(mode="json"),
            },
        ) from exc
    except HistoricalExecutionRecoveryRefused as exc:
        raise _historical_recovery_refusal(exc) from exc
    except ExecutionCoverageResolutionUnavailable as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "coverage_proof_insufficient",
                "message": str(exc),
                "next_step": "Keep new exposure blocked and refresh the recovery evidence.",
            },
        ) from exc
    except ProjectionReadError as exc:
        raise _projection_read_error(exc) from exc
    return HistoricalExecutionRecoveryPlanResponse.model_validate(plan)


@router.post(
    "/accounts/{account_id}/bots/{strategy_instance_id}/historical-execution-recovery/confirm",
    response_model=HistoricalExecutionRecoveryReceiptResponse,
    responses=_HISTORICAL_EXECUTION_RECOVERY_RESPONSES,
)
async def confirm_bot_historical_execution_recovery(
    account_id: str,
    strategy_instance_id: str,
    body: HistoricalExecutionRecoveryConfirmRequest,
) -> HistoricalExecutionRecoveryReceiptResponse:
    """Append only the signed plan's exact evidence and its existing proof result."""
    if (
        body.plan.account_id != account_id
        or body.plan.strategy_instance_id != strategy_instance_id
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "historical_recovery_scope_mismatch",
                "message": "The recovery plan belongs to another account or bot.",
                "next_step": "Return to the affected bot and prepare a fresh recovery plan.",
            },
        )
    facade = _active_sqlite_facade(account_id)
    try:
        receipt = await facade.confirm_historical_execution_recovery(
            plan=HistoricalExecutionRecoveryPlan(**body.plan.model_dump()),
            confirmation_token=body.confirmation_token,
        )
    except HistoricalExecutionRecoveryRefused as exc:
        raise _historical_recovery_refusal(exc) from exc
    except (ExecutionLeaseLost, RepositoryPoisoned) as exc:
        raise _unavailable_response(exc) from exc
    return HistoricalExecutionRecoveryReceiptResponse.model_validate(receipt)


async def _execute_presented_recovery_action(
    account_id: str,
    *,
    strategy_instance_id: str | None,
    body: RecoveryActionExecuteRequest,
) -> RecoveryActionExecuteResponse:
    facade = _active_sqlite_facade(account_id)

    async def current_context():
        context = await asyncio.to_thread(
            _read_projection,
            facade.repository,
            lambda reader: reader.recovery_context(
                strategy_instance_id=strategy_instance_id,
            ),
        )
        if context is None:
            raise HTTPException(
                status_code=404,
                detail={"reason": "unknown_strategy_instance"},
            )
        return context

    try:
        result = await execute_recovery_action(
            facade,
            request=RecoveryExecutionRequest(
                action_id=body.action_id,
                concurrency_token=body.concurrency_token,
                execution_ref=body.execution_ref,
                reason=body.reason,
            ),
            current_context=current_context,
        )
    except StaleRecoveryTokenError as exc:
        raise HTTPException(
            status_code=409,
            detail={"reason": "stale_action_token", "message": str(exc)},
        ) from exc
    except RecoveryActionUnavailableError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "recovery_action_unavailable",
                "message": str(exc),
                "capability": RecoveryCapabilityResponse.model_validate(
                    exc.capability
                ).model_dump(mode="json"),
            },
        ) from exc
    except RecoveryExecutionError as exc:
        raise HTTPException(
            status_code=409,
            detail={"reason": "recovery_execution_rejected", "message": str(exc)},
        ) from exc
    except ExecutionCoverageResolutionUnavailable as exc:
        raise HTTPException(
            status_code=409,
            detail={"reason": "coverage_proof_insufficient", "message": str(exc)},
        ) from exc
    except (ExecutionLeaseLost, RepositoryPoisoned) as exc:
        raise _unavailable_response(exc) from exc
    except BrokerError as exc:
        raise HTTPException(
            status_code=503,
            detail={"reason": "broker_unavailable", "message": str(exc)},
        ) from exc
    return RecoveryActionExecuteResponse.from_result(result)


@router.post(
    "/accounts/{account_id}/recovery-actions/execute",
    response_model=RecoveryActionExecuteResponse,
)
async def execute_account_recovery_action(
    account_id: str,
    body: RecoveryActionExecuteRequest,
) -> RecoveryActionExecuteResponse:
    return await _execute_presented_recovery_action(
        account_id,
        strategy_instance_id=None,
        body=body,
    )


@router.post(
    "/accounts/{account_id}/bots/{strategy_instance_id}/recovery-actions/execute",
    response_model=RecoveryActionExecuteResponse,
)
async def execute_bot_recovery_action(
    account_id: str,
    strategy_instance_id: str,
    body: RecoveryActionExecuteRequest,
) -> RecoveryActionExecuteResponse:
    return await _execute_presented_recovery_action(
        account_id,
        strategy_instance_id=strategy_instance_id,
        body=body,
    )


@router.post(
    "/accounts/{account_id}/reconcile",
    response_model=ReconciliationResponse,
)
async def reconcile_now(account_id: str) -> ReconciliationResponse:
    """Run the same fail-closed account pass used by the automatic sweep."""
    repo = await _repo(account_id)
    try:
        port = get_broker_registry().resolve("alpaca")
    except UnknownBrokerError as exc:
        raise HTTPException(
            status_code=503,
            detail={"reason": "alpaca_broker_unavailable", "message": str(exc)},
        ) from exc
    if not isinstance(port, BrokerReadPort) or not isinstance(port, BrokerTradePort):
        raise HTTPException(
            status_code=503,
            detail={
                "reason": "alpaca_trade_port_unavailable",
                "message": "The registered Alpaca adapter cannot reconcile order identity.",
            },
        )
    try:
        broker_account = await port.get_account()
        if broker_account.account_id != account_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "broker_account_mismatch",
                    "message": (
                        f"Requested SQLite authority {account_id!r}, but the configured "
                        f"Alpaca adapter is bound to {broker_account.account_id!r}."
                    ),
                },
            )
        result = await reconcile_account(
            repo,
            read=port,
            trade=port,
            trigger="OPERATOR_RECONCILE_NOW",
        )
    except (ExecutionLeaseLost, RepositoryPoisoned) as exc:
        raise _unavailable_response(exc) from exc
    except BrokerError as exc:
        raise HTTPException(
            status_code=503,
            detail={"reason": "broker_unavailable", "message": str(exc)},
        ) from exc
    return ReconciliationResponse.from_result(result)
