"""Fail-closed Clerk-journal recovery state shared by writer and desk projections."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from app.engine.live.account_artifacts import (
    account_artifact_file_path,
    safe_account_artifact_id,
)
from app.engine.live.live_state_sidecar import _file_lock
from app.schemas.journal_recovery import JournalRecoveryState

JOURNAL_RECOVERY_STATE_FILENAME = "clerk_journal_recovery.json"
JOURNAL_RECOVERY_ADMISSION_FILENAME = "clerk_journal_recovery_admission"


class JournalRecoveryStateCorruptError(RuntimeError):
    """The durable recovery state cannot establish that broker writes are safe."""

    def __init__(self, path: Path, detail: str) -> None:
        super().__init__(f"journal recovery state at {path} is corrupt: {detail}")
        self.path = path
        self.detail = detail


@dataclass(frozen=True)
class JournalRecoveryFence:
    """One explicit write-admission verdict from the durable ceremony state."""

    state: JournalRecoveryState | None
    reason_code: str | None

    @property
    def blocks_broker_writes(self) -> bool:
        return self.reason_code is not None


def journal_recovery_state_path(artifacts_root: Path, account_id: str) -> Path:
    """Return the account-scoped ceremony record without creating a directory."""

    return account_artifact_file_path(
        artifacts_root,
        safe_account_artifact_id(account_id),
        JOURNAL_RECOVERY_STATE_FILENAME,
    )


def journal_recovery_admission_path(artifacts_root: Path, account_id: str) -> Path:
    """Return the shared broker-write/recovery exclusion target for an account."""

    return account_artifact_file_path(
        artifacts_root,
        safe_account_artifact_id(account_id),
        JOURNAL_RECOVERY_ADMISSION_FILENAME,
    )


@contextmanager
def journal_recovery_admission_lock(artifacts_root: Path, account_id: str) -> Iterator[None]:
    """Serialize a recovery claim with an already-admitted broker invocation.

    The Clerk takes this lock across its final fence check and broker call;
    recovery takes it before recording QUARANTINE_PENDING.  This is a separate
    lock from the ledger lock so a broker write can safely journal its
    pre-submit marker while still excluding a concurrent recovery claim.
    """

    with _file_lock(journal_recovery_admission_path(artifacts_root, account_id)):
        yield


def read_journal_recovery_state(
    artifacts_root: Path,
    account_id: str,
) -> JournalRecoveryState | None:
    """Read recovery state strictly; unreadable state always blocks Clerk writes."""

    path = journal_recovery_state_path(artifacts_root, account_id)
    try:
        if path.is_symlink():
            raise OSError("recovery state must not be a symlink")
        if not path.exists():
            return None
        state = JournalRecoveryState.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        raise JournalRecoveryStateCorruptError(path, str(exc)) from exc
    if state.account_id != account_id:
        raise JournalRecoveryStateCorruptError(path, "recovery state account_id does not match its artifact directory")
    return state


def assess_journal_recovery_fence(
    artifacts_root: Path,
    account_id: str,
) -> JournalRecoveryFence:
    """Keep incomplete ceremonies and unowned baseline exposure out of broker writes."""

    state = read_journal_recovery_state(artifacts_root, account_id)
    if state is None:
        return JournalRecoveryFence(state=None, reason_code=None)
    if state.phase != "COMPLETE":
        return JournalRecoveryFence(state=state, reason_code="CLERK_JOURNAL_RECOVERY_REQUIRED")
    if state.broker_evidence_positions:
        return JournalRecoveryFence(state=state, reason_code="CLERK_BROKER_EVIDENCE_ONLY_HOLD")
    return JournalRecoveryFence(state=state, reason_code=None)


__all__ = [
    "JOURNAL_RECOVERY_ADMISSION_FILENAME",
    "JOURNAL_RECOVERY_STATE_FILENAME",
    "JournalRecoveryFence",
    "JournalRecoveryStateCorruptError",
    "assess_journal_recovery_fence",
    "journal_recovery_admission_lock",
    "journal_recovery_admission_path",
    "journal_recovery_state_path",
    "read_journal_recovery_state",
]
