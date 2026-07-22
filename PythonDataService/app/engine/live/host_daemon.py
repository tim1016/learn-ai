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
import contextlib
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
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from subprocess import Popen as _RealPopen
from typing import TextIO

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware

from app.engine.live.account_clerk_rpc import (
    AccountClerkRpcClient,
    AccountClerkRpcError,
    AccountClerkRpcRejectedError,
)
from app.engine.live.account_clerk_supervisor import (
    AccountClerkProcessEvidence,
    AccountClerkSupervisor,
    ManagedClerk,
)
from app.engine.live.account_clerk_supervisor import (
    AdoptedAccountClerkProcess as _AdoptedAccountClerkProcess,
)
from app.engine.live.account_clerk_supervisor import (
    inspect_account_clerk_process as _inspect_account_clerk_process,
)
from app.engine.live.broker_socket_probe import BrokerSocketProbeError, LsofSocketEnumerator
from app.engine.live.daemon_auth import TOKEN_HEADER, ensure_daemon_token, token_file_path
from app.engine.live.deploy import (
    ActionPlanReadinessError,
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
    UnsupportedBarSourceDescriptorError,
    deploy_run,
    git_head_sha,
)
from app.engine.live.desired_state import (
    DesiredState,
    DesiredStateCorruptError,
    DesiredStateRepo,
    stable_desired_state_path,
)
from app.engine.live.exit_taxonomy import classify_run_exit, read_run_exit_evidence
from app.engine.live.host_daemon_bot_events import (
    record_child_crash_launch_failure,
    record_spawn_launch_failure,
    redacted_daemon_path,
    should_record_child_launch_failure,
)
from app.engine.live.host_runner_policy import (
    host_process_ibkr_host,
    load_policy_env_file,
    validate_ibkr_host_allowed,
)
from app.engine.live.run_ledger import LiveRunStartDefaults
from app.engine.strategy.spec.schema import load_spec_from_path
from app.schemas.broker_session import GatewaySocketsSnapshot
from app.schemas.live_runs import (
    AccountClerkHealth,
    AccountEmergencyFlattenResponse,
    AuditCopySizingLookup,
    EmergencyFlattenRequest,
    HostRunnerActionResponse,
    HostRunnerClerkEnsureRequest,
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

# Existing focused host tests import this private evidence type. Keep that
# import-level compatibility while the real owner is the Clerk supervisor.
_AccountClerkProcessEvidence = AccountClerkProcessEvidence

_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{1,127}$")
# Fixed subdirectory holding committed QC audit copies (ADR 0006).
_QC_SHADOW_SUBDIR = Path("references") / "qc-shadow"
_DEFAULT_ALLOWED_ORIGINS = "http://localhost:4200,http://127.0.0.1:4200"
_STOP_WAIT_SECONDS = 2.0
_DEFAULT_IBKR_CLIENT_ID_POOL = "50-99"
_PROCESS_REAPER_INTERVAL_SECONDS = 1.0
_DEFAULT_EXITED_RECORD_RETENTION_COUNT = 10_000
_DEFAULT_EXITED_RECORD_RETENTION_TTL_MS = 7 * 24 * 60 * 60 * 1000
_IBKR_CLIENT_ID_MIN = 1
_IBKR_CLIENT_ID_MAX = 2**31 - 1
_IBKR_CLIENT_ID_POOL_MAX_SPAN = 1_000
_IBKR_CLIENT_ID_IN_USE = "IBKR_CLIENT_ID_IN_USE"
_EMERGENCY_FLATTEN_IBKR_CLIENT_ID = 1_000_000
_ACCOUNT_CLERK_HANDSHAKE_TIMEOUT_SECONDS = 5.0
_ACCOUNT_CLERK_TERMINATE_WAIT_SECONDS = 2.0
_ACCOUNT_CLERK_KILL_WAIT_SECONDS = 2.0


def _parse_ibkr_client_id_pool(raw: str) -> tuple[int, ...]:
    """Parse a daemon-owned IBKR client-id pool.

    Accepts comma-separated integers and inclusive ranges, e.g. ``50-99`` or
    ``50,51,60-65``. Client IDs are runtime session identity, not strategy
    identity; the daemon owns this pool so sibling bot processes can coexist
    without sharing a Gateway ``clientId``. Values must be nonzero and ranges
    are bounded before expansion so malformed env values cannot explode memory.
    """

    ids: list[int] = []
    seen: set[int] = set()
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            try:
                start = int(start_text.strip())
                end = int(end_text.strip())
            except ValueError as exc:
                raise ValueError(f"invalid IBKR client id range {token!r}") from exc
            if end < start:
                raise ValueError(f"invalid IBKR client id range {token!r}: end is before start")
            span = end - start + 1
            if span > _IBKR_CLIENT_ID_POOL_MAX_SPAN:
                raise ValueError(
                    f"IBKR client id range {token!r} spans {span} values; "
                    f"maximum is {_IBKR_CLIENT_ID_POOL_MAX_SPAN}"
                )
            values = range(start, end + 1)
        else:
            try:
                values = (int(token),)
            except ValueError as exc:
                raise ValueError(f"invalid IBKR client id {token!r}") from exc
        for value in values:
            if not (_IBKR_CLIENT_ID_MIN <= value <= _IBKR_CLIENT_ID_MAX):
                raise ValueError(f"IBKR client id {value} is outside the supported range")
            if value in seen:
                continue
            seen.add(value)
            ids.append(value)
    if not ids:
        raise ValueError("IBKR client id pool is empty")
    return tuple(ids)


def _host_platform() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform or "unknown"


def _host_supervisor() -> str:
    configured = os.environ.get("LIVE_RUNNER_SUPERVISOR")
    if configured:
        return configured.strip().lower()
    if sys.platform.startswith("win"):
        return "nssm"
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux"):
        return "systemd"
    return "manual"


def _orphan_candidate_payload(candidate: object) -> dict[str, object]:
    run_dir = getattr(candidate, "run_dir", None)
    sidecar = getattr(candidate, "sidecar", None)
    return {
        "run_id": getattr(candidate, "run_id", None),
        "run_dir": redacted_daemon_path(run_dir),
        "state": getattr(candidate, "state", None),
        "sidecar_age_ms": getattr(candidate, "sidecar_age_ms", None),
        "reason": getattr(candidate, "reason", None),
        "pid": getattr(sidecar, "pid", None) if sidecar is not None else None,
        "process_start_identity": (
            getattr(sidecar, "process_start_identity", None)
            if sidecar is not None
            else None
        ),
    }


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


HostRunnerErrorDetail = str | dict[str, object]


class HostRunnerError(RuntimeError):
    """Error that should be translated into a daemon HTTP response."""

    def __init__(self, status_code: int, detail: HostRunnerErrorDetail) -> None:
        super().__init__(
            detail if isinstance(detail, str) else detail.get("message", str(detail))
        )
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
    ibkr_client_id: int | None = None
    stopping: bool = False
    ended_at_ms: int | None = None
    registry_retired_at_ms: int | None = None
    lifecycle_outcome_recorded_at_ms: int | None = None
    launch_failure_recorded_at_ms: int | None = None


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
        exited_record_retention_count: int = _DEFAULT_EXITED_RECORD_RETENTION_COUNT,
        exited_record_retention_ttl_ms: int = _DEFAULT_EXITED_RECORD_RETENTION_TTL_MS,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        if exited_record_retention_count < 0:
            raise ValueError("exited_record_retention_count must be >= 0")
        if exited_record_retention_ttl_ms < 0:
            raise ValueError("exited_record_retention_ttl_ms must be >= 0")
        self.repo_root = repo_root.resolve()
        self.live_runs_root = live_runs_root.resolve()
        self._now_ms_override = now_ms
        self._exited_record_retention_count = exited_record_retention_count
        self._exited_record_retention_ttl_ms = exited_record_retention_ttl_ms
        self._exited_records_pruned_total = 0
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
        # A retirement prepares a durable fail-closed fence before touching
        # multiple account registries. Complete any interrupted transaction on
        # daemon boot so an AFK recovery cannot strand a bot between registry
        # and lifecycle state. A failed replay remains PENDING and therefore
        # continues to reject starts and Clerk intake for that identity.
        from app.services.bot_deletion import BotDeletionCorruptError, recover_pending_bot_retirements

        try:
            self._recovered_bot_retirements = recover_pending_bot_retirements(self.artifacts_root)
        except (BotDeletionCorruptError, OSError):
            self._recovered_bot_retirements = ()
            logger.exception("failed to recover pending bot retirement transitions")
        # PRD #619-B — periodic lease writer + the orphan-candidates list
        # the data plane reads via /health. Both are populated during the
        # FastAPI lifespan (``create_app``); ``None`` here until startup
        # has run.
        self._lease_writer: object | None = None
        self._orphan_candidates: list[object] = []
        self._managed: dict[str, ManagedProcess] = {}
        # IDs are reserved while a subprocess is being started outside the
        # registry lock.  A reservation closes the gap between allocation and
        # registration without making a socket readiness wait block every
        # health/status/start operation.
        self._reserved_ibkr_client_ids: set[int] = set()
        # VCR-P3-P / Phase 6D — per-instance start locks. Each key holds an
        # ``rlock`` so the (check_existing, spawn, register) sequence in
        # ``start`` runs serialised for one instance while different instances
        # remain free to start concurrently. Without this lock, two requests
        # both observe an absent ``_managed[key]`` and spawn duplicate
        # processes that race on the same run dir.
        import threading as _threading

        self._start_lock_per_instance: dict[str, _threading.RLock] = {}
        self._start_lock_table_lock = _threading.Lock()
        # Cross-instance registry guard. Client-id allocation observes the
        # managed-process registry, so allocate -> spawn -> register must be one
        # critical section; otherwise sibling starts can pick the same free id.
        self._managed_lock = _threading.RLock()
        self._rejected_ibkr_client_ids: set[int] = set()
        self._clerk_supervisor = AccountClerkSupervisor(
            repo_root=self.repo_root,
            artifacts_root=self.artifacts_root,
            now_ms=self._clock_ms,
            client_id_pool=self._ibkr_client_id_pool,
            external_client_ids=self._external_bot_client_ids,
            verify_generation=lambda artifacts_root, account_id: self._verify_account_clerk_generation(
                account_artifacts_root=artifacts_root,
                account_id=account_id,
            ),
            wait_for_readiness=lambda artifacts_root, account_id, generation: self._wait_for_account_clerk_socket(
                account_artifacts_root=artifacts_root,
                account_id=account_id,
                expected_generation=generation,
            ),
            # Resolve through this module's aliases at invocation time so the
            # host's process-observation seam remains patchable in focused tests.
            inspect_process=lambda pid: _inspect_account_clerk_process(pid),
            is_real_popen=lambda process: isinstance(process, _RealPopen),
            creation_flags=_creation_flags,
            terminate_wait_seconds=lambda: _ACCOUNT_CLERK_TERMINATE_WAIT_SECONDS,
            kill_wait_seconds=lambda: _ACCOUNT_CLERK_KILL_WAIT_SECONDS,
        )
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

    @property
    def _clerks(self) -> dict[str, ManagedClerk]:
        """Compatibility view of Clerk state owned by the dedicated supervisor."""

        return self._clerk_supervisor.clerks

    @property
    def _quarantined_account_clerk_client_ids(self) -> set[int]:
        """Compatibility view of Clerk IDs withheld pending confirmed exit."""

        return self._clerk_supervisor.quarantined_client_ids

    @property
    def _account_clerk_start_blockers(self) -> dict[str, str]:
        """Compatibility view of durable Clerk replacement blockers."""

        return self._clerk_supervisor.start_blockers

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
        lease_threshold_ms: int | None = None
        lease_write_error: str | None = None
        if self._lease_writer is not None:
            lease_status = getattr(self._lease_writer, "status", None)
            last_written_at_ms = getattr(self._lease_writer, "last_written_at_ms", None)
            lease_threshold_ms = getattr(self._lease_writer, "lease_threshold_ms", None)
            lease_write_error = getattr(self._lease_writer, "last_write_error", None)
        return HostRunnerHealth(
            ok=True,
            repo_root=str(self.repo_root),
            live_runs_root=str(self.live_runs_root),
            fetched_at_ms=self._clock_ms(),
            process=self._first_process_status(),
            clerks=self._clerk_health(),
            git_sha=running,
            repo_head_sha=on_disk,
            code_stale=bool(running and on_disk and running != on_disk),
            commits_behind=self._commits_behind(running, on_disk),
            daemon_boot_id=self.boot_id,
            lease_status=lease_status,
            last_lease_written_at_ms=last_written_at_ms,
            lease_threshold_ms=lease_threshold_ms,
            lease_write_error=lease_write_error,
            orphan_candidates_count=len(self._orphan_candidates),
            orphan_candidates=[
                _orphan_candidate_payload(candidate)
                for candidate in self._orphan_candidates
            ],
            platform=_host_platform(),
            supervisor=_host_supervisor(),
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
        with self._managed_lock:
            to_refresh = tuple(self._managed.values())
        for managed in to_refresh:
            self._refresh(managed)
        with self._managed_lock:
            self._prune_exited_records_locked()
            exited_record_count = 0
            for managed in self._managed.values():
                if managed.process.poll() is not None:
                    exited_record_count += 1
                out.append(
                    HostRunnerInstance(
                        strategy_instance_id=managed.strategy_instance_id,
                        run_id=managed.run_id,
                        run_dir=str(managed.run_dir),
                        process=self._status_of(managed),
                    )
                )
        return HostRunnerInstancesStatus(
            instances=out,
            fetched_at_ms=self._clock_ms(),
            exited_record_retention_count=self._exited_record_retention_count,
            exited_record_retention_ttl_ms=self._exited_record_retention_ttl_ms,
            exited_record_count=exited_record_count,
            exited_records_pruned_total=self._exited_records_pruned_total,
        )

    def running_managed_run_ids(self) -> frozenset[str]:
        """Run ids backed by a child process owned by this daemon boot."""
        with self._managed_lock:
            return frozenset(
                managed.run_id
                for managed in self._managed.values()
                if managed.process.poll() is None
            )

    def _clock_ms(self) -> int:
        if self._now_ms_override is not None:
            return self._now_ms_override()
        return _now_ms()

    def reap_exited_processes(self) -> None:
        """Settle every exited child independently of status read traffic."""
        with self._managed_lock:
            to_refresh = tuple(self._managed.values())
        for managed in to_refresh:
            self._refresh(managed)
        with self._managed_lock:
            self._prune_exited_records_locked()
        self._supervise_account_clerks()

    def instance_status(self, strategy_instance_id: str) -> HostRunnerProcessStatus:
        """Return a pure process observation for one strategy instance.

        This endpoint is also used by retirement while it holds the durable
        per-bot fence. It must never trigger lifecycle reaping, otherwise the
        daemon would wait for that fence while the router waits for this read.
        Background reaping owns terminal persistence.
        """
        with self._managed_lock:
            managed = self._managed.get(strategy_instance_id)
        if managed is None:
            return HostRunnerProcessStatus(
                state=HostRunnerProcessState.idle,
                message=f"No managed process for strategy_instance_id {strategy_instance_id!r}.",
            )
        return self._status_of(managed)

    def process_status(self, run_id: str | None = None) -> HostRunnerProcessStatus:
        """Return a run's subprocess state (back-compat, run-addressed).

        With no ``run_id``, returns the first running managed process (or
        idle). With a ``run_id``, returns that run's process if the registry
        tracks it, else idle — so selected-run controls don't inherit another
        run's status.
        """
        if run_id is None:
            process_status = self._first_process_status()
        else:
            managed = self._by_run_id(run_id)
            process_status = (
                self._persisted_terminal_process_status(run_id)
                or HostRunnerProcessStatus(
                    state=HostRunnerProcessState.idle,
                    message=f"No host runner process for {run_id}.",
                )
                if managed is None
                else self._status_of(managed)
            )
        # The compatibility status endpoint historically performed the idle
        # Clerk reap after observing a bot exit.  Keep that behavior, but do
        # the bounded process work after releasing the registry lock.
        self._supervise_account_clerks()
        return process_status

    def _persisted_terminal_process_status(self, run_id: str) -> HostRunnerProcessStatus | None:
        """Recover a dead run's proof after daemon restart without trusting stale files alone."""

        if _RUN_ID_RE.fullmatch(run_id) is None:
            return None
        try:
            payload = json.loads((self.live_runs_root / run_id / "run_status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get("run_id") != run_id:
            return None

        ended_at_ms = payload.get("ended_at_ms")
        host_pid = payload.get("host_pid")
        exit_code = payload.get("exit_code")
        exit_reason = payload.get("exit_reason")
        started_at_ms = payload.get("started_at_ms")
        if (
            type(ended_at_ms) is not int
            or type(host_pid) is not int
            or host_pid < 1
            or type(exit_code) is not int
            or (exit_reason is not None and not isinstance(exit_reason, str))
        ):
            return None
        try:
            os.kill(host_pid, 0)
        except ProcessLookupError:
            pass
        except OSError:
            return None
        else:
            return None

        return HostRunnerProcessStatus(
            state=HostRunnerProcessState.exited,
            run_id=run_id,
            pid=host_pid,
            started_at_ms=started_at_ms if type(started_at_ms) is int else None,
            ended_at_ms=ended_at_ms,
            exit_code=exit_code,
            exit_reason=exit_reason,
            message="Host runner process has a persisted terminal status and its PID is absent.",
        )

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
                ibkr_client_id=managed.ibkr_client_id,
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
            ibkr_client_id=managed.ibkr_client_id,
            started_at_ms=managed.started_at_ms,
            ended_at_ms=managed.ended_at_ms,
            exit_code=exit_code,
            exit_reason=_exit_reason_from_code(exit_code),
            command=managed.command,
            log_path=str(managed.log_path),
            message=f"Host runner process exited with code {exit_code}.",
        )

    def _first_process_status(self) -> HostRunnerProcessStatus:
        with self._managed_lock:
            to_refresh = tuple(self._managed.values())
        for managed in to_refresh:
            self._refresh(managed)
        with self._managed_lock:
            for managed in self._managed.values():
                if managed.process.poll() is None:
                    return self._status_of(managed)
            return HostRunnerProcessStatus(state=HostRunnerProcessState.idle, message="No host runner process.")

    def _by_run_id(self, run_id: str) -> ManagedProcess | None:
        with self._managed_lock:
            for managed in self._managed.values():
                if managed.run_id == run_id:
                    break
            else:
                return None
        self._refresh(managed)
        return managed

    @contextlib.contextmanager
    def _bot_lifecycle_operation_fence(self, strategy_instance_id: str | None):
        """Use the durable cross-writer fence when this is a valid bot ID."""

        if not strategy_instance_id:
            yield
            return
        from app.engine.live.identity import validate_strategy_instance_id
        from app.services.bot_deletion import bot_lifecycle_operation_fence

        try:
            validate_strategy_instance_id(strategy_instance_id)
        except ValueError:
            # Keep legacy/invalid-id errors at the existing admission
            # boundary; an unsafe value must never become a lock path.
            yield
            return
        with bot_lifecycle_operation_fence(self.artifacts_root, strategy_instance_id):
            yield

    @contextlib.contextmanager
    def _instance_start_lock(self, key: str, strategy_instance_id: str | None):
        """Fence a Start through admission, registry activation, and spawn.

        The in-memory lock prevents duplicate starts in this daemon. The
        durable fence adds the retirement and Clerk writers in other processes
        to the same critical section.
        """
        with self._start_lock_table_lock:
            lock = self._start_lock_per_instance.get(key)
            if lock is None:
                import threading as _threading

                lock = _threading.RLock()
                self._start_lock_per_instance[key] = lock
        with lock, self._bot_lifecycle_operation_fence(strategy_instance_id):
            yield

    def _account_clerk_lock(self, account_id: str):
        """Return the supervisor-owned lifecycle lock for one Clerk account."""

        return self._clerk_supervisor.account_lock(account_id)

    def _reject_start_during_legacy_emergency(self, account_id: str | None) -> None:
        """Keep a new bot from joining an account while its legacy panic lane is fenced."""

        if account_id is None:
            return
        from app.engine.live.account_artifacts import active_legacy_emergency_fence_id

        try:
            fence_id = active_legacy_emergency_fence_id(self.artifacts_root, account_id)
        except (OSError, ValueError) as exc:
            raise HostRunnerError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "could not read the legacy emergency fence before start",
            ) from exc
        if fence_id is not None:
            raise HostRunnerError(
                status.HTTP_409_CONFLICT,
                {
                    "reason_code": "CLERK_LEGACY_EMERGENCY_FENCE",
                    "message": "Start is refused while the account emergency fence is active.",
                },
            )

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
        account_id, _ = self._account_registry_identity(run_dir)
        self._reject_start_during_legacy_emergency(account_id)

        with self._instance_start_lock(key, sid):
            clerk_client_ids = self._clerk_supervisor.in_use_client_ids()
            # Reserve the bot's client ID under the registry lock, then release
            # it before any filesystem/broker readiness work.  The reservation
            # prevents a concurrent start from allocating the same ID.
            with self._managed_lock:
                existing = self._managed.get(key)
                if existing is not None:
                    self._refresh(existing, operation_fence_held=True)
                    if existing.process.poll() is None:
                        raise HostRunnerError(
                            status.HTTP_409_CONFLICT,
                            f"Host runner already active for {existing.run_id} (instance {key!r}). "
                            "Stop it before starting another run for this instance.",
                        )

            self._enforce_desired_state_allows_start(sid)
            self._enforce_lifecycle_allows_start(sid)
            if sid:
                from app.services.bot_deletion import BotDeletionCorruptError, bot_retirement_is_pending

                try:
                    retirement_pending = bot_retirement_is_pending(self.artifacts_root, sid)
                except BotDeletionCorruptError as exc:
                    raise HostRunnerError(
                        status.HTTP_409_CONFLICT,
                        "bot retirement transition is unreadable; repair it before starting",
                    ) from exc
                if retirement_pending:
                    raise HostRunnerError(
                        status.HTTP_409_CONFLICT,
                        "bot retirement transition is pending; wait for recovery before starting",
                    )
            self._enforce_crash_retired_recovery(run_dir)
            command = self._build_start_command(
                run_dir,
                request,
                self._sibling_symbols(key),
                self._sibling_all_in_symbols(key),
            )
            ibkr_client_id = self._allocate_ibkr_client_id(
                exclude_key=key,
                clerk_client_ids=clerk_client_ids,
            )

            env = self._build_child_env(request, ibkr_client_id=ibkr_client_id)
            log_path = run_dir / "host_daemon.log"
            account_id = None
            active_binding_written = False
            try:
                log_handle = log_path.open("a", encoding="utf-8")
            except OSError:
                with self._managed_lock:
                    self._reserved_ibkr_client_ids.discard(ibkr_client_id)
                raise

            try:
                account_freeze = self._write_account_registry_binding(
                    run_dir,
                    run_id=run_id,
                    lifecycle_state="ACTIVE",
                    source="host_daemon.start",
                )
                active_binding_written = True
                if account_freeze is not None:
                    raise HostRunnerError(
                        status.HTTP_409_CONFLICT,
                        f"Account is frozen by {account_freeze.source}: {account_freeze.reason}",
                    )
                account_id, _ = self._account_registry_identity(run_dir)
                if account_id is not None:
                    # The initial lease is synchronously persisted by
                    # ``_ensure_account_clerk`` before the bot is allowed to
                    # reach any submit surface.  A bot must never race ahead
                    # of the account authority it depends on.
                    self._ensure_account_clerk(account_id, ibkr_host=request.ibkr_host)
                process = subprocess.Popen(
                    command,
                    cwd=str(self.repo_root),
                    env=env,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    creationflags=_creation_flags(),
                    start_new_session=(os.name != "nt"),
                )
            except HostRunnerError:
                log_handle.close()
                with self._managed_lock:
                    self._reserved_ibkr_client_ids.discard(ibkr_client_id)
                if active_binding_written:
                    try:
                        self._write_account_registry_binding(
                            run_dir,
                            run_id=run_id,
                            lifecycle_state="RETIRED",
                            source="host_daemon.start_rejected_before_spawn",
                        )
                    except HostRunnerError:
                        logger.exception(
                            "Failed to retire account registry binding after rejected pre-spawn start",
                            extra={"run_id": run_id, "strategy_instance_id": key},
                        )
                try:
                    self._record_failed_launch_outcome(
                        run_dir,
                        run_id=run_id,
                        strategy_instance_id=sid,
                        source="host_daemon.start_rejected_before_spawn",
                    )
                except (OSError, ValueError, RuntimeError):
                    logger.exception(
                        "Failed to record rejected launch lifecycle outcome",
                        extra={"run_id": run_id, "strategy_instance_id": key},
                    )
                raise
            except OSError as exc:
                log_handle.close()
                with self._managed_lock:
                    self._reserved_ibkr_client_ids.discard(ibkr_client_id)
                record_spawn_launch_failure(
                    run_dir,
                    run_id=run_id,
                    strategy_instance_id=sid or run_id,
                    command=command,
                    log_path=log_path,
                    exc=exc,
                    ts_ms=_now_ms(),
                )
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
                try:
                    self._record_failed_launch_outcome(
                        run_dir,
                        run_id=run_id,
                        strategy_instance_id=sid,
                        source="host_daemon.start_failed",
                    )
                except (OSError, ValueError, RuntimeError):
                    logger.exception(
                        "Failed to record spawn-failure lifecycle outcome",
                        extra={"run_id": run_id, "strategy_instance_id": key},
                    )
                raise HostRunnerError(
                    status.HTTP_503_SERVICE_UNAVAILABLE, f"Could not start host runner: {exc}"
                ) from exc

            with self._managed_lock:
                self._managed[key] = ManagedProcess(
                    strategy_instance_id=sid,
                    run_id=run_id,
                    run_dir=run_dir,
                    process=process,
                    command=command,
                    started_at_ms=_now_ms(),
                    log_path=log_path,
                    log_handle=log_handle,
                    ibkr_client_id=ibkr_client_id,
                )
                self._reserved_ibkr_client_ids.discard(ibkr_client_id)
            logger.info(
                "Started host live runner for run=%s instance=%s with pid=%s ibkr_client_id=%s",
                run_id,
                key,
                process.pid,
                ibkr_client_id,
            )
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
        self._execute_emergency_flatten(run_id=run_id, run_dir=run_dir, account=account)
        return HostRunnerActionResponse(accepted=True, process=self.process_status(run_id))

    def emergency_flatten_account(self, account: str) -> AccountEmergencyFlattenResponse:
        """Mint an audit run and flatten a paper account without a surviving bot run."""

        run_id = f"eflat-{uuid.uuid4().hex}"
        run_dir = self.live_runs_root / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        self._execute_emergency_flatten(run_id=run_id, run_dir=run_dir, account=account)
        return AccountEmergencyFlattenResponse(
            accepted=True,
            account_id=account,
            audit_run_id=run_id,
            completed_at_ms=_now_ms(),
        )

    def _execute_emergency_flatten(self, *, run_id: str, run_dir: Path, account: str) -> None:
        """Run the canonical one-shot CLI under the per-account exclusion fence."""

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
        # Generate the id before the RPC.  A timed-out request may already have
        # committed the durable open event, so this identity is what lets us
        # distinguish that safe-to-release case from an activation failure.
        fence_id = uuid.uuid4().hex
        fence_activated = False
        command_error: HostRunnerError | None = None
        try:
            self._activate_legacy_emergency_fence(account, fence_id=fence_id)
            fence_activated = True
            self._refuse_legacy_emergency_with_active_runs(account)
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
            env["IBKR_HOST"] = host_process_ibkr_host(env.get("IBKR_HOST", "auto"))
            # This one-shot client must not inherit the data-plane's or an
            # ordinary bot's session identity.  The dedicated ID is outside
            # the configurable host-runner pool (whose default is 50-99).
            env["IBKR_CLIENT_ID"] = str(_EMERGENCY_FLATTEN_IBKR_CLIENT_ID)
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
                command_error = HostRunnerError(http_code, f"emergency-flatten failed: {detail}")
                raise command_error
            logger.info("emergency-flatten completed for run=%s account=%s", run_id, account)
        except HostRunnerError as exc:
            command_error = exc
            raise
        finally:
            if fence_activated:
                try:
                    self._release_legacy_emergency_fence(account, fence_id=fence_id)
                except HostRunnerError:
                    logger.exception(
                        "legacy emergency fence could not be released after subprocess exit",
                        extra={"account_id": account, "fence_id": fence_id},
                    )
                    if command_error is None:
                        raise
            with self._flatten_lock:
                self._flatten_in_flight.discard(account)

    def _activate_legacy_emergency_fence(self, account_id: str, *, fence_id: str) -> None:
        """Close Clerk broker-write intake before invoking the temporary second writer."""

        try:
            self._ensure_account_clerk(account_id)
            receipt = asyncio.run(
                AccountClerkRpcClient(
                    artifacts_root=self.artifacts_root,
                    account_id=account_id,
                ).activate_legacy_emergency_fence(fence_id=fence_id)
            )
        except AccountClerkRpcError as exc:
            # The Clerk may have appended the open event just before a socket
            # timeout or malformed response reached the host.  Re-read the
            # durable authority record using the caller-owned identity before
            # deciding whether the fence needs a compensating release.
            from app.engine.live.account_artifacts import active_legacy_emergency_fence_id

            try:
                active_fence_id = active_legacy_emergency_fence_id(self.artifacts_root, account_id)
            except (OSError, ValueError) as read_exc:
                raise HostRunnerError(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "Emergency flatten could not confirm whether Clerk intake was fenced.",
                ) from read_exc
            if active_fence_id == fence_id:
                logger.warning(
                    "legacy emergency fence activation response was lost; durable fence confirmed",
                    extra={"account_id": account_id, "fence_id": fence_id, "reason_code": exc.reason_code},
                )
                return
            unavailable = exc.reason_code.startswith("ACCOUNT_CLERK_UNAVAILABLE:")
            reason_code = exc.reason if isinstance(exc, AccountClerkRpcRejectedError) else exc.reason_code
            raise HostRunnerError(
                status.HTTP_503_SERVICE_UNAVAILABLE if unavailable else status.HTTP_409_CONFLICT,
                {
                    "reason_code": reason_code,
                    "message": "Emergency flatten could not close Clerk broker-write intake.",
                },
            ) from exc
        except OSError as exc:
            raise HostRunnerError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Emergency flatten could not start the account Clerk to close intake.",
            ) from exc
        if receipt.fence_id != fence_id:
            raise HostRunnerError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Emergency flatten received a mismatched Clerk fence receipt.",
            )

    def _release_legacy_emergency_fence(self, account_id: str, *, fence_id: str) -> None:
        """Reopen Clerk intake only after the external emergency process has exited."""

        try:
            asyncio.run(
                AccountClerkRpcClient(
                    artifacts_root=self.artifacts_root,
                    account_id=account_id,
                ).release_legacy_emergency_fence(fence_id=fence_id)
            )
        except AccountClerkRpcError as exc:
            unavailable = exc.reason_code.startswith("ACCOUNT_CLERK_UNAVAILABLE:")
            reason_code = exc.reason if isinstance(exc, AccountClerkRpcRejectedError) else exc.reason_code
            raise HostRunnerError(
                status.HTTP_503_SERVICE_UNAVAILABLE if unavailable else status.HTTP_409_CONFLICT,
                {
                    "reason_code": reason_code,
                    "message": "Emergency flatten exited but Clerk intake remains fenced.",
                },
            ) from exc

    def _refuse_legacy_emergency_with_active_runs(self, account_id: str) -> None:
        """Refuse the second broker writer while any current binding remains ACTIVE."""

        from app.engine.live.account_registry import index_account_instance_bindings, read_account_instance_registry

        try:
            bindings = index_account_instance_bindings(
                read_account_instance_registry(self.artifacts_root, account_id),
                account_id=account_id,
            ).latest_by_instance.values()
        except (OSError, ValueError) as exc:
            raise HostRunnerError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "could not read account bindings before emergency flatten",
            ) from exc
        active_instance_ids = sorted(
            binding.strategy_instance_id
            for binding in bindings
            if binding.lifecycle_state == "ACTIVE"
        )
        if active_instance_ids:
            raise HostRunnerError(
                status.HTTP_409_CONFLICT,
                {
                    "reason_code": "EMERGENCY_FLATTEN_ACTIVE_RUNS_SURVIVE",
                    "message": "Emergency flatten is refused while ACTIVE bot bindings survive.",
                    "strategy_instance_ids": active_instance_ids,
                },
            )

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
            start_defaults=LiveRunStartDefaults(
                strategy=request.start_options.strategy,
                readonly=request.start_options.readonly,
                hydrate_policy=request.start_options.hydrate_policy,
                max_orders_per_day=request.start_options.max_orders_per_day,
                ibkr_host=request.start_options.ibkr_host,
            ),
            parent_run_id=request.parent_run_id,
            redeploy_reason=request.redeploy_reason,
            force=request.force,
            idempotent=True,
        )
        try:
            result = self._deploy_and_persist_lifecycle(request, params)
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
        except UnsupportedBarSourceDescriptorError as exc:
            raise HostRunnerError(status.HTTP_400_BAD_REQUEST, f"Unsupported bar source: {exc}") from exc
        except ActionPlanReadinessError as exc:
            raise HostRunnerError(
                status.HTTP_409_CONFLICT,
                {
                    "reason_code": exc.reason_code,
                    "message": exc.message,
                    "remediation": (
                        "Open the Action plan step and add the required entry/exit legs, "
                        "or choose a strategy/runtime path that supports this plan shape."
                    ),
                    "gate_id": "deploy.action_plan",
                },
            ) from exc
        except SpecOrAuditMissingError as exc:
            raise HostRunnerError(status.HTTP_400_BAD_REQUEST, f"Missing input: {exc}") from exc
        except GitUnavailableError as exc:
            raise HostRunnerError(status.HTTP_503_SERVICE_UNAVAILABLE, f"git unavailable: {exc}") from exc
        except DeployIOError as exc:
            raise HostRunnerError(status.HTTP_503_SERVICE_UNAVAILABLE, f"deploy filesystem error: {exc}") from exc

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

    def _deploy_and_persist_lifecycle(
        self,
        request: HostRunnerDeployRequest,
        params: DeployParams,
    ):
        """Make the deliberate deploy/reopen successor atomic with retirement."""

        with self._bot_lifecycle_operation_fence(request.strategy_instance_id):
            result = deploy_run(params)
            if request.strategy_instance_id:
                self._reopen_retired_lifecycle_for_deploy(request.strategy_instance_id, result.run_id)
            self._write_account_registry_binding(
                result.run_dir,
                run_id=result.run_id,
                lifecycle_state="DEPLOYED",
                source="host_daemon.deploy",
            )
            return result

    def _reopen_retired_lifecycle_for_deploy(self, strategy_instance_id: str, run_id: str) -> None:
        """Record the explicit deploy successor while its operation fence is held."""

        from app.engine.live.bot_lifecycle_state import (
            BotLifecyclePhase,
            BotLifecycleStateCorruptError,
            BotLifecycleStateRepo,
            stable_bot_lifecycle_state_path,
        )

        try:
            repo = BotLifecycleStateRepo(
                stable_bot_lifecycle_state_path(self.artifacts_root, strategy_instance_id)
            )
            current = repo.read()
            if current is not None and current.phase is BotLifecyclePhase.RETIRED:
                repo.reopen_for_deploy(
                    now_ms=self._clock_ms(),
                    updated_by="host_daemon",
                    reason="deploy.replacement",
                )
        except (BotLifecycleStateCorruptError, OSError) as exc:
            raise HostRunnerError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                {
                    "reason_code": "BOT_LIFECYCLE_REOPEN_FAILED",
                    "message": "The deploy could not reopen the bot lifecycle marker.",
                    "strategy_instance_id": strategy_instance_id,
                    "run_id": run_id,
                },
            ) from exc

    def _write_account_registry_binding(
        self,
        run_dir: Path,
        *,
        run_id: str,
        lifecycle_state: str,
        source: str,
    ):
        account_id, strategy_instance_id = self._account_registry_identity(run_dir)
        if account_id is None or strategy_instance_id is None:
            return None
        from app.engine.live.account_artifacts import (
            evaluate_restart_intensity,
            read_account_freeze,
        )
        from app.engine.live.account_registry import (
            AccountInstanceBinding,
            bot_order_namespace_for_instance,
            write_account_instance_binding,
        )

        try:
            recorded_at_ms = self._clock_ms()
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

    def _account_registry_identity(self, run_dir: Path) -> tuple[str | None, str | None]:
        ledger_path = run_dir / "run_ledger.json"
        if not ledger_path.is_file():
            return None, None
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
            return None, None
        if not isinstance(strategy_instance_id, str) or not strategy_instance_id:
            return None, None
        return account_id, strategy_instance_id

    def _enforce_crash_retired_recovery(self, run_dir: Path) -> None:
        account_id, strategy_instance_id = self._account_registry_identity(run_dir)
        if account_id is None or strategy_instance_id is None:
            return
        from app.engine.live.account_registry import crash_retired_restart_blocking_binding

        try:
            blocking_binding = crash_retired_restart_blocking_binding(
                self.artifacts_root,
                account_id=account_id,
                strategy_instance_id=strategy_instance_id,
            )
        except (OSError, ValueError) as exc:
            raise HostRunnerError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"could not read account recovery evidence: {exc}",
            ) from exc
        if blocking_binding is None:
            return
        from app.engine.live.exit_taxonomy import (
            LIVENESS_UNPROVEN_RETIRED_BINDING_SOURCES,
        )

        failure = (
            "is not owned by the current host daemon and its liveness is unproven"
            if blocking_binding.source in LIVENESS_UNPROVEN_RETIRED_BINDING_SOURCES
            else "crashed"
        )
        raise HostRunnerError(
            status.HTTP_409_CONFLICT,
            (
                f"Previous host runner for {strategy_instance_id!r} {failure} without later account recovery proof. "
                "Reconcile or record an audited recovery override before restarting this binding."
            ),
        )

    def _enforce_desired_state_allows_start(self, strategy_instance_id: str | None) -> None:
        if not strategy_instance_id:
            return
        try:
            path = stable_desired_state_path(self.artifacts_root, strategy_instance_id)
            desired_state = DesiredStateRepo(
                path, trusted_root=self.artifacts_root / "live_state"
            ).read_state()
        except (DesiredStateCorruptError, OSError, ValueError) as exc:
            raise HostRunnerError(
                status.HTTP_409_CONFLICT,
                f"desired_state sidecar is unreadable for {strategy_instance_id!r}: {exc}",
            ) from exc
        if desired_state is DesiredState.STOPPED:
            raise HostRunnerError(
                status.HTTP_409_CONFLICT,
                {
                    "reason_code": "STOPPED_REQUIRES_RESUME",
                    "message": (
                        f"{strategy_instance_id} is durably STOPPED. Resume the bot to clear "
                        "the stop latch before starting or using Deploy & start."
                    ),
                    "remediation": (
                        "Use Resume to set desired_state=RUNNING, then start the bot. "
                        "Use Deploy only when you want to stage a new run without starting it."
                    ),
                    "gate_id": "desired_state.start",
                    "desired_state": "STOPPED",
                    "strategy_instance_id": strategy_instance_id,
                },
            )

    def _enforce_lifecycle_allows_start(self, strategy_instance_id: str | None) -> None:
        """Do not resurrect a retirement unless deploy reopened it under the fence."""

        if not strategy_instance_id:
            return
        from app.engine.live.bot_lifecycle_state import (
            BotLifecyclePhase,
            BotLifecycleStateCorruptError,
            BotLifecycleStateRepo,
            stable_bot_lifecycle_state_path,
        )

        try:
            lifecycle = BotLifecycleStateRepo(
                stable_bot_lifecycle_state_path(self.artifacts_root, strategy_instance_id)
            ).read()
        except (BotLifecycleStateCorruptError, OSError, ValueError) as exc:
            raise HostRunnerError(
                status.HTTP_409_CONFLICT,
                f"bot lifecycle state is unreadable for {strategy_instance_id!r}: {exc}",
            ) from exc
        if lifecycle is not None and lifecycle.phase is BotLifecyclePhase.RETIRED:
            raise HostRunnerError(
                status.HTTP_409_CONFLICT,
                {
                    "reason_code": "RETIRED_REQUIRES_DEPLOY",
                    "message": (
                        f"{strategy_instance_id} is retired. Deploy its deliberate replacement "
                        "before attempting to start it."
                    ),
                    "strategy_instance_id": strategy_instance_id,
                },
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
            "--artifacts-root",
            str(self.artifacts_root),
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
        """Best-effort: resolve a run's effective trading symbol.

        Live configuration is authoritative because a deployment-validation
        run can override the shared strategy spec's default symbol.  The spec
        remains the fallback for legacy runs without an explicit live symbol.
        """
        try:
            data = json.loads((run_dir / "run_ledger.json").read_text(encoding="utf-8"))
            live_config = data.get("live_config")
            if isinstance(live_config, dict):
                live_symbol = live_config.get("symbol")
                if isinstance(live_symbol, str) and live_symbol.strip():
                    return live_symbol.strip().upper()
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
        with self._managed_lock:
            siblings = tuple(
                managed for key, managed in self._managed.items() if key != exclude_key
            )
        for managed in siblings:
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
        with self._managed_lock:
            siblings = tuple(
                managed for key, managed in self._managed.items() if key != exclude_key
            )
        for managed in siblings:
            self._refresh(managed)
            if managed.process.poll() is None and _is_set_holdings_full(
                self._read_sibling_sizing(managed.run_dir)
            ):
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

    def _ibkr_client_id_pool(self) -> tuple[int, ...]:
        raw = os.environ.get("LIVE_RUNNER_IBKR_CLIENT_ID_POOL", _DEFAULT_IBKR_CLIENT_ID_POOL)
        try:
            return _parse_ibkr_client_id_pool(raw)
        except ValueError as exc:
            raise HostRunnerError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"Invalid LIVE_RUNNER_IBKR_CLIENT_ID_POOL: {exc}",
            ) from exc

    def _external_bot_client_ids(self, *, exclude_key: str = "") -> set[int]:
        """Snapshot bot-side identities without reaching into Clerk state."""

        active: set[int] = set()
        with self._managed_lock:
            active.update(self._rejected_ibkr_client_ids)
            active.update(self._reserved_ibkr_client_ids)
            managed_snapshot = tuple(
                managed for key, managed in self._managed.items() if key != exclude_key
            )
        for managed in managed_snapshot:
            self._refresh(managed)
            if managed.process.poll() is None and managed.ibkr_client_id is not None:
                active.add(managed.ibkr_client_id)
        return active

    def _active_ibkr_client_ids(self, *, exclude_key: str) -> set[int]:
        """All broker identities unavailable to a bot allocation."""

        return self._external_bot_client_ids(exclude_key=exclude_key) | self._clerk_supervisor.in_use_client_ids()

    def _allocate_ibkr_client_id(self, *, exclude_key: str, clerk_client_ids: set[int] | None = None) -> int:
        active = self._external_bot_client_ids(exclude_key=exclude_key)
        active.update(
            self._clerk_supervisor.in_use_client_ids()
            if clerk_client_ids is None
            else clerk_client_ids
        )
        with self._managed_lock:
            active.update(self._rejected_ibkr_client_ids)
            active.update(self._reserved_ibkr_client_ids)
            active.update(
                managed.ibkr_client_id
                for key, managed in self._managed.items()
                if key != exclude_key
                and managed.process.poll() is None
                and managed.ibkr_client_id is not None
            )
            for client_id in self._ibkr_client_id_pool():
                if client_id not in active:
                    self._reserved_ibkr_client_ids.add(client_id)
                    return client_id
        raise HostRunnerError(
            status.HTTP_409_CONFLICT,
            (
                "No IBKR client IDs are available in LIVE_RUNNER_IBKR_CLIENT_ID_POOL. "
                "Stop a sibling bot, expand the daemon client-id pool, or restart IB Gateway "
                "and the host daemon if a stale session is holding a quarantined slot."
            ),
        )

    def _build_child_env(self, request: HostRunnerStartRequest, *, ibkr_client_id: int) -> dict[str, str]:
        env = os.environ.copy()
        python_path = str(self.repo_root / "PythonDataService")
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = python_path if not existing else f"{python_path}{os.pathsep}{existing}"
        env["IBKR_HOST"] = host_process_ibkr_host(request.ibkr_host)
        env["IBKR_CLIENT_ID"] = str(ibkr_client_id)
        # A bot submits only through the Account Clerk RPC boundary.  Override
        # any write-capable value inherited from the daemon environment.
        env["IBKR_READONLY"] = "true"
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

    def reconcile_account_clerks_on_boot(self) -> None:
        """Resolve durable Clerk evidence before a bot can request a writer."""

        self._clerk_supervisor.reconcile_on_boot()

    def _account_ids_with_clerk_evidence(self) -> tuple[str, ...]:
        """Compatibility delegate for Clerk evidence discovery."""

        return self._clerk_supervisor.account_ids_with_evidence()

    def _resolve_orphan_account_clerk(self, account_id: str) -> ManagedClerk | None:
        """Compatibility delegate for orphan Clerk adoption."""

        return self._clerk_supervisor.resolve_orphan(account_id)

    def _terminate_account_clerk_process(
        self,
        process: subprocess.Popen | _AdoptedAccountClerkProcess,
        *,
        account_id: str,
    ) -> bool:
        """Compatibility delegate for Clerk TERM/KILL escalation."""

        return self._clerk_supervisor.terminate(process, account_id=account_id)

    def _ensure_account_clerk(
        self,
        account_id: str,
        *,
        ibkr_host: str | None = None,
    ) -> ManagedClerk:
        """Ensure account authority through the dedicated Clerk supervisor."""

        return self._clerk_supervisor.ensure(
            account_id,
            ibkr_host=ibkr_host,
        )

    def _release_account_clerk(self, account_id: str) -> bool:
        """Release account authority after an explicit broker detach."""

        return self._clerk_supervisor.release(account_id)

    @staticmethod
    def _verify_account_clerk_generation(
        *,
        account_artifacts_root: Path,
        account_id: str,
    ) -> int:
        """Complete one RPC handshake; socket presence is never readiness."""

        from app.engine.live.account_clerk_rpc import AccountClerkRpcClient

        return asyncio.run(
            AccountClerkRpcClient(
                artifacts_root=account_artifacts_root,
                account_id=account_id,
            ).verify_generation()
        )

    @staticmethod
    def _wait_for_account_clerk_socket(
        *,
        account_artifacts_root: Path,
        account_id: str,
        expected_generation: int,
    ) -> None:
        """Release bots only after a matching-generation Clerk proves readiness."""

        from app.engine.live.account_clerk_rpc import AccountClerkRpcError

        deadline = time.monotonic() + _ACCOUNT_CLERK_HANDSHAKE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                served_generation = RunnerProcessManager._verify_account_clerk_generation(
                    account_artifacts_root=account_artifacts_root,
                    account_id=account_id,
                )
            except AccountClerkRpcError:
                time.sleep(0.05)
                continue
            if served_generation == expected_generation:
                return
            time.sleep(0.05)
        raise OSError(
            f"Account clerk RPC generation {expected_generation} did not become ready for {account_id}"
        )

    def _clerk_health(self) -> list[AccountClerkHealth]:
        """Return the dedicated supervisor health snapshot."""

        return self._clerk_supervisor.health()

    def _supervise_account_clerks(self, *, account_ids: set[str] | None = None) -> None:
        """Keep every attached Account service healthy."""

        self._clerk_supervisor.supervise(account_ids=account_ids)

    def _refresh(self, managed: ManagedProcess, *, operation_fence_held: bool = False) -> None:
        """Settle a managed process if it has exited: stamp ``ended_at_ms``
        and close its log handle exactly once."""
        if managed.process.poll() is None:
            return
        if managed.strategy_instance_id and not operation_fence_held:
            with self._bot_lifecycle_operation_fence(managed.strategy_instance_id):
                self._refresh(managed, operation_fence_held=True)
            return
        if self._should_record_child_launch_failure(managed) and managed.launch_failure_recorded_at_ms is None:
            recorded_at_ms = _now_ms()
            if record_child_crash_launch_failure(
                managed.run_dir,
                run_id=managed.run_id,
                strategy_instance_id=managed.strategy_instance_id or managed.run_id,
                command=managed.command,
                log_path=managed.log_path,
                pid=managed.process.pid,
                returncode=managed.process.returncode,
                ts_ms=recorded_at_ms,
            ):
                managed.launch_failure_recorded_at_ms = recorded_at_ms
        self._quarantine_rejected_ibkr_client_id(managed)
        if managed.registry_retired_at_ms is None:
            try:
                if self._can_retire_account_registry_binding(managed):
                    self._write_account_registry_binding(
                        managed.run_dir,
                        run_id=managed.run_id,
                        lifecycle_state="RETIRED",
                        source=self._account_registry_retirement_source(managed),
                    )
                else:
                    logger.warning(
                        "Skipped stale host runner registry retirement after a newer binding",
                        extra={
                            "run_id": managed.run_id,
                            "strategy_instance_id": managed.strategy_instance_id,
                        },
                    )
                managed.registry_retired_at_ms = self._clock_ms()
            except HostRunnerError:
                logger.exception(
                    "Failed to retire account registry binding for exited host runner",
                    extra={
                        "run_id": managed.run_id,
                        "strategy_instance_id": managed.strategy_instance_id,
                    },
                )
        if managed.lifecycle_outcome_recorded_at_ms is None:
            try:
                self._record_terminal_lifecycle_outcome(managed, operation_fence_held=True)
                managed.lifecycle_outcome_recorded_at_ms = self._clock_ms()
            except (OSError, ValueError, RuntimeError):
                logger.exception(
                    "Failed to record terminal lifecycle outcome for exited host runner",
                    extra={
                        "run_id": managed.run_id,
                        "strategy_instance_id": managed.strategy_instance_id,
                    },
                )
        if managed.ended_at_ms is None:
            managed.ended_at_ms = self._clock_ms()
            managed.log_handle.close()

    def _prune_exited_records_locked(self) -> None:
        """Bound exited process records by TTL and count while preserving live ones."""

        if not self._managed:
            return
        now_ms = self._clock_ms()
        cutoff_ms = now_ms - self._exited_record_retention_ttl_ms
        pruned_keys: set[str] = set()
        retained_exited: list[tuple[str, ManagedProcess]] = []
        for key, managed in self._managed.items():
            if managed.process.poll() is None:
                continue
            ended_at_ms = managed.ended_at_ms
            if ended_at_ms is None:
                ended_at_ms = now_ms
                managed.ended_at_ms = ended_at_ms
            if self._exited_record_retention_ttl_ms == 0 or ended_at_ms < cutoff_ms:
                pruned_keys.add(key)
                continue
            retained_exited.append((key, managed))
        if self._exited_record_retention_count == 0:
            pruned_keys.update(key for key, _managed in retained_exited)
        elif len(retained_exited) > self._exited_record_retention_count:
            retained_exited.sort(
                key=lambda item: (
                    item[1].ended_at_ms if item[1].ended_at_ms is not None else -1,
                    item[1].started_at_ms,
                    item[0],
                ),
                reverse=True,
            )
            pruned_keys.update(
                key for key, _managed in retained_exited[self._exited_record_retention_count :]
            )
        for key in pruned_keys:
            self._managed.pop(key, None)
        self._exited_records_pruned_total += len(pruned_keys)

    def _quarantine_rejected_ibkr_client_id(self, managed: ManagedProcess) -> None:
        if managed.ibkr_client_id is None or managed.ibkr_client_id in self._rejected_ibkr_client_ids:
            return
        try:
            payload = json.loads((managed.run_dir / "run_status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(payload, dict) or payload.get("exit_error_code") != _IBKR_CLIENT_ID_IN_USE:
            return
        self._rejected_ibkr_client_ids.add(managed.ibkr_client_id)
        logger.warning(
            "Quarantined IBKR client id after Gateway rejected it",
            extra={
                "run_id": managed.run_id,
                "strategy_instance_id": managed.strategy_instance_id,
                "ibkr_client_id": managed.ibkr_client_id,
            },
        )

    @staticmethod
    def _should_record_child_launch_failure(managed: ManagedProcess) -> bool:
        return should_record_child_launch_failure(
            managed.run_dir,
            returncode=managed.process.returncode,
            stopping=managed.stopping,
        )

    @staticmethod
    def _account_registry_retirement_source(managed: ManagedProcess) -> str:
        evidence = read_run_exit_evidence(managed.run_dir)
        verdict = classify_run_exit(
            evidence,
            returncode=managed.process.returncode,
            stopping=managed.stopping,
        )
        return verdict.registry_source

    def _can_retire_account_registry_binding(self, managed: ManagedProcess) -> bool:
        """Never let a late reaper supersede a newer DEPLOYED/ACTIVE run."""

        account_id, strategy_instance_id = self._account_registry_identity(managed.run_dir)
        if account_id is None or strategy_instance_id is None:
            return True
        from app.engine.live.account_registry import (
            index_account_instance_bindings,
            read_account_instance_registry,
        )

        try:
            latest = index_account_instance_bindings(
                read_account_instance_registry(self.artifacts_root, account_id),
                account_id=account_id,
            ).latest_by_instance.get(strategy_instance_id)
        except (OSError, ValueError) as exc:
            raise HostRunnerError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"could not read account registry before reaping {managed.run_id}: {exc}",
            ) from exc
        return latest is None or latest.run_id == managed.run_id or latest.lifecycle_state not in {
            "DEPLOYED",
            "ACTIVE",
        }

    def _record_failed_launch_outcome(
        self,
        run_dir: Path,
        *,
        run_id: str,
        strategy_instance_id: str,
        source: str,
    ) -> None:
        """Record an unspawned run as a terminal lifecycle fact when identifiable."""

        if not strategy_instance_id:
            return
        from app.engine.live.bot_lifecycle_state import (
            BotDutyOutcome,
            BotLifecyclePhase,
            BotLifecycleStateRepo,
            stable_bot_lifecycle_state_path,
        )

        now_ms = self._clock_ms()
        repo = BotLifecycleStateRepo(stable_bot_lifecycle_state_path(self.artifacts_root, strategy_instance_id))
        current = repo.read()
        if current is not None and current.phase is BotLifecyclePhase.RETIRED:
            return
        repo.record_terminal_outcome(
            BotDutyOutcome(
                kind="FAILED_LAUNCH",
                reason_code="FAILED_LAUNCH",
                recorded_at_ms=now_ms,
                run_id=run_id,
            ),
            updated_by="host_daemon",
            reason=source,
        )

    def _record_terminal_lifecycle_outcome(
        self,
        managed: ManagedProcess,
        *,
        operation_fence_held: bool = False,
    ) -> None:
        """Project a reaped process into durable duty state without guessing success."""

        if not managed.strategy_instance_id:
            return
        if not operation_fence_held:
            with self._bot_lifecycle_operation_fence(managed.strategy_instance_id):
                self._record_terminal_lifecycle_outcome(managed, operation_fence_held=True)
            return
        from app.engine.live.bot_lifecycle_state import (
            BotDutyOutcome,
            BotLifecyclePhase,
            BotLifecycleStateRepo,
            stable_bot_lifecycle_state_path,
        )
        from app.engine.live.clock_out import (
            ClockOutReceiptCorruptError,
            clock_out_completion_is_durable,
            read_clock_out_receipt,
        )

        now_ms = self._clock_ms()
        repo = BotLifecycleStateRepo(
            stable_bot_lifecycle_state_path(self.artifacts_root, managed.strategy_instance_id)
        )
        current = repo.read()
        if current is not None and current.phase is BotLifecyclePhase.RETIRED:
            return
        try:
            receipt = read_clock_out_receipt(managed.run_dir)
        except ClockOutReceiptCorruptError:
            receipt = None
            reason_code = "CLOCK_OUT_RECEIPT_CORRUPT"
        else:
            reason_code = ""
        if (
            receipt is not None
            and receipt.run_id == managed.run_id
            and clock_out_completion_is_durable(managed.run_dir, receipt)
            and self._clock_out_stop_latch_is_durable(managed.strategy_instance_id)
        ):
            outcome = BotDutyOutcome(
                kind="CLOCKED_OUT_FLAT",
                reason_code=receipt.reason_code,
                recorded_at_ms=receipt.completed_at_ms,
                run_id=managed.run_id,
            )
            reason = "clock_out.flat_broker_evidence"
        else:
            verdict = classify_run_exit(
                read_run_exit_evidence(managed.run_dir),
                returncode=managed.process.returncode,
                stopping=managed.stopping,
            )
            kind = {
                "controlled_stop": "STOPPED",
                "interrupted": "STOPPED",
                "halted": "HALTED",
                "poisoned": "HALTED",
                "crashed": "CRASHED",
            }.get(verdict.category, "EXITED_UNVERIFIED")
            outcome = BotDutyOutcome(
                kind=kind,
                reason_code=reason_code or verdict.registry_source.upper().replace(".", "_"),
                recorded_at_ms=now_ms,
                run_id=managed.run_id,
            )
            reason = verdict.registry_source
        repo.record_terminal_outcome(
            outcome,
            updated_by="host_daemon",
            reason=reason,
            expected_active_run_id=managed.run_id,
        )

    def _clock_out_stop_latch_is_durable(self, strategy_instance_id: str) -> bool:
        """Require the completed command's STOPPED latch before a clean outcome."""

        try:
            return (
                DesiredStateRepo(
                    stable_desired_state_path(self.artifacts_root, strategy_instance_id),
                    trusted_root=self.artifacts_root / "live_state",
                ).read_state()
                is DesiredState.STOPPED
            )
        except (DesiredStateCorruptError, OSError, ValueError):
            return False

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
    # candidates left behind by a previous daemon boot, retire prior ACTIVE
    # account bindings that are not backed by this daemon's process registry,
    # then spawn the ``DaemonLeaseWriter`` which writes ``daemon_lease.json``
    # at 1Hz.
    # On shutdown: switch to ``DRAINING`` (an immediate flush) and
    # bounded-stop the writer so the watchdog observes the planned
    # transition. Failures are logged and tolerated — the daemon must
    # still start even if the control_plane directory is unwritable.
    from contextlib import asynccontextmanager

    from app.engine.live.account_registry import (
        retire_unmanaged_active_bindings_on_daemon_boot,
    )
    from app.engine.live.control_plane import DaemonLeaseWriter
    from app.engine.live.orphan_classifier import classify_runtime_candidates_on_boot

    async def _process_reaper(stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.to_thread(process_manager.reap_exited_processes)
            except Exception:
                logger.exception("host runner process reaper iteration failed")
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=_PROCESS_REAPER_INTERVAL_SECONDS,
                )
            except TimeoutError:
                continue

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
        try:
            # RPC handshakes and bounded process waits are synchronous host
            # operations.  Keep them off the FastAPI loop while ensuring this
            # happens before the daemon accepts a start request.
            await asyncio.to_thread(process_manager.reconcile_account_clerks_on_boot)
        except Exception:
            logger.exception("account Clerk reconciliation on boot failed")
        boot_reconcile = retire_unmanaged_active_bindings_on_daemon_boot(
            process_manager.artifacts_root,
            managed_run_ids=process_manager.running_managed_run_ids(),
            now_ms=_now_ms(),
        )
        if boot_reconcile.bindings_retired:
            logger.warning(
                "Retired account bindings with unproven daemon-boot liveness",
                extra={
                    "accounts_scanned": boot_reconcile.accounts_scanned,
                    "active_bindings_found": boot_reconcile.active_bindings_found,
                    "bindings_retired": boot_reconcile.bindings_retired,
                },
            )
        reaper_stop = asyncio.Event()
        reaper_task = asyncio.create_task(
            _process_reaper(reaper_stop),
            name="host-runner-process-reaper",
        )
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
            reaper_stop.set()
            try:
                await asyncio.wait_for(reaper_task, timeout=2.0)
            except TimeoutError:
                reaper_task.cancel()
                await asyncio.gather(reaper_task, return_exceptions=True)
                logger.error("host runner process reaper did not stop within 2 seconds")
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

    @app.get("/broker/sockets", response_model=GatewaySocketsSnapshot, dependencies=auth)
    async def broker_sockets(
        gateway_port: int = Query(default=4002, ge=1, le=65535),
    ) -> GatewaySocketsSnapshot:
        try:
            sockets = await run_in_threadpool(
                LsofSocketEnumerator().enumerate,
                gateway_port,
            )
        except BrokerSocketProbeError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail) from exc
        return GatewaySocketsSnapshot(
            fetched_at_ms=_now_ms(),
            gateway_port=gateway_port,
            sockets=sockets,
        )

    @app.post("/control-plane/renew-lease", response_model=HostRunnerHealth, dependencies=auth)
    async def renew_control_plane_lease() -> HostRunnerHealth:
        try:
            return await run_in_threadpool(process_manager.renew_control_plane_lease)
        except HostRunnerError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail) from exc

    @app.post("/accounts/{account_id}/clerk/ensure", response_model=HostRunnerHealth, dependencies=auth)
    async def ensure_account_clerk(
        account_id: str,
        request: HostRunnerClerkEnsureRequest,
    ) -> HostRunnerHealth:
        """Start and generation-handshake the sole Clerk before an operator cure."""

        try:
            validate_ibkr_host_allowed(request.ibkr_host)
            await run_in_threadpool(
                process_manager._ensure_account_clerk,
                account_id,
                ibkr_host=request.ibkr_host,
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        except HostRunnerError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail) from exc
        except OSError as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "reason_code": "ACCOUNT_CLERK_START_FAILED",
                    "message": str(exc),
                },
            ) from exc
        return process_manager.health()

    @app.post("/accounts/{account_id}/clerk/release", response_model=HostRunnerHealth, dependencies=auth)
    async def release_account_clerk(account_id: str) -> HostRunnerHealth:
        """Stop the Account service only for an explicit account detach."""

        released = await run_in_threadpool(process_manager._release_account_clerk, account_id)
        if not released:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "reason_code": "ACCOUNT_CLERK_RELEASE_UNCONFIRMED",
                    "message": "The Account service process did not confirm shutdown.",
                },
            )
        return process_manager.health()

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

    @app.post(
        "/accounts/{account_id}/emergency-flatten",
        response_model=AccountEmergencyFlattenResponse,
        dependencies=auth,
    )
    async def emergency_flatten_account(
        account_id: str,
        request: EmergencyFlattenRequest,
    ) -> AccountEmergencyFlattenResponse:
        if not request.confirm:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="emergency-flatten requires confirm=true")
        if request.account != account_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="request account does not match path")
        try:
            return await run_in_threadpool(process_manager.emergency_flatten_account, account_id)
        except HostRunnerError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail) from exc

    return app


def _manager_from_env() -> RunnerProcessManager:
    repo_root = Path(os.environ.get("LEARN_AI_REPO_ROOT", Path.cwd())).resolve()
    return RunnerProcessManager(
        repo_root=repo_root,
        live_runs_root=_live_runs_root_from_env(repo_root),
        exited_record_retention_count=_env_int(
            "LIVE_RUNNER_EXITED_RECORD_RETENTION_COUNT",
            _DEFAULT_EXITED_RECORD_RETENTION_COUNT,
            minimum=0,
        ),
        exited_record_retention_ttl_ms=(
            _env_int(
                "LIVE_RUNNER_EXITED_RECORD_RETENTION_TTL_SECONDS",
                _DEFAULT_EXITED_RECORD_RETENTION_TTL_MS // 1000,
                minimum=0,
            )
            * 1000
        ),
    )


def _env_int(name: str, default: int, *, minimum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}")
    return value


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
            "Dotenv file that supplies daemon-owned IBKR policy keys "
            "(IBKR_HOST_ALLOWLIST / IBKR_HOST / LIVE_RUNNER_IBKR_CLIENT_ID_POOL). "
            "Defaults to <repo-root>/.env."
        ),
    )
    parser.add_argument(
        "--exited-record-retention-count",
        type=int,
        default=_DEFAULT_EXITED_RECORD_RETENTION_COUNT,
        help="Maximum exited child-process records retained by /instances.",
    )
    parser.add_argument(
        "--exited-record-retention-ttl-seconds",
        type=int,
        default=_DEFAULT_EXITED_RECORD_RETENTION_TTL_MS // 1000,
        help="Maximum age for exited child-process records retained by /instances.",
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
    manager = RunnerProcessManager(
        repo_root=repo_root,
        live_runs_root=live_runs_root,
        exited_record_retention_count=args.exited_record_retention_count,
        exited_record_retention_ttl_ms=args.exited_record_retention_ttl_seconds * 1000,
    )
    # Resolve/generate the shared secret next to live_runs/ so the data-plane
    # container reads the same token through the artifacts bind mount.
    token = ensure_daemon_token(live_runs_root.parent)
    app = create_app(
        manager,
        allowed_origins=[origin.strip() for origin in args.allowed_origins.split(",") if origin.strip()],
        auth_token=token,
    )
    if loaded_policy_keys:
        logger.info("host daemon loaded IBKR policy keys from %s: %s", env_file, ",".join(loaded_policy_keys))
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
