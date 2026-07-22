"""Host-only Account Clerk mutations exposed by the live-run daemon.

The data plane reaches this module over authenticated HTTP, but the Account
Clerk is a host process.  Keeping these routes outside ``host_daemon`` makes
the AF_UNIX ownership boundary explicit: only host code opens a Clerk socket.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Protocol, TypeVar

from fastapi import FastAPI, HTTPException, status
from fastapi.concurrency import run_in_threadpool

from app.engine.live.account_clerk_rpc import (
    AccountClerkRpcClient,
    AccountClerkRpcError,
    AccountClerkRpcRejectedError,
)
from app.engine.live.account_registry import AccountInstanceBinding
from app.schemas.account_reconciliation import StaleBindingRetirementRequest
from app.schemas.journal_cures import (
    JournalCureReceipt,
    JournalCureRequest,
    OperatorRecoveryFlattenRequest,
    OperatorRecoveryFlattenResponse,
)

_ClerkMutationResult = TypeVar("_ClerkMutationResult")


class AccountOperationsProcessManager(Protocol):
    """Narrow host-process capabilities required by account mutation routes."""

    artifacts_root: Path

    def _ensure_account_clerk(self, account_id: str, *, ibkr_host: str | None = None) -> object: ...

    def retire_stale_account_binding(
        self,
        *,
        account_id: str,
        strategy_instance_id: str,
        run_id: str,
    ) -> AccountInstanceBinding: ...


HostErrorTranslator = Callable[[Exception], HTTPException | None]
ClerkClientFactory = Callable[..., AccountClerkRpcClient]


def install_account_operation_routes(
    app: FastAPI,
    *,
    process_manager: AccountOperationsProcessManager,
    auth_dependencies: Sequence[object],
    translate_host_error: HostErrorTranslator,
    clerk_client_factory: ClerkClientFactory = AccountClerkRpcClient,
) -> None:
    """Install authenticated routes requiring host-local Clerk authority."""

    async def relay_clerk_mutation(
        account_id: str,
        operation: Callable[[AccountClerkRpcClient], Awaitable[_ClerkMutationResult]],
    ) -> _ClerkMutationResult:
        try:
            await run_in_threadpool(
                process_manager._ensure_account_clerk,
                account_id,
                ibkr_host="127.0.0.1",
            )
            client = clerk_client_factory(
                artifacts_root=process_manager.artifacts_root,
                account_id=account_id,
            )
            return await operation(client)
        except AccountClerkRpcError as exc:
            unavailable = exc.reason_code.startswith("ACCOUNT_CLERK_UNAVAILABLE:")
            reason_code = exc.reason if isinstance(exc, AccountClerkRpcRejectedError) else exc.reason_code
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE if unavailable else status.HTTP_409_CONFLICT,
                detail={
                    "reason_code": reason_code,
                    "message": "Clerk rejected or could not complete the request.",
                },
            ) from exc
        except OSError as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "reason_code": "ACCOUNT_CLERK_START_FAILED",
                    "message": str(exc),
                },
            ) from exc
        except Exception as exc:
            translated = translate_host_error(exc)
            if translated is not None:
                raise translated from exc
            raise

    @app.post(
        "/accounts/{account_id}/clerk/operator-adjustment",
        response_model=JournalCureReceipt,
        dependencies=list(auth_dependencies),
    )
    async def apply_clerk_operator_adjustment(
        account_id: str,
        request: JournalCureRequest,
    ) -> JournalCureReceipt:
        """Relay one idempotent journal cure through the host-local Clerk."""

        return await relay_clerk_mutation(
            account_id,
            lambda client: client.apply_operator_adjustment(request),
        )

    @app.post(
        "/accounts/{account_id}/clerk/operator-recovery-flatten",
        response_model=OperatorRecoveryFlattenResponse,
        dependencies=list(auth_dependencies),
    )
    async def apply_clerk_operator_recovery_flatten(
        account_id: str,
        request: OperatorRecoveryFlattenRequest,
    ) -> OperatorRecoveryFlattenResponse:
        """Relay a server-authored retired-namespace recovery flatten."""

        if request.intent.account_id != account_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "intent account_id does not match path")
        receipt = await relay_clerk_mutation(
            account_id,
            lambda client: client.submit_operator_recovery_flatten(request.intent),
        )
        return OperatorRecoveryFlattenResponse(recovery_flatten=receipt)

    @app.post(
        "/accounts/{account_id}/bindings/retire",
        response_model=AccountInstanceBinding,
        dependencies=list(auth_dependencies),
    )
    async def retire_stale_account_binding(
        account_id: str,
        request: StaleBindingRetirementRequest,
    ) -> AccountInstanceBinding:
        """Atomically retire an inactive binding from the host lifecycle authority."""

        try:
            return await run_in_threadpool(
                process_manager.retire_stale_account_binding,
                account_id=account_id,
                strategy_instance_id=request.strategy_instance_id,
                run_id=request.run_id,
            )
        except Exception as exc:
            translated = translate_host_error(exc)
            if translated is not None:
                raise translated from exc
            raise
