"""Versioned read models for the Account desk event journal."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AccountEventView = Literal["operations"]
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
    """One backend-classified journal event for a desk view."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    event_id: str = Field(min_length=1, max_length=256)
    seq: int = Field(ge=1)
    kind: AccountEventKind
    occurred_at_ms: int = Field(ge=0, le=9_223_372_036_854_775_807)
    trader_narration: str | None = Field(default=None, max_length=512)
    operator_detail: str = Field(min_length=1, max_length=512)
    evidence_refs: list[AccountEventEvidenceRef] = Field(default_factory=list)
    # The trader view deliberately omits this receipt. The operator receives
    # the concise audit fields, while the Clerk journal remains canonical.
    operator_order_receipt: AccountEventOperatorOrderReceipt | None = None


class AccountEventsResponse(BaseModel):
    """Cursor page from the immutable account event journal."""

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
