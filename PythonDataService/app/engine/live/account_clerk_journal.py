"""Durable account-Clerk journal storage, recovery, and callback attribution.

The Clerk is the policy and broker-write coordinator.  This module owns its
strict JSONL state machine: durable intake, crash replay, serial receipt
appends, and the process-local order-reference attribution index rebuilt from
the durable recorded-intent rows.
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, overload

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.broker.ibkr.models import IbkrOrderEvent
from app.engine.live.account_artifacts import account_artifacts_root
from app.engine.live.account_owner import AccountOwnerSubmitIntent
from app.engine.live.broker_callbacks import broker_callback_idempotency_key
from app.engine.live.live_state_sidecar import _file_lock, _fsync_parent_dir

ACCOUNT_CLERK_INBOX_FILENAME = "clerk_inbox.jsonl"
ACCOUNT_CLERK_JOURNAL_FILENAME = "clerk_journal.jsonl"
_MAX_INT64 = 9_223_372_036_854_775_807


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class AccountClerkJournalCorruptError(RuntimeError):
    """Raised when an account Clerk inbox or journal cannot be safely replayed."""

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

    schema_version: Literal[1] = 1
    seq: int = Field(ge=1)
    received_at_ms: int = Field(ge=0, le=_MAX_INT64)
    intent: AccountOwnerSubmitIntent


class AccountClerkOperatorAdjustment(BaseModel):
    """Immutable local correction of a stale Clerk-attributed claim."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    account_id: str = Field(min_length=1, max_length=64)
    bot_order_namespace: str = Field(min_length=1, max_length=256)
    symbol: str = Field(min_length=1, max_length=32)
    signed_quantity: float
    operator_attribution: Literal["local-operator"] = "local-operator"
    request_provenance: str = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=512)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=160)
    recorded_at_ms: int = Field(ge=0, le=_MAX_INT64)

    @model_validator(mode="after")
    def validate_signed_quantity(self) -> AccountClerkOperatorAdjustment:
        """Reject a no-op cure before it becomes durable history."""

        if self.signed_quantity == 0 or not math.isfinite(self.signed_quantity):
            raise ValueError("signed_quantity must be finite and non-zero")
        return self


class AccountClerkOperatorAdjustmentConflict(ValueError):
    """A durable operator-adjustment idempotency key was reused differently."""


class AccountClerkJournalEntry(BaseModel):
    """One serial, durable receipt-#1 ledger entry for an account intent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    seq: int = Field(ge=1)
    entry_kind: Literal[
        "recorded",
        "broker_submitting",
        "broker_uncertain",
        "recovery_cancelling",
        "recovery_cancelled",
        "cancel_submitting",
        "cancel_confirmed",
        "cancel_uncertain",
        "broker_acked",
        "broker_event",
        "reconciliation",
        "operator_adjustment",
    ] = "recorded"
    recorded_at_ms: int = Field(ge=0, le=_MAX_INT64)
    # All intent lifecycle entries are attributed. A broker callback without a
    # durable Clerk intent remains an account fact, never a guessed namespace.
    intent: AccountOwnerSubmitIntent | None = None
    order_id: int | None = Field(default=None, ge=0)
    perm_id: int | None = Field(default=None, ge=0)
    exec_id: str | None = None
    broker_event: dict[str, object] | None = None
    cancelled_order_ids: tuple[int, ...] | None = None
    reconciliation_verdict: Literal["RECOVER_ADOPT", "RETRY_ONCE", "HALT"] | None = None
    reconciliation_reason: str | None = None
    broker_error: str | None = None
    event_account_id: str | None = Field(default=None, min_length=1)
    broker_callback_idempotency_key: str | None = Field(default=None, min_length=1)
    operator_adjustment: AccountClerkOperatorAdjustment | None = None

    @model_validator(mode="after")
    def validate_attribution_shape(self) -> AccountClerkJournalEntry:
        """Keep attributed rows readable while forbidding guessed ownership."""

        if self.entry_kind == "operator_adjustment":
            if self.intent is not None or self.operator_adjustment is None:
                raise ValueError("operator_adjustment rows require only operator_adjustment")
            if self.event_account_id is not None or self.broker_callback_idempotency_key is not None:
                raise ValueError("callback metadata is invalid on operator_adjustment rows")
            return self

        if self.entry_kind != "broker_event":
            if self.intent is None:
                raise ValueError("non-broker-event journal rows require an intent")
            if (
                self.event_account_id is not None
                or self.broker_callback_idempotency_key is not None
                or self.operator_adjustment is not None
            ):
                raise ValueError("callback metadata is only valid on broker_event rows")
            return self

        if self.broker_event is None:
            raise ValueError("broker_event journal rows require broker_event")
        if (
            self.intent is not None
            and self.event_account_id is not None
            and self.event_account_id != self.intent.account_id
        ):
            raise ValueError("event_account_id must match the attributed intent account")
        if self.intent is None and self.event_account_id is None:
            raise ValueError("unattributed broker_event rows require event_account_id")
        if self.operator_adjustment is not None:
            raise ValueError("operator adjustment is invalid on broker_event rows")
        return self


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
        intent = _require_entry_intent(entry)
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
    """Receipt #2, appended by the Clerk only after the paper broker acks."""

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


class AccountClerkCancelNamespaceReceipt(BaseModel):
    """Durable terminal-cancel receipt for one bot namespace."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["cancel_confirmed"] = "cancel_confirmed"
    recorded: AccountClerkRecordedReceipt
    cancelled_order_ids: tuple[int, ...]


@dataclass(frozen=True)
class AccountClerkBrokerEventReceipt:
    """Durable callback result used to gate relay after persistence."""

    journal_seq: int
    event: IbkrOrderEvent
    intent: AccountOwnerSubmitIntent | None
    newly_recorded: bool


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
            if self._recovery_operation_started_for_namespace_entries(entries, intent):
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
            return self._recovery_operation_started_for_namespace_entries(entries, intent)

    @staticmethod
    def _recovery_operation_started_for_namespace_entries(
        entries: list[AccountClerkJournalEntry],
        intent: AccountOwnerSubmitIntent,
    ) -> bool:
        terminal_intent_ids = {
            entry.intent.intent_id
            for entry in entries
            if entry.intent is not None
            and entry.entry_kind == "broker_acked"
        }
        return any(
            entry.intent is not None
            and entry.intent.bot_order_namespace == intent.bot_order_namespace
            and entry.intent.intent_id not in terminal_intent_ids
            and entry.entry_kind
            in {"recovery_cancelling", "recovery_cancelled", "broker_submitting", "broker_uncertain"}
            for entry in entries
        )

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
                return AccountClerkBrokerEventReceipt(
                    journal_seq=existing.seq,
                    event=event,
                    intent=existing.intent,
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

    for entry in entries:
        if entry.entry_kind != "broker_event" or entry.intent is not None or entry.broker_event is None:
            continue
        event = IbkrOrderEvent.model_validate(entry.broker_event)
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


def _require_entry_intent(entry: AccountClerkJournalEntry) -> AccountOwnerSubmitIntent:
    if entry.intent is None:
        raise ValueError("receipt entry unexpectedly lacks an intent")
    return entry.intent


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
