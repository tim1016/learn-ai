"""SQLite Alpaca Clerk command endpoints (#1376).

Purely-local operator-lifecycle commands (Start/Stop a run) proving out the
full R2 content-addressed command lifecycle ahead of the broker-facing
slices (#1377+). This router is additive and self-contained: it does not
touch the existing JSONL-backed Alpaca clerk routers, which remain
canonical until the cutover (#1382).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.broker.alpaca.clerk.journal import get_clerk_settings
from app.broker.alpaca.clerk.sqlite.commands import (
    DurableConflictError,
    InvalidIdentityError,
    NoActiveRunError,
    submit_start_run,
    submit_stop_run,
)
from app.broker.alpaca.clerk.sqlite.process_repositories import get_or_open_repository
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteError, ClerkSqliteRepository
from app.schemas.alpaca_clerk_sqlite import (
    CommandResponse,
    StartRunRequest,
    StopRunRequest,
)

router = APIRouter(prefix="/api/alpaca-clerk-sqlite", tags=["alpaca-clerk-sqlite"])


def _repo(account_id: str) -> ClerkSqliteRepository:
    """Open (or reuse) this process's repository for ``account_id``.

    Until the cutover (#1382) runs, most accounts have no ``clerk.db`` yet —
    that is expected, not a bug, so it gets a typed 503 rather than an
    unhandled 500.
    """
    try:
        return get_or_open_repository(account_id=account_id, artifacts_root=get_clerk_settings().dir)
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
    repo = _repo(account_id)
    try:
        submission = submit_start_run(
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
    return CommandResponse.from_resource(submission.command)


@router.post(
    "/accounts/{account_id}/bots/{strategy_instance_id}/runs/stop",
    response_model=CommandResponse,
    status_code=202,
)
async def stop_run(
    account_id: str, strategy_instance_id: str, body: StopRunRequest
) -> CommandResponse:
    """Reserve and admit a Stop command against the currently active run."""
    repo = _repo(account_id)
    try:
        submission = submit_stop_run(
            repo,
            account_id=account_id,
            strategy_instance_id=strategy_instance_id,
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
    return CommandResponse.from_resource(submission.command)


@router.get(
    "/accounts/{account_id}/commands/{command_id}",
    response_model=CommandResponse,
)
async def get_command(account_id: str, command_id: str) -> CommandResponse:
    repo = _repo(account_id)
    resource = repo.get_command(command_id)
    if resource is None:
        raise HTTPException(status_code=404, detail={"reason": "command_not_found"})
    return CommandResponse.from_resource(resource)
