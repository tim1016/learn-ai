"""Clerk journal entry and result models (Alpaca phase 2).

These are a **lean, Alpaca-scoped** entry vocabulary — deliberately *not*
imported from or coupled to the IBKR clerk journal. Entry payloads carry
broker-neutral contract models (``BrokerOrderLeg``, ``BrokerOrder``) so the
ledger stays vendor-portable, and every temporal field is ``int64`` ms UTC.

S1 needs only three entry kinds; S3 adds the three cancel kinds. The full
lifecycle vocabulary (``submit_uncertain``, ``lifecycle_update``,
``reconciled``, …) lands across later slices. Add kinds as slices need them —
never speculatively.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.broker.contract.models import BrokerOrder, BrokerOrderEvent, BrokerOrderLeg


class ClerkEntryKind(StrEnum):
    """Order-journal entry kinds (S1 submit + S3 cancel + S4 lifecycle + S5 resolution + S6 sweep/hold)."""

    INTENT_RECORDED = "intent_recorded"
    SUBMIT_ACKED = "submit_acked"
    SUBMIT_FAILED = "submit_failed"
    # S5 crash-safety: the submit's HTTP outcome was UNKNOWN — the response may
    # have been lost (timeout / 5xx / network → ``BrokerUnavailable``), so the
    # order MAY have landed. Journaled AFTER ``intent_recorded`` and resolved by
    # querying Alpaca for the order by ``client_order_id``: found → a terminal
    # ``submit_acked``; definitively absent (404) → a terminal ``submit_failed``;
    # lookup itself uncertain → the intent stays here for a later replay/sweep.
    # NEVER a fabricated terminal outcome.
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
    # S6 reconciliation + flag-and-hold path.
    # ``RECONCILIATION``: one periodic sweep result, carrying a named ``verdict``
    # (``clean`` / ``unexplained_order`` / ``missing_intent`` / ``stale``). It is
    # observational — the sweep gates no lifecycle — with the sole exception that
    # an ``unexplained_order`` verdict also raises the exposure hold.
    # ``HOLD_SET`` / ``HOLD_CLEARED``: the account-level exposure hold's audit
    # trail. The hold is journal-derived (a ``HOLD_SET`` with no later
    # ``HOLD_CLEARED`` is active), so it survives a restart. ``HOLD_SET`` refuses
    # new submits (cancels stay allowed); an operator clears it with
    # ``HOLD_CLEARED``. Both carry a ``reason_code`` + ``reason`` what/why.
    RECONCILIATION = "reconciliation"
    HOLD_SET = "hold_set"
    HOLD_CLEARED = "hold_cleared"


# The named reconciliation verdicts (kept in lockstep with the sweep). ``clean``
# — journal-owned exposure matches the broker. ``unexplained_order`` — an order
# at the broker whose ``client_order_id`` is foreign/absent (raises the hold).
# ``missing_intent`` — the broker reflects an owned order/position with no
# recorded intent (drift; observational). ``stale`` — the sweep could not
# complete (broker unreachable / read failed); surfaced, not fatal.
#
# A ``TypeAlias`` (not the PEP-695 ``type`` keyword) on purpose: this alias is
# used as a Pydantic field annotation below, and with ``from __future__ import
# annotations`` Pydantic resolves it via ``get_type_hints`` — which cannot
# resolve a ``type``-statement ``TypeAliasType`` and raises a schema-generation
# error. So UP040 is suppressed here.
ReconciliationVerdict: TypeAlias = Literal[  # noqa: UP040
    "clean", "unexplained_order", "missing_intent", "stale"
]

# The reason code stamped on the exposure hold raised by an unexplained order.
# Rendered code-like through the frontend ``receiptLabel`` pipe.
UNEXPLAINED_ORDER_HOLD_CODE = "UNEXPLAINED_ORDER_HOLD"


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
    # Present on submit_failed / cancel_failed only: why the broker rejected or
    # was unreachable.
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
    # ── S6 reconciliation + flag-and-hold fields ─────────────────────────────
    # Present on RECONCILIATION lines: the named sweep verdict.
    verdict: ReconciliationVerdict | None = None
    # Present on HOLD_SET / HOLD_CLEARED lines: the code-like reason code
    # (rendered through the frontend ``receiptLabel`` pipe) and the human what/why
    # prose (backend-authored, rendered unpiped).
    reason_code: str | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _kind_requires_fields(self) -> OrderJournalEntry:
        """Codify each kind's required fields now the vocabulary is complete (S3-deferred).

        A correctness net, not new behavior: every append site in the Clerk
        already constructs these lines this way. It catches a future drift where
        a kind is journaled without its identifying payload. Faithful to how each
        kind is actually built in ``clerk.py``:

        - ``INTENT_RECORDED`` — a submit-side line the Clerk minted, so it carries
          the full durable identity (operator / intent_id / order_ref / leg).
        - ``SUBMIT_ACKED`` — carries the accepted ``order``.
        - ``SUBMIT_FAILED`` — a definitive failure, so it carries an
          ``error_message`` (the what).
        - ``CANCEL_*`` — key on the ``broker_order_id`` being canceled.
        - ``UNEXPLAINED_ORDER`` — foreign/absent identity is *permitted* to be
          empty (never fabricated); no field is required.
        - ``RECONCILIATION`` — carries a ``verdict``.
        - ``HOLD_SET`` / ``HOLD_CLEARED`` — carry a ``reason_code`` and ``reason``.
        """
        if self.kind is ClerkEntryKind.INTENT_RECORDED:
            self._require("operator", "intent_id", "order_ref")
            if self.leg is None:
                raise ValueError("intent_recorded requires a leg")
        elif self.kind is ClerkEntryKind.SUBMIT_ACKED:
            if self.order is None:
                raise ValueError("submit_acked requires an order")
        elif self.kind is ClerkEntryKind.SUBMIT_FAILED:
            self._require("error_message")
        elif self.kind in (
            ClerkEntryKind.CANCEL_RECORDED,
            ClerkEntryKind.CANCEL_ACKED,
            ClerkEntryKind.CANCEL_FAILED,
        ):
            self._require("broker_order_id")
        elif self.kind is ClerkEntryKind.RECONCILIATION:
            if self.verdict is None:
                raise ValueError("reconciliation requires a verdict")
        elif self.kind in (ClerkEntryKind.HOLD_SET, ClerkEntryKind.HOLD_CLEARED):
            self._require("reason_code", "reason")
        return self

    def _require(self, *fields: str) -> None:
        """Raise when any of the named fields is empty/None (validator helper)."""
        for field in fields:
            if not getattr(self, field):
                raise ValueError(f"{self.kind.value} requires {field}")


class OrderLegError(BaseModel):
    """A typed leg failure: a what/why the UI renders, never a raw 500."""

    model_config = ConfigDict(extra="forbid")

    message: str
    why: str | None = None


class OrderLegResult(BaseModel):
    """The per-leg outcome the router shapes into its response.

    Keyed by ``status``:

    - ``acked`` — the broker accepted the order; ``order`` is set.
    - ``failed`` — the order definitively did not land; ``error`` is set.
    - ``uncertain`` — the submit's HTTP outcome was unknown AND resolving it by
      ``client_order_id`` was itself unreachable (S5). Neither ``order`` nor
      ``error`` is authoritative yet; the intent is durably journaled as
      ``submit_uncertain`` and startup replay / a later sweep will finish it. The
      operator must not assume the order failed — it may still have landed.

    ``order_ref`` is always present — an operator can find the intent in the
    journal in every case, including uncertain.
    """

    model_config = ConfigDict(extra="forbid")

    status: str = Field(pattern="^(acked|failed|uncertain)$")
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


class HoldState(BaseModel):
    """The account-level exposure-hold state, journal-derived (S6).

    A hold is active when a ``HOLD_SET`` line has no later ``HOLD_CLEARED``.
    ``reason_code`` is code-like (rendered through ``receiptLabel`` on the UI);
    ``reason`` is backend-authored what/why prose (rendered unpiped). When not
    held, every field but ``active`` is ``None``.
    """

    model_config = ConfigDict(extra="forbid")

    active: bool
    reason_code: str | None = None
    reason: str | None = None
    since_ms: int | None = None


class ReconciliationSummary(BaseModel):
    """The latest reconciliation-sweep result (S6), or ``None`` if never run."""

    model_config = ConfigDict(extra="forbid")

    verdict: ReconciliationVerdict
    recorded_at_ms: int


class ClerkStatus(BaseModel):
    """The clerk's observable state for the operator status surface (S6).

    Composes the exposure hold, the latest reconciliation verdict, and the count
    of outstanding (unresolved) intents (the S5 unfinished set) — everything the
    desk needs to render the hold banner and a health line.
    """

    model_config = ConfigDict(extra="forbid")

    broker: str
    account_id: str
    hold: HoldState
    latest_reconciliation: ReconciliationSummary | None = None
    outstanding_intents: int
    observed_at_ms: int


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
