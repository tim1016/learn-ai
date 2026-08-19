"""Authenticated host bridge for IBKR account capabilities.

The retired evaluator used this process as a second bot-control plane. Alpaca
Broker V2 now owns bot control. This process remains only because the
containerized data service cannot reach the Account Clerk's host-local socket
or inspect host Gateway sockets. No route accepts a bot, strategy instance, or
run identifier.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import ipaddress
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from subprocess import Popen as _RealPopen

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware

from app.engine.live.account_clerk_journal_models import AccountClerkEmergencyAuthorization
from app.engine.live.account_clerk_rpc import (
    AccountClerkHostRpcClient,
    AccountClerkRpcClient,
    AccountClerkRpcError,
    clerk_rejection_reason,
)
from app.engine.live.account_clerk_supervisor import (
    AccountClerkSupervisor,
    inspect_account_clerk_process,
)
from app.engine.live.account_identity import normalize_account_id
from app.engine.live.broker_socket_probe import BrokerSocketProbeError, LsofSocketEnumerator
from app.engine.live.daemon_auth import (
    TOKEN_HEADER,
    ensure_clerk_host_binding_capability,
    ensure_daemon_token,
    token_file_path,
)
from app.engine.live.daemon_command_idempotency import (
    DaemonCommandIdempotencyService,
    DaemonCommandOutcome,
    validate_idempotency_key,
)
from app.engine.live.host_runner_policy import load_policy_env_file, validate_ibkr_host_allowed
from app.schemas.broker_session import GatewaySocketsSnapshot
from app.schemas.journal_cures import (
    JournalCureReceipt,
    JournalCureRequest,
    OperatorExactCancelRequest,
    OperatorExactCancelResponse,
    OperatorPendingCancelResponse,
    OperatorRecoveryFlattenRequest,
    OperatorRecoveryFlattenResponse,
)
from app.schemas.live_runs import (
    AccountEmergencyFlattenAuthorizationRequest,
    AccountEmergencyFlattenDispatchRequest,
    AccountEmergencyFlattenResponse,
    HostRunnerClerkEnsureRequest,
    HostRunnerHealth,
    HostRunnerProcessState,
    HostRunnerProcessStatus,
)

logger = logging.getLogger(__name__)

_DEFAULT_ALLOWED_ORIGINS = "http://localhost:4200,http://127.0.0.1:4200"
_DEFAULT_IBKR_CLIENT_ID_POOL = "50-99"
_MAX_POOL_SPAN = 1_000
_HANDSHAKE_TIMEOUT_SECONDS = 5.0
_CLERK_SUPERVISION_INTERVAL_SECONDS = 1.0


class HostRunnerError(RuntimeError):
    """Error translated into an authenticated bridge HTTP response."""

    def __init__(self, status_code: int, detail: str | dict[str, object]) -> None:
        super().__init__(
            detail if isinstance(detail, str) else detail.get("message", str(detail))
        )
        self.status_code = status_code
        self.detail = detail


def _parse_ibkr_client_id_pool(raw: str) -> tuple[int, ...]:
    """Parse a bounded account-Clerk client-id pool."""

    client_ids: list[int] = []
    seen: set[int] = set()
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            try:
                start, end = int(start_text), int(end_text)
            except ValueError as exc:
                raise ValueError(f"invalid IBKR client id range {token!r}") from exc
            if end < start or end - start + 1 > _MAX_POOL_SPAN:
                raise ValueError(f"invalid IBKR client id range {token!r}")
            values = range(start, end + 1)
        else:
            try:
                values = (int(token),)
            except ValueError as exc:
                raise ValueError(f"invalid IBKR client id {token!r}") from exc
        for value in values:
            if not 1 <= value <= 2**31 - 1:
                raise ValueError(f"IBKR client id {value} is outside the supported range")
            if value not in seen:
                seen.add(value)
                client_ids.append(value)
    if not client_ids:
        raise ValueError("IBKR client id pool is empty")
    return tuple(client_ids)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _host_platform() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform or "unknown"


class AccountClerkHost:
    """Own Account Clerk supervision and account-scoped recovery RPC only."""

    def __init__(self, *, repo_root: Path, artifacts_root: Path) -> None:
        self.repo_root = repo_root.resolve()
        self.artifacts_root = artifacts_root.resolve()
        self.boot_id = uuid.uuid4().hex
        self._launch_git_sha = self._git_sha()
        self._lease_writer: object | None = None
        self._host_capability = ensure_clerk_host_binding_capability(self.artifacts_root)
        self._flatten_lock = threading.Lock()
        self._flatten_in_flight: set[str] = set()
        self.clerks = AccountClerkSupervisor(
            repo_root=self.repo_root,
            artifacts_root=self.artifacts_root,
            now_ms=_now_ms,
            client_id_pool=self._client_id_pool,
            external_client_ids=set,
            verify_generation=self._verify_generation,
            wait_for_readiness=self._wait_for_readiness,
            inspect_process=inspect_account_clerk_process,
            is_real_popen=lambda process: isinstance(process, _RealPopen),
            creation_flags=lambda: int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            if os.name == "nt"
            else 0,
            terminate_wait_seconds=lambda: 2.0,
            kill_wait_seconds=lambda: 2.0,
            host_binding_capability=self._host_capability,
        )

    def _client_id_pool(self) -> tuple[int, ...]:
        try:
            return _parse_ibkr_client_id_pool(
                os.environ.get("LIVE_RUNNER_IBKR_CLIENT_ID_POOL", _DEFAULT_IBKR_CLIENT_ID_POOL)
            )
        except ValueError as exc:
            raise OSError(f"Invalid LIVE_RUNNER_IBKR_CLIENT_ID_POOL: {exc}") from exc

    @staticmethod
    def _verify_generation(artifacts_root: Path, account_id: str) -> int:
        return asyncio.run(
            AccountClerkRpcClient(
                artifacts_root=artifacts_root,
                account_id=account_id,
            ).verify_generation()
        )

    @classmethod
    def _wait_for_readiness(
        cls, artifacts_root: Path, account_id: str, expected_generation: int
    ) -> None:
        deadline = time.monotonic() + _HANDSHAKE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                generation = cls._verify_generation(artifacts_root, account_id)
            except AccountClerkRpcError:
                time.sleep(0.05)
                continue
            if generation == expected_generation:
                return
            time.sleep(0.05)
        raise OSError(
            f"Account Clerk generation {expected_generation} did not become ready for {account_id}"
        )

    def _git_sha(self) -> str | None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.repo_root,
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() or None

    def health(self) -> HostRunnerHealth:
        """Return host health with the retired process slot explicitly idle."""

        on_disk = self._git_sha()
        return HostRunnerHealth(
            ok=True,
            repo_root=str(self.repo_root),
            live_runs_root=str(self.artifacts_root / "live_runs"),
            fetched_at_ms=_now_ms(),
            process=HostRunnerProcessStatus(state=HostRunnerProcessState.idle),
            clerks=self.clerks.health(),
            git_sha=self._launch_git_sha,
            repo_head_sha=on_disk,
            code_stale=bool(self._launch_git_sha and on_disk and self._launch_git_sha != on_disk),
            daemon_boot_id=self.boot_id,
            lease_status=getattr(self._lease_writer, "status", None),
            last_lease_written_at_ms=getattr(self._lease_writer, "last_written_at_ms", None),
            lease_threshold_ms=getattr(self._lease_writer, "lease_threshold_ms", None),
            lease_write_error=getattr(self._lease_writer, "last_write_error", None),
            platform=_host_platform(),
            supervisor=os.environ.get("LIVE_RUNNER_SUPERVISOR", "manual"),
        )

    def renew_lease(self) -> HostRunnerHealth:
        renew = getattr(self._lease_writer, "renew_now", None)
        if not callable(renew):
            raise HostRunnerError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "account capability lease writer is unavailable",
            )
        try:
            renew()
        except OSError as exc:
            raise HostRunnerError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "account capability lease renewal failed",
            ) from exc
        return self.health()

    def emergency_flatten(
        self, account_id: str, *, operation_id: str, authorization_id: str
    ) -> AccountEmergencyFlattenResponse:
        """Run account recovery through the sole held Clerk broker session."""

        with self._flatten_lock:
            if account_id in self._flatten_in_flight:
                raise HostRunnerError(
                    status.HTTP_409_CONFLICT,
                    f"emergency flatten already in progress for account {account_id}",
                )
            self._flatten_in_flight.add(account_id)
        try:
            self.clerks.ensure(account_id)
            client = AccountClerkHostRpcClient(
                artifacts_root=self.artifacts_root,
                account_id=account_id,
                host_capability=self._host_capability,
            )
            asyncio.run(
                client.prepare_emergency_flatten(
                    operation_id=operation_id,
                    authorization_id=authorization_id,
                )
            )
            # Bot supervision is gone, so this journal prerequisite is vacuous.
            asyncio.run(
                client.mark_emergency_bots_paused(
                    operation_id=operation_id,
                    authorization_id=authorization_id,
                )
            )
            receipt = asyncio.run(
                client.emergency_flatten_account(
                    operation_id=operation_id,
                    authorization_id=authorization_id,
                )
            )
            return AccountEmergencyFlattenResponse(
                accepted=True,
                account_id=account_id,
                audit_run_id=receipt.operation_id,
                completed_at_ms=receipt.observed_at_ms,
            )
        except AccountClerkRpcError as exc:
            retry_safe, reason_code = clerk_rejection_reason(exc)
            raise HostRunnerError(
                status.HTTP_503_SERVICE_UNAVAILABLE if retry_safe else status.HTTP_409_CONFLICT,
                {
                    "reason_code": reason_code,
                    "message": "The Account Clerk could not prove account recovery complete.",
                },
            ) from exc
        except OSError as exc:
            raise HostRunnerError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Emergency flatten could not start the Account Clerk.",
            ) from exc
        finally:
            with self._flatten_lock:
                self._flatten_in_flight.discard(account_id)


def _rpc_http_error(exc: AccountClerkRpcError) -> HTTPException:
    retry_safe, reason_code = clerk_rejection_reason(exc)
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE if retry_safe else status.HTTP_409_CONFLICT,
        detail={
            "reason_code": reason_code,
            "message": "The Account Clerk rejected or could not complete the request.",
        },
    )


async def _supervise_account_clerks(
    host: AccountClerkHost,
    *,
    interval_seconds: float,
) -> None:
    """Run bounded Clerk supervision passes until the host shuts down."""

    while True:
        pass_task = asyncio.create_task(
            asyncio.to_thread(host.clerks.supervise),
            name="account-clerk-supervision-pass",
        )
        try:
            await asyncio.shield(pass_task)
        except asyncio.CancelledError:
            # Cancelling ``to_thread`` does not stop its worker.  Let an in-flight
            # pass finish before the host marks its lease drained and exits.
            with suppress(Exception):
                await pass_task
            raise
        except Exception:
            logger.exception("account Clerk supervision pass failed")
        await asyncio.sleep(interval_seconds)


def create_app(
    manager: AccountClerkHost | None = None,
    *,
    allowed_origins: list[str] | None = None,
    auth_token: str | None = None,
    clerk_supervision_interval_seconds: float = _CLERK_SUPERVISION_INTERVAL_SECONDS,
) -> FastAPI:
    """Create the authenticated account capability host application."""

    if clerk_supervision_interval_seconds <= 0:
        raise ValueError("clerk_supervision_interval_seconds must be positive")
    host = manager or _manager_from_env()
    token = auth_token or ensure_daemon_token(host.artifacts_root)
    idempotency = DaemonCommandIdempotencyService(host.artifacts_root)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        from app.engine.live.control_plane import DaemonLeaseWriter

        await asyncio.to_thread(host.clerks.reconcile_on_boot)
        writer = DaemonLeaseWriter(
            artifacts_root=host.artifacts_root, boot_id=host.boot_id, now_ms=_now_ms
        )
        host._lease_writer = writer
        await writer.start()
        supervision_task = asyncio.create_task(
            _supervise_account_clerks(
                host,
                interval_seconds=clerk_supervision_interval_seconds,
            ),
            name="account-clerk-supervisor",
        )
        try:
            yield
        finally:
            supervision_task.cancel()
            with suppress(asyncio.CancelledError):
                await supervision_task
            writer.set_draining()
            await asyncio.sleep(0)
            await writer.stop()

    app = FastAPI(
        title="learn-ai account capability host",
        description="Host bridge for IBKR account evidence and Clerk recovery.",
        version="2.0.0",
        lifespan=_lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins or _allowed_origins_from_env(),
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    async def verify_token(
        supplied: str | None = Header(default=None, alias=TOKEN_HEADER),
    ) -> None:
        if not hmac.compare_digest((supplied or "").encode(), token.encode()):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, detail=f"missing or wrong {TOKEN_HEADER}"
            )

    auth = [Depends(verify_token)]

    async def ensure(account_id: str, ibkr_host: str | None = None) -> str:
        try:
            normalized = normalize_account_id(account_id)
            if ibkr_host is not None:
                validate_ibkr_host_allowed(ibkr_host)
            await run_in_threadpool(host.clerks.ensure, normalized, ibkr_host=ibkr_host)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"reason_code": "ACCOUNT_CLERK_START_FAILED", "message": str(exc)},
            ) from exc
        return normalized

    @app.get("/health", response_model=HostRunnerHealth, dependencies=auth)
    async def health() -> HostRunnerHealth:
        return host.health()

    @app.get("/broker/sockets", response_model=GatewaySocketsSnapshot, dependencies=auth)
    async def sockets(
        gateway_port: int = Query(default=4002, ge=1, le=65535),
    ) -> GatewaySocketsSnapshot:
        try:
            rows = await run_in_threadpool(LsofSocketEnumerator().enumerate, gateway_port)
        except BrokerSocketProbeError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail) from exc
        return GatewaySocketsSnapshot(
            fetched_at_ms=_now_ms(),
            gateway_port=gateway_port,
            sockets=rows,
            account_clerks=await run_in_threadpool(host.clerks.health),
        )

    @app.post("/control-plane/renew-lease", response_model=HostRunnerHealth, dependencies=auth)
    async def renew_lease() -> HostRunnerHealth:
        try:
            return await run_in_threadpool(host.renew_lease)
        except HostRunnerError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail) from exc

    @app.post("/accounts/{account_id}/clerk/ensure", response_model=HostRunnerHealth, dependencies=auth)
    async def ensure_clerk(
        account_id: str, request: HostRunnerClerkEnsureRequest
    ) -> HostRunnerHealth:
        await ensure(account_id, request.ibkr_host)
        return host.health()

    @app.post(
        "/accounts/{account_id}/clerk/operator-adjustment",
        response_model=JournalCureReceipt,
        dependencies=auth,
    )
    async def operator_adjustment(
        account_id: str, request: JournalCureRequest
    ) -> JournalCureReceipt:
        normalized = await ensure(account_id)
        try:
            return await AccountClerkRpcClient(
                artifacts_root=host.artifacts_root, account_id=normalized
            ).apply_operator_adjustment(request)
        except AccountClerkRpcError as exc:
            raise _rpc_http_error(exc) from exc

    @app.post(
        "/accounts/{account_id}/clerk/operator-recovery-flatten",
        response_model=OperatorRecoveryFlattenResponse,
        dependencies=auth,
    )
    async def operator_recovery_flatten(
        account_id: str, request: OperatorRecoveryFlattenRequest
    ) -> OperatorRecoveryFlattenResponse:
        normalized = await ensure(account_id)
        if request.intent.account_id != normalized:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "intent account_id does not match path")
        try:
            receipt = await AccountClerkRpcClient(
                artifacts_root=host.artifacts_root, account_id=normalized
            ).submit_operator_recovery_flatten(request.intent)
        except AccountClerkRpcError as exc:
            raise _rpc_http_error(exc) from exc
        return OperatorRecoveryFlattenResponse(recovery_flatten=receipt)

    async def cancel(
        account_id: str, request: OperatorExactCancelRequest, *, pending: bool
    ):
        normalized = await ensure(account_id)
        if request.intent.account_id != normalized:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "intent account_id does not match path")
        client = AccountClerkRpcClient(
            artifacts_root=host.artifacts_root, account_id=normalized
        )
        try:
            return (
                await client.cancel_pending_a0(request.intent)
                if pending
                else await client.cancel_exact_order(request.intent)
            )
        except AccountClerkRpcError as exc:
            raise _rpc_http_error(exc) from exc

    @app.post(
        "/accounts/{account_id}/clerk/operator-pending-cancel",
        response_model=OperatorPendingCancelResponse,
        dependencies=auth,
    )
    async def operator_pending_cancel(
        account_id: str, request: OperatorExactCancelRequest
    ) -> OperatorPendingCancelResponse:
        return OperatorPendingCancelResponse(
            cancelled_before_submit=await cancel(account_id, request, pending=True)
        )

    @app.post(
        "/accounts/{account_id}/clerk/operator-exact-cancel",
        response_model=OperatorExactCancelResponse,
        dependencies=auth,
    )
    async def operator_exact_cancel(
        account_id: str, request: OperatorExactCancelRequest
    ) -> OperatorExactCancelResponse:
        return OperatorExactCancelResponse(
            cancel_confirmed=await cancel(account_id, request, pending=False)
        )

    @app.post(
        "/accounts/{account_id}/clerk/authorize-emergency-flatten",
        response_model=AccountClerkEmergencyAuthorization,
        dependencies=auth,
    )
    async def authorize_flatten(
        account_id: str, request: AccountEmergencyFlattenAuthorizationRequest
    ) -> AccountClerkEmergencyAuthorization:
        normalized = await ensure(account_id)
        try:
            return await AccountClerkRpcClient(
                artifacts_root=host.artifacts_root, account_id=normalized
            ).authorize_emergency_flatten(
                operation_id=request.operation_id,
                reconciliation_evidence_version=request.reconciliation_evidence_version,
            )
        except AccountClerkRpcError as exc:
            raise _rpc_http_error(exc) from exc

    @app.post("/accounts/{account_id}/clerk/release", response_model=HostRunnerHealth, dependencies=auth)
    async def release_clerk(account_id: str) -> HostRunnerHealth:
        try:
            released = await run_in_threadpool(
                host.clerks.release, normalize_account_id(account_id)
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        if not released:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"reason_code": "ACCOUNT_CLERK_RELEASE_UNCONFIRMED"},
            )
        return host.health()

    @app.post(
        "/accounts/{account_id}/emergency-flatten",
        response_model=AccountEmergencyFlattenResponse,
        dependencies=auth,
    )
    async def emergency_flatten(
        account_id: str, request: AccountEmergencyFlattenDispatchRequest
    ) -> AccountEmergencyFlattenResponse:
        try:
            normalized = normalize_account_id(account_id)
            key = validate_idempotency_key(request.idempotency_key)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        if request.account != normalized:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "request account does not match path")

        def invoke() -> DaemonCommandOutcome:
            try:
                response = host.emergency_flatten(
                    normalized,
                    operation_id=key,
                    authorization_id=request.authorization_id,
                )
            except HostRunnerError as exc:
                return DaemonCommandOutcome(
                    status_code=exc.status_code,
                    body={"detail": exc.detail},
                    replayed=False,
                )
            return DaemonCommandOutcome(
                status_code=status.HTTP_200_OK,
                body=response.model_dump(mode="json"),
                replayed=False,
            )

        outcome = await run_in_threadpool(
            idempotency.execute,
            idempotency_key=key,
            command="emergency_flatten_account",
            account_id=normalized,
            semantic_payload={
                "account_id": normalized,
                "request": request.model_dump(mode="json", exclude={"idempotency_key"}),
            },
            invoke=invoke,
        )
        if outcome.status_code >= status.HTTP_400_BAD_REQUEST:
            raise HTTPException(outcome.status_code, detail=outcome.body.get("detail", outcome.body))
        response = AccountEmergencyFlattenResponse.model_validate(outcome.body)
        return response.model_copy(
            update={"idempotency_key": key, "idempotency_replayed": outcome.replayed}
        )

    return app


def _manager_from_env() -> AccountClerkHost:
    repo_root = Path(os.environ.get("LEARN_AI_REPO_ROOT", Path.cwd())).resolve()
    live_runs_root = Path(
        os.environ.get(
            "LIVE_RUNS_ROOT",
            str(repo_root / "PythonDataService" / "artifacts" / "live_runs"),
        )
    ).resolve()
    return AccountClerkHost(repo_root=repo_root, artifacts_root=live_runs_root.parent)


def _allowed_origins_from_env() -> list[str]:
    raw = os.environ.get("LIVE_RUNNER_DAEMON_ALLOWED_ORIGINS", _DEFAULT_ALLOWED_ORIGINS)
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def _valid_bind_host(host: str) -> str:
    if host == "localhost":
        return host
    try:
        ipaddress.ip_address(host)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--host must be an IP address or 'localhost', got {host!r}."
        ) from exc
    return host


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the account capability host bridge.")
    parser.add_argument("--host", default="127.0.0.1", type=_valid_bind_host)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--live-runs-root", type=Path, default=None)
    parser.add_argument("--allowed-origins", default=_DEFAULT_ALLOWED_ORIGINS)
    parser.add_argument("--env-file", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    repo_root = args.repo_root.resolve()
    load_policy_env_file(args.env_file.resolve() if args.env_file else repo_root / ".env")
    live_runs_root = (
        args.live_runs_root.resolve()
        if args.live_runs_root
        else (repo_root / "PythonDataService" / "artifacts" / "live_runs").resolve()
    )
    host = AccountClerkHost(repo_root=repo_root, artifacts_root=live_runs_root.parent)
    token = ensure_daemon_token(host.artifacts_root)
    app = create_app(
        host,
        allowed_origins=[item.strip() for item in args.allowed_origins.split(",") if item.strip()],
        auth_token=token,
    )
    logger.info(
        "account capability host binding %s:%s with %s auth (token at %s)",
        args.host,
        args.port,
        TOKEN_HEADER,
        token_file_path(host.artifacts_root),
    )
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
