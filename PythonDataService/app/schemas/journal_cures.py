"""Operator requests and receipts for immutable Clerk journal cures."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.engine.live.account_clerk_journal import AccountClerkRecoveryFlattenReceipt
from app.engine.live.account_owner import AccountOwnerSubmitIntent


class JournalCureRequest(BaseModel):
    """One operator request to reduce an already-attributed journal claim."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bot_order_namespace: str = Field(min_length=1, max_length=256)
    symbol: str = Field(min_length=1, max_length=32)
    signed_quantity: float
    reason: str = Field(min_length=1, max_length=512)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=32)
    request_provenance: str = Field(min_length=1, max_length=256)
    idempotency_key: str = Field(min_length=1, max_length=160)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        """Store symbols in the canonical journal-projection spelling."""

        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("symbol must not be blank")
        return normalized

    @field_validator("bot_order_namespace", "reason", "request_provenance", "idempotency_key")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        """Keep durable operator provenance meaningful after whitespace normalization."""

        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("evidence_refs")
    @classmethod
    def reject_blank_evidence_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Require every durable evidence reference to identify real evidence."""

        normalized = tuple(reference.strip() for reference in value)
        if any(not reference for reference in normalized):
            raise ValueError("evidence_refs must not contain blank references")
        return normalized

    @field_validator("signed_quantity")
    @classmethod
    def reject_zero_quantity(cls, value: float) -> float:
        """A zero adjustment could hide an accidental operator action."""

        if value == 0 or not math.isfinite(value):
            raise ValueError("signed_quantity must be finite and non-zero")
        return value


class JournalCureReceipt(BaseModel):
    """The immutable adjustment row accepted by the Clerk journal."""

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
    recorded_at_ms: int = Field(ge=0)
    journal_seq: int = Field(ge=1)


class JournalCurePreview(BaseModel):
    """Server-derived claim state shown before an operator creates a cure."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str = Field(min_length=1, max_length=64)
    bot_order_namespace: str = Field(min_length=1, max_length=256)
    symbol: str = Field(min_length=1, max_length=32)
    journal_quantity: float
    required_adjustment_sign: Literal["positive", "negative"] | None = None
    can_cure: bool
    reason_code: str


class OperatorRecoveryFlattenRequest(BaseModel):
    """Provenance-bearing request for the Clerk's existing operator flatten lane."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    intent: AccountOwnerSubmitIntent
    request_provenance: str = Field(min_length=1, max_length=256)


class OperatorRecoveryFlattenResponse(BaseModel):
    """Durable receipt returned by the Clerk-owned operator recovery lane."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    recovery_flatten: AccountClerkRecoveryFlattenReceipt
