"""Append-only, fsync'd order journal for the Alpaca Clerk (phase 2).

Two files per account under an Alpaca-scoped path::

    <root>/accounts/alpaca/<account_id>/order_inbox.jsonl    # intent WAL
    <root>/accounts/alpaca/<account_id>/order_journal.jsonl  # canonical ledger

**"No journal → no order."** The order path is fail-**closed** (unlike the
phase-1 read-path capture journal, which is best-effort): an intent is written
*and* ``fsync``'d to both files BEFORE any broker HTTP call. A write failure
raises, so no order is ever sent without a durable record. This is the
recoverable-uncertainty spine that later slices (S5 replay) build on.

This is a NEW lean journal, deliberately not coupled to the IBKR clerk journal:
it shares the *pattern* (single-writer lock, append + flush + fsync,
traversal-safe path components) but its own entry model. Entries are
broker-neutral (``OrderJournalEntry``), so the on-disk ledger is portable.

Single writer per process (an ``asyncio.Lock`` in the Clerk serializes intake;
this class serializes the file appends themselves with a ``threading.Lock``).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.broker.alpaca.clerk.models import OrderJournalEntry
from app.broker.alpaca.paths import (
    fsync_directory as _shared_fsync_directory,
)
from app.broker.alpaca.paths import (
    resolve_contained_path,
    safe_path_component,
)

# PythonDataService/ — app/broker/alpaca/clerk/journal.py → parents[4].
_SERVICE_ROOT = Path(__file__).resolve().parents[4]

INBOX_FILENAME = "order_inbox.jsonl"
JOURNAL_FILENAME = "order_journal.jsonl"
_CURSOR_GUARD_BYTES = 64


def _cursor_guard(handle: BinaryIO, offset: int) -> str | None:
    """Hash constant-size prefix and cursor-boundary evidence."""
    if offset <= 0:
        return None
    handle.seek(0)
    prefix = handle.read(min(_CURSOR_GUARD_BYTES, offset))
    guard_start = max(0, offset - _CURSOR_GUARD_BYTES)
    handle.seek(guard_start)
    boundary = handle.read(offset - guard_start)
    return hashlib.sha256(prefix + b"\0" + boundary).hexdigest()


@dataclass(frozen=True)
class JournalTailRead:
    """One cursor-based read of complete canonical-ledger records.

    ``next_offset`` always points immediately after the last complete newline,
    so a concurrent writer's partial final record is retried on the next read.
    ``reset`` tells the projection owner that rotation or truncation invalidated
    its in-memory projection and a cold replay was performed.
    """

    entries: tuple[OrderJournalEntry, ...]
    next_offset: int
    file_identity: tuple[int, int] | None
    reset: bool
    bytes_read: int
    cursor_guard: str | None = None


class ClerkSettings(BaseSettings):
    """Alpaca-clerk journal settings.

    ``ALPACA_CLERK_DIR`` roots the order journals under the service's mounted,
    git-ignored ``artifacts/`` tree, **separate** from the broker capture
    journals (``BROKER_CAPTURE_DIR``) and from IBKR live-runs artifacts. Each
    broker's journaling stays in its own tree.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="ALPACA_CLERK_",
        case_sensitive=False,
        extra="ignore",
    )

    dir: Path = _SERVICE_ROOT / "artifacts" / "alpaca_clerk"


class OrderJournal:
    """Single-writer append-only order journal for one Alpaca account.

    An instance is bound to one ``account_id`` (the Clerk holds one per account
    it serves). ``append`` writes the entry to the inbox then the journal, each
    flushed and ``fsync``'d, before returning — the durability guarantee the
    order path relies on.
    """

    def __init__(self, *, account_id: str, root: Path) -> None:
        self._account_id = safe_path_component(account_id, "account_id")
        self._root = Path(root).resolve()
        self._dir = self._account_dir()
        self._lock = threading.Lock()
        self._appended = 0
        self._projection_task: asyncio.Task[None] | None = None
        self._projection_requested = False

    @property
    def account_dir(self) -> Path:
        return self._dir

    @property
    def appended(self) -> int:
        with self._lock:
            return self._appended

    def _account_dir(self) -> Path:
        """Resolve this account's journal directory, guaranteeing containment."""
        return resolve_contained_path(self._root, "accounts", "alpaca", self._account_id)

    def append(self, entry: OrderJournalEntry) -> None:
        """Append one entry to the inbox and the journal; ``fsync`` each path.

        Fail-closed: any I/O error propagates so the caller (the Clerk) never
        proceeds to the broker without a durable intent record.
        """
        line = entry.model_dump_json() + "\n"
        with self._lock:
            inbox_path = self._dir / INBOX_FILENAME
            journal_path = self._dir / JOURNAL_FILENAME
            # Directory metadata changes only when one of the fixed journal
            # files is first created. Avoid four unnecessary directory fsyncs
            # on every later append while retaining the first-write guarantee.
            needs_directory_sync = not inbox_path.is_file() or not journal_path.is_file()
            self._dir.mkdir(parents=True, exist_ok=True)
            self._append_fsynced(inbox_path, line)
            self._append_fsynced(journal_path, line)
            # File fsync alone does not make newly-created file or directory
            # entries durable on POSIX. Sync the account directory (for both
            # journal files) and each ancestor created by mkdir before the
            # caller is allowed to submit an order.
            if needs_directory_sync:
                self._fsync_directory_chain()
            self._appended += 1

    async def append_async(self, entry: OrderJournalEntry) -> None:
        """Append durably without blocking the async service's event loop."""
        await asyncio.to_thread(self.append, entry)
        # The JSONL acknowledgement is canonical. The shared SQL projection is
        # best-effort and rebuildable, so schedule it only after fsync and never
        # delay or alter a Clerk outcome when Postgres is unavailable.
        self._projection_requested = True
        if self._projection_task is None or self._projection_task.done():
            self._projection_task = asyncio.create_task(
                self._drain_projection_requests(),
                name="alpaca-transaction-projection",
            )

    async def _drain_projection_requests(self) -> None:
        """Coalesce bursts of fsync completions into one projection tail at a time."""

        from app.services.clerk_transaction_projection import project_alpaca_journal_best_effort

        while self._projection_requested:
            self._projection_requested = False
            await project_alpaca_journal_best_effort(
                artifacts_root=self._root,
                account_id=self._account_id,
            )

    @staticmethod
    def _append_fsynced(path: Path, line: str) -> None:
        """Append one line and ``fsync`` the file descriptor before returning."""
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def _fsync_directory_chain(self) -> None:
        """Persist journal file entries and any newly-created parent entries."""
        directory = self._dir
        while True:
            self._fsync_directory(directory)
            if directory == self._root:
                return
            directory = directory.parent

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        """``fsync`` one POSIX directory, propagating failures fail-closed."""
        _shared_fsync_directory(path)

    def read_entries(self) -> list[OrderJournalEntry]:
        """Replay the canonical ledger into entries (recovery / test seam)."""
        candidate = resolve_contained_path(
            self._root, "accounts", "alpaca", self._account_id, JOURNAL_FILENAME
        )
        if not candidate.is_file():
            return []
        with self._lock, open(candidate, encoding="utf-8") as handle:
            return [
                OrderJournalEntry.model_validate_json(stripped)
                for raw in handle
                if (stripped := raw.strip())
            ]

    def read_from(
        self,
        offset: int,
        *,
        file_identity: tuple[int, int] | None,
        cursor_guard: str | None = None,
    ) -> JournalTailRead:
        """Read only complete records appended after ``offset``.

        This is the warm-projection API. The journal is replayed from byte zero
        only when the caller has no cursor, or when inode, size, or constant-size
        prefix/boundary evidence proves the cursor no longer names the ledger.
        """
        if offset < 0:
            raise ValueError("journal offset must be non-negative")
        candidate = resolve_contained_path(
            self._root, "accounts", "alpaca", self._account_id, JOURNAL_FILENAME
        )
        if not candidate.is_file():
            return JournalTailRead(
                (),
                0,
                None,
                file_identity is not None or offset > 0,
                0,
                None,
            )

        with self._lock, open(candidate, "rb") as handle:
            stat = os.fstat(handle.fileno())
            identity = (stat.st_dev, stat.st_ino)
            guard_mismatch = False
            if cursor_guard is not None and 0 < offset <= stat.st_size:
                guard_mismatch = _cursor_guard(handle, offset) != cursor_guard
            reset = (
                (file_identity is not None and identity != file_identity)
                or offset > stat.st_size
                or guard_mismatch
            )
            start = 0 if reset else offset
            handle.seek(start)
            payload = handle.read()
            last_newline = payload.rfind(b"\n")
            complete = payload[: last_newline + 1] if last_newline >= 0 else b""
            next_offset = start + len(complete)
            next_guard = _cursor_guard(handle, next_offset)

        entries = tuple(
            OrderJournalEntry.model_validate_json(line)
            for line in complete.splitlines()
            if line.strip()
        )
        return JournalTailRead(
            entries=entries,
            next_offset=next_offset,
            file_identity=identity,
            reset=reset,
            bytes_read=len(payload),
            cursor_guard=next_guard,
        )


_settings: ClerkSettings | None = None


def get_clerk_settings() -> ClerkSettings:
    """Return the process-wide clerk settings, instantiated on first use."""
    global _settings
    if _settings is None:
        _settings = ClerkSettings()
    return _settings


def reset_clerk_settings_for_testing() -> None:
    """Drop cached settings so a test can rebind the environment."""
    global _settings
    _settings = None
