"""Durable operator desired-state — persists cross-run intent so a
PAUSED bot stays PAUSED across crash + reboot and a STOPPED bot refuses
to restart on its own.

Distinct from ``command_channel.py``: commands are one-shot, per-run
events (``artifacts/live_runs/<run_id>/commands/``); desired-state is
persistent operator intent keyed by ``strategy_instance_id``
(``artifacts/live_state/<strategy_instance_id>/desired_state.json``),
surviving across runs. See plan §16.4 Resolution 7.

Mirrors ``live_state_sidecar.py``'s envelope + repo + atomic-write
pattern and reuses its ``_file_lock`` / ``_fsync_parent_dir`` helpers
rather than copying them a fourth time (the shared-helper extraction
flagged in #367 review is the proper follow-up).
"""

from __future__ import annotations

import contextlib
import os
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

# Reuse the atomic-write primitives instead of duplicating them. The
# reviewer-flagged extraction of these into a shared util is the
# follow-up; importing keeps a single source of truth in the meantime.
from app.engine.live.identity import (
    strategy_instance_artifact_dir,
    validate_strategy_instance_id,
)
from app.engine.live.live_state_sidecar import _file_lock, _fsync_parent_dir

# Re-exported so existing callers can keep importing it from here.
__all__ = [
    "DesiredState",
    "DesiredStateCorruptError",
    "DesiredStateRecord",
    "DesiredStateRepo",
    "stable_desired_state_path",
    "validate_strategy_instance_id",
]


class DesiredStateCorruptError(RuntimeError):
    """Raised by ``DesiredStateRepo.read`` when the on-disk bytes are
    unparseable JSON or fail schema validation.

    A corrupt control file must never be the source of a clean restart:
    ``run.py start`` treats this as a refusal (exit non-zero) so the
    operator inspects the file rather than the bot guessing intent.
    """

    def __init__(self, path: Path, cause: BaseException) -> None:
        super().__init__(f"desired_state at {path} is unreadable: {cause}")
        self.path = path
        self.__cause__ = cause


def stable_desired_state_path(artifacts_root: Path, strategy_instance_id: str) -> Path:
    """Canonical on-disk path for a strategy instance's desired-state file.

    Layout: <artifacts_root>/live_state/<strategy_instance_id>/desired_state.json
    Sits alongside ``live_state.json`` (the order-idempotency sidecar)
    under the same per-strategy directory — see
    ``live_state_sidecar.stable_live_state_path``.

    The id is validated as a single safe path segment (fail-fast at the
    boundary) so a caller-controlled value can never escape
    ``artifacts_root``.
    """
    return (
        strategy_instance_artifact_dir(
            artifacts_root, "live_state", strategy_instance_id
        )
        / "desired_state.json"
    )


class DesiredState(StrEnum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


class DesiredStateRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    desired_state: DesiredState
    updated_at_ms: int
    updated_by: str
    reason: str | None = None
    version: int = 1


class DesiredStateRepo:
    def __init__(self, path: Path, *, trusted_root: Path | None = None) -> None:
        self._path = path
        self._trusted_root = trusted_root if trusted_root is not None else path.parent

    def _confined_path(self) -> Path:
        root_real = os.path.realpath(os.fspath(self._trusted_root))
        candidate = os.path.realpath(os.fspath(self._path))
        root_prefix = root_real.rstrip(os.sep) + os.sep
        if not candidate.startswith(root_prefix):
            raise ValueError(f"desired-state path {candidate} escapes root {root_real}")
        return Path(candidate)

    def read(self) -> DesiredStateRecord | None:
        """Return the on-disk record, or ``None`` when the file is absent.

        Absence means "no operator has expressed intent yet" — the
        caller defaults to RUNNING (see ``read_state``).
        """
        root_real = os.path.realpath(os.fspath(self._trusted_root))
        candidate = os.path.realpath(os.fspath(self._path))
        root_prefix = root_real.rstrip(os.sep) + os.sep
        if not candidate.startswith(root_prefix):
            raise ValueError(f"desired-state path {candidate} escapes root {root_real}")
        path = Path(candidate)
        if not path.exists():
            return None
        try:
            return DesiredStateRecord.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (ValidationError, ValueError) as exc:
            raise DesiredStateCorruptError(path, exc) from exc

    def read_state(self) -> DesiredState:
        """Convenience: the desired state, defaulting to RUNNING when no
        file exists. Raises ``DesiredStateCorruptError`` on a malformed file.
        """
        record = self.read()
        return record.desired_state if record is not None else DesiredState.RUNNING

    def write(self, record: DesiredStateRecord) -> None:
        """Atomic write under advisory lock: serialise to a sibling .tmp,
        fsync, os.replace, then fsync the parent dir so the rename
        survives a crash. Mirrors ``LiveStateSidecarRepo.write``.
        """
        root_real = os.path.realpath(os.fspath(self._trusted_root))
        candidate = os.path.realpath(os.fspath(self._path))
        root_prefix = root_real.rstrip(os.sep) + os.sep
        if not candidate.startswith(root_prefix):
            raise ValueError(f"desired-state path {candidate} escapes root {root_real}")
        path = Path(candidate)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock(path, trusted_root=self._trusted_root):
            self._write_locked(path, record)

    def delete(self) -> None:
        """Remove the sidecar when restoring an absent pre-mutation state."""

        path = self._confined_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock(path, trusted_root=self._trusted_root):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            _fsync_parent_dir(path)

    def set(
        self,
        state: DesiredState,
        *,
        updated_by: str,
        now_ms: int,
        reason: str | None = None,
    ) -> DesiredStateRecord:
        """Read-modify-write the desired state under a single lock,
        bumping ``version`` from the prior record (or starting at 1).

        ``now_ms`` is supplied by the caller — timestamp rigor: the
        int64 ms UTC value is produced at the boundary, not inside this
        repo, so the write stays deterministic and testable.
        """
        root_real = os.path.realpath(os.fspath(self._trusted_root))
        candidate = os.path.realpath(os.fspath(self._path))
        root_prefix = root_real.rstrip(os.sep) + os.sep
        if not candidate.startswith(root_prefix):
            raise ValueError(f"desired-state path {candidate} escapes root {root_real}")
        path = Path(candidate)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock(path, trusted_root=self._trusted_root):
            existing = self.read()
            next_version = (existing.version + 1) if existing is not None else 1
            record = DesiredStateRecord(
                desired_state=state,
                updated_at_ms=now_ms,
                updated_by=updated_by,
                reason=reason,
                version=next_version,
            )
            self._write_locked(path, record)
            return record

    def _write_locked(self, path: Path, record: DesiredStateRecord) -> None:
        """File-mechanics half of write(). Caller must hold ``_file_lock``.

        Split out so ``set`` can hold one lock across its full
        read-modify-write without re-entering ``_file_lock`` (the
        fcntl/msvcrt locks are per-fd; nesting would surprise).
        """
        root_real = os.path.realpath(os.fspath(self._trusted_root))
        candidate = os.path.realpath(os.fspath(path))
        root_prefix = root_real.rstrip(os.sep) + os.sep
        if not candidate.startswith(root_prefix):
            raise ValueError(f"desired-state path {candidate} escapes root {root_real}")
        safe_path = Path(candidate)
        tmp_path = safe_path.with_suffix(safe_path.suffix + ".tmp")
        payload = record.model_dump_json().encode("utf-8")
        with open(tmp_path, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.replace(tmp_path, safe_path)
        except Exception:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            raise
        _fsync_parent_dir(safe_path)
