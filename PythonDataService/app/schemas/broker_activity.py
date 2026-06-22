"""Broker-activity reconciliation schemas (ADR 0014).

The wire and storage models for the cockpit Activity tab's broker-authored
row stream. Every row is one IBKR execution (or one engine-only-pending
intent), joined to engine state via ``order_ref``, classified into a
four-value ``Verdict``, and authored into ``headline`` + ``narrative``
by a versioned template.

Per ADR 0014:

- All timestamps are ``int64`` ms UTC (numerical-rigor invariant).
- Every operator-facing string is produced by a versioned template
  (deterministic-pure function of the row's structured facts).
- The row carries BOTH structured facts AND the rendered strings —
  historical operator view is reproducible from the row alone.
- ``Verdict`` is a closed four-value enum; forensic detail lives in
  ``reason_codes`` and the ``divergence_facts`` drill-down, not in
  additional verdict cardinality.

The frontend renders these models verbatim. It MUST NOT compose its
own headline / narrative / verdict from sub-fields — that surface
is the truthfulness boundary the publisher owns.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

OrderSide = Literal["BUY", "SELL"]


class Verdict(StrEnum):
    """The four-value operator-facing verdict on a broker-activity row.

    Cardinality is intentionally closed at four. Forensic detail (which
    divergence categories fired, how lag broke down by phase) lives in
    the row's ``reason_codes`` and ``divergence_facts``, not in extra
    verdict values.
    """

    EXPECTED = "expected"
    EXPECTED_WITH_CAVEAT = "expected_with_caveat"
    UNEXPECTED = "unexpected"
    ENGINE_ONLY_PENDING = "engine_only_pending"


class ReasonCode(StrEnum):
    """Closed-vocabulary classification of *why* a row got its verdict.

    Drives template selection (``select_template``) and surfaces in the
    drill-down. Adding a new value requires a code change *and* a
    matching template; templates may not reference reasons outside this
    enum.
    """

    # Happy paths
    NORMAL_FILL = "normal_fill"
    PENDING_ACKNOWLEDGEMENT = "pending_acknowledgement"

    # Within-tolerance caveats
    PARTIAL_FILL = "partial_fill"
    TIMING_CAVEAT = "timing_caveat"
    RECONNECT_RECOVERY = "reconnect_recovery"
    MISSING_COMMISSION = "missing_commission"

    # Divergences that demand attention
    PRICE_DIVERGENCE = "price_divergence"
    QUANTITY_DIVERGENCE = "quantity_divergence"
    UNMATCHED_EXECUTION = "unmatched_execution"
    DUPLICATE_EXECUTION = "duplicate_execution"
    CANCELLATION = "cancellation"
    REJECTION = "rejection"


class LagBreakdown(BaseModel):
    """The four phases of intent-to-observation latency.

    Operator-facing chip surfaces a single number (``intent_to_exec_ms``,
    the decision-to-trade lag). The full phase breakdown lives in
    drill-down. Each phase is ``int64`` ms; ``None`` when the phase's
    bounding timestamps are not both available (e.g. the engine has
    no record of an intent for a foreign exec, so all phases are
    ``None``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    intent_to_dispatch_ms: int | None = None
    dispatch_to_ack_ms: int | None = None
    ack_to_exec_ms: int | None = None
    exec_to_observed_ms: int | None = None

    # Derived convenience: the operator-facing chip's number. Stored
    # explicitly so the row is self-describing and so frontend can render
    # without arithmetic.
    intent_to_exec_ms: int | None = None


class SizingProvenance(BaseModel):
    """The subset of ``LiveStateEnvelope.sizing_resolutions`` carried as
    overlay on a broker-activity row. Captures *why* the engine sized
    this intent the way it did, so the operator can answer "did the
    engine intend exactly this trade?" from the row drill-down.

    Mirrors the fields ``SizingAuditTableComponent`` rendered before
    its deletion — provenance survives, just under the canonical
    broker-activity row instead of a parallel surface.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy: str | None = None  # e.g. "SetHoldings", "ExplicitMarketOrder"
    requested_qty: float | None = None
    reference_price_decimal_str: str | None = None
    provenance: str | None = None  # "reference_native" / "live_override" / …
    surface: str | None = None  # which order-submission surface fired
    skip_reason: str | None = None  # populated only if the intent was skipped


class EngineOverlay(BaseModel):
    """Engine-side context joined onto the broker-authored row.

    ``None``-valued for foreign executions (no matching engine intent,
    by namespace). For matched executions, carries the deterministic
    intent identity and sizing provenance so the operator can answer
    "what did the engine think it was doing?" without leaving the row.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    intent_id: str | None = None
    mutation_attempt_id: str | None = None  # join to durable mutation log
    requested_qty: float | None = None
    requested_price: float | None = None  # None for market orders
    sizing_provenance: SizingProvenance | None = None
    lag_breakdown: LagBreakdown = Field(default_factory=LagBreakdown)


class DivergenceFacts(BaseModel):
    """Structured facts that justify a non-``EXPECTED`` verdict.

    Every value here must be derivable from the broker event and the
    engine overlay alone — no speculation, no inferred state. Templates
    that reference fields here must reference only fields present;
    absent context means the template cannot be selected.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    price_delta: float | None = None  # broker_price − engine_requested_price
    quantity_delta: float | None = None  # broker_qty − engine_requested_qty
    lag_total_ms: int | None = None
    # Free-form structured context (e.g. {"reconnect_window_start_ms": ...,
    # "reconnect_window_end_ms": ...}). Templates whitelist the keys
    # they consume; unknown keys are ignored at render time and surfaced
    # in drill-down as-is.
    window_context: dict[str, int | str | float] = Field(default_factory=dict)


class BrokerActivityRow(BaseModel):
    """One row in the cockpit Activity tab's broker-activity stream.

    Persisted verbatim to ``broker_activity.jsonl`` and emitted verbatim
    on the SSE channel. The frontend renders this model as-is; it MUST
    NOT compose its own headline / narrative / verdict.

    Per ADR 0014 §3, the row carries both the structured facts and the
    rendered strings. The authored strings are frozen at write time;
    template improvements never re-render history.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # ── WAL identity ────────────────────────────────────────────────
    seq: int = Field(ge=0, description="Per-instance monotonic WAL sequence")
    ts_ms: int = Field(gt=0, description="Wall-clock observation time (int64 ms UTC)")

    # ── Broker-recognisable columns (CP Trades mirror) ─────────────
    # ``None`` for ``ENGINE_ONLY_PENDING`` rows (no exec yet).
    exec_id: str | None = None
    perm_id: int | None = None
    # ``order_ref`` is the namespace join key; ``None`` for foreign execs.
    order_ref: str | None = None
    symbol: str
    side: OrderSide
    quantity: float
    price: float | None = None
    commission: float | None = None
    net_amount: float | None = None
    order_type: str  # "MKT" | "LMT" | …
    # Exchange-stamped fill time; ``None`` for pending rows.
    exec_ts_ms: int | None = None

    # ── Authored output (frozen at write time) ──────────────────────
    verdict: Verdict
    template_key: str
    template_version: int = Field(ge=1)
    headline: str
    narrative: str
    reason_codes: tuple[ReasonCode, ...] = Field(default_factory=tuple)

    # ── Drill-down structured facts ─────────────────────────────────
    engine_overlay: EngineOverlay | None = None
    divergence_facts: DivergenceFacts | None = None


class BrokerActivityPage(BaseModel):
    """Paginated REST backfill response.

    The SSE channel pushes live increments; this endpoint serves
    cold-start backfill from the WAL. Pagination by ``seq`` (the
    monotonic per-instance counter) avoids time-window edge cases
    around the SSE/REST handoff.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: list[BrokerActivityRow]
    next_seq: int | None = Field(
        default=None,
        description=(
            "Lowest ``seq`` not yet returned. ``None`` when the page "
            "drained the WAL — caller can resume from the SSE channel."
        ),
    )


class ReconciliationTimingPolicy(BaseModel):
    """Per-instance lag-driven verdict thresholds (ADR 0014 §6).

    Lives on the strategy-instance configuration so different strategies
    can carry different latency expectations without forcing universal
    constants. Conservative defaults ship in the schema; an instance's
    config overrides explicitly.

    A high-lag execution with a *known* explanation (e.g. captured during
    a reconnect window) renders as ``EXPECTED_WITH_CAVEAT`` via the
    reconnect-recovery template, NOT ``UNEXPECTED`` — verdicts depend on
    what the publisher knows, not the raw clock alone.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    caveat_lag_ms: int = Field(
        default=2_000,
        gt=0,
        description=(
            "intent-to-exec lag (ms) above which the row earns a "
            "``timing_caveat`` reason and verdict promotes to "
            "``expected_with_caveat`` (absent a more specific reason)."
        ),
    )
    excessive_lag_ms: int = Field(
        default=10_000,
        gt=0,
        description=(
            "intent-to-exec lag (ms) above which the row's verdict is "
            "``unexpected`` UNLESS the publisher has authored a known "
            "explanation (reconnect window, etc.). Must be strictly "
            "greater than ``caveat_lag_ms``."
        ),
    )

    @model_validator(mode="after")
    def _validate_lag_ordering(self) -> ReconciliationTimingPolicy:
        if self.excessive_lag_ms <= self.caveat_lag_ms:
            raise ValueError(
                "reconciliation_timing_policy.excessive_lag_ms "
                f"({self.excessive_lag_ms}) must be strictly greater "
                f"than caveat_lag_ms ({self.caveat_lag_ms})"
            )
        return self
