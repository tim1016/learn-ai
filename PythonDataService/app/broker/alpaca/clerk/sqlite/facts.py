"""Typed transition facts — corrective foundation slice, Scope A2.

Every registered fold parses ``facts_json`` into one of these dataclasses,
never an untyped bag. Each one carries exactly the immutable inputs the fold
needs to rebuild an identical ``commands`` row that are *not* already present
as ``custody_transitions`` columns (``strategy_instance_id``, ``run_id``,
``command_id`` are transition columns already; ``idempotency_key``,
``payload_hash``, ``kind``, ``action``, ``intended_end_state`` are not, so
they must round-trip through facts). A finalized mirror line — payload plus
this facts string — is therefore sufficient to reconstruct the command
resource on rebuild without a live database or a caller closure.

``ENTER_ACCEPTED`` facts are pinned in
``docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md`` §3.d; #1377 adds
the dataclass here alongside the fold that consumes it
(``folds._fold_enter_accepted``). The evidence-fold kinds that follow it
(``ORDER_SUBMIT_UNCERTAIN``, ``ORDER_SUBMIT_FAILED``, ``ORDER_FILL_OBSERVED``)
only ever *update* an already-existing ``effect_operations``/``orders`` row —
none of their fields are needed to rebuild that row's identity — but §3d's
"no untyped snapshot bag" rule still applies to whatever they *do* carry
beyond outer transition columns, so those get typed dataclasses too.
``ORDER_SUBMIT_ACKED`` is the one exception: every field its fold reads
(``broker_order_id``, ``broker_state``, ``source_event_at_ms``) is already an
outer ``custody_transitions`` column, so its ``facts_json`` is legitimately
``{}`` — there is nothing left to type.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any

from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize

FACTS_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RunStartedFacts:
    idempotency_key: str
    payload_hash: str
    kind: str
    action: str
    intended_end_state: str
    lifecycle_run_id: str
    operator_reason: str | None = None

    def to_facts_json(self) -> str:
        return canonicalize(asdict(self))

    @classmethod
    def from_facts_json(cls, facts_json: str) -> RunStartedFacts:
        return cls(**json.loads(facts_json))


@dataclass(frozen=True)
class RunStoppedFacts:
    idempotency_key: str
    payload_hash: str
    kind: str
    action: str
    intended_end_state: str
    lifecycle_run_id: str
    operator_reason: str | None = None

    def to_facts_json(self) -> str:
        return canonicalize(asdict(self))

    @classmethod
    def from_facts_json(cls, facts_json: str) -> RunStoppedFacts:
        return cls(**json.loads(facts_json))


@dataclass(frozen=True)
class CommandRejectedFacts:
    idempotency_key: str
    payload_hash: str
    kind: str
    action: str
    intended_end_state: str
    reason_code: str
    operator_reason: str | None = None

    def to_facts_json(self) -> str:
        return canonicalize(asdict(self))

    @classmethod
    def from_facts_json(cls, facts_json: str) -> CommandRejectedFacts:
        return cls(**json.loads(facts_json))


@dataclass(frozen=True)
class EnterAcceptedFacts:
    """§3d's ``ENTER_ACCEPTED`` row: command idempotency key/hash/kind/action,
    the decision id, the effect idempotency key/kind, and the complete
    immutable broker leg — everything ``_fold_enter_accepted`` needs to
    rebuild the ``commands``, ``effect_operations``, and ``orders`` rows from
    a finalized mirror line alone."""

    idempotency_key: str
    payload_hash: str
    kind: str
    action: str
    intended_end_state: str | None
    effect_idempotency_key: str
    effect_kind: str
    decision_id: str
    # Already-validated ``BrokerOrderLeg.model_dump(mode="json")`` — kept as a
    # plain dict, not re-typed as a nested ``BrokerOrderLeg`` field, because
    # ``asdict()`` (this module's uniform (de)serialization path) only
    # recurses into stdlib dataclasses, not Pydantic models; storing the
    # already-dumped, already-validated leg is the one shape that keeps every
    # facts dataclass here going through the same to/from_facts_json pair.
    leg: dict[str, Any]

    def to_facts_json(self) -> str:
        return canonicalize(asdict(self))

    @classmethod
    def from_facts_json(cls, facts_json: str) -> EnterAcceptedFacts:
        return cls(**json.loads(facts_json))


@dataclass(frozen=True)
class OrderSubmitUncertainFacts:
    """A lost/timed-out broker response — R4's ``why`` explanation."""

    why: str

    def to_facts_json(self) -> str:
        return canonicalize(asdict(self))

    @classmethod
    def from_facts_json(cls, facts_json: str) -> OrderSubmitUncertainFacts:
        return cls(**json.loads(facts_json))


@dataclass(frozen=True)
class OrderSubmitFailedFacts:
    """A definitive broker rejection, or an absence proven past the R4
    uncertainty grace window."""

    reason: str
    why: str

    def to_facts_json(self) -> str:
        return canonicalize(asdict(self))

    @classmethod
    def from_facts_json(cls, facts_json: str) -> OrderSubmitFailedFacts:
        return cls(**json.loads(facts_json))


@dataclass(frozen=True)
class ExitAcceptedFacts:
    """``EXIT_ACCEPTED`` (#1379): command idempotency key/hash/kind/action,
    the decision id, the effect idempotency key, the primary
    ``entry_order_ref``, and every captured sibling reference — everything
    ``_fold_exit_accepted`` needs to rebuild the command, operation, and
    immutable-provenance custody links from a finalized mirror line. Unlike
    ``EnterAcceptedFacts``, no ``leg`` is captured here: the reducing
    order's side/quantity aren't known at acceptance time (they depend on
    how the entry's cancellation resolves), so there is nothing immutable
    about them to snapshot yet — the eventual reducing order's shape is
    captured by ``ExitReducingOrderCreatedFacts`` once it's actually
    computed."""

    idempotency_key: str
    payload_hash: str
    kind: str
    action: str
    intended_end_state: str | None
    effect_idempotency_key: str
    effect_kind: str
    decision_id: str
    entry_order_ref: str
    entry_order_refs: list[str]

    def to_facts_json(self) -> str:
        return canonicalize(asdict(self))

    @classmethod
    def from_facts_json(cls, facts_json: str) -> ExitAcceptedFacts:
        return cls(**json.loads(facts_json))


@dataclass(frozen=True)
class ExitReducingOrderCreatedFacts:
    """``EXIT_REDUCING_ORDER_CREATED`` (#1379): the immutable inputs
    ``_fold_exit_reducing_order_created`` needs to create the ``orders`` row
    for the reducing/close order — symbol and side are needed to place the
    order; ``quantity`` is the Clerk-proven remaining attributed quantity at
    the moment cancellation resolved (the acceptance criterion this fact
    exists to prove — see ``docs/references/clerk-exit-reducing-quantity.md``)."""

    symbol: str
    side: str
    quantity: float

    def to_facts_json(self) -> str:
        return canonicalize(asdict(self))

    @classmethod
    def from_facts_json(cls, facts_json: str) -> ExitReducingOrderCreatedFacts:
        return cls(**json.loads(facts_json))


@dataclass(frozen=True)
class ReconciliationAttemptedFacts:
    """A reconciliation pass's own attempt to resolve one uncertain order
    (#1378) — beyond the outer transition row (``effect_operation_id``,
    ``order_ref``), the durable audit needs what drove the attempt and what
    it concluded; ``why`` carries the human-readable reasoning the paired
    ``reconciliations`` row's ``outcome`` column doesn't have room for."""

    trigger: str  # 'AUTOMATIC' | 'OPERATOR_RECONCILE_NOW'
    outcome: str  # 'STILL_UNKNOWN' | 'RESOLVED_SUCCESS' | 'RESOLVED_FAILURE'
    why: str

    def to_facts_json(self) -> str:
        return canonicalize(asdict(self))

    @classmethod
    def from_facts_json(cls, facts_json: str) -> ReconciliationAttemptedFacts:
        return cls(**json.loads(facts_json))


@dataclass(frozen=True)
class AccountHoldRaisedFacts:
    """An ``ACCOUNT_CLERK``-scoped hold raised by reconciliation (#1378) —
    currently only the unexplained/foreign-order reason. ``evidence_refs`` is
    the broker-assigned ``order_id`` of every foreign order observed, not the
    (possibly absent) ``client_order_id`` — a genuinely foreign order may
    have no ``client_order_id`` at all."""

    reason_code: str
    evidence_refs: list[str]

    def to_facts_json(self) -> str:
        return canonicalize(asdict(self))

    @classmethod
    def from_facts_json(cls, facts_json: str) -> AccountHoldRaisedFacts:
        return cls(**json.loads(facts_json))


@dataclass(frozen=True)
class AccountHoldResolvedFacts:
    """Evidence-backed closure of one Account Clerk hold episode."""

    reason_code: str
    evidence_refs: list[str]

    def to_facts_json(self) -> str:
        return canonicalize(asdict(self))

    @classmethod
    def from_facts_json(cls, facts_json: str) -> AccountHoldResolvedFacts:
        return cls(**json.loads(facts_json))


@dataclass(frozen=True)
class ExternalOrderObservedFacts:
    """A complete immutable observation of a broker order outside bot custody."""

    external_order_id: str
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: str
    qty: float
    price: float | None
    observed_at_ms: int
    evidence_refs: list[str]

    def to_facts_json(self) -> str:
        return canonicalize(asdict(self))

    @classmethod
    def from_facts_json(cls, facts_json: str) -> ExternalOrderObservedFacts:
        return cls(**json.loads(facts_json))


@dataclass(frozen=True)
class ExternalOrderAcknowledgedFacts:
    """Operator review of one durable external-order observation only."""

    external_order_id: str
    ack_operator: str

    def to_facts_json(self) -> str:
        return canonicalize(asdict(self))

    @classmethod
    def from_facts_json(cls, facts_json: str) -> ExternalOrderAcknowledgedFacts:
        return cls(**json.loads(facts_json))


@dataclass(frozen=True)
class UncertaintyRaisedFacts:
    """``UNCERTAINTY_RAISED`` (#1380): the R5 envelope, minus what's already
    an outer ``custody_transitions``/``uncertainties`` column —
    ``strategy_instance_id`` is already an outer transition column, and
    ``uncertainties.scope`` is derived from it being non-null (``BOT``) or
    null (``ACCOUNT_CLERK``), the same truthful scope/identity coupling
    ``holds`` already uses. Every other envelope field (severity, the two
    independent admission axes, and the four backend-authored operator-
    facing strings) has nowhere else to live, so it's typed here."""

    severity: str
    blocks_new_exposure: bool
    allows_reduction: bool
    reason_code: str
    headline: str
    explanation: str
    operator_impact: str
    next_step: str
    evidence_refs: list[str]
    cause_facts: dict[str, Any] = field(default_factory=dict)

    def to_facts_json(self) -> str:
        return canonicalize(asdict(self))

    @classmethod
    def from_facts_json(cls, facts_json: str) -> UncertaintyRaisedFacts:
        return cls(**json.loads(facts_json))


@dataclass(frozen=True)
class UncertaintyResolvedFacts:
    """``UNCERTAINTY_RESOLVED`` (#1380): which uncertainty resolved, and why —
    ``uncertainty_id`` is not an outer transition column (unlike
    ``order_ref``/``effect_operation_id``), so it must round-trip through
    facts for the fold to know which ``uncertainties`` row to close."""

    uncertainty_id: str
    resolution_kind: str
    evidence_refs: list[str]

    def to_facts_json(self) -> str:
        return canonicalize(asdict(self))

    @classmethod
    def from_facts_json(cls, facts_json: str) -> UncertaintyResolvedFacts:
        return cls(**json.loads(facts_json))


@dataclass(frozen=True)
class OrderFillObservedFacts:
    """The evidence ``_fold_order_fill_observed`` needs beyond the outer
    transition row: Alpaca's REST-reported *cumulative* ``filled_quantity``
    for the order, from which the fold derives both the fill delta and its
    idempotent identity (see the fold's own docstring for why)."""

    symbol: str
    side: str
    cumulative_filled_quantity: float
    avg_price: float
    is_correction: bool

    def to_facts_json(self) -> str:
        return canonicalize(asdict(self))

    @classmethod
    def from_facts_json(cls, facts_json: str) -> OrderFillObservedFacts:
        return cls(**json.loads(facts_json))


@dataclass(frozen=True)
class ExecutionSliceFilledFacts:
    """One immutable broker execution slice, never an order-level total.

    ``execution_id`` is the broker's durable identity and becomes both the
    capture-path ``fills.execution_id`` and ``fill_id``.  Fees are nullable
    because Alpaca trade-update frames do not currently report them; the
    accompanying fidelity flag makes that absence explicit rather than
    fabricating a zero fee.
    """

    execution_id: str
    symbol: str
    side: str
    slice_qty: float
    slice_price: float
    fee: float | None
    fee_fidelity: str
    evidence_source: str
    source_event_at_ms: int

    def to_facts_json(self) -> str:
        return canonicalize(asdict(self))

    @classmethod
    def from_facts_json(cls, facts_json: str) -> ExecutionSliceFilledFacts:
        return cls(**json.loads(facts_json))


@dataclass(frozen=True)
class ExecutionCorrectedFacts:
    """An auditable effective replacement for one prior execution slice.

    The correction's ``execution_id`` is a distinct broker identity.  The
    superseded row remains in ``fills`` so every exposure adjustment can be
    reconstructed from the custody transition stream.
    """

    execution_id: str
    superseded_execution_ref: str
    symbol: str
    side: str
    corrected_qty: float
    corrected_price: float
    why: str

    def to_facts_json(self) -> str:
        return canonicalize(asdict(self))

    @classmethod
    def from_facts_json(cls, facts_json: str) -> ExecutionCorrectedFacts:
        return cls(**json.loads(facts_json))


def _require_finite_positive(value: float, *, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{field} must be finite and positive")


def _require_finite_nonnegative(value: float, *, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{field} must be finite and non-negative")


def validate_execution_slice_facts(facts: ExecutionSliceFilledFacts) -> None:
    """Reject malformed broker-execution inputs before a custody transition.

    Formula: n/a — input-boundary validation for the execution-slice fold.
    Reference: docs/prds/2026-08-10-sqlite-sole-authority-alpaca-execution.md
      § Task S1.2.
    Canonical implementation: this file.
    Validated against: PythonDataService/tests/broker/alpaca/clerk/sqlite/
      test_folds_execution.py::test_execution_slice_filled_rejects_invalid_facts.
    """
    if not isinstance(facts.execution_id, str) or not facts.execution_id:
        raise ValueError("execution_id must be non-empty")
    if not isinstance(facts.symbol, str) or not facts.symbol:
        raise ValueError("symbol must be non-empty")
    if facts.side not in {"BUY", "SELL"}:
        raise ValueError(f"invalid execution side {facts.side!r}")
    if facts.evidence_source not in {"websocket", "activity_recovery"}:
        raise ValueError(f"invalid exact-execution evidence source {facts.evidence_source!r}")
    if facts.fee_fidelity not in {"reported", "not_reported"}:
        raise ValueError(f"invalid fee_fidelity {facts.fee_fidelity!r}")
    _require_finite_positive(facts.slice_qty, field="slice_qty")
    _require_finite_positive(facts.slice_price, field="slice_price")
    if facts.fee_fidelity == "reported":
        if facts.fee is None:
            raise ValueError("reported fee_fidelity requires fee")
        _require_finite_nonnegative(facts.fee, field="fee")
    elif facts.fee is not None:
        raise ValueError("not_reported fee_fidelity requires fee=None")
    if isinstance(facts.source_event_at_ms, bool) or not isinstance(facts.source_event_at_ms, int):
        raise ValueError("source_event_at_ms must be an int64 millisecond timestamp")


def validate_execution_corrected_facts(facts: ExecutionCorrectedFacts) -> None:
    """Reject malformed broker correction inputs before transition admission.

    Formula: n/a — input-boundary validation for replacement execution facts.
    Reference: docs/prds/2026-08-10-sqlite-sole-authority-alpaca-execution.md
      § Task S1.2.
    Canonical implementation: this file.
    Validated against: PythonDataService/tests/broker/alpaca/clerk/sqlite/
      test_folds_execution.py::test_execution_correction_invalid_target_raises_uncertainty.
    """
    if not isinstance(facts.execution_id, str) or not facts.execution_id:
        raise ValueError("correction execution_id must be non-empty")
    if not isinstance(facts.superseded_execution_ref, str) or not facts.superseded_execution_ref:
        raise ValueError("superseded_execution_ref must be non-empty")
    if facts.execution_id == facts.superseded_execution_ref:
        raise ValueError("a correction cannot supersede itself")
    if not isinstance(facts.symbol, str) or not facts.symbol:
        raise ValueError("correction symbol must be non-empty")
    if facts.side not in {"BUY", "SELL"}:
        raise ValueError(f"invalid correction side {facts.side!r}")
    _require_finite_positive(facts.corrected_qty, field="corrected_qty")
    _require_finite_positive(facts.corrected_price, field="corrected_price")
    if not isinstance(facts.why, str) or not facts.why:
        raise ValueError("correction why must be non-empty")
