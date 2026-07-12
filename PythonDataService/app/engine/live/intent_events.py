"""Shared vocabulary for the durable submit protocol (ADR-0008, PRD #446).

The intent ledger's event types and the single append-only WAL record. This
module is **pure data**: no I/O, no broker, no filesystem. ``IntentWal``
writes these, ``intent_ledger`` folds them, and ``submit_state_machine`` /
``reconciliation_classifier`` branch on them.

Every event carries a per-run, strictly-monotonic ``seq`` — the fold cursor
(persisted as ``last_intent_wal_seq`` on the projection). ``ts_ms`` is recorded
as human-facing provenance and is **never** the fold boundary: wall-clock can
collide, drift, or reorder around fsync (ADR-0008 §3, §5).

``intent_kind`` and ``reason`` are human-readable provenance only — ownership
must **never** branch on them (ADR-0008 §1).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# PR 3 / operator-notice — typed reason carried on INTENT_DROPPED_BEFORE_SUBMIT.
# Kept as a module-level type alias so callers can annotate without importing
# the full event model.
DropReason = Literal[
    "operator_paused",
    "control_plane_lease_lost",
    "submissions_blocked",
    "max_orders_per_day",
    "broker_safety_halt",
    "account_truth_block",
    "account_freeze_block",
    "account_registry_block",
    "session_policy_block",
]


class IntentEventType(StrEnum):
    """The submit-lifecycle states (ADR-0008 §3).

    ``SIZING_RESOLVED`` (ADR 0009 § 11) is an **audit-trail** event, not a
    submit-lifecycle transition: it captures the sizing decision the engine
    made for a given intent before the broker call. It is appended **before**
    ``SUBMITTED`` / ``ACK_FAILED_UNCERTAIN`` and is **never** considered an
    unresolved submit state — the fold treats it as informational.

    ``INTENT_DROPPED_BEFORE_SUBMIT`` (PR 3 / operator-notice) records that a
    pending intent was evicted from memory by a submission gate (operator
    pause, lease-lost block, max-orders cap, or broker-safety halt) BEFORE
    it ever reached ``PENDING_INTENT``. This makes silent drops auditable via
    the existing WAL fold path. The event carries a typed ``drop_reason`` so
    the publisher can classify the cause without string-matching.
    """

    PENDING_INTENT = "PENDING_INTENT"
    SUBMITTED = "SUBMITTED"
    ACK_FAILED_UNCERTAIN = "ACK_FAILED_UNCERTAIN"
    SUBMITTED_RECOVERED = "SUBMITTED_RECOVERED"
    INTENT_NOT_ACCEPTED = "INTENT_NOT_ACCEPTED"
    SUBMIT_UNCERTAIN_HALTED = "SUBMIT_UNCERTAIN_HALTED"
    ADOPTED_BROKER_ORDER = "ADOPTED_BROKER_ORDER"
    # ADR 0009 § 11 — audit-only sizing decision record. Joins each broker
    # fill back to the live_config.sizing rule that produced its order, so
    # the Sizing card's per-trade audit list can render the rule, intended
    # qty, reference price, and sizing_provenance at resolve time.
    SIZING_RESOLVED = "SIZING_RESOLVED"
    # PR 3 / operator-notice — drop audit record. Emitted by the bar loop at
    # each submission gate that discards intents silently today.
    INTENT_DROPPED_BEFORE_SUBMIT = "INTENT_DROPPED_BEFORE_SUBMIT"


class IntentKind(StrEnum):
    """Human-readable provenance only. Ownership must NEVER branch on this."""

    STRATEGY = "STRATEGY"
    RECOVERY_FLATTEN = "RECOVERY_FLATTEN"
    SHUTDOWN_FLATTEN = "SHUTDOWN_FLATTEN"
    FORCE_FLAT = "FORCE_FLAT"
    EMERGENCY_FLATTEN = "EMERGENCY_FLATTEN"


class IntentEvent(BaseModel):
    """One append-only WAL record on an intent's submit lifecycle.

    The ``order_ref == f"{bot_order_namespace}:{intent_id}"`` invariant is
    enforced here (ADR-0008 §1): the three are stored separately and their
    equality is validated, so a reader never has to trust a parse.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1, description="Per-run strictly-monotonic WAL sequence; the fold cursor.")
    event_type: IntentEventType
    intent_id: str = Field(min_length=1)
    bot_order_namespace: str = Field(min_length=1)
    order_ref: str = Field(min_length=1)

    intent_kind: IntentKind = IntentKind.STRATEGY
    reason: str | None = None

    # Broker-echoed ids, populated as they arrive. None until known.
    order_id: int | None = None
    perm_id: int | None = None
    exec_id: str | None = None

    # Intended order details, carried on PENDING_INTENT so a provably-absent
    # retry can re-place the SAME intent. Opaque to the pure modules.
    order_spec: dict[str, Any] | None = None

    # ADR 0009 § 11 — sizing-decision payload, populated ONLY on
    # ``SIZING_RESOLVED`` events. All fields are optional on the type so
    # other event types parse cleanly with extra="forbid". The fold
    # surfaces them as ``submitted_orders[intent_id].sizing_resolution``.
    # ``reference_price`` is stored as a decimal string (never a float —
    # wire/storage rule for money values).
    # ``symbol`` (VCR-0003 PR A) is carried so the Sizing card's per-trade
    # audit fold can survive a restart with the symbol column populated; old
    # WAL events without it fold to ``symbol=""``.
    policy_kind: str | None = None
    policy_value: str | None = None
    intended_qty: int | None = None
    reference_price: str | None = None
    sizing_provenance_at_resolve_time: str | None = None
    sized_via: str | None = None
    symbol: str | None = None

    # PR 3 / operator-notice — populated ONLY on INTENT_DROPPED_BEFORE_SUBMIT.
    # The model validator below enforces the biconditional:
    #   event_type == INTENT_DROPPED_BEFORE_SUBMIT  <=>  drop_reason is not None
    # All other event types must have drop_reason = None.
    drop_reason: DropReason | None = None

    # Human-facing provenance. NEVER the fold cursor (use seq). Bounded to
    # int64 ms UTC: it is serialized into the WAL, so it must honor the repo's
    # int64-ms boundary contract rather than accept an arbitrary-width Python int.
    ts_ms: int | None = Field(
        default=None,
        ge=0,
        le=9_223_372_036_854_775_807,
        description="int64 ms UTC epoch timestamp (provenance only).",
    )

    # Reviewer finding 2: process wall-clock at WAL append time (NOT bar time).
    # ``ts_ms`` for SIZING_RESOLVED events carries the STRATEGY BAR TIMESTAMP
    # (set_holdings(..., time)), which can precede the engine process start in
    # delayed live feeds or historical runs. ``appended_at_ms`` is populated by
    # IntentWal.append() using time.time_ns() // 1_000_000 so it is always in
    # the same time domain as ``legacy_sizing_only_cutoff_ms`` (engine_started_at_ms).
    # Backward-compat: events on disk before this field was added parse with
    # ``appended_at_ms=None``; the fold treats None as pre-cutoff (safe default
    # — publisher will classify those as legacy and not double-report).
    appended_at_ms: int | None = Field(
        default=None,
        ge=0,
        le=9_223_372_036_854_775_807,
        description="Process wall-clock ms at WAL append time (not bar time).",
    )

    @model_validator(mode="after")
    def _check_order_ref_invariant(self) -> IntentEvent:
        expected = f"{self.bot_order_namespace}:{self.intent_id}"
        if self.order_ref != expected:
            raise ValueError(
                f"order_ref {self.order_ref!r} != {expected!r} "
                "(namespace:intent_id invariant, ADR-0008 §1)"
            )
        return self

    @model_validator(mode="after")
    def _check_drop_reason_invariant(self) -> IntentEvent:
        """drop_reason is present iff event_type is INTENT_DROPPED_BEFORE_SUBMIT."""
        is_drop = self.event_type is IntentEventType.INTENT_DROPPED_BEFORE_SUBMIT
        has_reason = self.drop_reason is not None
        if is_drop and not has_reason:
            raise ValueError(
                "drop_reason must be set for INTENT_DROPPED_BEFORE_SUBMIT events"
            )
        if not is_drop and has_reason:
            raise ValueError(
                f"drop_reason must be None for {self.event_type!r} events "
                "(only INTENT_DROPPED_BEFORE_SUBMIT carries a drop_reason)"
            )
        return self


__all__ = ["DropReason", "IntentEvent", "IntentEventType", "IntentKind"]
