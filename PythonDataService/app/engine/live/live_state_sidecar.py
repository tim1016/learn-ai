"""Order-idempotency sidecar — persists what the bot believes about
its in-flight orders, fills, positions, and bar cursor so a crash
between submit and acknowledgement cannot cause a double trade.

Grown vertically via TDD: each cycle adds one field or one
mechanic. See plan §16.4 Resolution 3 for the 12-field target
schema this module grows toward, and ``indicator_state.py`` for
the envelope+repo+atomic-write pattern this mirrors.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class LiveStateSidecarCorruptError(RuntimeError):
    """Raised by ``LiveStateSidecarRepo.read`` when the on-disk bytes
    are unparseable JSON or fail envelope validation.

    Routes to ColdStartReconciler as a hard ``Poisoned`` outcome:
    a corrupt sidecar cannot be safely resumed and the bot must not
    submit new orders until the operator inspects the file.
    """

    def __init__(self, path: Path, cause: BaseException) -> None:
        super().__init__(f"live-state sidecar at {path} is unreadable: {cause}")
        self.path = path
        self.__cause__ = cause


def stable_live_state_path(artifacts_root: Path, strategy_instance_id: str) -> Path:
    """Canonical on-disk path for a strategy instance's live-state sidecar.

    Layout: <artifacts_root>/live_state/<strategy_instance_id>/live_state.json
    Matches plan §16.4 / §16.5 and parallels indicator_state's
    stable_global_path so both sidecars sit side-by-side under the same
    per-strategy directory.
    """
    return artifacts_root / "live_state" / strategy_instance_id / "live_state.json"


class LiveStateEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_instance_id: str
    run_id: str
    bot_order_namespace: str
    ib_client_id: int

    pending_intents: list[dict[str, Any]] = Field(default_factory=list)
    submitted_orders: dict[str, dict[str, Any]] = Field(default_factory=dict)
    known_perm_ids: list[int] = Field(default_factory=list)
    known_exec_ids: list[str] = Field(default_factory=list)

    expected_position_by_symbol: dict[str, int] = Field(default_factory=dict)
    last_processed_bar_ms: int = Field(gt=0)
    last_artifact_flush_ms: int = Field(gt=0)

    # WAL fold cursor: the highest ``intent_events.jsonl`` seq already folded
    # into ``submitted_orders`` (ADR-0008 §3/§5, PRD #446). The cold-start fold
    # replays events *after* this seq — NOT after ``last_artifact_flush_ms``, a
    # wall-clock value that can collide, drift, or reorder around fsync.
    # Defaults to 0 so envelopes written before this field read back cleanly.
    last_intent_wal_seq: int = Field(default=0, ge=0)

    poisoned_reason: str | None = None


class LiveStateSidecarRepo:
    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self) -> LiveStateEnvelope | None:
        if not self._path.exists():
            return None
        try:
            return LiveStateEnvelope.model_validate_json(
                self._path.read_text(encoding="utf-8")
            )
        except (ValidationError, ValueError) as exc:
            raise LiveStateSidecarCorruptError(self._path, exc) from exc

    def update_after_flush(
        self, *, last_processed_bar_ms: int, last_artifact_flush_ms: int
    ) -> None:
        """Advance the bar/flush cursors on the existing envelope.

        Read-modify-write under the same atomic-write contract: read the
        current envelope, replace just the two cursor fields, write it
        back. Every other field is preserved verbatim. Called by the
        engine after a successful artifact flush.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock(self._path):
            existing = self.read()
            if existing is None:
                raise FileNotFoundError(
                    f"cannot update flush cursors: no live-state sidecar at {self._path}"
                )
            self._write_locked(
                existing.model_copy(
                    update={
                        "last_processed_bar_ms": last_processed_bar_ms,
                        "last_artifact_flush_ms": last_artifact_flush_ms,
                    }
                )
            )

    def write(self, envelope: LiveStateEnvelope) -> None:
        """Atomic write under advisory lock: serialise to a sibling .tmp,
        fsync, os.replace.

        Lock window covers the full tempfile-write-then-rename sequence so
        two concurrent writers don't race on the shared tempfile name.
        Without the lock, the loser's os.replace finds the tempfile
        already renamed by the winner and raises FileNotFoundError.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock(self._path):
            self._write_locked(envelope)

    def _write_locked(self, envelope: LiveStateEnvelope) -> None:
        """File-mechanics half of write(). Caller must hold _file_lock.

        Split out so update_after_flush can hold a single lock across
        its full read-modify-write without re-entering _file_lock
        (fcntl/msvcrt locks are per-fd; nesting would deadlock or
        produce surprising semantics).
        """
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = envelope.model_dump_json().encode("utf-8")
        with open(tmp_path, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.replace(tmp_path, self._path)
        except Exception:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            raise
        _fsync_parent_dir(self._path)


def _fsync_parent_dir(child_path: Path) -> None:
    """Fsync the parent directory entry so a fresh rename survives crash.

    Tempfile fsync flushes the file's own contents, but on POSIX the
    rename's directory entry can be lost on power loss without a
    separate dir fsync. On Windows this is a no-op — ReplaceFile is
    not subject to the same metadata-durability gap.
    """
    if sys.platform == "win32":
        return
    dir_fd = os.open(child_path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        with contextlib.suppress(OSError):
            os.close(dir_fd)


@contextlib.contextmanager
def _file_lock(target_path: Path) -> Iterator[None]:
    """Advisory lock on a sibling .lock file for the duration of the write.

    Ported from indicator_state.py's _file_lock. POSIX uses fcntl.flock,
    Windows uses msvcrt.locking. Concurrent processes / threads writing
    the same path serialise here; the lock window is only as long as the
    atomic write.
    """
    lock_path = target_path.with_suffix(target_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+b")  # noqa: SIM115
    fh.seek(0)
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()
