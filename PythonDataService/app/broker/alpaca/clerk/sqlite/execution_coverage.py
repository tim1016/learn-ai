"""Closed proof vocabulary for exact executions that overlap aggregate recovery.

Formula: Q = fsum(qty); C = fsum(qty × price); P = C / Q. The canonical
  set proof accepts only abs(Q_E - Q_R) < 1e-9 shares,
  abs(P_E - P_R) < 1e-9 currency/share, and
  abs(C_E - C_R) <= max(|Q_E|, |Q_R|) × 1e-9
  + max(|P_E|, |P_R|) × 1e-9 + 1e-18 currency.
Reference: Project-authored execution-coverage contract in PRD #1543, stories
  18, 19, 28, and 33; this is authored project logic, not a reused proof.
Canonical implementation: this file's prove_execution_coverage_set. The
  direct exact-to-one-cumulative replacement is consumed by the #1554 Clerk
  fold; the existing exact_replaces_cumulative remains the temporary S0
  operator proof.
Validated against: tests/broker/alpaca/clerk/sqlite/test_execution_coverage_set_proof.py

The existing typed query below continues to describe the shipped S0 operator
flow. The direct automatic fold reuses this proof without changing that
operator path. Accumulated quarantined-exact reconciliation remains a later
slice, so keeping both vocabularies together preserves the temporary boundary
for audit.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict, dataclass, field
from enum import StrEnum

from app.broker.alpaca.clerk.sqlite.facts import (
    ExecutionCoverageQuarantinedFacts,
    ExecutionSliceFilledFacts,
    UncertaintyRaisedFacts,
    validate_execution_coverage_quarantined_facts,
    validate_execution_slice_facts,
)
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
    ExecutionCoverageConflictCause,
)

#: Numerical-rigor tolerance for an exact slice that replaces an aggregate
#: recovery row. See ``docs/references/clerk-fill-quantity-tolerance.md``.
FILL_QTY_EPSILON = 1e-9

#: Set-proof quantity tolerance in shares. The comparison is intentionally strict.
QTY_ATOL = 1e-9

#: Set-proof gross-cost tolerance basis in currency units per share.
PRICE_ATOL = 1e-9


@dataclass(frozen=True)
class ExecutionCoverageIdentity:
    """The one authority and economic identity a proof may compare."""

    account_id: str
    authority_generation: int
    database_identity_token: str
    order_ref: str
    symbol: str
    side: str


@dataclass(frozen=True)
class CumulativeCoverageObservation:
    """One current cumulative-recovery row, named by immutable source identity."""

    source_id: str
    identity: ExecutionCoverageIdentity
    quantity: float
    price: float


@dataclass(frozen=True)
class ExactCoverageObservation:
    """One quarantined or incoming exact execution, retaining its exact fee."""

    source_id: str
    identity: ExecutionCoverageIdentity
    quantity: float
    price: float
    fee: float | None


@dataclass(frozen=True)
class ExecutionCoverageExactProvenance:
    """One prior quarantined exact's immutable custody observation clocks.

    The automatic supersession fold restores the effective fill from this
    record rather than assigning the later resolving transition's clocks. The
    two sequence fields intentionally agree: one names the custody transition
    that quarantined the source, while the other is persisted on the rebuilt
    fill for deterministic FIFO ordering when broker event timestamps tie.
    """

    exact_execution: ExecutionSliceFilledFacts
    observation_transition_sequence: int
    clerk_observed_at_ms: int
    recorded_at_ms: int
    recorded_transition_sequence: int


@dataclass(frozen=True)
class ExecutionCoverageSupersededFacts:
    """Automatic replacement of cumulative coverage by exact evidence.

    The cumulative source remains a prior custody transition; only its
    rebuildable ``fills`` contribution is replaced. ``exact_execution`` is
    the incoming trigger; ``prior_exact_observations`` retains every earlier
    quarantined exact and its original custody clocks. The fact records both
    proof aggregates and the authority revision that admitted the replacement
    so replay can independently reject a stale or broadened plan.
    """

    actor: str
    account_id: str
    authority_generation: int
    db_identity_token: str
    expected_control_revision: int
    order_ref: str
    symbol: str
    side: str
    superseded_cumulative_fill_ids: list[str]
    exact_execution: ExecutionSliceFilledFacts
    exact_quantity: float
    exact_gross_cost: float
    cumulative_quantity: float
    cumulative_gross_cost: float
    quantity_tolerance: float
    gross_cost_tolerance: float
    resolved_uncertainty_id: str | None
    evidence_refs: list[str]
    prior_exact_observations: list[ExecutionCoverageExactProvenance] = field(
        default_factory=list
    )

    def to_facts_json(self) -> str:
        value = asdict(self)
        value["exact_execution"] = json.loads(self.exact_execution.to_facts_json())
        if not self.prior_exact_observations:
            # Keep #1554's direct-transition representation byte-compatible.
            value.pop("prior_exact_observations")
        return canonicalize(value)

    @classmethod
    def from_facts_json(cls, facts_json: str) -> ExecutionCoverageSupersededFacts:
        value = json.loads(facts_json)
        if not isinstance(value, dict):
            raise ValueError("coverage supersession facts must be an object")
        try:
            value["exact_execution"] = ExecutionSliceFilledFacts.from_facts_json(
                canonicalize(value["exact_execution"])
            )
            value["prior_exact_observations"] = [
                ExecutionCoverageExactProvenance(
                    exact_execution=ExecutionSliceFilledFacts.from_facts_json(
                        canonicalize(item["exact_execution"])
                    ),
                    observation_transition_sequence=item["observation_transition_sequence"],
                    clerk_observed_at_ms=item["clerk_observed_at_ms"],
                    recorded_at_ms=item["recorded_at_ms"],
                    recorded_transition_sequence=item["recorded_transition_sequence"],
                )
                for item in value.pop("prior_exact_observations", [])
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("coverage supersession exact execution is invalid") from exc
        return cls(**value)


@dataclass(frozen=True)
class ExecutionCoverageSetCandidate:
    """All immutable observations required for one automatic set-proof attempt."""

    cumulative_recovery: tuple[CumulativeCoverageObservation, ...]
    prior_quarantined_exact: tuple[ExactCoverageObservation, ...]
    incoming_exact: ExactCoverageObservation
    active_episode_ids: tuple[str, ...] = ()
    effective_exact_source_ids: frozenset[str] = field(default_factory=frozenset)
    unreadable_source_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutionCoverageAggregate:
    """Deterministic aggregate economics, in shares and currency units."""

    quantity: float
    gross_cost: float
    vwap: float


class ExecutionCoverageSetProofRefusalReason(StrEnum):
    """Every fail-closed outcome the pure set proof can explain."""

    MISSING_CUMULATIVE_COVERAGE = "missing_cumulative_coverage"
    UNREADABLE_EVIDENCE = "unreadable_evidence"
    MULTIPLE_ACTIVE_EPISODES = "multiple_active_episodes"
    INVALID_SOURCE_IDENTITY = "invalid_source_identity"
    DUPLICATE_SOURCE_ID = "duplicate_source_id"
    EXACT_ALREADY_EFFECTIVE = "exact_already_effective"
    IDENTITY_MISMATCH = "identity_mismatch"
    NONFINITE_QUANTITY = "nonfinite_quantity"
    NONPOSITIVE_QUANTITY = "nonpositive_quantity"
    NONFINITE_PRICE = "nonfinite_price"
    NONFINITE_FEE = "nonfinite_fee"
    NONFINITE_GROSS_COST = "nonfinite_gross_cost"
    NONFINITE_AGGREGATE = "nonfinite_aggregate"
    QUANTITY_MISMATCH = "quantity_mismatch"
    VWAP_MISMATCH = "vwap_mismatch"
    COST_MISMATCH = "cost_mismatch"


@dataclass(frozen=True)
class ExecutionCoverageSetProofSuccess:
    """A fully explained no-position-delta replacement proof."""

    exact: ExecutionCoverageAggregate
    cumulative: ExecutionCoverageAggregate
    position_delta: float
    gross_cost_tolerance: float
    retained_exact_observations: tuple[ExactCoverageObservation, ...]


@dataclass(frozen=True)
class ExecutionCoverageSetProofRefusal:
    """A specific malformed-state or economic reason to retain quarantine."""

    reason: ExecutionCoverageSetProofRefusalReason
    detail: str


ExecutionCoverageSetProofResult = ExecutionCoverageSetProofSuccess | ExecutionCoverageSetProofRefusal


def prove_execution_coverage_set(
    candidate: ExecutionCoverageSetCandidate,
) -> ExecutionCoverageSetProofResult:
    """Prove whether complete cumulative and exact sets carry equal economics.

    Fees are deliberately absent from the arithmetic because cumulative recovery
    has no fee observation. The success result returns the exact observations
    unchanged, preserving any reported exact fees for the caller's fold.
    """
    if not candidate.cumulative_recovery:
        return _set_refusal(
            ExecutionCoverageSetProofRefusalReason.MISSING_CUMULATIVE_COVERAGE,
            "No cumulative-recovery rows are available for the proposed replacement.",
        )
    if candidate.unreadable_source_ids:
        return _set_refusal(
            ExecutionCoverageSetProofRefusalReason.UNREADABLE_EVIDENCE,
            "Immutable coverage evidence is unreadable: "
            + ", ".join(sorted(candidate.unreadable_source_ids)),
        )
    if len(candidate.active_episode_ids) > 1:
        return _set_refusal(
            ExecutionCoverageSetProofRefusalReason.MULTIPLE_ACTIVE_EPISODES,
            "More than one active execution-coverage uncertainty episode is incompatible with automatic resolution.",
        )

    exact = (*candidate.prior_quarantined_exact, candidate.incoming_exact)
    observations = candidate.cumulative_recovery + exact
    source_ids = tuple(observation.source_id for observation in observations)
    if any(not isinstance(source_id, str) or not source_id for source_id in source_ids):
        return _set_refusal(
            ExecutionCoverageSetProofRefusalReason.INVALID_SOURCE_IDENTITY,
            "Every cumulative and exact observation requires a non-empty source identity.",
        )
    if len(source_ids) != len(set(source_ids)):
        return _set_refusal(
            ExecutionCoverageSetProofRefusalReason.DUPLICATE_SOURCE_ID,
            "Coverage proof observations contain a duplicate immutable source identity.",
        )
    if any(observation.source_id in candidate.effective_exact_source_ids for observation in exact):
        return _set_refusal(
            ExecutionCoverageSetProofRefusalReason.EXACT_ALREADY_EFFECTIVE,
            "A candidate exact execution is already effective and cannot be inserted again.",
        )

    identity = candidate.incoming_exact.identity
    if any(observation.identity != identity for observation in observations):
        return _set_refusal(
            ExecutionCoverageSetProofRefusalReason.IDENTITY_MISMATCH,
            "Coverage observations must share account, authority, order, symbol, and side identity.",
        )
    validation = _validate_set_economics(observations)
    if validation is not None:
        return validation

    try:
        exact_aggregate = _aggregate_coverage(exact)
        cumulative_aggregate = _aggregate_coverage(candidate.cumulative_recovery)
    except OverflowError:
        return _set_refusal(
            ExecutionCoverageSetProofRefusalReason.NONFINITE_AGGREGATE,
            "Coverage rows cannot be accumulated into finite aggregate economics.",
        )
    if not _is_finite(exact_aggregate.gross_cost) or not _is_finite(cumulative_aggregate.gross_cost):
        return _set_refusal(
            ExecutionCoverageSetProofRefusalReason.NONFINITE_GROSS_COST,
            "Coverage quantity and price multiply to a non-finite gross cost.",
        )
    position_delta = exact_aggregate.quantity - cumulative_aggregate.quantity
    if abs(position_delta) >= QTY_ATOL:
        return _set_refusal(
            ExecutionCoverageSetProofRefusalReason.QUANTITY_MISMATCH,
            "Exact and cumulative coverage quantities differ outside the strict share tolerance.",
        )
    if abs(exact_aggregate.vwap - cumulative_aggregate.vwap) >= PRICE_ATOL:
        return _set_refusal(
            ExecutionCoverageSetProofRefusalReason.VWAP_MISMATCH,
            "Exact and cumulative VWAPs differ outside the strict currency-per-share tolerance.",
        )
    cost_tolerance = execution_coverage_gross_cost_tolerance(
        exact=exact_aggregate,
        cumulative=cumulative_aggregate,
    )
    if abs(exact_aggregate.gross_cost - cumulative_aggregate.gross_cost) > cost_tolerance:
        return _set_refusal(
            ExecutionCoverageSetProofRefusalReason.COST_MISMATCH,
            "Exact and cumulative gross costs differ outside the inclusive currency tolerance.",
        )
    return ExecutionCoverageSetProofSuccess(
        exact=exact_aggregate,
        cumulative=cumulative_aggregate,
        position_delta=position_delta,
        gross_cost_tolerance=cost_tolerance,
        retained_exact_observations=tuple(sorted(exact, key=lambda observation: observation.source_id)),
    )


def _validate_set_economics(
    observations: tuple[CumulativeCoverageObservation | ExactCoverageObservation, ...],
) -> ExecutionCoverageSetProofRefusal | None:
    for observation in observations:
        if not _is_finite(observation.quantity):
            return _set_refusal(
                ExecutionCoverageSetProofRefusalReason.NONFINITE_QUANTITY,
                f"Coverage quantity is non-finite for source {observation.source_id!r}.",
            )
        if observation.quantity <= 0:
            return _set_refusal(
                ExecutionCoverageSetProofRefusalReason.NONPOSITIVE_QUANTITY,
                f"Coverage quantity must be positive for source {observation.source_id!r}.",
            )
        if not _is_finite(observation.price):
            return _set_refusal(
                ExecutionCoverageSetProofRefusalReason.NONFINITE_PRICE,
                f"Coverage price is non-finite for source {observation.source_id!r}.",
            )
        if not _is_finite(observation.quantity * observation.price):
            return _set_refusal(
                ExecutionCoverageSetProofRefusalReason.NONFINITE_GROSS_COST,
                f"Coverage gross cost is non-finite for source {observation.source_id!r}.",
            )
        if isinstance(observation, ExactCoverageObservation) and observation.fee is not None and not _is_finite(observation.fee):
            return _set_refusal(
                ExecutionCoverageSetProofRefusalReason.NONFINITE_FEE,
                f"Exact execution fee is non-finite for source {observation.source_id!r}.",
            )
    return None


def _aggregate_coverage(
    observations: tuple[CumulativeCoverageObservation | ExactCoverageObservation, ...],
) -> ExecutionCoverageAggregate:
    ordered = tuple(sorted(observations, key=lambda observation: observation.source_id))
    quantity = math.fsum(observation.quantity for observation in ordered)
    gross_cost = math.fsum(observation.quantity * observation.price for observation in ordered)
    return ExecutionCoverageAggregate(
        quantity=quantity,
        gross_cost=gross_cost,
        vwap=gross_cost / quantity,
    )


def execution_coverage_gross_cost_tolerance(
    *,
    exact: ExecutionCoverageAggregate,
    cumulative: ExecutionCoverageAggregate,
) -> float:
    """Propagate pinned quantity and VWAP error into a gross-cost envelope."""
    return (
        max(abs(exact.quantity), abs(cumulative.quantity)) * PRICE_ATOL
        + max(abs(exact.vwap), abs(cumulative.vwap)) * QTY_ATOL
        + QTY_ATOL * PRICE_ATOL
    )


def _is_finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _set_refusal(
    reason: ExecutionCoverageSetProofRefusalReason,
    detail: str,
) -> ExecutionCoverageSetProofRefusal:
    return ExecutionCoverageSetProofRefusal(reason=reason, detail=detail)


def validate_execution_coverage_superseded_facts(
    facts: ExecutionCoverageSupersededFacts,
) -> ExecutionSliceFilledFacts:
    """Validate the closed direct-coverage replacement record.

    The fold re-runs the canonical proof against current immutable rows. This
    boundary validator instead makes the recorded plan self-contained and
    unambiguous before it reaches the custody hash chain.
    """
    if facts.actor != "AUTOMATIC":
        raise ValueError("coverage supersession actor must be AUTOMATIC")
    if not isinstance(facts.account_id, str) or not facts.account_id:
        raise ValueError("coverage supersession requires account_id")
    if (
        isinstance(facts.authority_generation, bool)
        or not isinstance(facts.authority_generation, int)
        or facts.authority_generation < 1
    ):
        raise ValueError("coverage supersession requires authority_generation")
    if not isinstance(facts.db_identity_token, str) or not facts.db_identity_token:
        raise ValueError("coverage supersession requires db_identity_token")
    if (
        isinstance(facts.expected_control_revision, bool)
        or not isinstance(facts.expected_control_revision, int)
        or facts.expected_control_revision < 0
    ):
        raise ValueError("coverage supersession requires expected_control_revision")
    if not isinstance(facts.order_ref, str) or not facts.order_ref:
        raise ValueError("coverage supersession requires order_ref")
    if not isinstance(facts.symbol, str) or not facts.symbol:
        raise ValueError("coverage supersession requires symbol")
    if facts.side not in {"BUY", "SELL"}:
        raise ValueError("coverage supersession requires an exact side")
    fill_ids = facts.superseded_cumulative_fill_ids
    if (
        not isinstance(fill_ids, list)
        or not fill_ids
        or any(not isinstance(fill_id, str) or not fill_id for fill_id in fill_ids)
        or fill_ids != sorted(set(fill_ids))
    ):
        raise ValueError("coverage supersession requires sorted cumulative fill ids")
    exact = facts.exact_execution
    validate_execution_slice_facts(exact)
    if exact.symbol.upper() != facts.symbol.upper() or exact.side != facts.side:
        raise ValueError("coverage supersession exact identity does not match its authority")
    _require_finite_positive_coverage(facts.exact_quantity, field="exact_quantity")
    _require_finite_positive_coverage(facts.exact_gross_cost, field="exact_gross_cost")
    _require_finite_positive_coverage(facts.cumulative_quantity, field="cumulative_quantity")
    _require_finite_positive_coverage(facts.cumulative_gross_cost, field="cumulative_gross_cost")
    _require_finite_positive_coverage(facts.quantity_tolerance, field="quantity_tolerance")
    _require_finite_nonnegative_coverage(facts.gross_cost_tolerance, field="gross_cost_tolerance")
    if facts.resolved_uncertainty_id is not None and (
        not isinstance(facts.resolved_uncertainty_id, str) or not facts.resolved_uncertainty_id
    ):
        raise ValueError("coverage supersession resolved uncertainty id is invalid")
    prior = facts.prior_exact_observations
    if not isinstance(prior, list):
        raise ValueError("coverage supersession prior exact observations must be a list")
    prior_ids = [item.exact_execution.execution_id for item in prior]
    if prior_ids != sorted(set(prior_ids)) or exact.execution_id in prior_ids:
        raise ValueError("coverage supersession prior exact identities must be sorted and unique")
    for item in prior:
        validate_execution_slice_facts(item.exact_execution)
        if (
            item.exact_execution.symbol.upper() != facts.symbol.upper()
            or item.exact_execution.side != facts.side
            or item.observation_transition_sequence != item.recorded_transition_sequence
        ):
            raise ValueError("coverage supersession prior exact provenance is inconsistent")
        for field_name in (
            "observation_transition_sequence",
            "recorded_transition_sequence",
            "clerk_observed_at_ms",
            "recorded_at_ms",
        ):
            value = getattr(item, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"coverage supersession prior {field_name} is invalid")
    all_exact = [*(item.exact_execution for item in prior), exact]
    expected_exact_quantity = math.fsum(item.slice_qty for item in all_exact)
    expected_exact_gross_cost = math.fsum(
        item.slice_qty * item.slice_price for item in all_exact
    )
    if (
        facts.exact_quantity != expected_exact_quantity
        or facts.exact_gross_cost != expected_exact_gross_cost
    ):
        raise ValueError("coverage supersession exact aggregate is not its exact economics")
    if (
        not isinstance(facts.evidence_refs, list)
        or not facts.evidence_refs
        or any(not isinstance(reference, str) or not reference for reference in facts.evidence_refs)
        or facts.evidence_refs != sorted(set(facts.evidence_refs))
    ):
        raise ValueError("coverage supersession requires unique sorted evidence references")
    return exact


def _require_finite_positive_coverage(value: object, *, field: str) -> None:
    if not _is_finite(value) or value <= 0:
        raise ValueError(f"{field} must be finite and positive")


def _require_finite_nonnegative_coverage(value: object, *, field: str) -> None:
    if not _is_finite(value) or value < 0:
        raise ValueError(f"{field} must be finite and non-negative")


@dataclass(frozen=True)
class ActiveExecutionCoverageConflict:
    """One active coverage-conflict episode, rooted at its first exact ID."""

    uncertainty_id: str
    strategy_instance_id: str | None
    order_ref: str
    conflict_execution_id: str


@dataclass(frozen=True)
class CumulativeRecoveryFill:
    """The aggregate fill which a closed proof may replace once."""

    fill_id: str
    order_ref: str
    quantity: float
    price: float
    side: str


@dataclass(frozen=True)
class ExecutionCoverageProof:
    """The full immutable evidence set and its deliberately narrow result."""

    conflict: ActiveExecutionCoverageConflict
    quarantined: tuple[ExecutionCoverageQuarantinedFacts, ...]
    cumulative: CumulativeRecoveryFill | None
    proof_available: bool
    unavailable_reason: str | None

    @property
    def execution_ids(self) -> tuple[str, ...]:
        return tuple(item.exact_execution.execution_id for item in self.quarantined)

    @property
    def exact_execution(self) -> ExecutionSliceFilledFacts | None:
        if len(self.quarantined) != 1:
            return None
        return self.quarantined[0].exact_execution


def active_execution_coverage_conflicts(
    conn: sqlite3.Connection,
    *,
    order_ref: str | None = None,
    uncertainty_id: str | None = None,
) -> tuple[ActiveExecutionCoverageConflict, ...]:
    """Read well-formed active episodes without relying on SQLite JSON support."""
    rows = conn.execute(
        "SELECT uncertainty_id, strategy_instance_id, facts_json FROM uncertainties "
        "WHERE reason_code = ? AND resolved_at_ms IS NULL ORDER BY observed_at_ms ASC, uncertainty_id ASC",
        (EXECUTION_COVERAGE_CONFLICT_REASON_CODE,),
    ).fetchall()
    conflicts: list[ActiveExecutionCoverageConflict] = []
    for row in rows:
        if uncertainty_id is not None and row["uncertainty_id"] != uncertainty_id:
            continue
        try:
            raised = UncertaintyRaisedFacts.from_facts_json(row["facts_json"])
            cause = ExecutionCoverageConflictCause.from_mapping(raised.cause_facts)
        except (KeyError, TypeError, ValueError):
            continue
        if order_ref is not None and cause.order_ref != order_ref:
            continue
        conflicts.append(
            ActiveExecutionCoverageConflict(
                uncertainty_id=row["uncertainty_id"],
                strategy_instance_id=row["strategy_instance_id"],
                order_ref=cause.order_ref,
                conflict_execution_id=cause.execution_id,
            )
        )
    return tuple(conflicts)


def quarantined_executions_for_conflict(
    conn: sqlite3.Connection,
    *,
    conflict: ActiveExecutionCoverageConflict,
) -> tuple[ExecutionCoverageQuarantinedFacts, ...]:
    """Return every distinct exact slice retained for one active episode."""
    rows = conn.execute(
        "SELECT facts_json FROM custody_transitions WHERE order_ref = ? "
        "AND transition_kind = 'EXECUTION_COVERAGE_QUARANTINED' ORDER BY sequence ASC",
        (conflict.order_ref,),
    ).fetchall()
    quarantined: list[ExecutionCoverageQuarantinedFacts] = []
    seen_execution_ids: set[str] = set()
    for row in rows:
        try:
            facts = ExecutionCoverageQuarantinedFacts.from_facts_json(row["facts_json"])
            validate_execution_coverage_quarantined_facts(facts)
        except (KeyError, TypeError, ValueError):
            continue
        if facts.conflict_execution_id != conflict.conflict_execution_id:
            continue
        execution_id = facts.exact_execution.execution_id
        if execution_id in seen_execution_ids:
            continue
        seen_execution_ids.add(execution_id)
        quarantined.append(facts)
    return tuple(quarantined)


def execution_is_quarantined(
    conn: sqlite3.Connection,
    *,
    conflict: ActiveExecutionCoverageConflict,
    execution_id: str,
) -> bool:
    """Whether the immutable broker execution identity was already retained."""
    return any(
        item.exact_execution.execution_id == execution_id
        for item in quarantined_executions_for_conflict(conn, conflict=conflict)
    )


def _has_unreadable_quarantine_for_order(conn: sqlite3.Connection, *, order_ref: str) -> bool:
    """Fail closed rather than proving around malformed retained evidence."""
    from app.broker.alpaca.clerk.sqlite.execution_coverage_evidence import (
        unreadable_quarantine_source_ids_for_order,
    )

    return bool(unreadable_quarantine_source_ids_for_order(conn, order_ref=order_ref))


def execution_coverage_proof(
    conn: sqlite3.Connection,
    *,
    conflict: ActiveExecutionCoverageConflict,
) -> ExecutionCoverageProof:
    """Prove only the S0 one-exact-for-one-cumulative replacement case."""
    quarantined = quarantined_executions_for_conflict(conn, conflict=conflict)
    if _has_unreadable_quarantine_for_order(conn, order_ref=conflict.order_ref):
        return _unavailable(
            conflict,
            quarantined,
            "Execution coverage evidence is unreadable; no safe replacement can be proven.",
        )
    if not quarantined:
        return _unavailable(conflict, quarantined, "The exact execution quarantine is absent; fresh exact evidence is required.")
    if len(quarantined) != 1:
        return _unavailable(
            conflict,
            quarantined,
            "Multiple exact executions are quarantined for this aggregate recovery total; no one-slice replacement is safe.",
            cumulative=_current_cumulative_recovery_fill(conn, quarantined[0]),
        )
    quarantine = quarantined[0]
    exact = quarantine.exact_execution
    if exact.execution_id != conflict.conflict_execution_id:
        return _unavailable(
            conflict,
            quarantined,
            "The coverage episode is missing its originating exact execution; no safe replacement can be proven.",
        )
    if len(quarantine.conflicting_cumulative_fill_ids) != 1:
        return _unavailable(
            conflict,
            quarantined,
            "The exact execution overlaps multiple cumulative recovery rows; fresh per-slice evidence is required.",
        )
    cumulative = _current_cumulative_recovery_fill(conn, quarantine)
    if cumulative is None:
        return _unavailable(
            conflict,
            quarantined,
            "The cumulative recovery row is unavailable; fresh exact evidence is required.",
        )
    if cumulative.order_ref != conflict.order_ref:
        return _unavailable(
            conflict,
            quarantined,
            "The cumulative recovery row belongs to a different order; no safe replacement can be proven.",
            cumulative=cumulative,
        )
    if not exact_replaces_cumulative(exact=exact, cumulative=cumulative):
        return ExecutionCoverageProof(
            conflict=conflict,
            quarantined=quarantined,
            cumulative=cumulative,
            proof_available=False,
            unavailable_reason="The exact execution and cumulative recovery economics differ; no automatic replacement is safe.",
        )
    return ExecutionCoverageProof(
        conflict=conflict,
        quarantined=quarantined,
        cumulative=cumulative,
        proof_available=True,
        unavailable_reason=None,
    )


def _current_cumulative_recovery_fill(
    conn: sqlite3.Connection,
    quarantine: ExecutionCoverageQuarantinedFacts,
) -> CumulativeRecoveryFill | None:
    """Read the single aggregate identity carried by one quarantined slice."""
    if len(quarantine.conflicting_cumulative_fill_ids) != 1:
        return None
    return cumulative_recovery_fill_by_id(
        conn,
        fill_id=quarantine.conflicting_cumulative_fill_ids[0],
    )


def cumulative_recovery_fill_by_id(
    conn: sqlite3.Connection,
    *,
    fill_id: str,
) -> CumulativeRecoveryFill | None:
    """Return one typed aggregate-recovery row without inferring any exact slice."""
    row = conn.execute(
        "SELECT fill_id, order_ref, qty, price, side, evidence_source FROM fills WHERE fill_id = ?",
        (fill_id,),
    ).fetchone()
    if row is None or row["evidence_source"] != "cumulative_recovery":
        return None
    return CumulativeRecoveryFill(
        fill_id=row["fill_id"],
        order_ref=row["order_ref"],
        quantity=float(row["qty"]),
        price=float(row["price"]),
        side=row["side"],
    )


def cumulative_recovery_fills_for_order(
    conn: sqlite3.Connection,
    *,
    order_ref: str,
) -> tuple[CumulativeRecoveryFill, ...]:
    """Return every active aggregate recovery row for a historical proof check."""
    rows = conn.execute(
        "SELECT fill_id, order_ref, qty, price, side FROM fills "
        "WHERE order_ref = ? AND evidence_source = 'cumulative_recovery' "
        "ORDER BY fill_id ASC",
        (order_ref,),
    ).fetchall()
    return tuple(
        CumulativeRecoveryFill(
            fill_id=row["fill_id"],
            order_ref=row["order_ref"],
            quantity=float(row["qty"]),
            price=float(row["price"]),
            side=row["side"],
        )
        for row in rows
    )


def direct_cumulative_recovery_fill_for_order(
    conn: sqlite3.Connection,
    *,
    order_ref: str,
) -> CumulativeRecoveryFill | None:
    """Return one isolated aggregate row eligible for direct replacement.

    #1554 is deliberately narrower than the set proof: automatic admission
    is allowed only while this order's effective economics consist of exactly
    one cumulative-recovery row. Any other effective fill must retain the
    established fail-closed coverage path until the accumulated-set slice can
    account for it.
    """
    cumulative = cumulative_recovery_fills_for_order(conn, order_ref=order_ref)
    if len(cumulative) != 1:
        return None
    other_effective = conn.execute(
        "SELECT 1 FROM fills f WHERE f.order_ref = ? "
        "AND f.evidence_source != 'cumulative_recovery' "
        "AND NOT EXISTS (SELECT 1 FROM fills successor "
        "WHERE successor.superseded_execution_ref = f.execution_id) LIMIT 1",
        (order_ref,),
    ).fetchone()
    return None if other_effective is not None else cumulative[0]


def exact_replaces_cumulative(
    *,
    exact: ExecutionSliceFilledFacts,
    cumulative: CumulativeRecoveryFill,
) -> bool:
    """Temporary S0 one-to-one mirror of the canonical set-proof predicate.

    Formula: exact and cumulative sides/prices/quantities agree within the
      S0 strict `FILL_QTY_EPSILON` boundary.
    Reference: Project-authored S0 operator contract retained during the
      execution-coverage migration in PRD #1543.
    Canonical implementation: prove_execution_coverage_set in this file; this
      S0 predicate remains only for the shipped operator flow.
    Validated against: tests/broker/alpaca/clerk/sqlite/
      test_execution_coverage_set_proof.py::test_s0_one_to_one_predicate_matches_canonical_set_proof_on_unambiguous_inputs.
    """
    return (
        cumulative.side == exact.side
        and abs(cumulative.quantity - exact.slice_qty) < FILL_QTY_EPSILON
        and abs(cumulative.price - exact.slice_price) < FILL_QTY_EPSILON
    )


def _unavailable(
    conflict: ActiveExecutionCoverageConflict,
    quarantined: tuple[ExecutionCoverageQuarantinedFacts, ...],
    reason: str,
    *,
    cumulative: CumulativeRecoveryFill | None = None,
) -> ExecutionCoverageProof:
    return ExecutionCoverageProof(
        conflict=conflict,
        quarantined=quarantined,
        cumulative=cumulative,
        proof_available=False,
        unavailable_reason=reason,
    )
