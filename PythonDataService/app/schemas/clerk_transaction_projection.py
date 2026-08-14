"""Backend-owned contracts for Clerk-native transaction history."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.models import ExternalOrderResource

TransactionFeedState = Literal[
    "live", "reconnecting", "rebuilding", "stale", "offline_but_saved", "corrupt", "projection_unavailable"
]
TransactionOrigin = Literal[
    "manual", "strategy", "external", "unknown", "recovery", "emergency", "shutdown", "force_flat", "other"
]
TRANSACTION_FEED_STATES = frozenset({
    "live", "reconnecting", "rebuilding", "stale", "offline_but_saved", "corrupt", "projection_unavailable"
})


class ExternalOrderAcknowledgementRequest(BaseModel):
    """Operator evidence for reviewing one externally observed broker order."""

    model_config = ConfigDict(extra="forbid")

    operator: str = Field(min_length=1, max_length=64)

    @field_validator("operator")
    @classmethod
    def operator_must_contain_non_whitespace(cls, value: str) -> str:
        """Preserve meaningful, attributable operator acknowledgement evidence."""
        normalized = value.strip()
        if not normalized:
            raise ValueError("operator must contain non-whitespace characters")
        return normalized


class ExternalOrderAcknowledgementResponse(BaseModel):
    """Durable result of acknowledging one external-order observation."""

    model_config = ConfigDict(extra="forbid")

    external_order_id: str = Field(min_length=1, max_length=256)
    acknowledged_at_ms: int = Field(ge=0)
    ack_operator: str = Field(min_length=1, max_length=64)

    @classmethod
    def from_external_order(
        cls,
        resource: ExternalOrderResource,
    ) -> ExternalOrderAcknowledgementResponse:
        """Adapt a fold-owned acknowledgement without accepting client fields."""
        if resource.acknowledged_at_ms is None or resource.ack_operator is None:
            raise ValueError("external-order acknowledgement is not durably recorded")
        return cls(
            external_order_id=resource.external_order_id,
            acknowledged_at_ms=resource.acknowledged_at_ms,
            ack_operator=resource.ack_operator,
        )


class ClerkOrderInstruction(BaseModel):
    """Typed, receipt-supplied order fields; never a client-side inference."""

    model_config = ConfigDict(extra="forbid")

    symbol: str | None = Field(default=None, max_length=64)
    sec_type: str | None = Field(default=None, max_length=16)
    action: str | None = Field(default=None, max_length=16)
    quantity: float | None = None
    order_type: str | None = Field(default=None, max_length=16)
    limit_price: float | None = None
    stop_price: float | None = None
    time_in_force: str | None = Field(default=None, max_length=16)
    outside_rth: bool | None = None


class ClerkTransactionEventRow(BaseModel):
    """One immutable event materialized from a Clerk journal receipt."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=512)
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
    fee_fidelity: Literal["reported", "not_reported"] | None = None
    execution_quantity: float | None = None
    execution_price: float | None = None
    journal_seq: int = Field(ge=1)
    recorded_at_ms: int = Field(ge=0)
    receipt: dict[str, Any] = Field(default_factory=dict)


class ClerkCustodyDurations(BaseModel):
    """Measured same-clock durations for one Clerk-owned intent."""

    model_config = ConfigDict(extra="forbid")

    request_to_intake_ms: int | None = Field(default=None, ge=0)
    intake_to_a0_ms: int | None = Field(default=None, ge=0)
    a0_to_broker_write_ms: int | None = Field(default=None, ge=0)
    broker_write_to_return_ms: int | None = Field(default=None, ge=0)
    broker_return_to_first_callback_ms: int | None = Field(default=None, ge=0)
    terminal_age_ms: int | None = Field(default=None, ge=0)


class ClerkCustodyTimeline(BaseModel):
    """Distinct source, arrival, and durable clocks for one intent lifecycle."""

    model_config = ConfigDict(extra="forbid")

    intent_created_at_ms: int | None = Field(default=None, ge=0)
    clerk_request_received_at_ms: int | None = Field(default=None, ge=0)
    clerk_intake_admitted_at_ms: int | None = Field(default=None, ge=0)
    inbox_fsynced_at_ms: int | None = Field(default=None, ge=0)
    a0_custody_accepted_at_ms: int | None = Field(default=None, ge=0)
    broker_write_started_at_ms: int | None = Field(default=None, ge=0)
    broker_call_returned_at_ms: int | None = Field(default=None, ge=0)
    broker_ack_recorded_at_ms: int | None = Field(default=None, ge=0)
    earliest_broker_source_at_ms: int | None = Field(default=None, ge=0)
    first_callback_arrived_at_ms: int | None = Field(default=None, ge=0)
    first_callback_recorded_at_ms: int | None = Field(default=None, ge=0)
    economic_terminal_recorded_at_ms: int | None = Field(default=None, ge=0)
    durations: ClerkCustodyDurations = Field(default_factory=ClerkCustodyDurations)


class ClerkTransactionRow(BaseModel):
    """One operator transaction, projected from a durable Clerk receipt."""

    model_config = ConfigDict(extra="forbid")

    transaction_id: str = Field(min_length=1, max_length=512)
    broker: Literal["ibkr", "alpaca"] = "ibkr"
    account_id: str = Field(min_length=1, max_length=64)
    journal_seq: int = Field(ge=1)
    recorded_at_ms: int = Field(ge=0)
    transaction_kind: str = Field(min_length=1, max_length=64)
    transaction_origin: TransactionOrigin = "manual"
    order_instruction: ClerkOrderInstruction = Field(default_factory=ClerkOrderInstruction)
    subject_id: str | None = Field(default=None, min_length=1, max_length=128)
    strategy_instance_id: str | None = Field(default=None, min_length=1, max_length=128)
    run_id: str | None = Field(default=None, min_length=1, max_length=128)
    intent_id: str | None = Field(default=None, min_length=1, max_length=256)
    order_ref: str | None = Field(default=None, min_length=1, max_length=512)
    order_id: int | None = Field(default=None, ge=0)
    perm_id: int | None = Field(default=None, ge=0)
    exec_id: str | None = Field(default=None, max_length=256)
    native_order_id: str | None = Field(default=None, max_length=256)
    native_execution_id: str | None = Field(default=None, max_length=256)
    lifecycle_state: str = Field(min_length=1, max_length=64)
    commission_status: Literal["unknown", "reported"] = "unknown"
    fee: float | None = None
    fee_fidelity: Literal["reported", "not_reported"] | None = None
    execution_quantity: float | None = None
    execution_price: float | None = None
    external_order_id: str | None = Field(default=None, min_length=1, max_length=256)
    receipt: dict[str, Any] = Field(default_factory=dict)
    events: list[ClerkTransactionEventRow] = Field(default_factory=list)
    custody_timeline: ClerkCustodyTimeline | None = None


class ClerkTransactionSummaryRow(BaseModel):
    """Compact, receipt-free row for the operator transaction grid."""

    model_config = ConfigDict(extra="forbid")

    transaction_id: str = Field(min_length=1, max_length=512)
    broker: Literal["ibkr", "alpaca"] = "ibkr"
    account_id: str = Field(min_length=1, max_length=64)
    journal_seq: int = Field(ge=1)
    recorded_at_ms: int = Field(ge=0)
    transaction_kind: str = Field(min_length=1, max_length=64)
    transaction_origin: TransactionOrigin = "manual"
    order_instruction: ClerkOrderInstruction = Field(default_factory=ClerkOrderInstruction)
    subject_id: str | None = Field(default=None, min_length=1, max_length=128)
    strategy_instance_id: str | None = Field(default=None, min_length=1, max_length=128)
    run_id: str | None = Field(default=None, min_length=1, max_length=128)
    intent_id: str | None = Field(default=None, min_length=1, max_length=256)
    order_ref: str | None = Field(default=None, min_length=1, max_length=512)
    order_id: int | None = Field(default=None, ge=0)
    perm_id: int | None = Field(default=None, ge=0)
    exec_id: str | None = Field(default=None, max_length=256)
    native_order_id: str | None = Field(default=None, max_length=256)
    native_execution_id: str | None = Field(default=None, max_length=256)
    lifecycle_state: str = Field(min_length=1, max_length=64)
    commission_status: Literal["unknown", "reported"] = "unknown"
    fee: float | None = None
    fee_fidelity: Literal["reported", "not_reported"] | None = None
    execution_quantity: float | None = None
    execution_price: float | None = None
    external_order_id: str | None = Field(default=None, min_length=1, max_length=256)
    event_count: int = Field(ge=1)


class ClerkCustodyWindowSummary(BaseModel):
    """Server-folded custody stages for one bounded projected evidence window.

    Counts describe only the response's immutable receipt window; they never
    claim to be account-wide truth and are not a permission or safety verdict.
    """

    model_config = ConfigDict(extra="forbid")

    record_count: int = Field(ge=0)
    a0_custody_accepted_count: int = Field(ge=0)
    a1_broker_write_started_count: int = Field(ge=0)
    a2_broker_known_count: int = Field(ge=0)
    a3_economic_terminal_count: int = Field(ge=0)
    uncertain_count: int = Field(ge=0)


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
    custody_summary: ClerkCustodyWindowSummary
    rows: list[ClerkTransactionSummaryRow]
    next_cursor: str | None = None
