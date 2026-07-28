"""Versioned read models for deterministic Account Desk operational history."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AccountEventView = Literal["operations"]
AccountEventProvenance = Literal[
    "legacy_account_events",
    "producer_operational_log",
    "clerk_journal",
]
AccountEventKind = Literal[
    "activity",
    "safety",
    "reconciliation",
    "clerk",
    "configuration",
    "other",
]


class AccountEventEvidenceRef(BaseModel):
    """An opaque reference carried by an account-journal event."""

    model_config = ConfigDict(frozen=True)

    source: str = Field(min_length=1, max_length=128)
    ref: str = Field(min_length=1, max_length=512)
    detail: str | None = Field(default=None, max_length=512)


class AccountEventOperatorOrderReceipt(BaseModel):
    """The receipt fields an operator needs to audit one manual paper order."""

    model_config = ConfigDict(frozen=True)

    broker: Literal["ibkr"]
    order_id: int = Field(ge=0)
    perm_id: int | None = Field(default=None, ge=0)
    order_ref: str = Field(min_length=1, max_length=120)
    symbol: str = Field(min_length=1, max_length=32)
    action: Literal["BUY", "SELL"]
    quantity: float = Field(gt=0)
    order_type: Literal["MKT", "LMT"]
    limit_price: float | None = Field(default=None, gt=0)
    status: str = Field(min_length=1, max_length=64)
    acknowledged_at_ms: int = Field(ge=0, le=9_223_372_036_854_775_807)


class AccountEventRow(BaseModel):
    """One backend-classified operational-history row for a desk view."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    event_id: str = Field(min_length=1, max_length=512)
    provenance: AccountEventProvenance = "legacy_account_events"
    seq: int = Field(ge=1)
    kind: AccountEventKind
    occurred_at_ms: int = Field(ge=0, le=9_223_372_036_854_775_807)
    event_at_ms: int | None = Field(default=None, ge=0, le=9_223_372_036_854_775_807)
    arrived_at_ms: int | None = Field(default=None, ge=0, le=9_223_372_036_854_775_807)
    recorded_at_ms: int | None = Field(default=None, ge=0, le=9_223_372_036_854_775_807)
    producer: str = Field(default="legacy_account_events", min_length=1, max_length=128)
    producer_boot_id: str = Field(default="historical", min_length=1, max_length=128)
    producer_seq: int = Field(default=1, ge=1)
    trader_narration: str | None = Field(default=None, max_length=512)
    operator_detail: str = Field(min_length=1, max_length=512)
    evidence_refs: list[AccountEventEvidenceRef] = Field(default_factory=list)
    # The trader view deliberately omits this receipt. The operator receives
    # the concise audit fields, while the Clerk journal remains canonical.
    operator_order_receipt: AccountEventOperatorOrderReceipt | None = None


class AccountEventsResponse(BaseModel):
    """Cursor page from a merged operational history projection."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    account_id: str = Field(min_length=1, max_length=64)
    view: AccountEventView
    rows: list[AccountEventRow] = Field(default_factory=list)
    latest_seq: int | None = Field(default=None, ge=1)
    next_before_seq: int | None = Field(default=None, ge=1)


class TraderAccountEventRow(BaseModel):
    """Trader-safe outcome copy; deliberately contains no operator evidence."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    event_id: str = Field(min_length=1, max_length=256)
    seq: int = Field(ge=1)
    occurred_at_ms: int = Field(ge=0, le=9_223_372_036_854_775_807)
    outcome: str = Field(min_length=1, max_length=512)


class TraderAccountEventsResponse(BaseModel):
    """Trader event page with an intentionally disjoint schema from operations."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    account_id: str = Field(min_length=1, max_length=64)
    rows: list[TraderAccountEventRow] = Field(default_factory=list)
    latest_seq: int | None = Field(default=None, ge=1)
    next_before_seq: int | None = Field(default=None, ge=1)
