"""Backend-owned contracts for Clerk-native transaction history."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

TransactionFeedState = Literal[
    "live", "reconnecting", "rebuilding", "stale", "offline_but_saved", "corrupt", "projection_unavailable"
]
TransactionOrigin = Literal[
    "manual", "strategy", "recovery", "emergency", "shutdown", "force_flat", "other"
]
TRANSACTION_FEED_STATES = frozenset({
    "live", "reconnecting", "rebuilding", "stale", "offline_but_saved", "corrupt", "projection_unavailable"
})


class ClerkOrderInstruction(BaseModel):
    """Typed, receipt-supplied order fields; never a client-side inference."""

    model_config = ConfigDict(extra="forbid")

    symbol: str | None = Field(default=None, max_length=64)
    sec_type: str | None = Field(default=None, max_length=16)
    action: str | None = Field(default=None, max_length=16)
    quantity: float | None = None
    order_type: str | None = Field(default=None, max_length=16)
    limit_price: float | None = None
    time_in_force: str | None = Field(default=None, max_length=16)
    outside_rth: bool | None = None


class ClerkTransactionEventRow(BaseModel):
    """One immutable event materialized from a Clerk journal receipt."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=96)
    # The broker is explicit because the shared grid never infers lifecycle
    # semantics from a receipt shape.  ``ibkr`` remains the compatibility
    # default for rows projected before the Alpaca parity migration.
    broker: Literal["ibkr", "alpaca"] = "ibkr"
    event_kind: str = Field(min_length=1, max_length=64)
    callback_identity: str = Field(min_length=1, max_length=256)
    lifecycle_state: str = Field(min_length=1, max_length=64)
    native_order_id: str | None = Field(default=None, max_length=256)
    native_execution_id: str | None = Field(default=None, max_length=256)
    commission_status: Literal["unknown", "reported"] = "unknown"
    fee: float | None = None
    journal_seq: int = Field(ge=1)
    recorded_at_ms: int = Field(ge=0)
    receipt: dict[str, Any] = Field(default_factory=dict)


class ClerkTransactionRow(BaseModel):
    """One operator transaction, projected from a durable Clerk receipt."""

    model_config = ConfigDict(extra="forbid")

    transaction_id: str = Field(min_length=1, max_length=96)
    broker: Literal["ibkr", "alpaca"] = "ibkr"
    account_id: str = Field(min_length=1, max_length=64)
    journal_seq: int = Field(ge=1)
    recorded_at_ms: int = Field(ge=0)
    transaction_kind: str = Field(min_length=1, max_length=64)
    transaction_origin: TransactionOrigin = "manual"
    order_instruction: ClerkOrderInstruction = Field(default_factory=ClerkOrderInstruction)
    strategy_instance_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    intent_id: str = Field(min_length=1, max_length=256)
    order_ref: str = Field(min_length=1, max_length=512)
    order_id: int | None = Field(default=None, ge=0)
    perm_id: int | None = Field(default=None, ge=0)
    exec_id: str | None = Field(default=None, max_length=256)
    native_order_id: str | None = Field(default=None, max_length=256)
    native_execution_id: str | None = Field(default=None, max_length=256)
    lifecycle_state: str = Field(min_length=1, max_length=64)
    commission_status: Literal["unknown", "reported"] = "unknown"
    fee: float | None = None
    receipt: dict[str, Any] = Field(default_factory=dict)
    events: list[ClerkTransactionEventRow] = Field(default_factory=list)


class ClerkTransactionSummaryRow(BaseModel):
    """Compact, receipt-free row for the operator transaction grid."""

    model_config = ConfigDict(extra="forbid")

    transaction_id: str = Field(min_length=1, max_length=96)
    broker: Literal["ibkr", "alpaca"] = "ibkr"
    account_id: str = Field(min_length=1, max_length=64)
    journal_seq: int = Field(ge=1)
    recorded_at_ms: int = Field(ge=0)
    transaction_kind: str = Field(min_length=1, max_length=64)
    transaction_origin: TransactionOrigin = "manual"
    order_instruction: ClerkOrderInstruction = Field(default_factory=ClerkOrderInstruction)
    strategy_instance_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    intent_id: str = Field(min_length=1, max_length=256)
    order_ref: str = Field(min_length=1, max_length=512)
    order_id: int | None = Field(default=None, ge=0)
    perm_id: int | None = Field(default=None, ge=0)
    exec_id: str | None = Field(default=None, max_length=256)
    native_order_id: str | None = Field(default=None, max_length=256)
    native_execution_id: str | None = Field(default=None, max_length=256)
    lifecycle_state: str = Field(min_length=1, max_length=64)
    commission_status: Literal["unknown", "reported"] = "unknown"
    fee: float | None = None
    event_count: int = Field(ge=1)


class ClerkTransactionHistoryResponse(BaseModel):
    """One bounded keyset page, with backend-owned projection status."""

    model_config = ConfigDict(extra="forbid")

    projection_available: bool
    canonical_fallback_required: bool
    feed_state: TransactionFeedState
    feed_headline: str = Field(min_length=1, max_length=128)
    feed_detail: str = Field(min_length=1, max_length=512)
    high_water_journal_seq: int | None = Field(default=None, ge=0)
    lag_records: int | None = Field(default=None, ge=0)
    lag_is_lower_bound: bool = False
    rows: list[ClerkTransactionSummaryRow]
    next_cursor: str | None = None
