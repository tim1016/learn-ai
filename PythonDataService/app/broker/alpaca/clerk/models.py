"""Clerk journal entry and result models (Alpaca phase 2).

These are a **lean, Alpaca-scoped** entry vocabulary — deliberately *not*
imported from or coupled to the IBKR clerk journal. Entry payloads carry
broker-neutral contract models (``BrokerOrderLeg``, ``BrokerOrder``) so the
ledger stays vendor-portable, and every temporal field is ``int64`` ms UTC.

S1 records definitive outcomes plus ``submit_uncertain``: an unavailable
broker response may have created an order, so the clerk probes it by its minted
``client_order_id`` before ever reporting a terminal state. S3 adds the three
cancel kinds. The broader lifecycle vocabulary (``lifecycle_update``,
``reconciled``, …) lands across later slices. Add kinds as slices need them —
never speculatively.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.broker.contract.models import BrokerOrder, BrokerOrderEvent, BrokerOrderLeg


class ClerkEntryKind(StrEnum):
    """Order-journal entry kinds (S1 submit + S3 cancel + S4 lifecycle)."""

    INTENT_RECORDED = "intent_recorded"
    SUBMIT_ACKED = "submit_acked"
    SUBMIT_FAILED = "submit_failed"
    SUBMIT_UNCERTAIN = "submit_uncertain"
    # S3 cancel path — recorded BEFORE the broker call, acked/failed after.
    CANCEL_RECORDED = "cancel_recorded"
    CANCEL_ACKED = "cancel_acked"
    CANCEL_FAILED = "cancel_failed"
    # S4 live-lifecycle path (trade_updates websocket).
    # ``ORDER_EVENT``: a lifecycle event on an order this Clerk owns (its
    # ``client_order_id`` namespace is one of ours). ``UNEXPLAINED_ORDER``: a
    # lifecycle event whose ``client_order_id`` is foreign, absent, or
    # unparseable — an order this Clerk did not submit. The exposure hold that
    # blocks new submits on an unexplained order lands in S6; S4 only journals
    # the observation and increments a counter (see the consumer).
    ORDER_EVENT = "order_event"
    UNEXPLAINED_ORDER = "unexplained_order"


class OrderJournalEntry(BaseModel):
    """One append-only order-journal line.

    Written to both ``order_inbox.jsonl`` (the intent WAL) and
    ``order_journal.jsonl`` (the canonical ledger). ``kind`` names the lifecycle
    transition this line records.

    Submit entries carry the durable minted identity
    ``(intent_id, order_ref, client_order_id)`` and the full ``leg``. Cancel
    entries (S3) key on ``broker_order_id`` — the vendor-assigned id of the order
    being canceled. When the cancel targets an order this Clerk submitted, the
    owning intent's identity + leg are copied over from the ``submit_acked`` line
    (``owned=True``); when it targets a foreign/unowned order the identity fields
    are empty and ``owned=False`` — the attribution is honest, never fabricated.
    """

    model_config = ConfigDict(extra="forbid")

    kind: ClerkEntryKind
    account_id: str
    # Empty on an unowned cancel — the Clerk never mints identity for an order it
    # did not submit; the ledger records the truth, not a placeholder.
    operator: str = ""
    intent_id: str = ""
    order_ref: str = ""
    # ``client_order_id == order_ref`` (design invariant) — recorded explicitly
    # so a reader never re-derives it. Empty on an unowned cancel.
    client_order_id: str = ""
    # The full order leg — symbol, side, quantity, and (S2) order_type,
    # limit_price, time_in_force — so the ledger line fully describes the order.
    # Absent on an unowned cancel, where the Clerk holds no leg for the order.
    leg: BrokerOrderLeg | None = None
    # The broker-assigned order id a cancel targets (S3). Absent on submit lines.
    broker_order_id: str | None = None
    # True on a cancel of an order this Clerk submitted (identity resolved from
    # the journal); False on a cancel of a foreign/unowned order.
    owned: bool | None = None
    recorded_at_ms: int
    # Present on submit_acked (and copied onto S4 order_event lines) : the
    # broker order the event pertains to, when known.
    order: BrokerOrder | None = None
    # Present on failed/uncertain submit outcomes and cancel failures: the
    # broker-facing reason.
    error_message: str | None = None
    error_detail: str | None = None
    # ── S4 live-lifecycle fields (trade_updates) ─────────────────────────────
    # Present on order_event / unexplained_order lines: the parsed lifecycle
    # event (fill/partial_fill/canceled/…). The verbatim vendor frame is in the
    # capture journal; this is the contract-mapped view for the derived state.
    event: BrokerOrderEvent | None = None
    # The client_order_id observed on the wire. On an owned order_event it equals
    # ``order_ref``; on an unexplained_order it is the foreign/absent id exactly
    # as delivered (``None`` when the order carried no client_order_id) — honest
    # attribution, never fabricated. The stable dedup key for the event.
    event_key: str | None = None


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


class OrderCancelResult(BaseModel):
    """The outcome of a cancel request the router shapes into its response.

    ``status`` is ``acked`` when the broker accepted the cancel (HTTP 204) or
    ``failed`` when it rejected it (a typed what/why, never a raw 500).
    ``order_id`` always echoes the broker-assigned id the operator targeted, so
    the ledger line is findable. ``owned`` reports whether this Clerk submitted
    the canceled order — a foreign order still cancels (reducing exposure is the
    safe direction), but the fact is surfaced honestly, not hidden.
    """

    model_config = ConfigDict(extra="forbid")

    broker: str
    account_id: str
    order_id: str
    status: str = Field(pattern="^(acked|failed)$")
    owned: bool
    # ``order_ref`` present only when the canceled order was owned (resolved from
    # the journal); the operator can then find the originating intent.
    order_ref: str | None = None
    error: OrderLegError | None = None
