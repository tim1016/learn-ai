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

import os
import re
import threading
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.broker.alpaca.clerk.models import OrderJournalEntry

# PythonDataService/ — app/broker/alpaca/clerk/journal.py → parents[4].
_SERVICE_ROOT = Path(__file__).resolve().parents[4]

# Traversal-safe path component (the CodeQL-recognised sanitiser): an unsafe
# account id never reaches the filesystem path.
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")

INBOX_FILENAME = "order_inbox.jsonl"
JOURNAL_FILENAME = "order_journal.jsonl"


class ClerkSettings(BaseSettings):
    """Alpaca-clerk journal settings.

    ``ALPACA_CLERK_DIR`` roots the order journals under a git-ignored ``var/``
    tree, **separate** from the broker capture journals (``BROKER_CAPTURE_DIR``)
    and from any IBKR live-runs artifacts. Each broker's journaling stays in its
    own tree.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="ALPACA_CLERK_",
        case_sensitive=False,
        extra="ignore",
    )

    dir: Path = _SERVICE_ROOT / "var" / "alpaca_clerk"


def _safe_component(value: str, kind: str) -> str:
    """Return a traversal-safe path component or raise."""
    if not _SAFE_COMPONENT.match(value):
        raise ValueError(f"unsafe {kind} path component: {value!r}")
    return value


class OrderJournal:
    """Single-writer append-only order journal for one Alpaca account.

    An instance is bound to one ``account_id`` (the Clerk holds one per account
    it serves). ``append`` writes the entry to the inbox then the journal, each
    flushed and ``fsync``'d, before returning — the durability guarantee the
    order path relies on.
    """

    def __init__(self, *, account_id: str, root: Path) -> None:
        self._account_id = _safe_component(account_id, "account_id")
        self._root = Path(root).resolve()
        self._dir = self._account_dir()
        self._lock = threading.Lock()
        self._appended = 0

    @property
    def account_dir(self) -> Path:
        return self._dir

    @property
    def appended(self) -> int:
        with self._lock:
            return self._appended

    def _account_dir(self) -> Path:
        """Resolve this account's journal directory, guaranteeing containment."""
        path = (self._root / "accounts" / "alpaca" / self._account_id).resolve()
        # Defence in depth: the account id is charset-validated above, so this
        # can only trip on a symlinked root — a fatal journal misconfiguration.
        if not str(path).startswith(str(self._root)):
            raise ValueError(f"clerk journal path escapes root: {path}")
        return path

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
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def read_entries(self) -> list[OrderJournalEntry]:
        """Replay the canonical ledger into entries (recovery / test seam)."""
        path = self._dir / JOURNAL_FILENAME
        if not path.is_file():
            return []
        with self._lock, path.open("r", encoding="utf-8") as handle:
            return [
                OrderJournalEntry.model_validate_json(stripped)
                for raw in handle
                if (stripped := raw.strip())
            ]


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
