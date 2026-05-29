"""Process registry — multi-process replacement for the single-_current
model in ``host_daemon.RunnerProcessManager``.

Keys by ``strategy_instance_id`` (per plan §16.4 Resolution 7). Owns
``Popen`` lifecycle: start / stop / list / status. Exit-code
disambiguation (clean exit 0 vs crashed N≠0) so the dispatcher can
decide whether to restart or escalate.

Engine-side wiring (FastAPI routes routing through this registry) is
a follow-up; this module is unit-testable in isolation.
"""

from __future__ import annotations

import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

State = Literal["running", "stopping", "exited"]


class AlreadyRunningError(RuntimeError):
    """Raised when start() is called for a strategy_instance_id that
    already has a running managed process. Carries the id so callers
    can surface it without re-parsing the message.
    """

    def __init__(self, strategy_instance_id: str) -> None:
        super().__init__(
            f"strategy_instance_id={strategy_instance_id!r} is already running"
        )
        self.strategy_instance_id = strategy_instance_id


@dataclass
class ManagedProcess:
    strategy_instance_id: str
    process: subprocess.Popen
    command: list[str]
    log_path: Path
    started_at_ms: int
    state: State = "running"
    ended_at_ms: int | None = None
    exit_code: int | None = None

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def exit_classification(self) -> Literal["intentional", "crashed"] | None:
        """Derived from exit_code: 0 → intentional, non-zero → crashed.

        None until the process has exited (exit_code is None). The
        dispatcher uses this to decide whether to restart (crashed) or
        leave idle (intentional).
        """
        if self.exit_code is None:
            return None
        return "intentional" if self.exit_code == 0 else "crashed"


class ProcessRegistry:
    def __init__(self) -> None:
        self._managed: dict[str, ManagedProcess] = {}

    def list(self) -> list[ManagedProcess]:
        return list(self._managed.values())

    def status(self, strategy_instance_id: str) -> ManagedProcess | None:
        return self._managed.get(strategy_instance_id)

    def start(
        self,
        *,
        strategy_instance_id: str,
        command: list[str],
        log_path: Path,
    ) -> ManagedProcess:
        existing = self._managed.get(strategy_instance_id)
        if existing is not None and existing.state == "running":
            raise AlreadyRunningError(strategy_instance_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("a", encoding="utf-8")
        process = subprocess.Popen(
            command,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        managed = ManagedProcess(
            strategy_instance_id=strategy_instance_id,
            process=process,
            command=command,
            log_path=log_path,
            started_at_ms=int(time.time() * 1000),
        )
        self._managed[strategy_instance_id] = managed
        return managed

    def stop(
        self, strategy_instance_id: str, *, timeout_s: float = 2.0
    ) -> ManagedProcess:
        """Send SIGTERM, wait up to timeout_s for the process to exit,
        then record the outcome. SIGKILL fallback comes in a later
        cycle when a test forces it.
        """
        managed = self._managed[strategy_instance_id]
        managed.state = "stopping"
        managed.process.send_signal(signal.SIGTERM)
        managed.process.wait(timeout=timeout_s)
        managed.exit_code = managed.process.returncode
        managed.ended_at_ms = int(time.time() * 1000)
        managed.state = "exited"
        return managed
