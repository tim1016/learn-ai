"""Versioned read models for the Account desk event journal."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AccountEventView = Literal["trader_today", "operations"]
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


class AccountEventsResponse(BaseModel):
    """Cursor page from the immutable account event journal."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    account_id: str = Field(min_length=1, max_length=64)
    view: AccountEventView
    rows: list[AccountEventRow] = Field(default_factory=list)
    latest_seq: int | None = Field(default=None, ge=1)
    next_before_seq: int | None = Field(default=None, ge=1)
