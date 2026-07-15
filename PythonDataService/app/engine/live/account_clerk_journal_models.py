"""Validated durable contracts for the account-Clerk journal.

The journal's storage and replay coordinator lives in
``account_clerk_journal``.  Keeping the on-disk row schemas and the receipts
returned to its callers here makes those contracts independently readable and
prevents the coordinator from becoming a second, sprawling API surface.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.broker.ibkr.models import IbkrOrderEvent
from app.engine.live.account_owner import AccountOwnerSubmitIntent

_MAX_INT64 = 9_223_372_036_854_775_807


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


def _require_entry_intent(entry: AccountClerkJournalEntry) -> AccountOwnerSubmitIntent:
    if entry.intent is None:
        raise ValueError("journal entry has no intent")
    return entry.intent


__all__ = [
    "AccountClerkBrokerAckReceipt",
    "AccountClerkBrokerEventReceipt",
    "AccountClerkCancelNamespaceReceipt",
    "AccountClerkInboxEntry",
    "AccountClerkIntentRejected",
    "AccountClerkJournalCorruptError",
    "AccountClerkJournalEntry",
    "AccountClerkOperatorAdjustment",
    "AccountClerkOperatorAdjustmentConflict",
    "AccountClerkRecordedReceipt",
    "AccountClerkRecoveryFlattenReceipt",
]
