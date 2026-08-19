"""Authenticated read-only host bridge for IBKR capability evidence.

The retired Account Clerk used this process as an order-mutation control
plane. The bridge now exposes only host health, Gateway socket evidence, and
its own capability-lease renewal. It never starts an account worker or accepts
an account-scoped broker command.
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import logging
import os
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware

from app.engine.live.broker_socket_probe import BrokerSocketProbeError, LsofSocketEnumerator
from app.engine.live.daemon_auth import TOKEN_HEADER, ensure_daemon_token, token_file_path
from app.engine.live.host_runner_policy import load_policy_env_file
from app.schemas.broker_session import GatewaySocketsSnapshot
from app.schemas.live_runs import HostRunnerHealth, HostRunnerProcessState, HostRunnerProcessStatus

logger = logging.getLogger(__name__)

_DEFAULT_ALLOWED_ORIGINS = "http://localhost:4200,http://127.0.0.1:4200"


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


class HostCapabilityError(RuntimeError):
    """Error translated into an authenticated bridge HTTP response."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class AccountCapabilityHost:
    """Own the host-only IBKR evidence and capability-lease boundary."""

    def __init__(self, *, repo_root: Path, artifacts_root: Path) -> None:
        self.repo_root = repo_root.resolve()
        self.artifacts_root = artifacts_root.resolve()
        self.boot_id = uuid.uuid4().hex
        self._launch_git_sha = self._git_sha()
        self._lease_writer: object | None = None

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
        """Return authenticated host evidence with no executable account workers."""

        on_disk = self._git_sha()
        return HostRunnerHealth(
            ok=True,
            repo_root=str(self.repo_root),
            live_runs_root=str(self.artifacts_root / "live_runs"),
            fetched_at_ms=_now_ms(),
            process=HostRunnerProcessStatus(state=HostRunnerProcessState.idle),
            clerks=[],
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
            raise HostCapabilityError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "account capability lease writer is unavailable",
            )
        try:
            renew()
        except OSError as exc:
            raise HostCapabilityError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "account capability lease renewal failed",
            ) from exc
        return self.health()


def create_app(
    manager: AccountCapabilityHost | None = None,
    *,
    allowed_origins: list[str] | None = None,
    auth_token: str | None = None,
) -> FastAPI:
    """Create the authenticated, evidence-only account capability host."""

    host = manager or _manager_from_env()
    token = auth_token or ensure_daemon_token(host.artifacts_root)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        from app.engine.live.control_plane import DaemonLeaseWriter

        writer = DaemonLeaseWriter(
            artifacts_root=host.artifacts_root,
            boot_id=host.boot_id,
            now_ms=_now_ms,
        )
        host._lease_writer = writer
        await writer.start()
        try:
            yield
        finally:
            writer.set_draining()
            await writer.stop()

    app = FastAPI(
        title="learn-ai account capability host",
        description="Host bridge for read-only IBKR account and Gateway evidence.",
        version="3.0.0",
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
                status.HTTP_401_UNAUTHORIZED,
                detail=f"missing or wrong {TOKEN_HEADER}",
            )

    auth = [Depends(verify_token)]

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
            account_clerks=[],
        )

    @app.post("/control-plane/renew-lease", response_model=HostRunnerHealth, dependencies=auth)
    async def renew_lease() -> HostRunnerHealth:
        try:
            return await run_in_threadpool(host.renew_lease)
        except HostCapabilityError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail) from exc

    return app


def _manager_from_env() -> AccountCapabilityHost:
    repo_root = Path(os.environ.get("LEARN_AI_REPO_ROOT", Path.cwd())).resolve()
    live_runs_root = Path(
        os.environ.get(
            "LIVE_RUNS_ROOT",
            str(repo_root / "PythonDataService" / "artifacts" / "live_runs"),
        )
    ).resolve()
    return AccountCapabilityHost(repo_root=repo_root, artifacts_root=live_runs_root.parent)


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
    host = AccountCapabilityHost(repo_root=repo_root, artifacts_root=live_runs_root.parent)
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
