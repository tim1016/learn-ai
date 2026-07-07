"""Bot-event capture helpers for host-daemon launch failures."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from app.engine.live.bot_event_capture import BotEventTerminalRecorder
from app.operator.incidents.store import IncidentStore
from app.schemas.bot_events import (
    FactValue,
    TerminalError,
    TerminalErrorCode,
    TerminalErrorSource,
)
from app.services.bot_event_incidents import append_terminal_incident

logger = logging.getLogger(__name__)

_LAUNCH_FAILURE_LOG_TAIL_CHARS = 4_000


def redacted_daemon_path(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\\", "/")
    parts = [part for part in text.split("/") if part]
    if "PythonDataService" in parts:
        idx = parts.index("PythonDataService")
        return "/".join(parts[idx:])
    if "artifacts" in parts:
        idx = parts.index("artifacts")
        return "/".join(parts[idx:])
    return Path(text).name or None


def record_spawn_launch_failure(
    run_dir: Path,
    *,
    run_id: str,
    strategy_instance_id: str,
    command: list[str],
    log_path: Path,
    exc: OSError,
    ts_ms: int,
) -> bool:
    errno_value = exc.errno
    return _append_launch_failed_event(
        run_dir,
        run_id=run_id,
        strategy_instance_id=strategy_instance_id,
        ts_ms=ts_ms,
        terminal_error=TerminalError(
            code=TerminalErrorCode.LAUNCH_FAILED,
            source=TerminalErrorSource.OS,
            gate_id="daemon.spawn",
            message="Host runner process could not be started.",
            detail=str(exc),
            external_code=errno_value,
            external_message=str(exc),
            cause_chain=(f"{type(exc).__name__}: {exc}",),
            forensic_facts={
                "exception_type": type(exc).__name__,
                "errno": errno_value,
                "filename": str(exc.filename) if exc.filename else None,
                "log_path": redacted_daemon_path(log_path),
            },
        ),
        facts={
            "failure_stage": "spawn",
            "command": command,
            "log_path": redacted_daemon_path(log_path),
        },
    )


def record_child_crash_launch_failure(
    run_dir: Path,
    *,
    run_id: str,
    strategy_instance_id: str,
    command: list[str],
    log_path: Path,
    pid: int,
    returncode: int,
    ts_ms: int,
) -> bool:
    stderr_tail = _text_tail(log_path, max_chars=_LAUNCH_FAILURE_LOG_TAIL_CHARS)
    return _append_launch_failed_event(
        run_dir,
        run_id=run_id,
        strategy_instance_id=strategy_instance_id,
        ts_ms=ts_ms,
        terminal_error=TerminalError(
            code=TerminalErrorCode.LAUNCH_FAILED,
            source=TerminalErrorSource.DAEMON,
            gate_id="daemon.child_process",
            message="Host runner process exited before it could keep the bot alive.",
            detail=f"process exited with code {returncode}",
            external_code=returncode,
            external_message=stderr_tail,
            forensic_facts={
                "returncode": returncode,
                "pid": pid,
                "log_path": redacted_daemon_path(log_path),
            },
        ),
        facts={
            "failure_stage": "child_process",
            "command": command,
            "pid": pid,
            "log_path": redacted_daemon_path(log_path),
        },
    )


def should_record_child_launch_failure(
    run_dir: Path,
    *,
    returncode: int | None,
    stopping: bool,
) -> bool:
    if stopping or returncode in (None, 0):
        return False
    status = _read_run_status(run_dir)
    if status is None:
        return True
    ended_at_ms = status.get("ended_at_ms")
    if not isinstance(ended_at_ms, int):
        return True
    return status.get("exit_reason") in {None, "exception"}


def _append_launch_failed_event(
    run_dir: Path,
    *,
    run_id: str,
    strategy_instance_id: str,
    ts_ms: int,
    terminal_error: TerminalError,
    facts: dict[str, FactValue],
) -> bool:
    recorder = BotEventTerminalRecorder.for_run(
        run_dir=run_dir,
        run_id=run_id,
        strategy_instance_id=strategy_instance_id,
    )
    if recorder is None:
        return False
    try:
        raw_event = recorder.append_launch_failed(ts_ms=ts_ms, terminal_error=terminal_error, facts=facts)
        append_terminal_incident(IncidentStore(run_dir), raw_event)
    except Exception:
        logger.exception(
            "Failed to record daemon launch_failed bot event",
            extra={"run_id": run_id, "strategy_instance_id": strategy_instance_id},
        )
        return False
    return True


def _read_run_status(run_dir: Path) -> dict[str, object] | None:
    path = run_dir / "run_status.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _text_tail(path: Path, *, max_chars: int) -> str | None:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_chars))
            tail = handle.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return None
    return tail or None
