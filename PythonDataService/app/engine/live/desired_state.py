"""Durable operator desired-state — persists cross-run intent so a
STOPPED bot refuses to restart on its own. Nothing writes PAUSED since
#2550 removed Pause; the value stays readable for records written before.

Distinct from ``command_channel.py``: commands are one-shot, per-run
events (``artifacts/live_runs/<run_id>/commands/``); desired-state is
persistent operator intent keyed by ``strategy_instance_id``
(``artifacts/live_state/<strategy_instance_id>/desired_state.json``),
surviving across runs. See plan §16.4 Resolution 7.

Mirrors ``live_state_sidecar.py``'s envelope + repo + atomic-write
pattern and reuses its ``_file_lock`` / ``fsync_parent_dir`` helpers
rather than copying them a fourth time (the shared-helper extraction
flagged in #367 review is the proper follow-up).
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable
from enum import Enum, StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

# Reuse the atomic-write primitives instead of duplicating them. The
# reviewer-flagged extraction of these into a shared util is the
# follow-up; importing keeps a single source of truth in the meantime.
from app.engine.live.identity import (
    strategy_instance_artifact_dir,
    validate_strategy_instance_id,
)
from app.engine.live.live_state_sidecar import _file_lock, fsync_parent_dir
from app.schemas.bot_end import BotEnd, RecordedEnd

# Re-exported so existing callers can keep importing it from here.
__all__ = [
    "END_UNCHANGED",
    "DesiredState",
    "DesiredStateCorruptError",
    "DesiredStateRecord",
    "DesiredStateRepo",
    "instances_with_recorded_desired_state",
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


_LIVE_STATE_NAMESPACE = "live_state"
_DESIRED_STATE_FILENAME = "desired_state.json"


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
            artifacts_root, _LIVE_STATE_NAMESPACE, strategy_instance_id
        )
        / _DESIRED_STATE_FILENAME
    )


def instances_with_recorded_desired_state(artifacts_root: Path) -> list[str]:
    """Every instance directory under ``artifacts_root`` that records a desired state.

    Sorted, and not validated: a directory name that is not a safe instance id
    is still listed, so the caller's ``stable_desired_state_path`` refuses it
    by name instead of it silently dropping out of a lane-wide sweep. An
    instance with no file is absent — its intent defaults to RUNNING only for
    a bot the runner already manages, never for a directory on disk.
    """
    live_state = Path(artifacts_root) / _LIVE_STATE_NAMESPACE
    if not live_state.is_dir():
        return []
    return sorted(
        child.name
        for child in live_state.iterdir()
        if child.is_dir() and (child / _DESIRED_STATE_FILENAME).is_file()
    )


class DesiredState(StrEnum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


class _EndUnchanged(Enum):
    UNCHANGED = "unchanged"


#: ``DesiredStateRepo.set``'s default: the write keeps the record's end as it is.
END_UNCHANGED = _EndUnchanged.UNCHANGED


class DesiredStateRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    desired_state: DesiredState
    updated_at_ms: int
    updated_by: str
    reason: str | None = None
    # The owner's one-time end for this deployment (#2607): the instant the
    # Clerk stops the bot, whether it sells or keeps its shares then, and when
    # the Clerk carried it out. The owner's schedule, not a sealed term -- it
    # is editable while the bot runs and enters no binding, hash or seal.
    # Absent (every record written before #2607) is "no end"; a carried-out
    # end stays readable but is no longer pending.
    end: RecordedEnd | None = None
    version: int = 1

    @model_validator(mode="before")
    @classmethod
    def discard_retired_fields(cls, value: object) -> object:
        """Read legacy files without retaining retired fields.

        The receipt witness is retired, and so is ``end_day_requested`` -- a
        marker nothing ever read, replaced by the scheduled end (#2607).
        """

        if isinstance(value, dict):
            value = dict(value)
            value.pop("last_disposition_id", None)
            value.pop("last_disposition_action", None)
            value.pop("end_day_requested", None)
        return value

    def pending_end(self) -> BotEnd | None:
        """The end the Clerk still has to carry out, if any."""
        return None if self.end is None else self.end.pending()


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
            fsync_parent_dir(path)

    def set(
        self,
        state: DesiredState,
        *,
        updated_by: str,
        now_ms: int,
        reason: str | None = None,
        end: BotEnd | Literal[_EndUnchanged.UNCHANGED] | None = END_UNCHANGED,
    ) -> DesiredStateRecord:
        """Read-modify-write the desired state under a single lock,
        bumping ``version`` from the prior record (or starting at 1).

        ``now_ms`` is supplied by the caller — timestamp rigor: the
        int64 ms UTC value is produced at the boundary, not inside this
        repo, so the write stays deterministic and testable.

        ``end`` replaces the scheduled end (``None``: no end) when given; by
        default the record keeps the end it has, so a crash, a restart or a
        runner write never loses the owner's schedule (#2607).
        """

        def build(existing: DesiredStateRecord | None, version: int) -> DesiredStateRecord:
            return DesiredStateRecord(
                desired_state=state,
                updated_at_ms=now_ms,
                updated_by=updated_by,
                reason=reason,
                version=version,
                end=(
                    (None if existing is None else existing.end)
                    if end is END_UNCHANGED
                    else _scheduled(end)
                ),
            )

        record = self._read_modify_write(build)
        assert record is not None
        return record

    def set_end(self, end: BotEnd | None, *, updated_by: str, now_ms: int) -> DesiredStateRecord:
        """Replace the scheduled end, keeping the desired state and its reason (#2607)."""

        def build(existing: DesiredStateRecord | None, version: int) -> DesiredStateRecord:
            return DesiredStateRecord(
                desired_state=DesiredState.RUNNING if existing is None else existing.desired_state,
                updated_at_ms=now_ms,
                updated_by=updated_by,
                reason=None if existing is None else existing.reason,
                version=version,
                end=_scheduled(end),
            )

        record = self._read_modify_write(build)
        assert record is not None
        return record

    def cancel_end(self, *, updated_by: str, now_ms: int) -> BotEnd | None:
        """Cancel the end still to be carried out, keeping the desired state and its reason; return it.

        The owner's Stop (#2607). Nothing is written when no end is pending --
        no record, no end, or one already carried out -- so a Stop of a bot
        with no desired state leaves none behind.
        """
        cancelled: BotEnd | None = None

        def build(existing: DesiredStateRecord | None, version: int) -> DesiredStateRecord | None:
            nonlocal cancelled
            cancelled = None if existing is None else existing.pending_end()
            if existing is None or cancelled is None:
                return None
            return existing.model_copy(
                update={"updated_at_ms": now_ms, "updated_by": updated_by, "end": None, "version": version}
            )

        self._read_modify_write(build)
        return cancelled

    def mark_end_carried_out(
        self, end: BotEnd, *, updated_by: str, now_ms: int
    ) -> DesiredStateRecord | None:
        """Record ``end`` carried out; a no-op unless it is still the pending end.

        Nothing is written when the end was already recorded, or when the
        owner changed it after the Clerk read it: the Clerk records only the
        end it carried out. Stopping the bot is the runner's own write.
        """

        def build(existing: DesiredStateRecord | None, version: int) -> DesiredStateRecord | None:
            if existing is None or existing.end is None or existing.pending_end() != end:
                return None
            return existing.model_copy(
                update={
                    "updated_at_ms": now_ms,
                    "updated_by": updated_by,
                    "end": existing.end.model_copy(update={"carried_out_at_ms": now_ms}),
                    "version": version,
                }
            )

        return self._read_modify_write(build)

    def _read_modify_write(
        self,
        build: Callable[[DesiredStateRecord | None, int], DesiredStateRecord | None],
    ) -> DesiredStateRecord | None:
        """One read-modify-write under the file lock; ``build`` returning ``None`` writes nothing."""
        path = self._confined_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock(path, trusted_root=self._trusted_root):
            existing = self.read()
            next_version = (existing.version + 1) if existing is not None else 1
            record = build(existing, next_version)
            if record is None:
                return existing
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
        fsync_parent_dir(safe_path)


def _scheduled(end: BotEnd | None) -> RecordedEnd | None:
    """A newly chosen end, not yet carried out; ``None`` is no end."""
    return None if end is None else RecordedEnd.scheduled(end)
