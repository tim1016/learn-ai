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
import ipaddress
import json
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
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware

from app.engine.live.deploy import (
    DeployIOError,
    DeployParams,
    DirtyTreeError,
    GitUnavailableError,
    RunAlreadyExistsError,
    SpecOrAuditMissingError,
    deploy_run,
)
from app.engine.strategy.spec.schema import load_spec_from_path
from app.schemas.live_runs import (
    HostRunnerActionResponse,
    HostRunnerDeployRequest,
    HostRunnerDeployResponse,
    HostRunnerHealth,
    HostRunnerInstance,
    HostRunnerInstancesStatus,
    HostRunnerProcessState,
    HostRunnerProcessStatus,
    HostRunnerStartRequest,
    HostRunnerStopRequest,
    QcAuditCopyListing,
)

logger = logging.getLogger(__name__)

_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{1,127}$")
# Fixed subdirectory holding committed QC audit copies (ADR 0006).
_QC_SHADOW_SUBDIR = Path("references") / "qc-shadow"
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

    strategy_instance_id: str
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
    """Own host-side live-run subprocesses — one per strategy instance.

    Keyed by ``strategy_instance_id`` (ADR 0002 / ADR 0004): an executing
    and a shadow strategy coexist as separate OS processes. Legacy runs
    with no ledger binding key by ``run_id``. This registry is the sole
    authority for the live ``strategy_instance_id -> run_id`` binding —
    "live" is a process fact, not an artifact fact.
    """

    def __init__(self, *, repo_root: Path, live_runs_root: Path) -> None:
        self.repo_root = repo_root.resolve()
        self.live_runs_root = live_runs_root.resolve()
        self._managed: dict[str, ManagedProcess] = {}

    def health(self) -> HostRunnerHealth:
        """Return daemon health plus a representative active subprocess.

        Back-compat for the run-spine UI: surfaces the first running
        managed process (or idle). The instance-addressed view is
        :meth:`instances`.
        """
        return HostRunnerHealth(
            ok=True,
            repo_root=str(self.repo_root),
            live_runs_root=str(self.live_runs_root),
            fetched_at_ms=_now_ms(),
            process=self._first_process_status(),
        )

    def instances(self) -> HostRunnerInstancesStatus:
        """All managed instances with their live binding (registry authority)."""
        out: list[HostRunnerInstance] = []
        for managed in self._managed.values():
            self._refresh(managed)
            out.append(
                HostRunnerInstance(
                    strategy_instance_id=managed.strategy_instance_id,
                    run_id=managed.run_id,
                    run_dir=str(managed.run_dir),
                    process=self._status_of(managed),
                )
            )
        return HostRunnerInstancesStatus(instances=out, fetched_at_ms=_now_ms())

    def instance_status(self, strategy_instance_id: str) -> HostRunnerProcessStatus:
        """Live process status for one strategy instance (idle if untracked)."""
        managed = self._managed.get(strategy_instance_id)
        if managed is None:
            return HostRunnerProcessStatus(
                state=HostRunnerProcessState.idle,
                message=f"No managed process for strategy_instance_id {strategy_instance_id!r}.",
            )
        self._refresh(managed)
        return self._status_of(managed)

    def process_status(self, run_id: str | None = None) -> HostRunnerProcessStatus:
        """Return a run's subprocess state (back-compat, run-addressed).

        With no ``run_id``, returns the first running managed process (or
        idle). With a ``run_id``, returns that run's process if the registry
        tracks it, else idle — so selected-run controls don't inherit another
        run's status.
        """
        if run_id is None:
            return self._first_process_status()
        managed = self._by_run_id(run_id)
        if managed is None:
            return HostRunnerProcessStatus(
                state=HostRunnerProcessState.idle,
                message=f"No host runner process for {run_id}.",
            )
        return self._status_of(managed)

    def _status_of(self, managed: ManagedProcess) -> HostRunnerProcessStatus:
        """Build a process-status snapshot from a managed process."""
        sid = managed.strategy_instance_id or None
        exit_code = managed.process.poll()
        if exit_code is None:
            state = HostRunnerProcessState.stopping if managed.stopping else HostRunnerProcessState.running
            return HostRunnerProcessStatus(
                state=state,
                run_id=managed.run_id,
                strategy_instance_id=sid,
                pid=managed.process.pid,
                started_at_ms=managed.started_at_ms,
                command=managed.command,
                log_path=str(managed.log_path),
                message="Host runner process is active.",
            )
        return HostRunnerProcessStatus(
            state=HostRunnerProcessState.exited,
            run_id=managed.run_id,
            strategy_instance_id=sid,
            pid=managed.process.pid,
            started_at_ms=managed.started_at_ms,
            ended_at_ms=managed.ended_at_ms,
            exit_code=exit_code,
            command=managed.command,
            log_path=str(managed.log_path),
            message=f"Host runner process exited with code {exit_code}.",
        )

    def _first_process_status(self) -> HostRunnerProcessStatus:
        for managed in self._managed.values():
            self._refresh(managed)
        for managed in self._managed.values():
            if managed.process.poll() is None:
                return self._status_of(managed)
        return HostRunnerProcessStatus(state=HostRunnerProcessState.idle, message="No host runner process.")

    def _by_run_id(self, run_id: str) -> ManagedProcess | None:
        for managed in self._managed.values():
            if managed.run_id == run_id:
                self._refresh(managed)
                return managed
        return None

    def start(self, run_id: str, request: HostRunnerStartRequest) -> HostRunnerActionResponse:
        """Start ``app.engine.live.run start`` for an existing run directory.

        Keyed by the run's ``strategy_instance_id`` (resolved from the
        ledger; falls back to ``run_id`` for legacy runs). A second start for
        the *same* instance while it is running is rejected; different
        instances coexist as separate processes.
        """
        run_dir = self._validate_run_dir(run_id)
        sid = self._resolve_strategy_instance_id(run_dir)
        key = sid or run_id

        existing = self._managed.get(key)
        if existing is not None:
            self._refresh(existing)
            if existing.process.poll() is None:
                raise HostRunnerError(
                    status.HTTP_409_CONFLICT,
                    f"Host runner already active for {existing.run_id} (instance {key!r}). "
                    "Stop it before starting another run for this instance.",
                )

        command = self._build_start_command(run_dir, request, self._sibling_symbols(key))
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

        self._managed[key] = ManagedProcess(
            strategy_instance_id=sid,
            run_id=run_id,
            run_dir=run_dir,
            process=process,
            command=command,
            started_at_ms=_now_ms(),
            log_path=log_path,
            log_handle=log_handle,
        )
        logger.info(
            "Started host live runner for run=%s instance=%s with pid=%s", run_id, key, process.pid
        )
        return HostRunnerActionResponse(accepted=True, process=self.process_status(run_id))

    def deploy(self, request: HostRunnerDeployRequest) -> HostRunnerDeployResponse:
        """Create a run on the host via ``deploy_run`` (ADR 0006), optionally
        starting it.

        Only the host has the git working tree, so ``init-ledger`` must run
        here, not in the data-plane container. ``repo_root`` / ``run_root`` are
        the daemon's own; the spec and QC audit paths are confined under
        ``repo_root``. Idempotent on the content-addressed ``run_id``: an
        identical re-deploy returns ``created=False``.
        """
        spec_path = self._resolve_under_repo(request.strategy_spec_path, field="strategy_spec_path")
        audit_path = self._resolve_under_repo(request.qc_audit_copy_path, field="qc_audit_copy_path")

        params = DeployParams(
            repo_root=self.repo_root,
            strategy_spec_path=spec_path,
            qc_audit_copy_path=audit_path,
            qc_cloud_backtest_id=request.qc_cloud_backtest_id,
            account_id=request.account_id,
            start_date_ms=request.start_date_ms,
            run_root=self.live_runs_root,
            live_config=request.live_config,
            strategy_instance_id=request.strategy_instance_id,
            strategy_key=request.strategy_key,
            force=request.force,
            idempotent=True,
        )
        try:
            result = deploy_run(params)
        except DirtyTreeError as exc:
            raise HostRunnerError(
                status.HTTP_409_CONFLICT,
                f"Working tree is dirty; commit or stash before deploying. {exc}",
            ) from exc
        except RunAlreadyExistsError as exc:
            raise HostRunnerError(
                status.HTTP_409_CONFLICT,
                f"Run directory already exists without a matching ledger: {exc.run_dir}",
            ) from exc
        except SpecOrAuditMissingError as exc:
            raise HostRunnerError(status.HTTP_400_BAD_REQUEST, f"Missing input: {exc}") from exc
        except GitUnavailableError as exc:
            raise HostRunnerError(status.HTTP_503_SERVICE_UNAVAILABLE, f"git unavailable: {exc}") from exc
        except DeployIOError as exc:
            raise HostRunnerError(
                status.HTTP_503_SERVICE_UNAVAILABLE, f"deploy filesystem error: {exc}"
            ) from exc

        start_action: HostRunnerActionResponse | None = None
        if request.start:
            start_action = self.start(result.run_id, request.start_options)

        logger.info(
            "Deployed run=%s instance=%s (created=%s, started=%s)",
            result.run_id,
            request.strategy_instance_id or "(legacy)",
            result.created,
            request.start,
        )
        return HostRunnerDeployResponse(
            run_id=result.run_id,
            run_dir=str(result.run_dir),
            created=result.created,
            start=start_action,
        )

    def _git_tracked_under(self, subdir: Path) -> list[str]:
        """Repo-relative POSIX paths of git-tracked files under ``subdir``.

        ``git ls-files`` lists only committed/staged (tracked) files, so ignored
        and untracked files are excluded by construction. A non-repo / git
        failure raises so the caller can fail closed rather than fall back to a
        raw filesystem walk that would surface untracked files."""
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--", str(subdir)],
            capture_output=True,
            text=True,
            cwd=str(self.repo_root),
            timeout=10.0,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git ls-files failed: rc={proc.returncode} stderr={proc.stderr!r}")
        return [p for p in proc.stdout.split("\0") if p]

    def list_qc_audit_copies(self) -> QcAuditCopyListing:
        """List committed QC audit copies under ``references/qc-shadow`` (ADR 0006).

        Returns repo-relative POSIX paths so each can be passed straight back as
        a deploy's ``qc_audit_copy_path``. Only **git-tracked** files are listed:
        the ADR 0006 provenance contract is "committed QC audit copies", and the
        deploy clean-tree check assumes the audit copy is committed — surfacing
        ignored/untracked files would hand the UI a deploy option not backed by
        committed source. The scope root is fixed (not client input); each entry
        is re-confined under it to guard against symlink escapes. An absent
        directory yields an empty list, not an error.
        """
        scope_root = (self.repo_root / _QC_SHADOW_SUBDIR).resolve()
        if not (scope_root.is_dir() and scope_root.is_relative_to(self.repo_root)):
            return QcAuditCopyListing(scope_root=_QC_SHADOW_SUBDIR.as_posix(), entries=[])

        entries: list[str] = []
        for rel in self._git_tracked_under(_QC_SHADOW_SUBDIR):
            resolved = (self.repo_root / rel).resolve()
            if not resolved.is_file():
                continue  # tracked but deleted-on-disk
            if not resolved.is_relative_to(scope_root):
                continue  # symlink pointing outside the scope root
            entries.append(resolved.relative_to(self.repo_root).as_posix())
        return QcAuditCopyListing(
            scope_root=_QC_SHADOW_SUBDIR.as_posix(), entries=sorted(entries)
        )

    def _resolve_under_repo(self, raw: str, *, field: str) -> Path:
        """Resolve an operator-supplied path against ``repo_root`` and confine
        it there. Relative paths resolve under the repo; absolute paths are
        accepted only if they fall within it. Anything escaping the repo root
        is rejected (path-injection barrier)."""
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.repo_root / candidate
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.repo_root):
            raise HostRunnerError(
                status.HTTP_400_BAD_REQUEST,
                f"{field} escapes the repo root: {raw!r}",
            )
        return resolved

    def stop(self, run_id: str, request: HostRunnerStopRequest) -> HostRunnerActionResponse:
        """Signal the host runner for ``run_id`` to stop."""
        current = self._by_run_id(run_id)
        if current is None:
            raise HostRunnerError(
                status.HTTP_404_NOT_FOUND, f"No host runner process is being tracked for {run_id}."
            )
        if current.process.poll() is not None:
            return HostRunnerActionResponse(accepted=False, process=self.process_status(run_id))

        current.stopping = True
        if current.process.poll() is None:
            try:
                _send_graceful_stop(current.process)
            except OSError:
                self._refresh(current)
                if current.process.poll() is not None:
                    return HostRunnerActionResponse(accepted=False, process=self.process_status(run_id))
                raise
        try:
            current.process.wait(timeout=_STOP_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            if request.force:
                if current.process.poll() is None:
                    try:
                        current.process.kill()
                    except OSError:
                        self._refresh(current)
                        if current.process.poll() is None:
                            raise
                current.process.wait(timeout=_STOP_WAIT_SECONDS)

        self._refresh(current)
        logger.info("Stop requested for host live runner %s", run_id)
        return HostRunnerActionResponse(accepted=True, process=self.process_status(run_id))

    def _build_start_command(
        self, run_dir: Path, request: HostRunnerStartRequest, managed_symbols: set[str]
    ) -> list[str]:
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
        if managed_symbols:
            # Sibling symbols from other running instances on this account, so
            # the unexpected-position gate excludes them rather than flagging a
            # sibling as foreign contamination (ADR 0005, completes #395).
            command += ["--managed-symbols", ",".join(sorted(managed_symbols))]
        return command

    def _resolve_symbol(self, run_dir: Path) -> str | None:
        """Best-effort: the single trading symbol of a run, from its spec."""
        try:
            data = json.loads((run_dir / "run_ledger.json").read_text(encoding="utf-8"))
            spec_path = Path(data["strategy_spec_path"])
            if not spec_path.is_absolute():
                spec_path = self.repo_root / spec_path
            spec = load_spec_from_path(spec_path)
            return spec.symbols[0] if spec.symbols else None
        except (OSError, ValueError, KeyError, IndexError):
            return None

    def _sibling_symbols(self, exclude_key: str) -> set[str]:
        """Symbols owned by other currently-running managed instances."""
        symbols: set[str] = set()
        for key, managed in self._managed.items():
            if key == exclude_key:
                continue
            self._refresh(managed)
            if managed.process.poll() is None:
                symbol = self._resolve_symbol(managed.run_dir)
                if symbol:
                    symbols.add(symbol)
        return symbols

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

    def _refresh(self, managed: ManagedProcess) -> None:
        """Settle a managed process if it has exited: stamp ``ended_at_ms``
        and close its log handle exactly once."""
        if managed.process.poll() is None:
            return
        if managed.ended_at_ms is None:
            managed.ended_at_ms = _now_ms()
            managed.log_handle.close()

    def _resolve_strategy_instance_id(self, run_dir: Path) -> str:
        """Read ``strategy_instance_id`` from the run ledger (UI-0 binding).

        Parsed directly from JSON to keep the host daemon free of the
        artifact-stack dependencies. Empty string = legacy / unknown, which
        makes the registry key fall back to ``run_id``.
        """
        ledger_path = run_dir / "run_ledger.json"
        try:
            data = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        sid = data.get("strategy_instance_id")
        return sid if isinstance(sid, str) else ""


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

    @app.get("/instances", response_model=HostRunnerInstancesStatus)
    async def instances() -> HostRunnerInstancesStatus:
        return process_manager.instances()

    @app.get("/instances/{strategy_instance_id}/process", response_model=HostRunnerProcessStatus)
    async def instance_process(strategy_instance_id: str) -> HostRunnerProcessStatus:
        return process_manager.instance_status(strategy_instance_id)

    @app.get("/runs/{run_id}/process", response_model=HostRunnerProcessStatus)
    async def run_process(run_id: str) -> HostRunnerProcessStatus:
        return process_manager.process_status(run_id)

    @app.get("/qc-audit-copies", response_model=QcAuditCopyListing)
    async def qc_audit_copies() -> QcAuditCopyListing:
        return process_manager.list_qc_audit_copies()

    @app.post("/deploy", response_model=HostRunnerDeployResponse)
    async def deploy(request: HostRunnerDeployRequest) -> HostRunnerDeployResponse:
        try:
            return await run_in_threadpool(process_manager.deploy, request)
        except HostRunnerError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail) from exc

    @app.post("/runs/{run_id}/start", response_model=HostRunnerActionResponse)
    async def start_run(run_id: str, request: HostRunnerStartRequest) -> HostRunnerActionResponse:
        try:
            return await run_in_threadpool(process_manager.start, run_id, request)
        except HostRunnerError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail) from exc

    @app.post("/runs/{run_id}/stop", response_model=HostRunnerActionResponse)
    async def stop_run(run_id: str, request: HostRunnerStopRequest) -> HostRunnerActionResponse:
        try:
            return await run_in_threadpool(process_manager.stop, run_id, request)
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
    parser.add_argument("--host", default="127.0.0.1", type=_loopback_host)
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


def _loopback_host(host: str) -> str:
    """Validate that the unauthenticated daemon binds only to loopback."""
    if host == "localhost":
        return host
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--host must be loopback (127.0.0.1 / ::1 / localhost), got {host!r}."
        ) from exc
    if not parsed.is_loopback:
        raise argparse.ArgumentTypeError(f"--host must be loopback (127.0.0.1 / ::1 / localhost), got {host!r}.")
    return host


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
