"""Account Clerk durable intake and recorded-receipt journal.

Issue #1016 deliberately stops at receipt #1.  The clerk accepts the existing
``AccountOwnerSubmitIntent`` wire model, validates its *individual* account
instance binding, and writes it through a durable inbox into the account
journal.  It never contacts a broker; the serialized broker drain and ack
receipt are later slices.

The inbox is the crash boundary.  A process can fail after the inbox fsync and
before the journal fsync.  The next intake replays that inbox row into the
journal before accepting new work, so an accepted intent cannot be silently
lost.  The journal is the canonical receipt #1 ledger and is replayable by a
new clerk process.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal, overload

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.engine.live.account_artifacts import account_artifacts_root
from app.engine.live.account_owner import AccountOwnerSubmitIntent
from app.engine.live.account_registry import (
    AccountInstanceBinding,
    latest_account_instance_binding,
    read_account_instance_registry,
)
from app.engine.live.live_state_sidecar import _file_lock, _fsync_parent_dir

ACCOUNT_CLERK_INBOX_FILENAME = "clerk_inbox.jsonl"
ACCOUNT_CLERK_JOURNAL_FILENAME = "clerk_journal.jsonl"
_MAX_INT64 = 9_223_372_036_854_775_807


class AccountClerkJournalCorruptError(RuntimeError):
    """Raised when an account clerk inbox or journal cannot be safely replayed."""

    def __init__(self, path: Path, detail: str) -> None:
        super().__init__(f"account clerk artifact at {path} is corrupt: {detail}")
        self.path = path
        self.detail = detail


class AccountClerkIntentRejected(RuntimeError):
    """Identity-scoped intake rejection before the durable inbox is written."""

    def __init__(self, *, reason: str, diagnostics: dict[str, object]) -> None:
        super().__init__(f"AccountClerkIntentRejected(reason={reason!r})")
        self.reason = reason
        self.diagnostics = diagnostics


class AccountClerkInboxEntry(BaseModel):
    """A validated durable intake row awaiting journal recording, if necessary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    seq: int = Field(ge=1)
    received_at_ms: int = Field(ge=0, le=_MAX_INT64)
    intent: AccountOwnerSubmitIntent


class AccountClerkJournalEntry(BaseModel):
    """One serial, durable receipt-#1 ledger entry for an account intent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    seq: int = Field(ge=1)
    recorded_at_ms: int = Field(ge=0, le=_MAX_INT64)
    intent: AccountOwnerSubmitIntent


class AccountClerkRecordedReceipt(BaseModel):
    """Durable receipt #1 returned before any future broker contact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["recorded"] = "recorded"
    trace_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    strategy_instance_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    bot_order_namespace: str = Field(min_length=1)
    intent_id: str = Field(min_length=1)
    order_ref: str = Field(min_length=1)
    journal_seq: int = Field(ge=1)
    recorded_at_ms: int = Field(ge=0, le=_MAX_INT64)

    @classmethod
    def from_journal_entry(cls, entry: AccountClerkJournalEntry) -> AccountClerkRecordedReceipt:
        intent = entry.intent
        return cls(
            trace_id=intent.trace_id,
            account_id=intent.account_id,
            strategy_instance_id=intent.strategy_instance_id,
            run_id=intent.run_id,
            bot_order_namespace=intent.bot_order_namespace,
            intent_id=intent.intent_id,
            order_ref=intent.order_ref,
            journal_seq=entry.seq,
            recorded_at_ms=entry.recorded_at_ms,
        )


class AccountClerk:
    """Per-account concurrent intake backed by one serialized durable journal.

    ``broker`` is intentionally retained only as a constructor seam for this
    first slice's no-contact characterization test.  Clerk core never calls
    it: #1020 owns the broker-drain cutover.
    """

    def __init__(
        self,
        *,
        artifacts_root: Path,
        account_id: str,
        broker: object | None = None,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._artifacts_root = artifacts_root
        self._account_id = account_id
        self._broker = broker
        self._now_ms = now_ms if now_ms is not None else _now_ms
        self._intake_lock = asyncio.Lock()

    async def record_intent(self, intent: AccountOwnerSubmitIntent) -> AccountClerkRecordedReceipt:
        """Validate, durably record, and acknowledge one intent without I/O to IBKR."""

        async with self._intake_lock:
            return self._record_intent_locked(intent)

    def replay_recorded_receipts(self) -> list[AccountClerkRecordedReceipt]:
        """Return receipt #1 values from the journal after a clerk restart."""

        return [
            AccountClerkRecordedReceipt.from_journal_entry(entry)
            for entry in read_account_clerk_journal(self._artifacts_root, self._account_id)
        ]

    async def recover_inbox(self) -> list[AccountClerkRecordedReceipt]:
        """Replay an inbox row left durable by a crash before journal fsync."""

        async with self._intake_lock:
            inbox_path = account_clerk_inbox_path(self._artifacts_root, self._account_id)
            journal_path = account_clerk_journal_path(self._artifacts_root, self._account_id)
            journal_path.parent.mkdir(parents=True, exist_ok=True)
            with _file_lock(journal_path):
                journal_entries = self._replay_inbox_locked(
                    inbox_entries=_read_jsonl(inbox_path, AccountClerkInboxEntry),
                    journal_entries=_read_jsonl(journal_path, AccountClerkJournalEntry),
                    journal_path=journal_path,
                )
            return [
                AccountClerkRecordedReceipt.from_journal_entry(entry)
                for entry in journal_entries
            ]

    def _record_intent_locked(self, intent: AccountOwnerSubmitIntent) -> AccountClerkRecordedReceipt:
        if intent.account_id != self._account_id:
            self._reject(intent, "CLERK_ACCOUNT_MISMATCH")

        inbox_path = account_clerk_inbox_path(self._artifacts_root, self._account_id)
        journal_path = account_clerk_journal_path(self._artifacts_root, self._account_id)
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock(journal_path):
            inbox_entries = _read_jsonl(inbox_path, AccountClerkInboxEntry)
            journal_entries = _read_jsonl(journal_path, AccountClerkJournalEntry)
            journal_entries = self._replay_inbox_locked(
                inbox_entries=inbox_entries,
                journal_entries=journal_entries,
                journal_path=journal_path,
            )

            existing = _journal_entry_for_intent(journal_entries, intent)
            if existing is not None:
                return AccountClerkRecordedReceipt.from_journal_entry(existing)

            self._validate_intent_identity(intent)
            next_seq = (journal_entries[-1].seq + 1) if journal_entries else 1
            inbox_entry = AccountClerkInboxEntry(
                seq=next_seq,
                received_at_ms=self._now_ms(),
                intent=intent,
            )
            _append_jsonl(inbox_path, inbox_entry)
            journal_entry = AccountClerkJournalEntry(
                seq=inbox_entry.seq,
                recorded_at_ms=self._now_ms(),
                intent=inbox_entry.intent,
            )
            _append_jsonl(journal_path, journal_entry)
            return AccountClerkRecordedReceipt.from_journal_entry(journal_entry)

    def _replay_inbox_locked(
        self,
        *,
        inbox_entries: list[AccountClerkInboxEntry],
        journal_entries: list[AccountClerkJournalEntry],
        journal_path: Path,
    ) -> list[AccountClerkJournalEntry]:
        journal_by_seq = {entry.seq: entry for entry in journal_entries}
        for inbox_entry in inbox_entries:
            journal_entry = journal_by_seq.get(inbox_entry.seq)
            if journal_entry is not None:
                if journal_entry.intent != inbox_entry.intent:
                    raise AccountClerkJournalCorruptError(
                        journal_path,
                        f"inbox and journal intent differ at seq {inbox_entry.seq}",
                    )
                continue
            expected_seq = (journal_entries[-1].seq + 1) if journal_entries else 1
            if inbox_entry.seq != expected_seq:
                raise AccountClerkJournalCorruptError(
                    journal_path,
                    f"inbox seq {inbox_entry.seq} cannot follow journal seq {expected_seq - 1}",
                )
            replayed = AccountClerkJournalEntry(
                seq=inbox_entry.seq,
                recorded_at_ms=inbox_entry.received_at_ms,
                intent=inbox_entry.intent,
            )
            _append_jsonl(journal_path, replayed)
            journal_entries.append(replayed)
            journal_by_seq[replayed.seq] = replayed
        return journal_entries

    def _validate_intent_identity(self, intent: AccountOwnerSubmitIntent) -> None:
        binding = latest_account_instance_binding(
            read_account_instance_registry(self._artifacts_root, self._account_id),
            account_id=self._account_id,
            strategy_instance_id=intent.strategy_instance_id,
        )
        if binding is None:
            self._reject(intent, "CLERK_UNKNOWN_INSTANCE")
        assert binding is not None
        self._validate_binding(intent, binding)

    def _validate_binding(
        self,
        intent: AccountOwnerSubmitIntent,
        binding: AccountInstanceBinding,
    ) -> None:
        if binding.lifecycle_state != "ACTIVE":
            self._reject(intent, "CLERK_INACTIVE_BINDING")
        if binding.account_id != intent.account_id:
            self._reject(intent, "CLERK_ACCOUNT_MISMATCH")
        if binding.run_id != intent.run_id:
            self._reject(intent, "CLERK_STALE_RUN")
        if binding.bot_order_namespace != intent.bot_order_namespace:
            self._reject(intent, "CLERK_NAMESPACE_MISMATCH")

    def _reject(self, intent: AccountOwnerSubmitIntent, reason: str) -> None:
        raise AccountClerkIntentRejected(
            reason=reason,
            diagnostics={
                "trace_id": intent.trace_id,
                "account_id": intent.account_id,
                "strategy_instance_id": intent.strategy_instance_id,
                "run_id": intent.run_id,
                "bot_order_namespace": intent.bot_order_namespace,
                "intent_id": intent.intent_id,
                "order_ref": intent.order_ref,
            },
        )


def account_clerk_inbox_path(artifacts_root: Path, account_id: str) -> Path:
    return account_artifacts_root(artifacts_root, account_id) / ACCOUNT_CLERK_INBOX_FILENAME


def account_clerk_journal_path(artifacts_root: Path, account_id: str) -> Path:
    return account_artifacts_root(artifacts_root, account_id) / ACCOUNT_CLERK_JOURNAL_FILENAME


def read_account_clerk_inbox(
    artifacts_root: Path,
    account_id: str,
) -> list[AccountClerkInboxEntry]:
    """Read the strict, replayable durable intake inbox for an account."""

    path = account_clerk_inbox_path(artifacts_root, account_id)
    journal_path = account_clerk_journal_path(artifacts_root, account_id)
    with _file_lock(journal_path):
        return _read_jsonl(path, AccountClerkInboxEntry)


def read_account_clerk_journal(
    artifacts_root: Path,
    account_id: str,
) -> list[AccountClerkJournalEntry]:
    """Read the strict, serial receipt-#1 ledger for an account."""

    path = account_clerk_journal_path(artifacts_root, account_id)
    with _file_lock(path):
        return _read_jsonl(path, AccountClerkJournalEntry)


@overload
def _read_jsonl(path: Path, model_type: type[AccountClerkInboxEntry]) -> list[AccountClerkInboxEntry]: ...


@overload
def _read_jsonl(path: Path, model_type: type[AccountClerkJournalEntry]) -> list[AccountClerkJournalEntry]: ...


def _read_jsonl(
    path: Path,
    model_type: type[AccountClerkInboxEntry] | type[AccountClerkJournalEntry],
) -> list[AccountClerkInboxEntry] | list[AccountClerkJournalEntry]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise AccountClerkJournalCorruptError(path, f"invalid UTF-8: {exc}") from exc

    entries = []
    expected_seq = 1
    for line_no, line in enumerate(lines, start=1):
        if not line:
            raise AccountClerkJournalCorruptError(path, f"blank row at line {line_no}")
        try:
            entry = model_type.model_validate_json(line)
        except (ValidationError, ValueError) as exc:
            raise AccountClerkJournalCorruptError(path, f"invalid row at line {line_no}: {exc}") from exc
        if entry.seq != expected_seq:
            raise AccountClerkJournalCorruptError(
                path,
                f"expected seq {expected_seq} at line {line_no}, found {entry.seq}",
            )
        entries.append(entry)
        expected_seq += 1
    return entries


def _append_jsonl(path: Path, entry: AccountClerkInboxEntry | AccountClerkJournalEntry) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as file_handle:
        file_handle.write(entry.model_dump_json() + "\n")
        file_handle.flush()
        os.fsync(file_handle.fileno())
    _fsync_parent_dir(path)


def _journal_entry_for_intent(
    journal_entries: list[AccountClerkJournalEntry],
    intent: AccountOwnerSubmitIntent,
) -> AccountClerkJournalEntry | None:
    matching = [entry for entry in journal_entries if entry.intent.intent_id == intent.intent_id]
    if not matching:
        return None
    existing = matching[0]
    if existing.intent != intent:
        raise AccountClerkIntentRejected(
            reason="CLERK_INTENT_ID_COLLISION",
            diagnostics={
                "existing_intent": existing.intent.model_dump(mode="json"),
                "received_intent": intent.model_dump(mode="json"),
            },
        )
    return existing


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


__all__ = [
    "ACCOUNT_CLERK_INBOX_FILENAME",
    "ACCOUNT_CLERK_JOURNAL_FILENAME",
    "AccountClerk",
    "AccountClerkInboxEntry",
    "AccountClerkIntentRejected",
    "AccountClerkJournalCorruptError",
    "AccountClerkJournalEntry",
    "AccountClerkRecordedReceipt",
    "account_clerk_inbox_path",
    "account_clerk_journal_path",
    "read_account_clerk_inbox",
    "read_account_clerk_journal",
]
