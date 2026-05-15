"""Host-side live-run daemon for UI-driven paper-run starts.

The FastAPI app in ``polygon-data-service`` can observe live-run artifacts,
but it cannot safely own the IBKR real-time-bar session under Windows/Podman:
Gateway rejects the container client when its source IP differs from the
Gateway login IP. This daemon runs on the Windows host and starts the existing
``app.engine.live.run start`` subprocess from there.

Trading state remains artifact-derived in ``/api/live-runs``. This module only
owns subprocess lifecycle.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware

from app.schemas.live_runs import (
    HostRunnerActionResponse,
    HostRunnerHealth,
    HostRunnerProcessState,
    HostRunnerProcessStatus,
    HostRunnerStartRequest,
    HostRunnerStopRequest,
)

logger = logging.getLogger(__name__)

_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{1,127}$")
_DEFAULT_ALLOWED_ORIGINS = "http://localhost:4200,http://127.0.0.1:4200"
_STOP_WAIT_SECONDS = 2.0


class HostRunnerError(RuntimeError):
    """Error that should be translated into a daemon HTTP response."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class ManagedProcess:
    """Subprocess plus the file handle used for stdout/stderr capture."""

    run_id: str
    run_dir: Path
    process: subprocess.Popen
    command: list[str]
    started_at_ms: int
    log_path: Path
    log_handle: TextIO
    stopping: bool = False
    ended_at_ms: int | None = None


class RunnerProcessManager:
    """Own exactly one host-side live-run subprocess at a time."""

    def __init__(self, *, repo_root: Path, live_runs_root: Path) -> None:
        self.repo_root = repo_root.resolve()
        self.live_runs_root = live_runs_root.resolve()
        self._current: ManagedProcess | None = None

    def health(self) -> HostRunnerHealth:
        """Return daemon health plus the active subprocess snapshot."""
        return HostRunnerHealth(
            ok=True,
            repo_root=str(self.repo_root),
            live_runs_root=str(self.live_runs_root),
            fetched_at_ms=_now_ms(),
            process=self.process_status(),
        )

    def process_status(self, run_id: str | None = None) -> HostRunnerProcessStatus:
        """Return the current subprocess state.

        If ``run_id`` is supplied and another run is active, return idle for
        the requested run. This lets the UI show selected-run controls without
        inheriting another run's process status.
        """
        self._refresh_current()
        current = self._current
        if current is None:
            return HostRunnerProcessStatus(state=HostRunnerProcessState.idle, message="No host runner process.")
        if run_id is not None and current.run_id != run_id:
            return HostRunnerProcessStatus(
                state=HostRunnerProcessState.idle,
                message=f"Host runner is tracking {current.run_id}, not {run_id}.",
            )

        exit_code = current.process.poll()
        if exit_code is None:
            state = HostRunnerProcessState.stopping if current.stopping else HostRunnerProcessState.running
            return HostRunnerProcessStatus(
                state=state,
                run_id=current.run_id,
                pid=current.process.pid,
                started_at_ms=current.started_at_ms,
                command=current.command,
                log_path=str(current.log_path),
                message="Host runner process is active.",
            )

        return HostRunnerProcessStatus(
            state=HostRunnerProcessState.exited,
            run_id=current.run_id,
            pid=current.process.pid,
            started_at_ms=current.started_at_ms,
            ended_at_ms=current.ended_at_ms,
            exit_code=exit_code,
            command=current.command,
            log_path=str(current.log_path),
            message=f"Host runner process exited with code {exit_code}.",
        )

    def start(self, run_id: str, request: HostRunnerStartRequest) -> HostRunnerActionResponse:
        """Start ``app.engine.live.run start`` for an existing run directory."""
        self._refresh_current()
        current = self._current
        if current is not None and current.process.poll() is None:
            raise HostRunnerError(
                status.HTTP_409_CONFLICT,
                f"Host runner already active for {current.run_id}. Stop it before starting another run.",
            )

        run_dir = self._validate_run_dir(run_id)
        command = self._build_start_command(run_dir, request)
        env = self._build_child_env(request)
        log_path = run_dir / "host_daemon.log"
        log_handle = log_path.open("a", encoding="utf-8")

        try:
            process = subprocess.Popen(
                command,
                cwd=str(self.repo_root),
                env=env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                creationflags=_creation_flags(),
                start_new_session=(os.name != "nt"),
            )
        except OSError as exc:
            log_handle.close()
            raise HostRunnerError(status.HTTP_503_SERVICE_UNAVAILABLE, f"Could not start host runner: {exc}") from exc

        self._current = ManagedProcess(
            run_id=run_id,
            run_dir=run_dir,
            process=process,
            command=command,
            started_at_ms=_now_ms(),
            log_path=log_path,
            log_handle=log_handle,
        )
        logger.info("Started host live runner for %s with pid=%s", run_id, process.pid)
        return HostRunnerActionResponse(accepted=True, process=self.process_status(run_id))

    def stop(self, run_id: str, request: HostRunnerStopRequest) -> HostRunnerActionResponse:
        """Signal the active host runner to stop."""
        self._refresh_current()
        current = self._current
        if current is None:
            raise HostRunnerError(status.HTTP_404_NOT_FOUND, "No host runner process is being tracked.")
        if current.run_id != run_id:
            raise HostRunnerError(
                status.HTTP_409_CONFLICT,
                f"Host runner is active for {current.run_id}, not {run_id}.",
            )
        if current.process.poll() is not None:
            return HostRunnerActionResponse(accepted=False, process=self.process_status(run_id))

        current.stopping = True
        _send_graceful_stop(current.process)
        try:
            current.process.wait(timeout=_STOP_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            if request.force:
                current.process.kill()
                current.process.wait(timeout=_STOP_WAIT_SECONDS)

        self._refresh_current()
        logger.info("Stop requested for host live runner %s", run_id)
        return HostRunnerActionResponse(accepted=True, process=self.process_status(run_id))

    def _build_start_command(self, run_dir: Path, request: HostRunnerStartRequest) -> list[str]:
        command = [
            sys.executable,
            "-m",
            "app.engine.live.run",
            "start",
            "--run-dir",
            str(run_dir),
            "--strategy",
            request.strategy,
            "--max-orders-per-day",
            str(request.max_orders_per_day),
            "--hydrate-policy",
            request.hydrate_policy,
        ]
        if request.readonly:
            command.append("--readonly")
        return command

    def _build_child_env(self, request: HostRunnerStartRequest) -> dict[str, str]:
        env = os.environ.copy()
        python_path = str(self.repo_root / "PythonDataService")
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = python_path if not existing else f"{python_path}{os.pathsep}{existing}"
        env["IBKR_HOST"] = request.ibkr_host
        return env

    def _validate_run_dir(self, run_id: str) -> Path:
        match = _RUN_ID_RE.fullmatch(run_id)
        if match is None:
            raise HostRunnerError(status.HTTP_400_BAD_REQUEST, f"Invalid run_id: {run_id!r}")
        run_dir = (self.live_runs_root / match.group(0)).resolve()
        if not run_dir.is_relative_to(self.live_runs_root):
            raise HostRunnerError(status.HTTP_400_BAD_REQUEST, f"Path traversal detected for run_id {run_id!r}")
        if not run_dir.is_dir():
            raise HostRunnerError(status.HTTP_404_NOT_FOUND, f"Run {run_id!r} not found under {self.live_runs_root}")
        return run_dir

    def _refresh_current(self) -> None:
        current = self._current
        if current is None:
            return
        if current.process.poll() is None:
            return
        if current.ended_at_ms is None:
            current.ended_at_ms = _now_ms()
            current.log_handle.close()


def create_app(manager: RunnerProcessManager | None = None, *, allowed_origins: list[str] | None = None) -> FastAPI:
    """Create the host-daemon FastAPI app."""
    process_manager = manager if manager is not None else _manager_from_env()
    app = FastAPI(
        title="learn-ai host live-run daemon",
        description="Host-side subprocess bridge for IBKR paper-run starts.",
        version="1.0.0",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins if allowed_origins is not None else _allowed_origins_from_env(),
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.get("/health", response_model=HostRunnerHealth)
    async def health() -> HostRunnerHealth:
        return process_manager.health()

    @app.get("/process", response_model=HostRunnerProcessStatus)
    async def process() -> HostRunnerProcessStatus:
        return process_manager.process_status()

    @app.get("/runs/{run_id}/process", response_model=HostRunnerProcessStatus)
    async def run_process(run_id: str) -> HostRunnerProcessStatus:
        return process_manager.process_status(run_id)

    @app.post("/runs/{run_id}/start", response_model=HostRunnerActionResponse)
    async def start_run(run_id: str, request: HostRunnerStartRequest) -> HostRunnerActionResponse:
        try:
            return process_manager.start(run_id, request)
        except HostRunnerError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail) from exc

    @app.post("/runs/{run_id}/stop", response_model=HostRunnerActionResponse)
    async def stop_run(run_id: str, request: HostRunnerStopRequest) -> HostRunnerActionResponse:
        try:
            return process_manager.stop(run_id, request)
        except HostRunnerError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail) from exc

    return app


def _manager_from_env() -> RunnerProcessManager:
    repo_root = Path(os.environ.get("LEARN_AI_REPO_ROOT", Path.cwd())).resolve()
    live_runs_root = Path(
        os.environ.get("LIVE_RUNS_ROOT", str(repo_root / "PythonDataService" / "artifacts" / "live_runs"))
    ).resolve()
    return RunnerProcessManager(repo_root=repo_root, live_runs_root=live_runs_root)


def _allowed_origins_from_env() -> list[str]:
    raw = os.environ.get("LIVE_RUNNER_DAEMON_ALLOWED_ORIGINS", _DEFAULT_ALLOWED_ORIGINS)
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def _creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) if os.name == "nt" else 0


def _send_graceful_stop(process: subprocess.Popen) -> None:
    if os.name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
        process.send_signal(signal.CTRL_BREAK_EVENT)
        return
    process.send_signal(signal.SIGINT)


def _now_ms() -> int:
    return int(time.time() * 1000)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the learn-ai host live-run daemon.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--live-runs-root",
        type=Path,
        default=None,
        help="Defaults to <repo-root>/PythonDataService/artifacts/live_runs.",
    )
    parser.add_argument(
        "--allowed-origins",
        default=_DEFAULT_ALLOWED_ORIGINS,
        help="Comma-separated browser origins allowed to call this daemon.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    live_runs_root = (
        args.live_runs_root.resolve()
        if args.live_runs_root is not None
        else (repo_root / "PythonDataService" / "artifacts" / "live_runs").resolve()
    )
    manager = RunnerProcessManager(repo_root=repo_root, live_runs_root=live_runs_root)
    app = create_app(
        manager,
        allowed_origins=[origin.strip() for origin in args.allowed_origins.split(",") if origin.strip()],
    )

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)
    return 0


app = create_app()


if __name__ == "__main__":
    raise SystemExit(main())
