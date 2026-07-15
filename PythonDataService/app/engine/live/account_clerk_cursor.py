"""Run-scoped durable acknowledgement cursors for Account Clerk callbacks.

The account Clerk journal is authoritative and never destructively drained.
Each bot records the highest Clerk journal sequence whose callback has already
been fsynced into that bot's own durable callback artifact.  A crash between
those two durable writes intentionally leaves the cursor behind, causing an
at-least-once redelivery on restart.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.engine.live.live_state_sidecar import _file_lock, _fsync_parent_dir
from app.services.jsonl_wal import confined_wal_path

ACCOUNT_CLERK_EVENT_CURSOR_FILENAME = "account_clerk_event_cursor.json"


class AccountClerkEventCursorCorruptError(RuntimeError):
    """Raised when a bot's durable Clerk-delivery cursor is unsafe to trust."""

    def __init__(self, path: Path, detail: str) -> None:
        super().__init__(f"account Clerk event cursor at {path} is corrupt: {detail}")
        self.path = path
        self.detail = detail


class AccountClerkEventConsumerIdentity(BaseModel):
    """One run-scoped consumer identity accepted by the Account Clerk."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str = Field(min_length=1, max_length=64)
    strategy_instance_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    bot_order_namespace: str = Field(min_length=1, max_length=256)


class AccountClerkEventCursor(BaseModel):
    """The durable acknowledgement boundary for one bot run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    consumer: AccountClerkEventConsumerIdentity
    last_journal_seq: int = Field(default=0, ge=0)


def account_clerk_event_cursor_path(run_dir: Path) -> Path:
    """Return the confined canonical cursor artifact for a run directory."""

    return confined_wal_path(run_dir, ACCOUNT_CLERK_EVENT_CURSOR_FILENAME)


@dataclass(frozen=True)
class AccountClerkEventCursorRepo:
    """Atomic read/advance API for one run's Clerk callback cursor."""

    run_dir: Path

    @property
    def path(self) -> Path:
        return account_clerk_event_cursor_path(self.run_dir)

    def last_journal_seq(self, consumer: AccountClerkEventConsumerIdentity) -> int:
        """Return the durable sequence for ``consumer`` without creating state."""

        cursor = self.read(consumer)
        return cursor.last_journal_seq if cursor is not None else 0

    def read(self, consumer: AccountClerkEventConsumerIdentity) -> AccountClerkEventCursor | None:
        """Read the cursor, refusing reuse of a run artifact by another consumer."""

        path = self.path
        if not path.exists():
            return None
        try:
            cursor = AccountClerkEventCursor.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError) as exc:
            raise AccountClerkEventCursorCorruptError(path, str(exc)) from exc
        if cursor.consumer != consumer:
            raise AccountClerkEventCursorCorruptError(
                path,
                "cursor consumer identity does not match the requesting run",
            )
        return cursor

    def advance_after_durable_event_write(
        self,
        consumer: AccountClerkEventConsumerIdentity,
        *,
        journal_seq: int,
    ) -> bool:
        """Persist an acknowledgement only after the caller fsynced its event.

        The caller deliberately invokes this after its own durable callback WAL
        write.  Lower/equal sequences are already acknowledged and therefore
        perform no write, keeping repeated acknowledgements crash-safe.
        """

        if journal_seq < 1:
            raise ValueError("journal_seq must be >= 1")
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock(path, trusted_root=self.run_dir):
            existing = self.read(consumer)
            if existing is not None and journal_seq <= existing.last_journal_seq:
                return False
            next_cursor = AccountClerkEventCursor(
                consumer=consumer,
                last_journal_seq=journal_seq,
            )
            _write_cursor_locked(path, next_cursor)
        return True


def _write_cursor_locked(path: Path, cursor: AccountClerkEventCursor) -> None:
    """Fsync + atomically replace a cursor while its advisory lock is held."""

    temporary_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary_path.open("w", encoding="utf-8") as file_handle:
            file_handle.write(cursor.model_dump_json())
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(temporary_path, path)
        _fsync_parent_dir(path)
    except Exception:
        with contextlib.suppress(OSError):
            temporary_path.unlink()
        raise


__all__ = [
    "ACCOUNT_CLERK_EVENT_CURSOR_FILENAME",
    "AccountClerkEventConsumerIdentity",
    "AccountClerkEventCursor",
    "AccountClerkEventCursorCorruptError",
    "AccountClerkEventCursorRepo",
    "account_clerk_event_cursor_path",
]
