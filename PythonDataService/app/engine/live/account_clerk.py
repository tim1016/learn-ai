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

import argparse
import asyncio
import hashlib
import os
import signal
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, overload

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.broker.ibkr.models import IbkrOrderEvent, IbkrOrderSpec
from app.engine.live.account_artifacts import (
    AccountClerkLease,
    account_artifacts_root,
    write_account_clerk_lease,
)
from app.engine.live.account_owner import AccountOwnerSubmitIntent
from app.engine.live.account_owner_fence import account_clerk_write_grant
from app.engine.live.account_registry import (
    AccountInstanceBinding,
    latest_account_instance_binding,
    read_account_instance_registry,
)
from app.engine.live.live_state_sidecar import _file_lock, _fsync_parent_dir

if TYPE_CHECKING:
    from app.engine.live.account_clerk_rpc import AccountClerkRpcClient, AccountClerkRpcServer

ACCOUNT_CLERK_INBOX_FILENAME = "clerk_inbox.jsonl"
ACCOUNT_CLERK_JOURNAL_FILENAME = "clerk_journal.jsonl"
_MAX_INT64 = 9_223_372_036_854_775_807
_CLERK_LEASE_TTL_MS = 5_000


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
    entry_kind: Literal["recorded", "recovery_cancelled", "broker_acked", "broker_event"] = "recorded"
    recorded_at_ms: int = Field(ge=0, le=_MAX_INT64)
    intent: AccountOwnerSubmitIntent
    order_id: int | None = Field(default=None, ge=0)
    perm_id: int | None = Field(default=None, ge=0)
    exec_id: str | None = None
    broker_event: dict[str, object] | None = None
    cancelled_order_ids: tuple[int, ...] | None = None


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


class AccountClerkBrokerAckReceipt(AccountClerkRecordedReceipt):
    """Receipt #2, appended by the clerk only after the paper broker acks."""

    status: Literal["broker_acked"] = "broker_acked"
    order_id: int = Field(ge=0)
    perm_id: int | None = Field(default=None, ge=0)
    exec_id: str | None = None

    @classmethod
    def from_journal_entry(cls, entry: AccountClerkJournalEntry) -> AccountClerkBrokerAckReceipt:
        if entry.entry_kind != "broker_acked" or entry.order_id is None:
            raise ValueError("journal entry is not a broker acknowledgement")
        recorded = AccountClerkRecordedReceipt.from_journal_entry(entry)
        return cls(
            **recorded.model_dump(exclude={"status"}),
            order_id=entry.order_id,
            perm_id=entry.perm_id,
            exec_id=entry.exec_id,
        )


class AccountClerkRecoveryFlattenReceipt(BaseModel):
    """Durable outcome of one Clerk-owned recovery liquidation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["recovery_flattened"] = "recovery_flattened"
    recorded: AccountClerkRecordedReceipt
    broker_acked: AccountClerkBrokerAckReceipt
    cancelled_order_ids: tuple[int, ...]


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
        clerk_generation: int | None = None,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._artifacts_root = artifacts_root
        self._account_id = account_id
        self._broker = broker
        self._clerk_generation = clerk_generation
        self._now_ms = now_ms if now_ms is not None else _now_ms
        self._intake_lock = asyncio.Lock()
        self._journal_entries: list[AccountClerkJournalEntry] | None = None

    async def record_intent(self, intent: AccountOwnerSubmitIntent) -> AccountClerkRecordedReceipt:
        """Validate, durably record, and acknowledge one intent without I/O to IBKR."""

        async with self._intake_lock:
            return await asyncio.to_thread(self._record_intent_locked, intent)

    async def submit_intent(
        self,
        intent: AccountOwnerSubmitIntent,
    ) -> tuple[AccountClerkRecordedReceipt, AccountClerkBrokerAckReceipt]:
        """Serially record then submit one paper intent through the clerk broker.

        The durable recorded receipt is always produced before the broker call.
        Repeating an acknowledged intent returns its existing receipt #2 rather
        than issuing a duplicate placement.
        """

        if self._broker is None:
            raise RuntimeError("ACCOUNT_CLERK_BROKER_UNAVAILABLE")
        if intent.intent_kind == "RECOVERY_FLATTEN":
            raise AccountClerkIntentRejected(
                reason="CLERK_RECOVERY_OPERATION_REQUIRED",
                diagnostics={"intent_id": intent.intent_id, "order_ref": intent.order_ref},
            )
        async with self._intake_lock:
            recorded = await asyncio.to_thread(self._record_intent_locked, intent)
            existing_ack = await asyncio.to_thread(self._ack_for_intent_locked, intent)
            if existing_ack is not None:
                return recorded, existing_ack
            self._require_paper_broker()
            spec = IbkrOrderSpec.model_validate(intent.order_spec)
            if self._clerk_generation is None:
                ack = await self._broker.place_order(spec)
            else:
                with account_clerk_write_grant(
                    account_id=self._account_id,
                    clerk_generation=self._clerk_generation,
                    boundary="account_clerk.broker.place_order",
                    clerk_generation_provider=lambda: self._clerk_generation,
                ):
                    ack = await self._broker.place_order(spec)
            broker_ack = await asyncio.to_thread(self._append_broker_ack_locked, intent, ack)
            return recorded, broker_ack

    async def submit_recovery_flatten(
        self,
        intent: AccountOwnerSubmitIntent,
        *,
        actor: Literal["bot", "operator"],
        actor_strategy_instance_id: str | None = None,
        actor_run_id: str | None = None,
        actor_bot_order_namespace: str | None = None,
    ) -> AccountClerkRecoveryFlattenReceipt:
        """Cancel this namespace's open orders then place one liquidation.

        The Clerk, not a fenced bot, owns both broker writes.  A bot may only
        name its own complete binding; the operator cure is deliberately
        restricted to a retired binding so it cannot become another submit
        lane for active bots.
        """

        if self._broker is None:
            raise RuntimeError("ACCOUNT_CLERK_BROKER_UNAVAILABLE")
        if intent.intent_kind != "RECOVERY_FLATTEN":
            self._reject(intent, "CLERK_RECOVERY_INTENT_KIND_REQUIRED")
        self._validate_recovery_actor(
            intent,
            actor=actor,
            actor_strategy_instance_id=actor_strategy_instance_id,
            actor_run_id=actor_run_id,
            actor_bot_order_namespace=actor_bot_order_namespace,
        )
        self._validate_recovery_order(intent)
        async with self._intake_lock:
            recorded = await asyncio.to_thread(self._record_intent_locked, intent)
            existing_ack = await asyncio.to_thread(self._ack_for_intent_locked, intent)
            if existing_ack is not None:
                cancelled = await asyncio.to_thread(self._recovery_cancelled_for_intent_locked, intent)
                return AccountClerkRecoveryFlattenReceipt(
                    recorded=recorded,
                    broker_acked=existing_ack,
                    cancelled_order_ids=cancelled,
                )
            self._require_paper_broker()
            cancelled = await self._cancel_namespace_open_orders(intent.bot_order_namespace)
            await asyncio.to_thread(self._append_recovery_cancelled_locked, intent, cancelled)
            spec = IbkrOrderSpec.model_validate(intent.order_spec)
            ack = await self._place_under_clerk_grant(spec)
            broker_ack = await asyncio.to_thread(self._append_broker_ack_locked, intent, ack)
            return AccountClerkRecoveryFlattenReceipt(
                recorded=recorded,
                broker_acked=broker_ack,
                cancelled_order_ids=tuple(cancelled),
            )

    def replay_recorded_receipts(self) -> list[AccountClerkRecordedReceipt]:
        """Return receipt #1 values from the journal after a clerk restart."""

        return [
            AccountClerkRecordedReceipt.from_journal_entry(entry)
            for entry in read_account_clerk_journal(self._artifacts_root, self._account_id)
            if entry.entry_kind == "recorded"
        ]

    async def recover_inbox(self) -> list[AccountClerkRecordedReceipt]:
        """Replay an inbox row left durable by a crash before journal fsync."""

        async with self._intake_lock:
            return await asyncio.to_thread(self._recover_inbox_locked)

    def _recover_inbox_locked(self) -> list[AccountClerkRecordedReceipt]:
        inbox_path = account_clerk_inbox_path(self._artifacts_root, self._account_id)
        journal_path = account_clerk_journal_path(self._artifacts_root, self._account_id)
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock(journal_path):
            journal_entries = self._load_journal_tail_locked(inbox_path, journal_path)
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
            journal_entries = self._load_journal_tail_locked(inbox_path, journal_path)

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
            self._journal_entries.append(journal_entry)
            _rewrite_jsonl(inbox_path, [])
            return AccountClerkRecordedReceipt.from_journal_entry(journal_entry)

    def _ack_for_intent_locked(
        self,
        intent: AccountOwnerSubmitIntent,
    ) -> AccountClerkBrokerAckReceipt | None:
        journal_path = account_clerk_journal_path(self._artifacts_root, self._account_id)
        with _file_lock(journal_path):
            entries = self._load_journal_tail_locked(
                account_clerk_inbox_path(self._artifacts_root, self._account_id),
                journal_path,
            )
            for entry in entries:
                if entry.entry_kind == "broker_acked" and entry.intent == intent:
                    return AccountClerkBrokerAckReceipt.from_journal_entry(entry)
        return None

    def _append_broker_ack_locked(
        self,
        intent: AccountOwnerSubmitIntent,
        ack: Any,
    ) -> AccountClerkBrokerAckReceipt:
        journal_path = account_clerk_journal_path(self._artifacts_root, self._account_id)
        with _file_lock(journal_path):
            entries = self._load_journal_tail_locked(
                account_clerk_inbox_path(self._artifacts_root, self._account_id),
                journal_path,
            )
            entry = AccountClerkJournalEntry(
                seq=entries[-1].seq + 1,
                entry_kind="broker_acked",
                recorded_at_ms=self._now_ms(),
                intent=intent,
                order_id=int(ack.order_id),
                perm_id=_try_int(getattr(ack, "perm_id", None)),
                exec_id=getattr(ack, "exec_id", None),
            )
            _append_jsonl(journal_path, entry)
            entries.append(entry)
            return AccountClerkBrokerAckReceipt.from_journal_entry(entry)

    def _append_recovery_cancelled_locked(
        self,
        intent: AccountOwnerSubmitIntent,
        cancelled_order_ids: list[int],
    ) -> None:
        journal_path = account_clerk_journal_path(self._artifacts_root, self._account_id)
        with _file_lock(journal_path):
            entries = self._load_journal_tail_locked(
                account_clerk_inbox_path(self._artifacts_root, self._account_id),
                journal_path,
            )
            if self._recovery_cancelled_for_intent_entries(entries, intent) is not None:
                return
            entry = AccountClerkJournalEntry(
                seq=entries[-1].seq + 1,
                entry_kind="recovery_cancelled",
                recorded_at_ms=self._now_ms(),
                intent=intent,
                cancelled_order_ids=tuple(cancelled_order_ids),
            )
            _append_jsonl(journal_path, entry)
            entries.append(entry)

    def _recovery_cancelled_for_intent_locked(self, intent: AccountOwnerSubmitIntent) -> tuple[int, ...]:
        journal_path = account_clerk_journal_path(self._artifacts_root, self._account_id)
        with _file_lock(journal_path):
            entries = self._load_journal_tail_locked(
                account_clerk_inbox_path(self._artifacts_root, self._account_id),
                journal_path,
            )
            entry = self._recovery_cancelled_for_intent_entries(entries, intent)
            return entry.cancelled_order_ids if entry is not None and entry.cancelled_order_ids is not None else ()

    @staticmethod
    def _recovery_cancelled_for_intent_entries(
        entries: list[AccountClerkJournalEntry],
        intent: AccountOwnerSubmitIntent,
    ) -> AccountClerkJournalEntry | None:
        return next(
            (
                entry
                for entry in entries
                if entry.entry_kind == "recovery_cancelled" and entry.intent == intent
            ),
            None,
        )

    def append_broker_event(self, intent: AccountOwnerSubmitIntent, event: IbkrOrderEvent) -> None:
        """Durably append a Clerk-observed broker callback before relay."""

        journal_path = account_clerk_journal_path(self._artifacts_root, self._account_id)
        with _file_lock(journal_path):
            entries = self._load_journal_tail_locked(
                account_clerk_inbox_path(self._artifacts_root, self._account_id),
                journal_path,
            )
            entry = AccountClerkJournalEntry(
                seq=entries[-1].seq + 1,
                entry_kind="broker_event",
                recorded_at_ms=self._now_ms(),
                intent=intent,
                broker_event=event.model_dump(mode="json"),
            )
            _append_jsonl(journal_path, entry)
            entries.append(entry)

    def _require_paper_broker(self) -> None:
        client = getattr(self._broker, "_client", None)
        settings = getattr(client, "settings", None)
        if getattr(settings, "mode", None) != "paper":
            raise RuntimeError("ACCOUNT_CLERK_PAPER_MODE_REQUIRED")

    def _load_journal_tail_locked(
        self,
        inbox_path: Path,
        journal_path: Path,
    ) -> list[AccountClerkJournalEntry]:
        """Recover once, then keep the serial journal tail in process memory."""

        if self._journal_entries is None:
            journal_entries = _read_jsonl(journal_path, AccountClerkJournalEntry)
            self._journal_entries = self._replay_inbox_locked(
                inbox_entries=_read_jsonl(inbox_path, AccountClerkInboxEntry),
                journal_entries=journal_entries,
                journal_path=journal_path,
            )
            _rewrite_jsonl(inbox_path, [])
        return self._journal_entries

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
        if intent.intent_kind == "RECOVERY_FLATTEN":
            self._validate_recovery_binding(intent, binding)
            return
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

    def _validate_recovery_binding(
        self,
        intent: AccountOwnerSubmitIntent,
        binding: AccountInstanceBinding,
    ) -> None:
        if binding.account_id != intent.account_id:
            self._reject(intent, "CLERK_ACCOUNT_MISMATCH")
        if binding.run_id != intent.run_id:
            self._reject(intent, "CLERK_STALE_RUN")
        if binding.bot_order_namespace != intent.bot_order_namespace:
            self._reject(intent, "CLERK_NAMESPACE_MISMATCH")
        if binding.lifecycle_state not in ("ACTIVE", "RETIRED"):
            self._reject(intent, "CLERK_INACTIVE_BINDING")

    def _validate_recovery_actor(
        self,
        intent: AccountOwnerSubmitIntent,
        *,
        actor: Literal["bot", "operator"],
        actor_strategy_instance_id: str | None,
        actor_run_id: str | None,
        actor_bot_order_namespace: str | None,
    ) -> None:
        bindings = read_account_instance_registry(self._artifacts_root, self._account_id)
        binding = latest_account_instance_binding(
            bindings,
            account_id=self._account_id,
            strategy_instance_id=intent.strategy_instance_id,
        )
        if binding is None:
            self._reject(intent, "CLERK_UNKNOWN_INSTANCE")
        assert binding is not None
        self._validate_recovery_binding(intent, binding)
        if actor == "operator":
            if binding.lifecycle_state != "RETIRED":
                self._reject(intent, "CLERK_OPERATOR_RECOVERY_REQUIRES_RETIRED_BINDING")
            return
        if (
            actor_strategy_instance_id != intent.strategy_instance_id
            or actor_run_id != intent.run_id
            or actor_bot_order_namespace != intent.bot_order_namespace
        ):
            self._reject(intent, "CLERK_RECOVERY_ACTOR_MISMATCH")

    def _validate_recovery_order(self, intent: AccountOwnerSubmitIntent) -> None:
        try:
            spec = IbkrOrderSpec.model_validate(intent.order_spec)
        except ValidationError as exc:
            self._reject(intent, f"CLERK_INVALID_RECOVERY_ORDER:{exc}")
        if spec.order_ref != intent.order_ref or spec.order_type != "MKT" or not spec.confirm_paper:
            self._reject(intent, "CLERK_INVALID_RECOVERY_ORDER")

    async def _cancel_namespace_open_orders(self, namespace: str) -> list[int]:
        cancel_namespace = getattr(self._broker, "cancel_open_orders_for_namespace", None)
        if not callable(cancel_namespace):
            return []
        return await self._run_broker_write(
            "account_clerk.broker.cancel_open_orders_for_namespace",
            lambda: cancel_namespace(namespace),
        )

    async def _place_under_clerk_grant(self, spec: IbkrOrderSpec) -> Any:
        return await self._run_broker_write(
            "account_clerk.broker.place_order",
            lambda: self._broker.place_order(spec),
        )

    async def _run_broker_write(self, boundary: str, write: Callable[[], Any]) -> Any:
        if self._clerk_generation is None:
            return await write()
        with account_clerk_write_grant(
            account_id=self._account_id,
            clerk_generation=self._clerk_generation,
            boundary=boundary,
            clerk_generation_provider=lambda: self._clerk_generation,
        ):
            return await write()

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


def account_clerk_socket_path(artifacts_root: Path, account_id: str) -> Path:
    """Short private Unix socket path (macOS caps AF_UNIX paths at 104 bytes)."""

    # The account artifact root can exceed the platform's AF_UNIX pathname
    # limit in temp-backed test and desktop workspaces.  The hash preserves a
    # stable one-account mapping without exposing the account id in /tmp.
    digest = hashlib.sha256(
        f"{account_artifacts_root(artifacts_root, account_id)}\0{account_id}".encode()
    ).hexdigest()[:32]
    return Path(tempfile.gettempdir()) / "learn-ai-clerk" / f"{digest}.sock"


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


def _rewrite_jsonl(path: Path, entries: list[AccountClerkInboxEntry]) -> None:
    """Atomically compact acknowledged inbox rows after journal durability."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8") as file_handle:
            for entry in entries:
                file_handle.write(entry.model_dump_json() + "\n")
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(temporary_path, path)
        _fsync_parent_dir(path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


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


def _try_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class AccountClerkLeaseWriter:
    """Renew one supervised clerk lease until the daemon reaps the process."""

    def __init__(
        self,
        *,
        artifacts_root: Path,
        account_id: str,
        generation: int,
        pid: int,
        now_ms: Callable[[], int] = _now_ms,
    ) -> None:
        self._artifacts_root = artifacts_root
        self._account_id = account_id
        self._generation = generation
        self._pid = pid
        self._now_ms = now_ms
        self._started_at_ms = now_ms()

    def renew(self, *, draining: bool = False) -> AccountClerkLease:
        now_ms = self._now_ms()
        lease = AccountClerkLease(
            account_id=self._account_id,
            generation=self._generation,
            pid=self._pid,
            status="DRAINING" if draining else "RUNNING",
            started_at_ms=self._started_at_ms,
            renewed_at_ms=now_ms,
            valid_until_ms=now_ms if draining else now_ms + _CLERK_LEASE_TTL_MS,
        )
        write_account_clerk_lease(self._artifacts_root, lease)
        return lease


async def _run_clerk_process(args: argparse.Namespace) -> int:
    stop = asyncio.Event()

    def _stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    from app.broker.ibkr.client import IbkrClient
    from app.engine.live.account_clerk_rpc import AccountClerkRpcServer
    from app.engine.live.live_portfolio import IbkrBrokerAdapter

    client = IbkrClient()
    if client.settings.mode != "paper":
        raise RuntimeError("ACCOUNT_CLERK_PAPER_MODE_REQUIRED")
    await client.connect()
    broker = IbkrBrokerAdapter(client)
    broker.require_account_owner_write_fence(lambda: args.generation)
    clerk = AccountClerk(
        artifacts_root=Path(args.artifacts_root),
        account_id=args.account_id,
        broker=broker,
        clerk_generation=args.generation,
    )
    server = AccountClerkRpcServer(clerk)
    writer = AccountClerkLeaseWriter(
        artifacts_root=Path(args.artifacts_root),
        account_id=args.account_id,
        generation=args.generation,
        pid=os.getpid(),
    )
    await server.start()
    try:
        while not stop.is_set():
            writer.renew()
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=1)
    finally:
        writer.renew(draining=True)
        await server.close()
        await client.disconnect()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one account clerk lease process.")
    parser.add_argument("--artifacts-root", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--generation", required=True, type=int)
    return asyncio.run(_run_clerk_process(parser.parse_args()))


def __getattr__(name: str):
    """Keep the #1020 RPC import surface while avoiding a transport cycle."""

    if name in {"AccountClerkRpcClient", "AccountClerkRpcServer"}:
        from app.engine.live import account_clerk_rpc

        return getattr(account_clerk_rpc, name)
    raise AttributeError(name)


__all__ = [
    "ACCOUNT_CLERK_INBOX_FILENAME",
    "ACCOUNT_CLERK_JOURNAL_FILENAME",
    "AccountClerk",
    "AccountClerkBrokerAckReceipt",
    "AccountClerkInboxEntry",
    "AccountClerkIntentRejected",
    "AccountClerkJournalCorruptError",
    "AccountClerkJournalEntry",
    "AccountClerkLeaseWriter",
    "AccountClerkRecordedReceipt",
    "AccountClerkRecoveryFlattenReceipt",
    "AccountClerkRpcClient",
    "AccountClerkRpcServer",
    "account_clerk_inbox_path",
    "account_clerk_journal_path",
    "account_clerk_socket_path",
    "read_account_clerk_inbox",
    "read_account_clerk_journal",
]


if __name__ == "__main__":
    raise SystemExit(main())
