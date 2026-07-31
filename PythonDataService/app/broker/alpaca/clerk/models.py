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

from app.broker.contract.models import BrokerActivity, BrokerOrder, BrokerOrderEvent, BrokerOrderLeg
from app.engine.live.account_clerk_journal_models import AccountClerkBrokerEvidenceBaseline
from app.schemas.action_plan import ActionPlan, StockEntryLeg


class EffectPurpose(StrEnum):
    """The only executable decisions a strategy runtime may emit to Alpaca."""

    ENTER = "ENTER"
    EXIT = "EXIT"


class EffectOperationState(StrEnum):
    """Clerk-authored state of one durable strategy decision."""

    ACCEPTED = "accepted"
    NOOP = "noop"
    SUBMITTED = "submitted"
    REJECTED = "rejected"
    UNCERTAIN = "uncertain"
    EXIT_PENDING = "exit_pending"
    FLAT = "flat"
    UNPROVABLE = "unprovable"


class AlpacaEffectOperation(BaseModel):
    """One durable Clerk operation, not a bot-authored broker order.

    The plan is deployed configuration.  It is recorded with the operation so
    replay after a runtime or Clerk restart has the exact immutable intent
    without consulting a second execution ledger.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_instance_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    decision_id: str = Field(min_length=1, max_length=256)
    purpose: EffectPurpose
    action_plan: ActionPlan
    quantity: int = Field(ge=1, le=100)

    @model_validator(mode="after")
    def _requires_one_stock_entry_and_matching_close(self) -> AlpacaEffectOperation:
        stock_entries = [leg for leg in self.action_plan.on_enter if isinstance(leg, StockEntryLeg)]
        if len(stock_entries) != 1 or len(self.action_plan.on_enter) != 1:
            raise ValueError("Alpaca effect operations require exactly one stock entry leg")
        entry = stock_entries[0]
        if len(self.action_plan.on_exit) != 1 or self.action_plan.on_exit[0].entry_leg_id != entry.leg_id:
            raise ValueError("Alpaca effect operations require one matching close_leg exit")
        return self

    @property
    def entry_leg(self) -> StockEntryLeg:
        entry = self.action_plan.on_enter[0]
        assert isinstance(entry, StockEntryLeg)
        return entry


class EffectOperationReceipt(BaseModel):
    """Backend-authored effect progress for trader/operator projections."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: AlpacaEffectOperation
    state: EffectOperationState
    explanation: str
    next_step: str | None = None
    child_order_refs: tuple[str, ...] = ()


class ClerkEntryKind(StrEnum):
    """Order-journal entry kinds (S1 submit + S3 cancel + S4 lifecycle + S5 resolution + S6 sweep/hold)."""

    INTENT_RECORDED = "intent_recorded"
    SUBMIT_ACKED = "submit_acked"
    SUBMIT_FAILED = "submit_failed"
    # S5 crash-safety: the submit's HTTP outcome was UNKNOWN — the response may
    # have been lost (timeout / 5xx / network → ``BrokerUnavailable``), so the
    # order MAY have landed. Journaled AFTER ``intent_recorded`` and resolved by
    # querying Alpaca for the order by ``client_order_id``: found → a terminal
    # ``submit_acked``; a 404 receives a bounded grace period before a later
    # recovery/sweep may record ``submit_failed``; a failed lookup stays here.
    # NEVER fabricate a terminal outcome from one lost response.
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
    ACTIVITY_RECOVERY = "activity_recovery"
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
    # An operator-confirmed cutover from pre-Clerk broker inventory to
    # journal-derived exposure. The snapshot is account truth only: it never
    # fabricates a bot/manual namespace owner and never rewrites older rows.
    BROKER_EVIDENCE_BASELINE = "broker_evidence_baseline"
    # S7 Clerk-owned strategy effects.  These are journal rows, not a second
    # execution store: one operation can link to zero or more child intents.
    EFFECT_ACCEPTED = "effect_accepted"
    EFFECT_RECEIPT = "effect_receipt"


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
AccountFreezeCategory: TypeAlias = Literal[  # noqa: UP040
    "ACCOUNT_STATE_UNATTRIBUTABLE",
    "ACCOUNT_STATE_UNPROVABLE",
]

# The reason code stamped on the exposure hold raised by an unexplained order.
# Rendered code-like through the frontend ``receiptLabel`` pipe.
UNEXPLAINED_ORDER_HOLD_CODE = "UNEXPLAINED_ORDER_HOLD"

# The reason code stamped on the hold raised by the dual-health submission
# gate (S4, #1262): the shared market-data feed or the Alpaca execution
# channel is unhealthy. A peer of ``UNEXPLAINED_ORDER_HOLD_CODE``; same hold
# semantics (blocks new submissions, never reductions/cancels, journal-derived,
# clearable — never auto-cleared).
STREAM_HEALTH_HOLD_CODE = "STREAM_HEALTH_HOLD"


class ChannelHealth(BaseModel):
    """One submission-affecting stream's health fact, with its age (P7).

    ``observed_at_ms`` is mandatory: no health fact is rendered without an
    observation time. ``reason`` is empty when healthy.
    """

    model_config = ConfigDict(extra="forbid")

    stream: Literal["market_data", "execution"]
    healthy: bool
    reason: str = ""
    observed_at_ms: int


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
    # A recovery event is broker evidence, not a websocket callback replay.
    # Keep its source/bound in the durable receipt so operators can distinguish
    # it without inventing a shared IBKR callback taxonomy.
    recovery_source: str | None = None
    recovery_window_limit: int | None = None
    activity: BrokerActivity | None = None
    # ── S6 reconciliation + flag-and-hold fields ─────────────────────────────
    # Present on RECONCILIATION lines: the named sweep verdict.
    verdict: ReconciliationVerdict | None = None
    # Present on HOLD_SET / HOLD_CLEARED lines: the code-like reason code
    # (rendered through the frontend ``receiptLabel`` pipe) and the human what/why
    # prose (backend-authored, rendered unpiped).
    reason_code: str | None = None
    reason: str | None = None
    # Present only on BROKER_EVIDENCE_BASELINE. Reuse the broker-neutral Clerk
    # evidence contract so IBKR and Alpaca retain the same snapshot meaning.
    broker_evidence_baseline: AccountClerkBrokerEvidenceBaseline | None = None
    # Present on EFFECT_* rows.  Child submit rows carry only the stable
    # decision id below, preserving their existing order identity shape.
    effect_receipt: EffectOperationReceipt | None = None
    effect_operation_id: str | None = None

    @classmethod
    def attributed_from(
        cls,
        owner: OrderJournalEntry | None,
        *,
        kind: ClerkEntryKind,
        account_id: str,
        client_order_id: str,
        recorded_at_ms: int,
        broker_order_id: str | None = None,
        owned: bool | None = None,
        order: BrokerOrder | None = None,
        error_message: str | None = None,
        error_detail: str | None = None,
        event: BrokerOrderEvent | None = None,
        event_key: str | None = None,
        recovery_source: str | None = None,
        recovery_window_limit: int | None = None,
        activity: BrokerActivity | None = None,
    ) -> OrderJournalEntry:
        """Build a line with honest identity copied from its durable owner.

        Unowned broker evidence passes ``owner=None`` and therefore receives
        empty identity fields. Callers must supply the wire
        ``client_order_id`` separately because unexplained lifecycle events
        preserve that evidence without claiming it as an owned ``order_ref``.
        """
        return cls(
            kind=kind,
            account_id=account_id,
            operator=owner.operator if owner is not None else "",
            intent_id=owner.intent_id if owner is not None else "",
            order_ref=owner.order_ref if owner is not None else "",
            client_order_id=client_order_id,
            leg=owner.leg if owner is not None else None,
            broker_order_id=broker_order_id,
            owned=owned,
            recorded_at_ms=recorded_at_ms,
            order=order,
            error_message=error_message,
            error_detail=error_detail,
            event=event,
            event_key=event_key,
            recovery_source=recovery_source,
            recovery_window_limit=recovery_window_limit,
            activity=activity,
        )

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
        elif self.kind is ClerkEntryKind.ACTIVITY_RECOVERY:
            if self.activity is None:
                raise ValueError("activity_recovery requires activity")
        elif self.kind in (ClerkEntryKind.HOLD_SET, ClerkEntryKind.HOLD_CLEARED):
            self._require("reason_code", "reason")
        elif self.kind is ClerkEntryKind.BROKER_EVIDENCE_BASELINE:
            self._require("operator", "reason")
            if self.broker_evidence_baseline is None:
                raise ValueError("broker_evidence_baseline requires broker_evidence_baseline")
            if self.broker_evidence_baseline.account_id != self.account_id:
                raise ValueError("broker_evidence_baseline account must match journal account")
            if any(
                (
                    self.intent_id,
                    self.order_ref,
                    self.client_order_id,
                    self.leg,
                    self.broker_order_id,
                    self.order,
                    self.event,
                    self.activity,
                    self.verdict,
                    self.reason_code,
                    self.effect_receipt,
                    self.effect_operation_id,
                )
            ):
                raise ValueError(
                    "broker_evidence_baseline cannot carry order, lifecycle, or effect data"
                )
        elif self.kind in (ClerkEntryKind.EFFECT_ACCEPTED, ClerkEntryKind.EFFECT_RECEIPT):
            if self.effect_receipt is None:
                raise ValueError(f"{self.kind.value} requires effect_receipt")
        if (
            self.kind is not ClerkEntryKind.BROKER_EVIDENCE_BASELINE
            and self.broker_evidence_baseline is not None
        ):
            raise ValueError(
                "broker_evidence_baseline is valid only on broker_evidence_baseline rows"
            )
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
    - ``uncertain`` — the submit's HTTP outcome was unknown. Neither ``order``
      nor ``error`` is authoritative yet; the intent is durably journaled as
      ``submit_uncertain`` and a later replay / sweep will finish it. The
      operator must not assume the order failed — it may still have landed.

    ``order_ref`` is always present — an operator can find the intent in the
    journal in every case, including uncertain.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["acked", "failed", "uncertain"]
    order_ref: str
    intent_id: str
    order: BrokerOrder | None = None
    error: OrderLegError | None = None

    @model_validator(mode="after")
    def _status_matches_payload(self) -> OrderLegResult:
        """Reject contradictory result shapes before they reach the wire."""
        if self.status == "acked":
            if self.order is None or self.error is not None:
                raise ValueError("acked results require order and forbid error")
        elif self.status == "failed":
            if self.error is None or self.order is not None:
                raise ValueError("failed results require error and forbid order")
        elif self.error is None or self.order is not None:
            raise ValueError("uncertain results require error and forbid order")
        return self


class OrderSubmitResult(BaseModel):
    """The whole request's outcome: one result per submitted leg, in order."""

    model_config = ConfigDict(extra="forbid")

    broker: str
    account_id: str
    results: list[OrderLegResult]


class HoldState(BaseModel):
    """The account-level exposure-hold state, journal-derived (S6).

    A hold is active when a ``HOLD_SET`` or ``UNEXPLAINED_ORDER`` line has no
    later ``HOLD_CLEARED``. The latter keeps a crash between the observation and
    its companion HOLD_SET receipt fail-closed. ``reason_code`` is code-like
    (rendered through ``receiptLabel`` on the UI); ``reason`` is backend-authored
    what/why prose (rendered unpiped). When not held, every field but ``active``
    is ``None``.
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


class AccountFreezeState(BaseModel):
    """The only two durable account-freeze outcomes allowed by ADR 0030."""

    model_config = ConfigDict(extra="forbid")

    active: bool = False
    category: AccountFreezeCategory | None = None
    explanation: str | None = None
    next_step: str | None = None
    observed_at_ms: int | None = None

    @model_validator(mode="after")
    def _payload_matches_active_state(self) -> AccountFreezeState:
        payload = (
            self.category,
            self.explanation,
            self.next_step,
            self.observed_at_ms,
        )
        if self.active and any(value is None for value in payload):
            raise ValueError("active account freeze requires its full authored payload")
        if not self.active and any(value is not None for value in payload):
            raise ValueError("inactive account freeze cannot carry freeze payload")
        return self


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
    freeze: AccountFreezeState = Field(default_factory=AccountFreezeState)
    latest_reconciliation: ReconciliationSummary | None = None
    outstanding_intents: int
    observed_at_ms: int
    # S4 (#1262): both submission-gate channel healths, each with its own
    # observation time. ``None`` = the stream-health gate is not installed
    # (distinct from "installed and healthy").
    channel_healths: list[ChannelHealth] | None = None


class InstanceCustodyProof(BaseModel):
    """Fresh Clerk proof used by STOP/Resume lifecycle custody."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str
    strategy_instance_id: str
    reconciliation_verdict: ReconciliationVerdict
    freeze: AccountFreezeState
    exposure: dict[str, float]
    working_order_refs: tuple[str, ...] = ()
    unresolved_intent_refs: tuple[str, ...] = ()
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
    status: Literal["acked", "failed"]
    owned: bool
    # ``order_ref`` present only when the canceled order was owned (resolved from
    # the journal); the operator can then find the originating intent.
    order_ref: str | None = None
    error: OrderLegError | None = None
