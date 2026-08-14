"""Closed proof vocabulary for exact executions that overlap aggregate recovery.

Formula: Q = fsum(qty); C = fsum(qty × price); P = C / Q. The canonical
  set proof accepts only abs(Q_E - Q_R) < 1e-9 shares and
  abs(C_E - C_R) <= max(Q_E, Q_R) × 1e-9 currency/share.
Reference: Project-authored execution-coverage contract in PRD #1543, stories
  18, 19, 28, and 33; this is authored project logic, not a reused proof.
Canonical implementation: this file's prove_execution_coverage_set. The
  existing exact_replaces_cumulative remains the temporary S0 operator proof.
Validated against: tests/broker/alpaca/clerk/sqlite/test_execution_coverage_set_proof.py

The existing typed query below continues to describe the shipped S0 operator
flow. The new pure set proof deliberately has no fold integration in this
slice; keeping both vocabularies together makes the temporary boundary
auditable without changing that operator path.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from enum import StrEnum

from app.broker.alpaca.clerk.sqlite.facts import (
    ExecutionCoverageQuarantinedFacts,
    ExecutionSliceFilledFacts,
    UncertaintyRaisedFacts,
    validate_execution_coverage_quarantined_facts,
)
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
    COST_MISMATCH = "cost_mismatch"


@dataclass(frozen=True)
class ExecutionCoverageSetProofSuccess:
    """A fully explained no-position-delta replacement proof."""

    exact: ExecutionCoverageAggregate
    cumulative: ExecutionCoverageAggregate
    position_delta: float
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
    cost_tolerance = max(exact_aggregate.quantity, cumulative_aggregate.quantity) * PRICE_ATOL
    if abs(exact_aggregate.gross_cost - cumulative_aggregate.gross_cost) > cost_tolerance:
        return _set_refusal(
            ExecutionCoverageSetProofRefusalReason.COST_MISMATCH,
            "Exact and cumulative gross costs differ outside the inclusive currency tolerance.",
        )
    return ExecutionCoverageSetProofSuccess(
        exact=exact_aggregate,
        cumulative=cumulative_aggregate,
        position_delta=position_delta,
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


def _is_finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _set_refusal(
    reason: ExecutionCoverageSetProofRefusalReason,
    detail: str,
) -> ExecutionCoverageSetProofRefusal:
    return ExecutionCoverageSetProofRefusal(reason=reason, detail=detail)


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
    rows = conn.execute(
        "SELECT facts_json FROM custody_transitions WHERE order_ref = ? "
        "AND transition_kind = 'EXECUTION_COVERAGE_QUARANTINED'",
        (order_ref,),
    ).fetchall()
    for row in rows:
        try:
            facts = ExecutionCoverageQuarantinedFacts.from_facts_json(row["facts_json"])
            validate_execution_coverage_quarantined_facts(facts)
        except (KeyError, TypeError, ValueError):
            return True
    return False


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


def exact_replaces_cumulative(
    *,
    exact: ExecutionSliceFilledFacts,
    cumulative: CumulativeRecoveryFill,
) -> bool:
    """Closed equality predicate shared by read, write, and fold paths."""
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
