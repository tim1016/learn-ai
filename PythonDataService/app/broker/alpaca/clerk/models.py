"""Clerk journal entry and result models (Alpaca phase 2).

These are a **lean, Alpaca-scoped** entry vocabulary — deliberately *not*
imported from or coupled to the IBKR clerk journal. Entry payloads carry
broker-neutral contract models (``BrokerOrderLeg``, ``BrokerOrder``) so the
ledger stays vendor-portable, and every temporal field is ``int64`` ms UTC.

S1 records definitive outcomes plus ``submit_uncertain``: an unavailable
broker response may have created an order, so the clerk probes it by its minted
``client_order_id`` before ever reporting a terminal state. The broader
lifecycle vocabulary (``lifecycle_update``, ``reconciled``, …) lands across
later slices. Add kinds as slices need them — never speculatively.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.broker.contract.models import BrokerOrder, BrokerOrderLeg


class ClerkEntryKind(StrEnum):
    """The S1 order-journal entry kinds."""

    INTENT_RECORDED = "intent_recorded"
    SUBMIT_ACKED = "submit_acked"
    SUBMIT_FAILED = "submit_failed"
    SUBMIT_UNCERTAIN = "submit_uncertain"


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
    # Present on failed or uncertain submit outcomes: the broker-facing reason.
    error_message: str | None = None
    error_detail: str | None = None


class OrderLegError(BaseModel):
    """A typed leg failure: a what/why the UI renders, never a raw 500."""

    model_config = ConfigDict(extra="forbid")

    message: str
    why: str | None = None


class OrderLegResult(BaseModel):
    """The per-leg outcome the router shapes into its response.

    ``acked`` carries the accepted ``order``; ``failed`` is a definitive
    rejection or a lookup that proved the order absent; ``uncertain`` means the
    broker could not confirm the result. ``order_ref`` is always present — an
    operator can find the durable intent in the journal before retrying.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["acked", "failed", "uncertain"]
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
