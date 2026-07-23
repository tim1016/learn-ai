"""Clerk journal entry and result models (Alpaca phase 2).

These are a **lean, Alpaca-scoped** entry vocabulary — deliberately *not*
imported from or coupled to the IBKR clerk journal. Entry payloads carry
broker-neutral contract models (``BrokerOrderLeg``, ``BrokerOrder``) so the
ledger stays vendor-portable, and every temporal field is ``int64`` ms UTC.

S1 needs only three entry kinds; the full lifecycle vocabulary
(``submit_uncertain``, ``lifecycle_update``, ``reconciled``, …) lands across
later slices. Add kinds as slices need them — never speculatively.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.broker.contract.models import BrokerOrder, BrokerOrderLeg


class ClerkEntryKind(StrEnum):
    """The S1 order-journal entry kinds."""

    INTENT_RECORDED = "intent_recorded"
    SUBMIT_ACKED = "submit_acked"
    SUBMIT_FAILED = "submit_failed"


class OrderJournalEntry(BaseModel):
    """One append-only order-journal line.

    Written to both ``order_inbox.jsonl`` (the intent WAL) and
    ``order_journal.jsonl`` (the canonical ledger). The tuple
    ``(intent_id, order_ref, client_order_id)`` is the durable identity minted
    before any broker call; ``kind`` names the lifecycle transition this line
    records.
    """

    model_config = ConfigDict(extra="forbid")

    kind: ClerkEntryKind
    account_id: str
    operator: str
    intent_id: str
    order_ref: str
    # ``client_order_id == order_ref`` (design invariant) — recorded explicitly
    # so a reader never re-derives it.
    client_order_id: str
    # The full order leg — symbol, side, quantity, and (S2) order_type,
    # limit_price, time_in_force — so the ledger line fully describes the order.
    leg: BrokerOrderLeg
    recorded_at_ms: int
    # Present on submit_acked only: the accepted broker order.
    order: BrokerOrder | None = None
    # Present on submit_failed only: why the broker rejected or was unreachable.
    error_message: str | None = None
    error_detail: str | None = None


class OrderLegError(BaseModel):
    """A typed leg failure: a what/why the UI renders, never a raw 500."""

    model_config = ConfigDict(extra="forbid")

    message: str
    why: str | None = None


class OrderLegResult(BaseModel):
    """The per-leg outcome the router shapes into its response.

    Exactly one of ``order`` (acked) / ``error`` (failed) is set, keyed by
    ``status``. ``order_ref`` is always present — an operator can find the
    intent in the journal even when the submit failed.
    """

    model_config = ConfigDict(extra="forbid")

    status: str = Field(pattern="^(acked|failed)$")
    order_ref: str
    intent_id: str
    order: BrokerOrder | None = None
    error: OrderLegError | None = None


class OrderSubmitResult(BaseModel):
    """The whole request's outcome: one result per submitted leg, in order."""

    model_config = ConfigDict(extra="forbid")

    broker: str
    account_id: str
    results: list[OrderLegResult]
