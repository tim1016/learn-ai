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
import asyncio
import hmac
import ipaddress
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware

from app.engine.live.daemon_auth import TOKEN_HEADER, ensure_daemon_token, token_file_path
from app.engine.live.deploy import (
    DeployIOError,
    DeployParams,
    DirtyTreeError,
    ExplicitSurfaceSizingMismatchError,
    GitUnavailableError,
    InvalidInstanceIdError,
    RunAlreadyExistsError,
    SizingPolicyMissingError,
    SpecOrAuditMissingError,
    StrategyInstanceIdAlreadyUsedError,
    UnknownLiveConfigKeyError,
    deploy_run,
    git_head_sha,
)
from app.engine.live.host_runner_policy import load_policy_env_file, validate_ibkr_host_allowed
from app.engine.strategy.spec.schema import load_spec_from_path
from app.schemas.live_runs import (
    AuditCopySizingLookup,
    EmergencyFlattenRequest,
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


def _exit_reason_from_code(returncode: int | None) -> str:
    """VCR-0018-B / Phase 6B — best-effort exit-reason classifier.

    Maps the runtime's documented exit codes (``cmd_start``) to operator-
    facing strings so the cockpit can render "process gone" with a
    readable cause. Unknown codes fall through to a generic
    ``"exited(<rc>)"`` so the operator still sees the raw return code.
    """
    if returncode is None:
        return "alive"
    if returncode == 0:
        return "normal"
    if returncode == 1:
        return "fatal_halt"
    if returncode == 2:
        return "operator_refusal"
    if returncode == 3:
        return "exception"
    if returncode == 4:
        return "hydration_failure"
    return f"exited({returncode})"


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

    def __init__(
        self,
        *,
        repo_root: Path,
        live_runs_root: Path,
        boot_id: str | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.live_runs_root = live_runs_root.resolve()
        # PRD #619-B — daemon boot identity. Immutable per process
        # start. Spawned children read it via LIVE_RUNNER_DAEMON_BOOT_ID
        # and the child watchdog treats a mismatch as BOOT_ID_CHANGED.
        # Tests can pin a deterministic value; production lets uuid4()
        # generate a fresh one.
        import uuid as _uuid

        self.boot_id: str = boot_id if boot_id is not None else _uuid.uuid4().hex
        # ``artifacts_root`` is ``<live_runs_root>/..`` per the daemon's
        # existing convention (token + cache live alongside live_runs).
        self.artifacts_root: Path = self.live_runs_root.parent
        # PRD #619-B — periodic lease writer + the orphan-candidates list
        # the data plane reads via /health. Both are populated during the
        # FastAPI lifespan (``create_app``); ``None`` here until startup
        # has run.
        self._lease_writer: object | None = None
        self._orphan_candidates: list[object] = []
        self._managed: dict[str, ManagedProcess] = {}
        # VCR-P3-P / Phase 6D — per-instance start locks. Each key holds an
        # ``rlock`` so the (check_existing, spawn, register) sequence in
        # ``start`` runs serialised for one instance while different instances
        # remain free to start concurrently. Without this lock, two requests
        # both observe an absent ``_managed[key]`` and spawn duplicate
        # processes that race on the same run dir.
        import threading as _threading

        self._start_lock_per_instance: dict[str, _threading.RLock] = {}
        self._start_lock_table_lock = _threading.Lock()
        # The git SHA of the code THIS process is running, captured ONCE at
        # launch. The daemon does not reload on `git pull`, so the running code
        # is frozen at startup even as the working tree advances — comparing this
        # frozen SHA to the live HEAD is how an operator sees "restart needed to
        # apply merged fixes". Computing it live on every /health would instead
        # report the on-disk HEAD (which moves on pull), masking stale code —
        # the bug this fixes.
        self._launch_git_sha = self._compute_git_sha()
        # Serialize emergency-flatten per account: each runs the CLI, which
        # fetches positions then submits liquidating market orders, so two
        # concurrent runs for one account (retry / double-click / second tab)
        # could both act on the same pre-fill snapshot and double-liquidate.
        self._flatten_lock = threading.Lock()
        self._flatten_in_flight: set[str] = set()

    def health(self) -> HostRunnerHealth:
        """Return daemon health plus a representative active subprocess.

        Back-compat for the run-spine UI: surfaces the first running
        managed process (or idle). The instance-addressed view is
        :meth:`instances`.

        Code-freshness fields let an operator confirm "the daemon is running the
        merged fixes" instead of eyeballing a hash. The daemon does NOT reload on
        ``git pull`` — it must be restarted — so:
          * ``git_sha`` — the SHA of the code this process is actually running
            (captured at launch).
          * ``repo_head_sha`` — the live on-disk HEAD (what a restart would run).
          * ``code_stale`` — ``True`` when the two differ: restart to apply.
          * ``commits_behind`` — best-effort count of how far behind the working
            tree the running code is.
        All best-effort: ``None``/``False`` if git is unavailable.
        """
        running = self._launch_git_sha
        on_disk = self._compute_git_sha()
        # PRD #619-B — control-plane diagnostics. The lease writer is
        # populated during the FastAPI lifespan; before that it's None
        # and the report degrades to "no lease yet" gracefully.
        lease_status: str | None = None
        last_written_at_ms: int | None = None
        if self._lease_writer is not None:
            lease_status = getattr(self._lease_writer, "status", None)
            last_written_at_ms = getattr(self._lease_writer, "last_written_at_ms", None)
        return HostRunnerHealth(
            ok=True,
            repo_root=str(self.repo_root),
            live_runs_root=str(self.live_runs_root),
            fetched_at_ms=_now_ms(),
            process=self._first_process_status(),
            git_sha=running,
            repo_head_sha=on_disk,
            code_stale=bool(running and on_disk and running != on_disk),
            commits_behind=self._commits_behind(running, on_disk),
            daemon_boot_id=self.boot_id,
            lease_status=lease_status,
            last_lease_written_at_ms=last_written_at_ms,
            orphan_candidates_count=len(self._orphan_candidates),
        )

    def renew_control_plane_lease(self) -> HostRunnerHealth:
        """Force an immediate daemon lease write and return fresh health.

        This is the cockpit-side recovery nudge for
        ``CONTROL_PLANE_LEASE_STALE``. It only succeeds when this daemon
        process is reachable and owns a lease writer; it does not restart
        child processes or submit broker orders.
        """
        if self._lease_writer is None:
            raise HostRunnerError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "daemon lease writer is not available",
            )
        renew = getattr(self._lease_writer, "renew_now", None)
        if not callable(renew):
            raise HostRunnerError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "daemon lease writer cannot renew on demand",
            )
        try:
            renew()
        except OSError as exc:
            raise HostRunnerError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "daemon lease renewal failed",
            ) from exc
        return self.health()

    def _compute_git_sha(self) -> str | None:
        """Live HEAD of the daemon's repo_root, or None if git is unavailable."""
        try:
            return git_head_sha(self.repo_root)
        except GitUnavailableError:
            return None

    def _commits_behind(self, running: str | None, on_disk: str | None) -> int | None:
        """How many commits the running code is behind the working-tree HEAD.

        Best-effort: ``None`` when equal/unknown or git can't compute it (e.g.
        the running SHA isn't an ancestor of HEAD after a rebase)."""
        if not running or not on_disk or running == on_disk:
            return None
        try:
            proc = subprocess.run(
                ["git", "rev-list", "--count", f"{running}..{on_disk}"],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        out = proc.stdout.strip()
        return int(out) if proc.returncode == 0 and out.isdigit() else None

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

    def _instance_start_lock(self, key: str):
        """VCR-P3-P / Phase 6D — return the RLock for ``key``, creating one
        lazily under a table-level guard so two concurrent first-time starts
        for the same instance share the same lock."""
        with self._start_lock_table_lock:
            lock = self._start_lock_per_instance.get(key)
            if lock is None:
                import threading as _threading

                lock = _threading.RLock()
                self._start_lock_per_instance[key] = lock
            return lock

    def start(self, run_id: str, request: HostRunnerStartRequest) -> HostRunnerActionResponse:
        """Start ``app.engine.live.run start`` for an existing run directory.

        Keyed by the run's ``strategy_instance_id`` (resolved from the
        ledger; falls back to ``run_id`` for legacy runs). A second start for
        the *same* instance while it is running is rejected; different
        instances coexist as separate processes.

        VCR-P3-P / Phase 6D — the entire ``(check_existing, spawn, register)``
        sequence runs under a per-instance lock. Without it, two requests
        could both observe an absent ``_managed[key]`` and spawn duplicate
        processes that race on the same run dir.
        """
        run_dir = self._validate_run_dir(run_id)
        try:
            validate_ibkr_host_allowed(request.ibkr_host)
        except ValueError as exc:
            raise HostRunnerError(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        sid = self._resolve_strategy_instance_id(run_dir)
        key = sid or run_id

        with self._instance_start_lock(key):
            existing = self._managed.get(key)
            if existing is not None:
                self._refresh(existing)
                if existing.process.poll() is None:
                    raise HostRunnerError(
                        status.HTTP_409_CONFLICT,
                        f"Host runner already active for {existing.run_id} (instance {key!r}). "
                        "Stop it before starting another run for this instance.",
                    )

            account_freeze = self._write_account_registry_binding(
                run_dir,
                run_id=run_id,
                lifecycle_state="ACTIVE",
                source="host_daemon.start",
            )
            if account_freeze is not None:
                raise HostRunnerError(
                    status.HTTP_409_CONFLICT,
                    f"Account is frozen by {account_freeze.source}: {account_freeze.reason}",
                )
            command = self._build_start_command(
                run_dir,
                request,
                self._sibling_symbols(key),
                self._sibling_all_in_symbols(key),
            )
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
                try:
                    self._write_account_registry_binding(
                        run_dir,
                        run_id=run_id,
                        lifecycle_state="RETIRED",
                        source="host_daemon.start_failed",
                    )
                except HostRunnerError:
                    logger.exception(
                        "Failed to retire account registry binding after host runner spawn failure",
                        extra={"run_id": run_id, "strategy_instance_id": key},
                    )
                raise HostRunnerError(
                    status.HTTP_503_SERVICE_UNAVAILABLE, f"Could not start host runner: {exc}"
                ) from exc

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
            logger.info("Started host live runner for run=%s instance=%s with pid=%s", run_id, key, process.pid)
            return HostRunnerActionResponse(accepted=True, process=self.process_status(run_id))

    def emergency_flatten(self, run_id: str, account: str) -> HostRunnerActionResponse:
        """Account-wide emergency flatten via the existing one-shot CLI (§ 7.2 #6).

        Independent of any live binding — after a halt/poison the binding is gone,
        so the binding-gated console FLATTEN command is unavailable exactly when
        an operator wants to flatten. This reuses ``app.engine.live.run
        emergency-flatten`` (paper-guarded, ``--confirm`` + ``--account`` gated),
        run synchronously so the CLI's exit code drives the HTTP response. It is
        the blunt account-wide liquidate only; namespace-attributed
        reconciliation stays fail-closed (we don't best-guess broker ownership).
        """
        run_dir = self._validate_run_dir(run_id)
        # Claim the account so a concurrent flatten for it is rejected rather than
        # run against the same pre-fill position snapshot (double-liquidate). The
        # CLI runs OUTSIDE the lock, so a 120s flatten never blocks the daemon.
        with self._flatten_lock:
            if account in self._flatten_in_flight:
                raise HostRunnerError(
                    status.HTTP_409_CONFLICT,
                    f"emergency-flatten already in progress for account {account}",
                )
            self._flatten_in_flight.add(account)
        try:
            command = [
                sys.executable,
                "-m",
                "app.engine.live.run",
                "emergency-flatten",
                "--run-dir",
                str(run_dir),
                "--account",
                account,
                "--confirm",
            ]
            env = os.environ.copy()
            python_path = str(self.repo_root / "PythonDataService")
            existing = env.get("PYTHONPATH")
            env["PYTHONPATH"] = python_path if not existing else f"{python_path}{os.pathsep}{existing}"
            try:
                proc = subprocess.run(
                    command,
                    cwd=str(self.repo_root),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            except subprocess.TimeoutExpired as exc:
                raise HostRunnerError(
                    status.HTTP_504_GATEWAY_TIMEOUT,
                    f"emergency-flatten timed out after {exc.timeout}s",
                ) from exc
            except OSError as exc:
                raise HostRunnerError(
                    status.HTTP_503_SERVICE_UNAVAILABLE, f"could not run emergency-flatten: {exc}"
                ) from exc
            if proc.returncode != 0:
                # CLI exit codes: 2 = operator precondition (account mismatch / no
                # --confirm) -> 400; everything else (3 = broker/runtime) -> 502.
                http_code = status.HTTP_400_BAD_REQUEST if proc.returncode == 2 else status.HTTP_502_BAD_GATEWAY
                detail = (proc.stderr or proc.stdout or "").strip()[:500] or (
                    f"emergency-flatten exited {proc.returncode}"
                )
                raise HostRunnerError(http_code, f"emergency-flatten failed: {detail}")
            logger.info("emergency-flatten completed for run=%s account=%s", run_id, account)
            return HostRunnerActionResponse(accepted=True, process=self.process_status(run_id))
        finally:
            with self._flatten_lock:
                self._flatten_in_flight.discard(account)

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
            parent_run_id=request.parent_run_id,
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
        except InvalidInstanceIdError as exc:
            raise HostRunnerError(status.HTTP_400_BAD_REQUEST, f"Invalid deployment name: {exc}") from exc
        except StrategyInstanceIdAlreadyUsedError as exc:
            raise HostRunnerError(
                status.HTTP_409_CONFLICT,
                (
                    "Deployment name is already used by an existing strategy instance. "
                    "Bot names are durable strategy instance IDs because paths and "
                    "broker order references remain evidence. Redeploy from the current "
                    "run to continue the same instance, or choose a new name. "
                    f"Existing run: {exc.existing_run_id}."
                ),
            ) from exc
        except ExplicitSurfaceSizingMismatchError as exc:
            raise HostRunnerError(status.HTTP_400_BAD_REQUEST, f"Invalid sizing policy: {exc}") from exc
        except SizingPolicyMissingError as exc:
            raise HostRunnerError(status.HTTP_400_BAD_REQUEST, f"Sizing policy required: {exc}") from exc
        except UnknownLiveConfigKeyError as exc:
            raise HostRunnerError(status.HTTP_400_BAD_REQUEST, f"Unknown live_config key: {exc}") from exc
        except SpecOrAuditMissingError as exc:
            raise HostRunnerError(status.HTTP_400_BAD_REQUEST, f"Missing input: {exc}") from exc
        except GitUnavailableError as exc:
            raise HostRunnerError(status.HTTP_503_SERVICE_UNAVAILABLE, f"git unavailable: {exc}") from exc
        except DeployIOError as exc:
            raise HostRunnerError(status.HTTP_503_SERVICE_UNAVAILABLE, f"deploy filesystem error: {exc}") from exc

        self._write_account_registry_binding(
            result.run_dir,
            run_id=result.run_id,
            lifecycle_state="DEPLOYED",
            source="host_daemon.deploy",
        )

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

    def _write_account_registry_binding(
        self,
        run_dir: Path,
        *,
        run_id: str,
        lifecycle_state: str,
        source: str,
    ):
        ledger_path = run_dir / "run_ledger.json"
        if not ledger_path.is_file():
            return None
        try:
            data = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HostRunnerError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"could not read run ledger for account registry: {exc}",
            ) from exc
        account_id = data.get("account_id")
        strategy_instance_id = data.get("strategy_instance_id")
        if not isinstance(account_id, str) or not account_id:
            return
        if not isinstance(strategy_instance_id, str) or not strategy_instance_id:
            return
        from app.engine.live.account_artifacts import (
            AccountInstanceBinding,
            bot_order_namespace_for_instance,
            evaluate_restart_intensity,
            read_account_freeze,
            write_account_instance_binding,
        )

        try:
            recorded_at_ms = _now_ms()
            write_account_instance_binding(
                self.artifacts_root,
                AccountInstanceBinding(
                    account_id=account_id,
                    strategy_instance_id=strategy_instance_id,
                    run_id=run_id,
                    bot_order_namespace=bot_order_namespace_for_instance(strategy_instance_id),
                    lifecycle_state=lifecycle_state,
                    recorded_at_ms=recorded_at_ms,
                    source=source,
                ),
            )
            if lifecycle_state == "ACTIVE":
                evaluate_restart_intensity(
                    self.artifacts_root,
                    account_id=account_id,
                    now_ms=recorded_at_ms,
                )
                return read_account_freeze(self.artifacts_root, account_id)
            return None
        except (OSError, ValueError) as exc:
            raise HostRunnerError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"could not write account instance registry: {exc}",
            ) from exc

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

    def lookup_audit_copy_sizing(self, audit_copy_path: str, proposed_sizing: dict | None) -> dict:
        """ADR 0009 § 3 — Reference parity gate status for the deploy form.

        Resolves the operator's audit-copy choice against
        ``docs/references/audit-copy-sizing-allow-list.json``, re-verifying the
        file's sha. ``proposed_sizing`` is the canonical ``live_config.sizing``
        dict; pass ``None`` to query the registered rule without proposing one
        (the deploy form's initial render uses this to populate the gate
        banner).
        """
        from app.engine.execution.audit_copy_allow_list import lookup as _lookup
        from app.engine.execution.order_sizer import (
            parse_sizing_policy,
            policy_to_ledger_dict,
        )

        proposed_policy = parse_sizing_policy(proposed_sizing) if proposed_sizing else None
        verdict = _lookup(
            audit_copy_path=audit_copy_path,
            proposed_policy=proposed_policy,
            repo_root=self.repo_root,
        )
        return {
            "verdict": verdict.verdict,
            "detail": verdict.detail,
            "expected_rule": policy_to_ledger_dict(verdict.expected_rule)
            if verdict.expected_rule is not None
            else None,
            "actual_rule": policy_to_ledger_dict(verdict.actual_rule) if verdict.actual_rule is not None else None,
        }

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
        return QcAuditCopyListing(scope_root=_QC_SHADOW_SUBDIR.as_posix(), entries=sorted(entries))

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
        """Signal the host runner for ``run_id`` to stop.

        VCR-0018-B / Phase 6B — distinguishes "signal accepted" from
        "process exited" in the response. ``command_id`` is the stable
        per-stop identifier; ``stop_outcome`` is one of
        ``"signal_accepted"`` (process is alive but won't be after the
        runtime drains), ``"exited"`` (poll returned non-None), or
        ``"still_running_after_2s"`` (process did not exit within
        ``_STOP_WAIT_SECONDS``). The cockpit/CLI renders both stages so
        the operator can tell "signal sent" from "process gone".
        """
        import uuid as _uuid

        command_id = f"stop-{_uuid.uuid4().hex[:12]}"
        current = self._by_run_id(run_id)
        if current is None:
            raise HostRunnerError(status.HTTP_404_NOT_FOUND, f"No host runner process is being tracked for {run_id}.")
        if current.process.poll() is not None:
            # Already-exited race: report it as ``exited`` with the runner's
            # actual exit code so the cockpit doesn't render a phantom stop.
            return HostRunnerActionResponse(
                accepted=False,
                process=self.process_status(run_id),
                command_id=command_id,
                stop_outcome="exited",
                exit_reason=_exit_reason_from_code(current.process.returncode),
            )

        current.stopping = True
        if current.process.poll() is None:
            try:
                _send_graceful_stop(current.process)
            except OSError:
                self._refresh(current)
                if current.process.poll() is not None:
                    return HostRunnerActionResponse(
                        accepted=False,
                        process=self.process_status(run_id),
                        command_id=command_id,
                        stop_outcome="exited",
                        exit_reason=_exit_reason_from_code(current.process.returncode),
                    )
                raise
        stop_outcome = "still_running_after_2s"
        exit_reason: str | None = None
        try:
            current.process.wait(timeout=_STOP_WAIT_SECONDS)
            stop_outcome = "exited"
            exit_reason = _exit_reason_from_code(current.process.returncode)
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
                stop_outcome = "exited"
                exit_reason = _exit_reason_from_code(current.process.returncode)

        self._refresh(current)
        logger.info(
            "Stop requested for host live runner %s: outcome=%s exit_reason=%s",
            run_id,
            stop_outcome,
            exit_reason,
        )
        return HostRunnerActionResponse(
            accepted=True,
            process=self.process_status(run_id),
            command_id=command_id,
            stop_outcome=stop_outcome,
            exit_reason=exit_reason,
        )

    def _build_start_command(
        self,
        run_dir: Path,
        request: HostRunnerStartRequest,
        managed_symbols: set[str],
        sibling_all_in_symbols: set[str],
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
        if sibling_all_in_symbols:
            # ADR 0009 § 9 / Decision 13 — symbols where a sibling managed
            # instance currently runs SetHoldings(1.0). The coexistence guard
            # refuses to start a SetHoldings(1.0) run on any of these.
            command += [
                "--sibling-all-in-symbols",
                ",".join(sorted(sibling_all_in_symbols)),
            ]
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

    def _sibling_all_in_symbols(self, exclude_key: str) -> set[str]:
        """ADR 0009 § 9 / Decision 13 — symbols where a sibling running
        managed instance currently holds ``SetHoldings(1.0)``.

        The coexistence guard refuses to start a new ``SetHoldings(1.0)``
        run on any of these symbols. Reads each sibling's ledger
        ``live_config.sizing`` field; a malformed or absent sizing block
        is treated as "not all-in" (no contribution to the set).
        """
        from app.engine.live.pre_flight import _is_set_holdings_full

        symbols: set[str] = set()
        for key, managed in self._managed.items():
            if key == exclude_key:
                continue
            self._refresh(managed)
            if managed.process.poll() is None and _is_set_holdings_full(self._read_sibling_sizing(managed.run_dir)):
                symbol = self._resolve_symbol(managed.run_dir)
                if symbol:
                    symbols.add(symbol)
        return symbols

    @staticmethod
    def _read_sibling_sizing(run_dir: Path) -> dict | None:
        """Read a sibling run's ``live_config.sizing`` from its ledger.

        Returns ``None`` when the ledger / live_config / sizing field is
        absent or unreadable — siblings that predate the sizing policy
        never trigger the all-in coexistence guard.
        """
        try:
            data = json.loads((run_dir / "run_ledger.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        live_config = data.get("live_config")
        if not isinstance(live_config, dict):
            return None
        sizing = live_config.get("sizing")
        return sizing if isinstance(sizing, dict) else None

    def _build_child_env(self, request: HostRunnerStartRequest) -> dict[str, str]:
        env = os.environ.copy()
        python_path = str(self.repo_root / "PythonDataService")
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = python_path if not existing else f"{python_path}{os.pathsep}{existing}"
        env["IBKR_HOST"] = request.ibkr_host
        # PRD #619-B — propagate this daemon's boot_id so the spawned
        # child captures it as ``expected_daemon_boot_id`` and the
        # watchdog can detect daemon restart.
        env["LIVE_RUNNER_DAEMON_BOOT_ID"] = self.boot_id
        # ``IbkrConfig.live_runs_root`` defaults to the *container*
        # bind-mount path (``/app/artifacts/live_runs``) — the daemon
        # spawns the engine on the host, so without this override the
        # child resolves ``IbkrClient._record_broker_event``'s sink to
        # ``/app/...`` and every IBKR lifecycle event logs ENOENT.
        env["IBKR_LIVE_RUNS_ROOT"] = str(self.live_runs_root)
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


def create_app(
    manager: RunnerProcessManager | None = None,
    *,
    allowed_origins: list[str] | None = None,
    auth_token: str | None = None,
) -> FastAPI:
    """Create the host-daemon FastAPI app.

    ``auth_token`` is the shared secret every route requires in the
    ``X-Live-Runner-Token`` header. When ``None`` it is resolved (or generated)
    via :func:`ensure_daemon_token` so there is no unauthenticated mode — auth
    is mandatory regardless of bind interface (ADR 0007). PRD #619-C followup
    (Codex review P2): ``/health`` is now auth-gated alongside every other
    route. The data plane probe holds the token via the artifacts bind mount
    and forwards it; this is the only way for the connectivity monitor to
    surface ``AUTH_FAILED`` for a stale/missing/rotated token. External
    process-supervisor healthchecks (systemd / launchd / podman) must send the
    token too.
    """
    process_manager = manager if manager is not None else _manager_from_env()
    token = auth_token if auth_token is not None else ensure_daemon_token(_artifacts_root_from_env())

    # PRD #619-B — daemon lease lifespan. On startup: classify orphan
    # candidates left behind by a previous daemon boot, then spawn the
    # ``DaemonLeaseWriter`` which writes ``daemon_lease.json`` at 1Hz.
    # On shutdown: switch to ``DRAINING`` (an immediate flush) and
    # bounded-stop the writer so the watchdog observes the planned
    # transition. Failures are logged and tolerated — the daemon must
    # still start even if the control_plane directory is unwritable.
    from contextlib import asynccontextmanager

    from app.engine.live.control_plane import DaemonLeaseWriter
    from app.engine.live.orphan_classifier import classify_runtime_candidates_on_boot

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        try:
            process_manager._orphan_candidates = classify_runtime_candidates_on_boot(
                process_manager.live_runs_root,
                this_boot_id=process_manager.boot_id,
                now_ms=_now_ms(),
            )
        except Exception:
            logger.exception("orphan classification on boot failed")
        writer = DaemonLeaseWriter(
            artifacts_root=process_manager.artifacts_root,
            boot_id=process_manager.boot_id,
            now_ms=_now_ms,
        )
        process_manager._lease_writer = writer
        try:
            await writer.start()
        except Exception:
            logger.exception("daemon lease writer failed to start")
        try:
            yield
        finally:
            writer.set_draining()
            # Give the writer task one event-loop tick to observe the
            # ``DRAINING`` wake and write a final lease before we
            # ``stop()`` it. Without the yield, the writer may not
            # have run between ``set_draining()`` and ``stop()`` and
            # the watchdog would see ``CONNECTED`` on the way out.
            await asyncio.sleep(0)
            try:
                await writer.stop()
            except Exception:
                logger.exception("daemon lease writer stop failed")

    app = FastAPI(
        title="learn-ai host live-run daemon",
        description="Host-side subprocess bridge for IBKR paper-run starts.",
        version="1.0.0",
        lifespan=_lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins if allowed_origins is not None else _allowed_origins_from_env(),
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    async def _verify_token(
        supplied: str | None = Header(default=None, alias=TOKEN_HEADER),
    ) -> None:
        # Constant-time compare: response latency must not depend on which
        # byte of the token is wrong. See VCR-0011 / ADR 0007.
        if not hmac.compare_digest((supplied or "").encode("utf-8"), token.encode("utf-8")):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                detail=f"missing or wrong {TOKEN_HEADER}",
            )

    auth = [Depends(_verify_token)]

    @app.get("/health", response_model=HostRunnerHealth, dependencies=auth)
    async def health() -> HostRunnerHealth:
        return process_manager.health()

    @app.post("/control-plane/renew-lease", response_model=HostRunnerHealth, dependencies=auth)
    async def renew_control_plane_lease() -> HostRunnerHealth:
        try:
            return await run_in_threadpool(process_manager.renew_control_plane_lease)
        except HostRunnerError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail) from exc

    @app.get("/process", response_model=HostRunnerProcessStatus, dependencies=auth)
    async def process() -> HostRunnerProcessStatus:
        return process_manager.process_status()

    @app.get("/instances", response_model=HostRunnerInstancesStatus, dependencies=auth)
    async def instances() -> HostRunnerInstancesStatus:
        return process_manager.instances()

    @app.get(
        "/instances/{strategy_instance_id}/process",
        response_model=HostRunnerProcessStatus,
        dependencies=auth,
    )
    async def instance_process(strategy_instance_id: str) -> HostRunnerProcessStatus:
        return process_manager.instance_status(strategy_instance_id)

    @app.get("/runs/{run_id}/process", response_model=HostRunnerProcessStatus, dependencies=auth)
    async def run_process(run_id: str) -> HostRunnerProcessStatus:
        return process_manager.process_status(run_id)

    @app.get("/qc-audit-copies", response_model=QcAuditCopyListing, dependencies=auth)
    async def qc_audit_copies() -> QcAuditCopyListing:
        return process_manager.list_qc_audit_copies()

    @app.get(
        "/audit-copy-sizing-lookup",
        response_model=AuditCopySizingLookup,
        dependencies=auth,
    )
    async def audit_copy_sizing_lookup(
        audit_copy_path: str,
        proposed_sizing: str | None = None,
    ) -> AuditCopySizingLookup:
        """ADR 0009 § 3 — Reference parity gate status for the deploy form.

        ``proposed_sizing`` is a URL-encoded JSON object (the same dict the
        deploy submits as ``live_config.sizing``). Pass nothing for an
        informational lookup of the registered rule.
        """
        import json as _json

        sizing: dict | None = None
        if proposed_sizing:
            try:
                parsed = _json.loads(proposed_sizing)
            except _json.JSONDecodeError as exc:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail=f"proposed_sizing must be JSON: {exc}",
                ) from exc
            if not isinstance(parsed, dict):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail="proposed_sizing must be a JSON object",
                )
            sizing = parsed
        return AuditCopySizingLookup.model_validate(process_manager.lookup_audit_copy_sizing(audit_copy_path, sizing))

    @app.post("/deploy", response_model=HostRunnerDeployResponse, dependencies=auth)
    async def deploy(request: HostRunnerDeployRequest) -> HostRunnerDeployResponse:
        try:
            return await run_in_threadpool(process_manager.deploy, request)
        except HostRunnerError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail) from exc

    @app.post("/runs/{run_id}/start", response_model=HostRunnerActionResponse, dependencies=auth)
    async def start_run(run_id: str, request: HostRunnerStartRequest) -> HostRunnerActionResponse:
        try:
            return await run_in_threadpool(process_manager.start, run_id, request)
        except HostRunnerError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail) from exc

    @app.post("/runs/{run_id}/stop", response_model=HostRunnerActionResponse, dependencies=auth)
    async def stop_run(run_id: str, request: HostRunnerStopRequest) -> HostRunnerActionResponse:
        try:
            return await run_in_threadpool(process_manager.stop, run_id, request)
        except HostRunnerError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail) from exc

    @app.post(
        "/runs/{run_id}/emergency-flatten",
        response_model=HostRunnerActionResponse,
        dependencies=auth,
    )
    async def emergency_flatten_run(run_id: str, request: EmergencyFlattenRequest) -> HostRunnerActionResponse:
        if not request.confirm:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="emergency-flatten requires confirm=true")
        try:
            return await run_in_threadpool(process_manager.emergency_flatten, run_id, request.account)
        except HostRunnerError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail) from exc

    return app


def _manager_from_env() -> RunnerProcessManager:
    repo_root = Path(os.environ.get("LEARN_AI_REPO_ROOT", Path.cwd())).resolve()
    return RunnerProcessManager(repo_root=repo_root, live_runs_root=_live_runs_root_from_env(repo_root))


def _live_runs_root_from_env(repo_root: Path) -> Path:
    return Path(
        os.environ.get("LIVE_RUNS_ROOT", str(repo_root / "PythonDataService" / "artifacts" / "live_runs"))
    ).resolve()


def _artifacts_root_from_env() -> Path:
    """Artifacts root holding the shared token file (sibling to live_runs/).

    Parent of the live-runs root so the host daemon and the data-plane container
    view the same ``.host-daemon-token`` through the
    ``./PythonDataService/artifacts:/app/artifacts`` bind mount.
    """
    repo_root = Path(os.environ.get("LEARN_AI_REPO_ROOT", Path.cwd())).resolve()
    return _live_runs_root_from_env(repo_root).parent


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
    parser.add_argument("--host", default="127.0.0.1", type=_valid_bind_host)
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
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help=(
            "Dotenv file that supplies daemon-owned IBKR host policy keys "
            "(IBKR_HOST_ALLOWLIST / IBKR_HOST). Defaults to <repo-root>/.env."
        ),
    )
    return parser


def _valid_bind_host(host: str) -> str:
    """Validate ``--host`` is a bindable IP (or ``localhost``).

    Non-loopback addresses (e.g. ``0.0.0.0`` so the data-plane container can
    reach the daemon on Linux rootless podman) are allowed because every
    protected route now enforces shared-secret auth (ADR 0007). Garbage is still
    rejected so a typo fails fast at startup rather than binding nothing.
    """
    if host == "localhost":
        return host
    try:
        ipaddress.ip_address(host)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--host must be an IP address or 'localhost', got {host!r}.") from exc
    return host


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    repo_root = args.repo_root.resolve()
    env_file = args.env_file.resolve() if args.env_file is not None else repo_root / ".env"
    loaded_policy_keys = load_policy_env_file(env_file)
    live_runs_root = (
        args.live_runs_root.resolve()
        if args.live_runs_root is not None
        else (repo_root / "PythonDataService" / "artifacts" / "live_runs").resolve()
    )
    manager = RunnerProcessManager(repo_root=repo_root, live_runs_root=live_runs_root)
    # Resolve/generate the shared secret next to live_runs/ so the data-plane
    # container reads the same token through the artifacts bind mount.
    token = ensure_daemon_token(live_runs_root.parent)
    app = create_app(
        manager,
        allowed_origins=[origin.strip() for origin in args.allowed_origins.split(",") if origin.strip()],
        auth_token=token,
    )
    if loaded_policy_keys:
        logger.info("host daemon loaded IBKR host policy keys from %s: %s", env_file, ",".join(loaded_policy_keys))
    logger.info(
        "host daemon binding %s:%s with mandatory %s auth (token at %s)",
        args.host,
        args.port,
        TOKEN_HEADER,
        token_file_path(live_runs_root.parent),
    )
    # Log the executing code's git SHA at startup: the daemon is long-lived and
    # does NOT reload on `git pull`, so this is the operator's anchor for "which
    # code is this daemon actually running" after a fix merges.
    logger.info("host daemon code git_sha=%s (repo_root=%s)", manager._launch_git_sha, repo_root)

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)
    return 0


# No module-level ``app = create_app()``: that ran at import time and, via the
# default ``auth_token=None`` path, generated a token using the cwd as repo root.
# Under the systemd unit (``WorkingDirectory=PythonDataService``) that wrote a
# doubly-nested ``PythonDataService/PythonDataService/artifacts/.host-daemon-token``
# outside the ignore rule, which the deploy clean-tree gate then saw as a dirty
# tree (ADR 0007 P1). The daemon is launched via ``main()`` (``python -m`` or the
# console entry), which resolves the token from the explicit ``--repo-root``. An
# ASGI ``:app`` target, if ever needed, must build with an explicit repo root.


if __name__ == "__main__":
    raise SystemExit(main())
