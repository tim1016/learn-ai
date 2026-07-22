"""Durable account-Clerk journal storage, recovery, and callback attribution.

The Clerk is the policy and broker-write coordinator.  This module owns its
strict JSONL state machine: durable intake, crash replay, serial receipt
appends, and the process-local order-reference attribution index rebuilt from
the durable recorded-intent rows.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any, Literal, overload

from pydantic import ValidationError

from app.broker.ibkr.models import IbkrOrderEvent
from app.engine.live import durable_append_log
from app.engine.live.account_artifacts import account_artifacts_root
from app.engine.live.account_clerk_journal_models import (
    AccountClerkBrokerAckReceipt,
    AccountClerkBrokerEventReceipt,
    AccountClerkCancelNamespaceReceipt,
    AccountClerkInboxEntry,
    AccountClerkIntentRejected,
    AccountClerkJournalCorruptError,
    AccountClerkJournalEntry,
    AccountClerkOperatorAdjustment,
    AccountClerkOperatorAdjustmentConflict,
    AccountClerkRecordedReceipt,
    AccountClerkRecoveryFlattenReceipt,
    _require_entry_intent,
)
from app.engine.live.account_owner import AccountOwnerSubmitIntent
from app.engine.live.broker_callbacks import broker_callback_idempotency_key
from app.engine.live.live_state_sidecar import _file_lock

ACCOUNT_CLERK_INBOX_FILENAME = "clerk_inbox.jsonl"
ACCOUNT_CLERK_JOURNAL_FILENAME = "clerk_journal.jsonl"


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class AccountClerkJournal:
    """One account's serial JSONL journal and durable attribution index.

    Callers provide policy validation before a previously unseen intent is
    appended. The class deliberately does not know broker or lifecycle policy.
    Its methods run in the Clerk's serialized worker-thread section.
    """

    def __init__(
        self,
        *,
        artifacts_root: Path,
        account_id: str,
        now_ms: Callable[[], int] = _now_ms,
    ) -> None:
        self._artifacts_root = artifacts_root
        self._account_id = account_id
        self._now_ms = now_ms
        self._entries: list[AccountClerkJournalEntry] | None = None
        self._intents_by_order_ref: dict[str, AccountOwnerSubmitIntent] = {}

    @property
    def intents_by_order_ref(self) -> dict[str, AccountOwnerSubmitIntent]:
        """Expose the live attribution index to legacy focused tests only."""

        return self._intents_by_order_ref

    def recover_inbox(self) -> list[AccountClerkRecordedReceipt]:
        inbox_path, journal_path = self._paths()
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock(journal_path):
            entries = self._load_tail_locked(inbox_path, journal_path)
        return [
            AccountClerkRecordedReceipt.from_journal_entry(entry)
            for entry in entries
            if entry.entry_kind == "recorded"
        ]

    def record_intent(
        self,
        intent: AccountOwnerSubmitIntent,
        *,
        validate_intent: Callable[[AccountOwnerSubmitIntent], None],
    ) -> AccountClerkRecordedReceipt:
        inbox_path, journal_path = self._paths()
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock(journal_path):
            entries = self._load_tail_locked(inbox_path, journal_path)
            existing = _journal_entry_for_intent(entries, intent)
            if existing is not None:
                return AccountClerkRecordedReceipt.from_journal_entry(existing)

            validate_intent(intent)
            _require_unique_order_ref(entries, intent)
            next_seq = entries[-1].seq + 1 if entries else 1
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
            entries.append(journal_entry)
            self.register_attribution(intent)
            _rewrite_jsonl(inbox_path, [])
            return AccountClerkRecordedReceipt.from_journal_entry(journal_entry)

    def ack_for_intent(self, intent: AccountOwnerSubmitIntent) -> AccountClerkBrokerAckReceipt | None:
        inbox_path, journal_path = self._paths()
        with _file_lock(journal_path):
            entries = self._load_tail_locked(inbox_path, journal_path)
            for entry in entries:
                if entry.entry_kind == "broker_acked" and entry.intent == intent:
                    return AccountClerkBrokerAckReceipt.from_journal_entry(entry)
        return None

    def append_broker_ack(self, intent: AccountOwnerSubmitIntent, ack: Any) -> AccountClerkBrokerAckReceipt:
        inbox_path, journal_path = self._paths()
        with _file_lock(journal_path):
            entries = self._load_tail_locked(inbox_path, journal_path)
            entry = AccountClerkJournalEntry(
                seq=_next_seq(entries),
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

    def append_broker_submitting(self, intent: AccountOwnerSubmitIntent) -> None:
        self._append_broker_transition(intent, entry_kind="broker_submitting")

    def append_broker_uncertain(self, intent: AccountOwnerSubmitIntent, error: Exception) -> None:
        self._append_broker_transition(
            intent,
            entry_kind="broker_uncertain",
            broker_error=f"{type(error).__name__}: {error}",
        )

    def _append_broker_transition(
        self,
        intent: AccountOwnerSubmitIntent,
        *,
        entry_kind: Literal["broker_submitting", "broker_uncertain"],
        broker_error: str | None = None,
    ) -> None:
        inbox_path, journal_path = self._paths()
        with _file_lock(journal_path):
            entries = self._load_tail_locked(inbox_path, journal_path)
            entry = AccountClerkJournalEntry(
                seq=_next_seq(entries),
                entry_kind=entry_kind,
                recorded_at_ms=self._now_ms(),
                intent=intent,
                broker_error=broker_error,
            )
            _append_jsonl(journal_path, entry)
            entries.append(entry)

    def append_recovery_cancelled(
        self,
        intent: AccountOwnerSubmitIntent,
        cancelled_order_ids: list[int],
    ) -> None:
        inbox_path, journal_path = self._paths()
        with _file_lock(journal_path):
            entries = self._load_tail_locked(inbox_path, journal_path)
            if self._recovery_cancelled_for_intent_entries(entries, intent) is not None:
                return
            entry = AccountClerkJournalEntry(
                seq=_next_seq(entries),
                entry_kind="recovery_cancelled",
                recorded_at_ms=self._now_ms(),
                intent=intent,
                cancelled_order_ids=tuple(cancelled_order_ids),
            )
            _append_jsonl(journal_path, entry)
            entries.append(entry)

    def append_recovery_cancelling(self, intent: AccountOwnerSubmitIntent) -> None:
        """Record the recovery cancel crash boundary before contacting IBKR."""

        inbox_path, journal_path = self._paths()
        with _file_lock(journal_path):
            entries = self._load_tail_locked(inbox_path, journal_path)
            if recovery_operation_started_for_namespace(entries, intent.bot_order_namespace):
                return
            entry = AccountClerkJournalEntry(
                seq=_next_seq(entries),
                entry_kind="recovery_cancelling",
                recorded_at_ms=self._now_ms(),
                intent=intent,
            )
            _append_jsonl(journal_path, entry)
            entries.append(entry)

    def recovery_cancelled_for_intent(self, intent: AccountOwnerSubmitIntent) -> tuple[int, ...]:
        inbox_path, journal_path = self._paths()
        with _file_lock(journal_path):
            entries = self._load_tail_locked(inbox_path, journal_path)
            entry = self._recovery_cancelled_for_intent_entries(entries, intent)
            return entry.cancelled_order_ids if entry is not None and entry.cancelled_order_ids is not None else ()

    def recovery_operation_started_for_namespace(self, intent: AccountOwnerSubmitIntent) -> bool:
        """Whether this namespace has an incomplete recovery broker boundary."""

        inbox_path, journal_path = self._paths()
        with _file_lock(journal_path):
            entries = self._load_tail_locked(inbox_path, journal_path)
            return recovery_operation_started_for_namespace(entries, intent.bot_order_namespace)

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

    def append_cancel_confirmed(
        self,
        intent: AccountOwnerSubmitIntent,
        cancelled_order_ids: list[int],
    ) -> AccountClerkCancelNamespaceReceipt:
        """Persist terminal broker confirmation after a namespace cancellation."""

        inbox_path, journal_path = self._paths()
        with _file_lock(journal_path):
            entries = self._load_tail_locked(inbox_path, journal_path)
            existing = self._cancel_confirmed_for_intent_entries(entries, intent)
            if existing is not None:
                return self._cancel_namespace_receipt(entries, intent, existing)
            entry = AccountClerkJournalEntry(
                seq=_next_seq(entries),
                entry_kind="cancel_confirmed",
                recorded_at_ms=self._now_ms(),
                intent=intent,
                cancelled_order_ids=tuple(cancelled_order_ids),
            )
            _append_jsonl(journal_path, entry)
            entries.append(entry)
            return self._cancel_namespace_receipt(entries, intent, entry)

    def append_cancel_submitting(self, intent: AccountOwnerSubmitIntent) -> None:
        """Record the crash boundary immediately before a broker cancel."""

        inbox_path, journal_path = self._paths()
        with _file_lock(journal_path):
            entries = self._load_tail_locked(inbox_path, journal_path)
            if self._cancel_submitting_for_intent_entries(entries, intent) is not None:
                return
            entry = AccountClerkJournalEntry(
                seq=_next_seq(entries),
                entry_kind="cancel_submitting",
                recorded_at_ms=self._now_ms(),
                intent=intent,
            )
            _append_jsonl(journal_path, entry)
            entries.append(entry)

    def cancel_confirmed_for_intent(
        self,
        intent: AccountOwnerSubmitIntent,
    ) -> AccountClerkCancelNamespaceReceipt | None:
        inbox_path, journal_path = self._paths()
        with _file_lock(journal_path):
            entries = self._load_tail_locked(inbox_path, journal_path)
            existing = self._cancel_confirmed_for_intent_entries(entries, intent)
            if existing is None:
                return None
            return self._cancel_namespace_receipt(entries, intent, existing)

    def append_cancel_uncertain(self, intent: AccountOwnerSubmitIntent, error: Exception) -> None:
        """Record an ambiguous cancellation before surfacing it to the caller."""

        inbox_path, journal_path = self._paths()
        with _file_lock(journal_path):
            entries = self._load_tail_locked(inbox_path, journal_path)
            entry = AccountClerkJournalEntry(
                seq=_next_seq(entries),
                entry_kind="cancel_uncertain",
                recorded_at_ms=self._now_ms(),
                intent=intent,
                broker_error=f"{type(error).__name__}: {error}",
            )
            _append_jsonl(journal_path, entry)
            entries.append(entry)

    def cancel_submitting_for_intent(self, intent: AccountOwnerSubmitIntent) -> bool:
        """Whether a prior process crossed the durable cancel-write boundary."""

        inbox_path, journal_path = self._paths()
        with _file_lock(journal_path):
            entries = self._load_tail_locked(inbox_path, journal_path)
            return self._cancel_submitting_for_intent_entries(entries, intent) is not None

    def has_unresolved_namespace_cancellation(self, bot_order_namespace: str) -> bool:
        """Return whether a prior cancel or recovery broker write fences this namespace.

        A terminal confirmation or a reconciliation adoption is the only
        durable clearing path.  In particular, a crash after
        ``cancel_submitting`` must not let a later strategy submit bypass an
        unknown broker-side cancellation.
        """

        inbox_path, journal_path = self._paths()
        with _file_lock(journal_path):
            entries = self._load_tail_locked(inbox_path, journal_path)
            terminal_intent_ids = {
                entry.intent.intent_id
                for entry in entries
                if entry.intent is not None
                and (
                    (
                        entry.intent.intent_kind == "CANCEL_NAMESPACE"
                        and entry.entry_kind == "cancel_confirmed"
                    )
                    or (
                        entry.intent.intent_kind == "RECOVERY_FLATTEN"
                        and entry.entry_kind == "broker_acked"
                    )
                    or (
                        entry.entry_kind == "reconciliation"
                        and entry.reconciliation_verdict == "RECOVER_ADOPT"
                    )
                )
            }
            return any(
                entry.intent is not None
                and entry.intent.intent_kind in {"CANCEL_NAMESPACE", "RECOVERY_FLATTEN"}
                and entry.intent.bot_order_namespace == bot_order_namespace
                and entry.intent.intent_id not in terminal_intent_ids
                and entry.entry_kind
                in {
                    "cancel_submitting",
                    "cancel_uncertain",
                    "broker_submitting",
                    "recovery_cancelling",
                    "broker_uncertain",
                }
                for entry in entries
            )

    @staticmethod
    def _cancel_submitting_for_intent_entries(
        entries: list[AccountClerkJournalEntry],
        intent: AccountOwnerSubmitIntent,
    ) -> AccountClerkJournalEntry | None:
        return next(
            (
                entry
                for entry in entries
                if entry.entry_kind == "cancel_submitting" and entry.intent == intent
            ),
            None,
        )

    @staticmethod
    def _cancel_confirmed_for_intent_entries(
        entries: list[AccountClerkJournalEntry],
        intent: AccountOwnerSubmitIntent,
    ) -> AccountClerkJournalEntry | None:
        return next(
            (
                entry
                for entry in entries
                if entry.entry_kind == "cancel_confirmed" and entry.intent == intent
            ),
            None,
        )

    @staticmethod
    def _cancel_namespace_receipt(
        entries: list[AccountClerkJournalEntry],
        intent: AccountOwnerSubmitIntent,
        confirmation: AccountClerkJournalEntry,
    ) -> AccountClerkCancelNamespaceReceipt:
        return AccountClerkCancelNamespaceReceipt(
            recorded=AccountClerkRecordedReceipt.from_journal_entry(
                _require_recorded_intent_entry(entries, intent)
            ),
            cancelled_order_ids=confirmation.cancelled_order_ids or (),
        )

    def record_broker_event(self, event: IbkrOrderEvent) -> AccountClerkBrokerEventReceipt:
        """Append one deduplicated callback after durable attribution lookup."""

        inbox_path, journal_path = self._paths()
        callback_key = broker_callback_idempotency_key(event)
        with _file_lock(journal_path):
            entries = self._load_tail_locked(inbox_path, journal_path)
            existing = _broker_callback_entry_for_key(entries, callback_key)
            if existing is not None:
                intent = existing.intent or self._intents_by_order_ref.get(event.order_ref or "")
                return AccountClerkBrokerEventReceipt(
                    journal_seq=existing.seq,
                    event=event,
                    intent=intent,
                    newly_recorded=False,
                )

            intent = self._intents_by_order_ref.get(event.order_ref or "")
            entry = AccountClerkJournalEntry(
                seq=_next_seq(entries),
                entry_kind="broker_event",
                recorded_at_ms=self._now_ms(),
                intent=intent,
                broker_event=event.model_dump(mode="json"),
                event_account_id=event.account_id,
                broker_callback_idempotency_key=callback_key,
            )
            _append_jsonl(journal_path, entry)
            entries.append(entry)
            return AccountClerkBrokerEventReceipt(
                journal_seq=entry.seq,
                event=event,
                intent=intent,
                newly_recorded=True,
            )

    def append_reconciliation_resolution(
        self,
        intent: AccountOwnerSubmitIntent,
        *,
        verdict: Literal["RECOVER_ADOPT", "RETRY_ONCE", "HALT"],
        reason: str,
    ) -> None:
        inbox_path, journal_path = self._paths()
        with _file_lock(journal_path):
            entries = self._load_tail_locked(inbox_path, journal_path)
            self.register_attribution(intent)
            entry = AccountClerkJournalEntry(
                seq=_next_seq(entries),
                entry_kind="reconciliation",
                recorded_at_ms=self._now_ms(),
                intent=intent,
                reconciliation_verdict=verdict,
                reconciliation_reason=reason,
            )
            _append_jsonl(journal_path, entry)
            entries.append(entry)

    def append_operator_adjustment(
        self,
        adjustment: AccountClerkOperatorAdjustment,
        *,
        validate_adjustment: Callable[[list[AccountClerkJournalEntry]], None],
    ) -> AccountClerkJournalEntry:
        """Atomically validate and append one idempotent compensating journal entry."""

        if adjustment.account_id != self._account_id:
            raise ValueError("operator adjustment account_id does not match journal account")
        inbox_path, journal_path = self._paths()
        with _file_lock(journal_path):
            entries = self._load_tail_locked(inbox_path, journal_path)
            existing = next(
                (
                    entry
                    for entry in entries
                    if entry.entry_kind == "operator_adjustment"
                    and entry.operator_adjustment is not None
                    and entry.operator_adjustment.idempotency_key == adjustment.idempotency_key
                ),
                None,
            )
            if existing is not None:
                if not _same_operator_adjustment_request(existing.operator_adjustment, adjustment):
                    raise AccountClerkOperatorAdjustmentConflict(
                        "operator adjustment idempotency key conflicts with prior payload"
                    )
                return existing
            validate_adjustment(entries)
            entry = AccountClerkJournalEntry(
                seq=_next_seq(entries),
                entry_kind="operator_adjustment",
                recorded_at_ms=adjustment.recorded_at_ms,
                operator_adjustment=adjustment,
            )
            _append_jsonl(journal_path, entry)
            entries.append(entry)
            return entry

    def snapshot(self) -> list[AccountClerkJournalEntry]:
        """Return the recovered in-memory journal tail for reconciliation."""

        inbox_path, journal_path = self._paths()
        with _file_lock(journal_path):
            return list(self._load_tail_locked(inbox_path, journal_path))

    def rebuild_attribution(self) -> list[tuple[IbkrOrderEvent, str]]:
        """Rebuild durable callback attribution and return unowned callbacks."""

        inbox_path, journal_path = self._paths()
        with _file_lock(journal_path):
            entries = self._load_tail_locked(inbox_path, journal_path)
            self._rebuild_attribution_from_entries(entries)
        return list(_unattributed_broker_events(entries))

    def register_attribution(self, intent: AccountOwnerSubmitIntent) -> None:
        existing = self._intents_by_order_ref.get(intent.order_ref)
        if existing is not None and existing != intent:
            raise AccountClerkIntentRejected(
                reason="CLERK_ORDER_REF_COLLISION",
                diagnostics={
                    "existing_intent": existing.model_dump(mode="json"),
                    "received_intent": intent.model_dump(mode="json"),
                },
            )
        self._intents_by_order_ref[intent.order_ref] = intent

    def _paths(self) -> tuple[Path, Path]:
        return (
            account_clerk_inbox_path(self._artifacts_root, self._account_id),
            account_clerk_journal_path(self._artifacts_root, self._account_id),
        )

    def _load_tail_locked(
        self,
        inbox_path: Path,
        journal_path: Path,
    ) -> list[AccountClerkJournalEntry]:
        """Recover once, then keep the serial journal tail in process memory."""

        if self._entries is None:
            journal_entries = _read_journal_jsonl(journal_path)
            self._entries = self._replay_inbox_locked(
                inbox_entries=_read_jsonl(inbox_path, AccountClerkInboxEntry),
                journal_entries=journal_entries,
                journal_path=journal_path,
            )
            _rewrite_jsonl(inbox_path, [])
            self._rebuild_attribution_from_entries(self._entries)
        return self._entries

    def _rebuild_attribution_from_entries(self, entries: list[AccountClerkJournalEntry]) -> None:
        self._intents_by_order_ref.clear()
        for entry in entries:
            if entry.entry_kind == "recorded" and entry.intent is not None:
                self.register_attribution(entry.intent)

    def _replay_inbox_locked(
        self,
        *,
        inbox_entries: list[AccountClerkInboxEntry],
        journal_entries: list[AccountClerkJournalEntry],
        journal_path: Path,
    ) -> list[AccountClerkJournalEntry]:
        journal_by_seq = {entry.seq: entry for entry in journal_entries}
        unique_inbox_entries: list[AccountClerkInboxEntry] = []
        inbox_by_seq: dict[int, AccountClerkInboxEntry] = {}
        for inbox_entry in inbox_entries:
            existing_inbox_entry = inbox_by_seq.get(inbox_entry.seq)
            if existing_inbox_entry is None:
                inbox_by_seq[inbox_entry.seq] = inbox_entry
                unique_inbox_entries.append(inbox_entry)
                continue
            if existing_inbox_entry != inbox_entry:
                raise AccountClerkJournalCorruptError(
                    journal_path,
                    f"duplicate incompatible inbox rows at seq {inbox_entry.seq}",
                )

        for inbox_entry in unique_inbox_entries:
            journal_entry = journal_by_seq.get(inbox_entry.seq)
            if journal_entry is not None:
                if journal_entry.intent != inbox_entry.intent:
                    raise AccountClerkJournalCorruptError(
                        journal_path,
                        f"inbox and journal intent differ at seq {inbox_entry.seq}",
                    )
                continue
            expected_seq = _next_seq(journal_entries)
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
        return _read_journal_jsonl(path)


def inspect_account_clerk_journal(
    artifacts_root: Path,
    account_id: str,
) -> list[AccountClerkJournalEntry]:
    """Strictly inspect an existing journal without creating an account directory.

    Desk projections remain observational for a previously unseen account.
    Once a journal exists, coordinate with its writer lock so a projection
    never parses a partially appended JSONL row.
    """

    path = account_clerk_journal_path(artifacts_root, account_id)
    if not path.exists():
        return []
    # The sibling lock already exists for an established journal; taking it
    # keeps this observational projection from parsing a partially appended
    # JSONL row while still avoiding directory creation for unseen accounts.
    with _file_lock(path):
        return _read_journal_jsonl(path)


def recovery_operation_started_for_namespace(
    entries: list[AccountClerkJournalEntry],
    namespace: str,
) -> bool:
    """Whether a namespace has crossed an unresolved recovery broker boundary."""

    terminal_intent_ids = {
        entry.intent.intent_id
        for entry in entries
        if entry.intent is not None and entry.entry_kind == "broker_acked"
    }
    return any(
        entry.intent is not None
        and entry.intent.bot_order_namespace == namespace
        and entry.intent.intent_id not in terminal_intent_ids
        and entry.entry_kind
        in {"recovery_cancelling", "recovery_cancelled", "broker_submitting", "broker_uncertain"}
        for entry in entries
    )


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
    for line_no, line in enumerate(lines, start=1):
        if not line:
            raise AccountClerkJournalCorruptError(path, f"blank row at line {line_no}")
        try:
            entry = model_type.model_validate_json(line)
        except (ValidationError, ValueError) as exc:
            raise AccountClerkJournalCorruptError(path, f"invalid row at line {line_no}: {exc}") from exc
        entries.append(entry)
    return entries


def _read_journal_jsonl(path: Path) -> list[AccountClerkJournalEntry]:
    entries = _read_jsonl(path, AccountClerkJournalEntry)
    expected_seq = 1
    for line_no, entry in enumerate(entries, start=1):
        if entry.seq != expected_seq:
            raise AccountClerkJournalCorruptError(
                path,
                f"expected seq {expected_seq} at line {line_no}, found {entry.seq}",
            )
        expected_seq += 1
    return entries


def _append_jsonl(path: Path, entry: AccountClerkInboxEntry | AccountClerkJournalEntry) -> None:
    durable_append_log.append_jsonl_record(path, entry.model_dump_json())


def _rewrite_jsonl(path: Path, entries: list[AccountClerkInboxEntry]) -> None:
    """Atomically compact acknowledged inbox rows after journal durability."""

    durable_append_log.rewrite_jsonl_records(path, (entry.model_dump_json() for entry in entries))


def _journal_entry_for_intent(
    journal_entries: list[AccountClerkJournalEntry],
    intent: AccountOwnerSubmitIntent,
) -> AccountClerkJournalEntry | None:
    matching = [
        entry
        for entry in journal_entries
        if entry.intent is not None and entry.intent.intent_id == intent.intent_id
    ]
    if not matching:
        return None
    existing = matching[0]
    existing_intent = _require_entry_intent(existing)
    if existing_intent != intent:
        raise AccountClerkIntentRejected(
            reason="CLERK_INTENT_ID_COLLISION",
            diagnostics={
                "existing_intent": existing_intent.model_dump(mode="json"),
                "received_intent": intent.model_dump(mode="json"),
            },
        )
    return existing


def _require_unique_order_ref(
    journal_entries: list[AccountClerkJournalEntry],
    intent: AccountOwnerSubmitIntent,
) -> None:
    """Reject an attribution collision before it can enter durable state."""

    existing = next(
        (
            entry.intent
            for entry in journal_entries
            if entry.entry_kind == "recorded"
            and entry.intent is not None
            and entry.intent.order_ref == intent.order_ref
        ),
        None,
    )
    if existing is None or existing == intent:
        return
    raise AccountClerkIntentRejected(
        reason="CLERK_ORDER_REF_COLLISION",
        diagnostics={
            "existing_intent": existing.model_dump(mode="json"),
            "received_intent": intent.model_dump(mode="json"),
        },
    )


def _require_recorded_intent_entry(
    entries: list[AccountClerkJournalEntry],
    intent: AccountOwnerSubmitIntent,
) -> AccountClerkJournalEntry:
    entry = next(
        (
            candidate
            for candidate in entries
            if candidate.entry_kind == "recorded" and candidate.intent == intent
        ),
        None,
    )
    if entry is None:
        raise RuntimeError("cancel confirmation has no durable recorded receipt")
    return entry


def _broker_callback_entry_for_key(
    entries: list[AccountClerkJournalEntry],
    callback_key: str,
) -> AccountClerkJournalEntry | None:
    """Find a callback row by the ADR 0014 idempotency identity."""

    for entry in entries:
        if entry.entry_kind != "broker_event" or entry.broker_event is None:
            continue
        if entry.broker_callback_idempotency_key == callback_key:
            return entry
        try:
            existing_event = IbkrOrderEvent.model_validate(entry.broker_event)
        except (TypeError, ValidationError, ValueError):
            continue
        if broker_callback_idempotency_key(existing_event) == callback_key:
            return entry
    return None


def normalize_broker_event(
    event: IbkrOrderEvent | Mapping[str, object],
) -> IbkrOrderEvent | None:
    """Validate the one broker-event model consumed by journal and drain paths."""

    try:
        return IbkrOrderEvent.model_validate(event)
    except (TypeError, ValidationError, ValueError):
        return None


def _unattributed_broker_events(
    entries: list[AccountClerkJournalEntry],
) -> Iterator[tuple[IbkrOrderEvent, str]]:
    """Yield durable unknown callbacks whose account safety guardrail is required."""

    recorded_intents_by_order_ref = {
        entry.intent.order_ref: entry.intent
        for entry in entries
        if entry.entry_kind == "recorded" and entry.intent is not None
    }

    for entry in entries:
        if entry.entry_kind != "broker_event" or entry.intent is not None or entry.broker_event is None:
            continue
        event = IbkrOrderEvent.model_validate(entry.broker_event)
        if event.order_ref and event.order_ref in recorded_intents_by_order_ref:
            continue
        yield event, entry.broker_callback_idempotency_key or broker_callback_idempotency_key(event)


def _next_seq(entries: list[AccountClerkJournalEntry]) -> int:
    return entries[-1].seq + 1 if entries else 1


def _same_operator_adjustment_request(
    existing: AccountClerkOperatorAdjustment,
    received: AccountClerkOperatorAdjustment,
) -> bool:
    """Compare stable request identity while allowing a replay's new clock value."""

    return existing.model_dump(exclude={"recorded_at_ms"}) == received.model_dump(
        exclude={"recorded_at_ms"}
    )


def _try_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "ACCOUNT_CLERK_INBOX_FILENAME",
    "ACCOUNT_CLERK_JOURNAL_FILENAME",
    "AccountClerkBrokerAckReceipt",
    "AccountClerkBrokerEventReceipt",
    "AccountClerkInboxEntry",
    "AccountClerkIntentRejected",
    "AccountClerkJournal",
    "AccountClerkJournalCorruptError",
    "AccountClerkJournalEntry",
    "AccountClerkOperatorAdjustment",
    "AccountClerkOperatorAdjustmentConflict",
    "AccountClerkRecordedReceipt",
    "AccountClerkRecoveryFlattenReceipt",
    "account_clerk_inbox_path",
    "account_clerk_journal_path",
    "normalize_broker_event",
    "read_account_clerk_inbox",
    "read_account_clerk_journal",
]
