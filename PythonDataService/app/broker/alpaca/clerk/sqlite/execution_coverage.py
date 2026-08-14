"""Closed proof vocabulary for exact executions that overlap aggregate recovery.

The same typed query is used to admit later exact evidence, describe recovery
to the operator, and apply the no-economic-delta replacement fold.  Keeping
that invariant here prevents an API path from accepting a proof the projection
would describe as unsafe (or vice versa).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

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
