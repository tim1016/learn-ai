"""Dedicated lifecycle supervision for the account-scoped Clerk process.

``RunnerProcessManager`` owns strategy-bot orchestration.  This module owns
the independent account authority process: durable evidence discovery,
identity-safe adoption, generation fencing, socket readiness, and retirement.
Keeping those concerns together is intentional: a Clerk can outlive the host
daemon that started it, whereas a managed bot cannot.
"""

from __future__ import annotations

import logging
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from subprocess import Popen as _RealPopen
from typing import TextIO

from app.schemas.live_runs import AccountClerkHealth

logger = logging.getLogger(__name__)


@dataclass
class ManagedClerk:
    """One daemon-supervised lease process for a broker account."""

    account_id: str
    generation: int
    ibkr_client_id: int
    process: subprocess.Popen | AdoptedAccountClerkProcess
    started_at_ms: int
    log_handle: TextIO


@dataclass(frozen=True)
class AccountClerkProcessEvidence:
    """A PID's stable start identity and command captured for safe adoption."""

    pid: int
    process_start_identity: str
    command: tuple[str, ...]


def observe_process_command(command: list[str]) -> tuple[int, str] | None:
    """Run an OS inspection command without consuming the daemon's Popen seam."""

    try:
        process = _RealPopen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, _stderr = process.communicate(timeout=2.0)
    except (OSError, subprocess.SubprocessError):
        return None
    return process.returncode, stdout


def inspect_account_clerk_process(pid: int) -> AccountClerkProcessEvidence | None:
    """Return identity evidence for a live PID, or ``None`` once it is gone.

    A lease PID is only a lead: it is never authority to adopt or signal a
    process.  The caller compares the stable OS start identity and the full
    Clerk command (including account, generation, and broker client identity)
    before doing either. ``ps`` is only an observation boundary; a failed
    observation is unresolved evidence, never permission to guess.
    """

    if pid < 1:
        return None
    if os.name == "nt":
        command = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "$p=Get-CimInstance Win32_Process -Filter 'ProcessId = "
                f"{pid}'; if ($null -ne $p) {{ $p.CreationDate; $p.CommandLine }}"
            ),
        ]
        result = observe_process_command(command)
        if result is None:
            return None
        returncode, stdout = result
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        if returncode != 0 or len(lines) < 2:
            return None
        start_identity, command_line = lines[0], lines[1]
    else:
        started = observe_process_command(["ps", "-p", str(pid), "-o", "lstart="])
        listed = observe_process_command(["ps", "-ww", "-p", str(pid), "-o", "command="])
        if started is None or listed is None:
            return None
        started_returncode, started_stdout = started
        listed_returncode, listed_stdout = listed
        start_identity = started_stdout.strip()
        command_line = listed_stdout.strip()
        if (
            started_returncode != 0
            or listed_returncode != 0
            or not start_identity
            or not command_line
        ):
            return None
    try:
        command_parts = tuple(shlex.split(command_line, posix=os.name != "nt"))
    except ValueError:
        return None
    if not command_parts:
        return None
    return AccountClerkProcessEvidence(
        pid=pid,
        process_start_identity=start_identity,
        command=command_parts,
    )


def account_clerk_process_matches(
    evidence: AccountClerkProcessEvidence,
    *,
    artifacts_root: Path,
    account_id: str,
    generation: int,
    ibkr_client_id: int,
) -> bool:
    """Require the observed process to be the exact Clerk named by its lease."""

    command = evidence.command
    try:
        module_index = command.index("-m")
        if command[module_index + 1] != "app.engine.live.account_clerk":
            return False
        artifacts_index = command.index("--artifacts-root")
        account_index = command.index("--account-id")
        generation_index = command.index("--generation")
        client_id_index = command.index("--ibkr-client-id")
        command_root = Path(command[artifacts_index + 1]).resolve()
        command_account_id = command[account_index + 1]
        command_generation = int(command[generation_index + 1])
        command_client_id = int(command[client_id_index + 1])
    except (IndexError, ValueError):
        return False
    return (
        command_root == artifacts_root.resolve()
        and command_account_id == account_id
        and command_generation == generation
        and command_client_id == ibkr_client_id
    )


class AdoptedAccountClerkProcess:
    """Popen-shaped safe handle for a Clerk inherited from a prior daemon."""

    def __init__(
        self,
        evidence: AccountClerkProcessEvidence,
        inspect_process: Callable[[int], AccountClerkProcessEvidence | None],
    ) -> None:
        self._evidence = evidence
        self._inspect_process = inspect_process
        self.pid = evidence.pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self._inspect_process(self.pid) == self._evidence:
            return None
        if self.returncode is None:
            # The OS does not expose another process's exit code. Identity loss
            # is terminal for this manager and never grants authority over a
            # PID that may now be reused by an unrelated process.
            self.returncode = 1
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(cmd="account-clerk", timeout=timeout)
            time.sleep(0.05)
        return self.returncode if self.returncode is not None else 1

    def terminate(self) -> None:
        if not self._signal(signal.SIGTERM):
            raise ProcessLookupError(self.pid)

    def kill(self) -> None:
        kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
        if not self._signal(kill_signal):
            raise ProcessLookupError(self.pid)

    def _signal(self, sig: int) -> bool:
        if self._inspect_process(self.pid) != self._evidence:
            return False
        try:
            os.kill(self.pid, sig)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        return True


class AccountClerkSupervisor:
    """Own the lifecycle of independent per-account Clerk processes.

    Callers provide the bot-side client identities as a snapshot callback. The
    supervisor never reaches into the bot registry or holds its lock, which
    keeps bounded Clerk readiness and process waits out of host process
    bookkeeping.
    """

    def __init__(
        self,
        *,
        repo_root: Path,
        artifacts_root: Path,
        now_ms: Callable[[], int],
        client_id_pool: Callable[[], tuple[int, ...]],
        external_client_ids: Callable[[], set[int]],
        verify_generation: Callable[[Path, str], int],
        wait_for_readiness: Callable[[Path, str, int], None],
        inspect_process: Callable[[int], AccountClerkProcessEvidence | None] = inspect_account_clerk_process,
        is_real_popen: Callable[[object], bool] | None = None,
        creation_flags: Callable[[], int] | None = None,
        terminate_wait_seconds: Callable[[], float] | None = None,
        kill_wait_seconds: Callable[[], float] | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.artifacts_root = artifacts_root
        self._now_ms = now_ms
        self._client_id_pool = client_id_pool
        self._external_client_ids = external_client_ids
        self._verify_generation = verify_generation
        self._wait_for_readiness = wait_for_readiness
        self._inspect_process = inspect_process
        self._is_real_popen = is_real_popen or (lambda process: isinstance(process, _RealPopen))
        self._creation_flags = creation_flags or (lambda: 0)
        self._terminate_wait_seconds = terminate_wait_seconds or (lambda: 2.0)
        self._kill_wait_seconds = kill_wait_seconds or (lambda: 2.0)
        self._clerks: dict[str, ManagedClerk] = {}
        self._reserved_client_ids: set[int] = set()
        self._quarantined_client_ids: set[int] = set()
        self._unidentified_live_clerk_accounts: set[str] = set()
        self._start_blockers: dict[str, str] = {}
        self._state_lock = threading.RLock()
        self._account_locks: dict[str, threading.RLock] = {}
        self._account_locks_lock = threading.Lock()

    @property
    def clerks(self) -> dict[str, ManagedClerk]:
        """The live Clerk registry, retained for host status compatibility."""

        return self._clerks

    @property
    def quarantined_client_ids(self) -> set[int]:
        """IDs withheld until a previous Clerk exit is confirmed."""

        return self._quarantined_client_ids

    @property
    def start_blockers(self) -> dict[str, str]:
        """Accounts whose durable process evidence cannot safely be replaced."""

        return self._start_blockers

    def in_use_client_ids(self) -> set[int]:
        """Return Clerk identities that callers must exclude from bot allocation."""

        with self._state_lock:
            in_use = (
                self._reserved_client_ids
                | self._quarantined_client_ids
                | {
                    clerk.ibkr_client_id
                    for clerk in self._clerks.values()
                    if clerk.process.poll() is None
                }
            )
            # A live legacy Clerk with no durable client identity could occupy
            # any configured session. Until its process evidence is resolved,
            # allocating even a bot-side ID would risk a second connection.
            if self._unidentified_live_clerk_accounts:
                in_use.update(self._client_id_pool())
            return in_use

    def account_lock(self, account_id: str) -> threading.RLock:
        """Return the lifecycle lock for one account, creating it safely."""

        with self._account_locks_lock:
            lock = self._account_locks.get(account_id)
            if lock is None:
                lock = threading.RLock()
                self._account_locks[account_id] = lock
            return lock

    def reconcile_on_boot(self) -> None:
        """Resolve all durable Clerk evidence before a replacement can spawn."""

        for account_id in self.account_ids_with_evidence():
            self.resolve_orphan(account_id)

    def account_ids_with_evidence(self) -> tuple[str, ...]:
        """Discover account roots that can contain Clerk lease/process/socket evidence."""

        from app.engine.live.account_artifacts import AccountArtifactError, account_artifacts_root

        accounts_root = self.artifacts_root / "accounts"
        try:
            entries = tuple(accounts_root.iterdir()) if accounts_root.is_dir() else ()
        except OSError as exc:
            logger.error("could not discover account Clerk evidence", extra={"error": str(exc)})
            return ()
        account_ids: list[str] = []
        for entry in entries:
            if entry.is_symlink() or not entry.is_dir():
                continue
            try:
                expected_root = account_artifacts_root(self.artifacts_root, entry.name)
            except AccountArtifactError:
                continue
            if entry.resolve() == expected_root:
                account_ids.append(entry.name)
        return tuple(sorted(account_ids))

    def resolve_orphan(self, account_id: str) -> ManagedClerk | None:
        """Adopt a healthy Clerk or establish safe replacement preconditions."""

        with self.account_lock(account_id):
            return self._resolve_orphan_locked(account_id)

    def ensure(self, account_id: str, *, ibkr_host: str | None = None) -> ManagedClerk:
        """Return an adopted Clerk or start a new fenced Clerk for an account."""

        with self.account_lock(account_id):
            existing = self._resolve_orphan_locked(account_id)
            if existing is not None:
                return existing
            clerk_client_id = self._reserve_client_id(account_id)
            return self._spawn_locked(account_id, clerk_client_id=clerk_client_id, ibkr_host=ibkr_host)

    def health(self) -> list[AccountClerkHealth]:
        """Build a Clerk health snapshot without holding the lifecycle lock for I/O."""

        from app.engine.live.account_artifacts import read_account_clerk_lease

        now_ms = self._now_ms()
        with self._state_lock:
            clerks = tuple(sorted(self._clerks.items()))
        rows: list[AccountClerkHealth] = []
        for account_id, clerk in clerks:
            lease = read_account_clerk_lease(self.artifacts_root, account_id)
            running = clerk.process.poll() is None
            lease_valid = bool(
                running
                and lease is not None
                and lease.generation == clerk.generation
                and lease.status == "RUNNING"
                and lease.valid_until_ms > now_ms
            )
            rows.append(
                AccountClerkHealth(
                    account_id=account_id,
                    generation=clerk.generation,
                    pid=lease.pid if lease is not None else clerk.process.pid,
                    status=(lease.status if lease is not None else "UNAVAILABLE") if running else "EXITED",
                    started_at_ms=clerk.started_at_ms,
                    renewed_at_ms=lease.renewed_at_ms if lease is not None else None,
                    valid_until_ms=lease.valid_until_ms if lease is not None else None,
                    lease_valid=lease_valid,
                )
            )
        return rows

    def supervise(self, active_accounts: set[str], *, account_ids: set[str] | None = None) -> None:
        """Reap idle/exited Clerks and replace only those backing active bots."""

        with self._state_lock:
            managed_accounts = set(self._clerks)
        candidate_accounts = managed_accounts
        if account_ids is not None:
            candidate_accounts &= account_ids
        for account_id in sorted(candidate_accounts):
            with self.account_lock(account_id):
                with self._state_lock:
                    clerk = self._clerks.get(account_id)
                if clerk is None:
                    continue
                if clerk.process.poll() is not None:
                    self._retire(account_id, clerk)
                    if account_id in active_accounts:
                        self._replace_exited_for_active_account(account_id, clerk)
                    continue
                if account_id not in active_accounts:
                    self._reap(account_id, clerk)

    def retire(self, account_id: str, clerk: ManagedClerk) -> None:
        """Remove a confirmed-dead Clerk and release its quarantined identity."""

        with self.account_lock(account_id):
            self._retire(account_id, clerk)

    def reap(self, account_id: str, clerk: ManagedClerk) -> bool:
        """Gracefully reap one Clerk while retaining unsafe identities."""

        with self.account_lock(account_id):
            return self._reap(account_id, clerk)

    def terminate(self, process: subprocess.Popen | AdoptedAccountClerkProcess, *, account_id: str) -> bool:
        """Terminate, wait, and escalate a proven Clerk before replacement."""

        if process.poll() is not None:
            return True
        try:
            process.terminate()
        except ProcessLookupError as exc:
            logger.warning(
                "could not send account Clerk termination",
                extra={"account_id": account_id, "pid": process.pid, "error": str(exc)},
            )
            return not (
                isinstance(process, AdoptedAccountClerkProcess)
                and self._inspect_process(process.pid) is not None
            )
        except OSError as exc:
            logger.warning(
                "could not send account Clerk termination",
                extra={"account_id": account_id, "pid": process.pid, "error": str(exc)},
            )
        try:
            process.wait(timeout=self._terminate_wait_seconds())
            return True
        except subprocess.TimeoutExpired:
            logger.warning(
                "account Clerk ignored graceful termination; escalating",
                extra={"account_id": account_id, "pid": process.pid},
            )
        try:
            process.kill()
        except ProcessLookupError as exc:
            logger.warning(
                "could not kill account Clerk during escalation",
                extra={"account_id": account_id, "pid": process.pid, "error": str(exc)},
            )
            return not (
                isinstance(process, AdoptedAccountClerkProcess)
                and self._inspect_process(process.pid) is not None
            )
        except OSError as exc:
            logger.warning(
                "could not kill account Clerk during escalation",
                extra={"account_id": account_id, "pid": process.pid, "error": str(exc)},
            )
        try:
            process.wait(timeout=self._kill_wait_seconds())
            return True
        except subprocess.TimeoutExpired:
            return False

    def _resolve_orphan_locked(self, account_id: str) -> ManagedClerk | None:
        retired: ManagedClerk | None = None
        with self._state_lock:
            existing = self._clerks.get(account_id)
            if existing is not None and existing.process.poll() is None:
                self._start_blockers.pop(account_id, None)
                return existing
            if existing is not None:
                retired = self._clerks.pop(account_id)
        if retired is not None:
            retired.log_handle.close()

        from app.engine.live.account_artifacts import (
            account_artifacts_root,
            read_account_clerk_generation,
            read_account_clerk_lease,
        )
        from app.engine.live.account_clerk import account_clerk_socket_path
        from app.engine.live.account_clerk_rpc import AccountClerkRpcError

        try:
            generation = read_account_clerk_generation(self.artifacts_root, account_id)
            lease = read_account_clerk_lease(self.artifacts_root, account_id)
            socket_path = account_clerk_socket_path(self.artifacts_root, account_id)
            socket_exists = socket_path.exists()
        except (OSError, ValueError) as exc:
            self._block_start(account_id, f"could not read Clerk evidence: {exc}")
            return None

        if lease is None:
            self._clear_unidentified_live_clerk(account_id)
            if socket_exists:
                self._block_start(
                    account_id,
                    "a Clerk socket exists without a lease PID; refusing to unlink an unproven writer",
                )
            else:
                self._clear_start_blocker(account_id)
            return None

        evidence = self._inspect_process(lease.pid)
        if evidence is None:
            self._clear_unidentified_live_clerk(account_id)
            if not self._remove_resolved_socket(account_id, socket_path):
                return None
            self._clear_start_blocker(account_id)
            return None

        if lease.ibkr_client_id is None:
            self._mark_unidentified_live_clerk(account_id)
            self._block_start(
                account_id,
                (
                    f"Clerk lease for PID {lease.pid} omits its IBKR client ID; "
                    "refusing to adopt or replace unproven live Clerk evidence"
                ),
            )
            return None

        self._clear_unidentified_live_clerk(account_id)

        if not account_clerk_process_matches(
            evidence,
            artifacts_root=self.artifacts_root,
            account_id=account_id,
            generation=lease.generation,
            ibkr_client_id=lease.ibkr_client_id,
        ):
            self._block_start(
                account_id,
                (
                    f"lease PID {lease.pid} does not identify Clerk generation {lease.generation}; "
                    "refusing PID-reuse adoption or termination"
                ),
            )
            return None

        healthy = bool(
            generation is not None
            and generation.generation == lease.generation
            and generation.phase == "accepting"
            and lease.status == "RUNNING"
            and lease.valid_until_ms > self._now_ms()
        )
        if healthy:
            try:
                served_generation = self._verify_generation(self.artifacts_root, account_id)
            except (AccountClerkRpcError, OSError):
                healthy = False
            else:
                healthy = served_generation == lease.generation

        if not healthy:
            process = AdoptedAccountClerkProcess(evidence, self._inspect_process)
            if not self.terminate(process, account_id=account_id):
                self._block_start(
                    account_id,
                    f"Clerk PID {lease.pid} did not stop after terminate/kill escalation",
                )
                return None
            if not self._remove_resolved_socket(account_id, socket_path):
                return None
            self._clear_start_blocker(account_id)
            return None

        root = account_artifacts_root(self.artifacts_root, account_id)
        try:
            log_handle = (root / "clerk.log").open("a", encoding="utf-8")
        except (OSError, ValueError) as exc:
            self._block_start(account_id, f"could not adopt Clerk: {exc}")
            return None
        managed = ManagedClerk(
            account_id=account_id,
            generation=lease.generation,
            ibkr_client_id=lease.ibkr_client_id,
            process=AdoptedAccountClerkProcess(evidence, self._inspect_process),
            started_at_ms=lease.started_at_ms,
            log_handle=log_handle,
        )
        with self._state_lock:
            self._clerks[account_id] = managed
            self._start_blockers.pop(account_id, None)
        logger.info(
            "adopted healthy account Clerk",
            extra={
                "account_id": account_id,
                "generation": lease.generation,
                "pid": lease.pid,
                "process_start_identity": evidence.process_start_identity,
            },
        )
        return managed

    def _reserve_client_id(self, account_id: str) -> int:
        with self._state_lock:
            blocker = self._start_blockers.get(account_id)
            if blocker is not None:
                raise OSError(f"Account Clerk replacement blocked for {account_id}: {blocker}")
            unavailable = (
                self._external_client_ids()
                | self._reserved_client_ids
                | self._quarantined_client_ids
                | {
                    clerk.ibkr_client_id
                    for clerk in self._clerks.values()
                    if clerk.process.poll() is None
                }
            )
            client_id = next(
                (candidate for candidate in self._client_id_pool() if candidate not in unavailable),
                None,
            )
            if client_id is None:
                raise OSError("No IBKR client ID is available for the account clerk")
            self._reserved_client_ids.add(client_id)
            return client_id

    def _spawn_locked(self, account_id: str, *, clerk_client_id: int, ibkr_host: str | None) -> ManagedClerk:
        from app.engine.live.account_artifacts import account_artifacts_root, advance_account_clerk_generation
        from app.engine.live.account_clerk import AccountClerkLeaseWriter, account_clerk_socket_path

        log_handle: TextIO | None = None
        process: subprocess.Popen | None = None
        confirmed_exit = True
        try:
            generation = advance_account_clerk_generation(
                self.artifacts_root,
                account_id,
                phase="accepting",
                recorded_at_ms=self._now_ms(),
                source="host_daemon.clerk_spawn",
            )
            root = account_artifacts_root(self.artifacts_root, account_id)
            log_path = root / "clerk.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_handle = log_path.open("a", encoding="utf-8")
            env = os.environ.copy()
            python_path = str(self.repo_root / "PythonDataService")
            existing_python_path = env.get("PYTHONPATH")
            env["PYTHONPATH"] = (
                python_path if not existing_python_path else f"{python_path}{os.pathsep}{existing_python_path}"
            )
            if ibkr_host is not None:
                env["IBKR_HOST"] = ibkr_host
            env["IBKR_CLIENT_ID"] = str(clerk_client_id)
            env["IBKR_MODE"] = "paper"
            env["IBKR_READONLY"] = "false"
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "app.engine.live.account_clerk",
                    "--artifacts-root",
                    str(self.artifacts_root),
                    "--account-id",
                    account_id,
                    "--generation",
                    str(generation.generation),
                    "--ibkr-client-id",
                    str(clerk_client_id),
                ],
                cwd=str(self.repo_root),
                env=env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                creationflags=self._creation_flags(),
                start_new_session=(os.name != "nt"),
            )
            if self._is_real_popen(process):
                self._wait_for_readiness(self.artifacts_root, account_id, generation.generation)
            AccountClerkLeaseWriter(
                artifacts_root=self.artifacts_root,
                account_id=account_id,
                generation=generation.generation,
                pid=process.pid,
                ibkr_client_id=clerk_client_id,
                now_ms=self._now_ms,
            ).renew()
        except (OSError, ValueError):
            if process is not None:
                confirmed_exit = self.terminate(process, account_id=account_id)
                if confirmed_exit:
                    self._remove_resolved_socket(
                        account_id,
                        account_clerk_socket_path(self.artifacts_root, account_id),
                    )
            if log_handle is not None:
                log_handle.close()
            with self._state_lock:
                self._reserved_client_ids.discard(clerk_client_id)
                if process is not None and not confirmed_exit:
                    self._quarantined_client_ids.add(clerk_client_id)
            raise

        managed = ManagedClerk(
            account_id=account_id,
            generation=generation.generation,
            ibkr_client_id=clerk_client_id,
            process=process,
            started_at_ms=self._now_ms(),
            log_handle=log_handle,
        )
        with self._state_lock:
            self._clerks[account_id] = managed
            self._reserved_client_ids.discard(clerk_client_id)
        return managed

    def _block_start(self, account_id: str, reason: str) -> None:
        with self._state_lock:
            self._start_blockers[account_id] = reason
        logger.error("account Clerk replacement blocked", extra={"account_id": account_id, "reason": reason})

    def _mark_unidentified_live_clerk(self, account_id: str) -> None:
        with self._state_lock:
            self._unidentified_live_clerk_accounts.add(account_id)

    def _clear_unidentified_live_clerk(self, account_id: str) -> None:
        with self._state_lock:
            self._unidentified_live_clerk_accounts.discard(account_id)

    def _clear_start_blocker(self, account_id: str) -> None:
        with self._state_lock:
            self._start_blockers.pop(account_id, None)

    def _remove_resolved_socket(self, account_id: str, socket_path: Path) -> bool:
        try:
            socket_path.unlink(missing_ok=True)
        except OSError as exc:
            self._block_start(account_id, f"could not remove resolved Clerk socket {socket_path}: {exc}")
            return False
        return True

    def _retire(self, account_id: str, clerk: ManagedClerk) -> None:
        removed = False
        with self._state_lock:
            if self._clerks.get(account_id) is clerk:
                self._clerks.pop(account_id, None)
                self._quarantined_client_ids.discard(clerk.ibkr_client_id)
                removed = True
        if removed:
            clerk.log_handle.close()

    def _reap(self, account_id: str, clerk: ManagedClerk) -> bool:
        with self._state_lock:
            self._quarantined_client_ids.add(clerk.ibkr_client_id)
        confirmed_exit = self.terminate(clerk.process, account_id=account_id)
        if not confirmed_exit:
            logger.error(
                "account Clerk reap is still unconfirmed; retaining client ID quarantine",
                extra={"account_id": account_id, "pid": clerk.process.pid, "ibkr_client_id": clerk.ibkr_client_id},
            )
            return False
        self._retire(account_id, clerk)
        logger.info(
            "reaped account Clerk after last bot exited",
            extra={"account_id": account_id, "pid": clerk.process.pid, "ibkr_client_id": clerk.ibkr_client_id},
        )
        return True

    def _replace_exited_for_active_account(self, account_id: str, clerk: ManagedClerk) -> None:
        try:
            self.ensure(account_id)
        except OSError:
            logger.exception(
                "could not replace exited account Clerk",
                extra={"account_id": account_id, "generation": clerk.generation},
            )
