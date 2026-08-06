"""SQLite Alpaca Clerk command endpoints (#1376, repaired by the corrective
foundation slice).

Purely-local operator-lifecycle commands (Start/Stop a run) proving out the
full R2 content-addressed command lifecycle ahead of the broker-facing
slices (#1377+). This router is additive and self-contained: it does not
touch the existing JSONL-backed Alpaca clerk routers, which remain
canonical until the cutover (#1382).

Every repository call is dispatched via ``asyncio.to_thread`` — the
repository is synchronous blocking I/O (SQLite + fsync), and calling it
directly from an ``async def`` handler would stall the FastAPI event loop
for every other in-flight request for as long as the write takes
(open-pr-review-2026-08-05.md P2 "Synchronous SQLite/fsync blocks the
FastAPI event loop").
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from app.broker.alpaca.clerk.journal import get_clerk_settings
from app.broker.alpaca.clerk.sqlite.commands import (
    DurableConflictError,
    InvalidIdentityError,
    NoActiveRunError,
    UnknownStrategyInstanceError,
    submit_start_run,
    submit_stop_run,
)
from app.broker.alpaca.clerk.sqlite.process_repositories import get_or_open_repository
from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
from app.broker.alpaca.clerk.sqlite.repository import (
    ClerkSqliteError,
    ClerkSqliteRepository,
    ExecutionLeaseLost,
    RepositoryPoisoned,
)
from app.broker.contract.errors import BrokerError, UnknownBrokerError
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort
from app.broker.contract.registry import get_broker_registry
from app.schemas.alpaca_clerk_sqlite import (
    CommandResponse,
    ReconciliationResponse,
    StartRunRequest,
    StopRunRequest,
)

router = APIRouter(prefix="/api/alpaca-clerk-sqlite", tags=["alpaca-clerk-sqlite"])


async def _repo(account_id: str) -> ClerkSqliteRepository:
    """Open (or reuse) this process's repository for ``account_id``.

    Until the cutover (#1382) runs, most accounts have no ``clerk.db`` yet —
    that is expected, not a bug, so it gets a typed 503 rather than an
    unhandled 500.
    """
    try:
        return await asyncio.to_thread(
            get_or_open_repository, account_id=account_id, artifacts_root=get_clerk_settings().dir
        )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "reason": "sqlite_authority_not_initialized",
                "message": (
                    f"Account {account_id!r} has no SQLite Clerk authority yet "
                    "(pre-cutover; see issue #1382)."
                ),
            },
        ) from exc
    except ClerkSqliteError as exc:
        raise HTTPException(
            status_code=503,
            detail={"reason": "sqlite_authority_unavailable", "message": str(exc)},
        ) from exc


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
