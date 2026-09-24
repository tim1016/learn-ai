"""SQLite evidence adapters for the canonical execution-coverage set proof.

This module owns current-state reads and the conversion of immutable custody
observations into the pure proof's input vocabulary. It deliberately contains
no transition append or fold behavior.
"""

from __future__ import annotations

import math
import sqlite3

from app.broker.alpaca.clerk.sqlite import reads
from app.broker.alpaca.clerk.sqlite.execution_coverage import (
    ActiveExecutionCoverageConflict,
    CumulativeCoverageObservation,
    CumulativeRecoveryFill,
    ExactCoverageObservation,
    ExecutionCoverageExactProvenance,
    ExecutionCoverageIdentity,
    ExecutionCoverageSetCandidate,
    OrderTotalCoverageEvidence,
    cumulative_recovery_fills_for_order,
)
from app.broker.alpaca.clerk.sqlite.facts import (
    ExecutionCoverageQuarantinedFacts,
    ExecutionSliceFilledFacts,
    validate_execution_coverage_quarantined_facts,
)


def quarantined_exact_provenance_for_conflict(
    conn: sqlite3.Connection,
    *,
    conflict: ActiveExecutionCoverageConflict,
) -> tuple[ExecutionCoverageExactProvenance, ...]:
    """Read each retained exact together with its immutable observation clocks."""
    rows = conn.execute(
        "SELECT sequence, clerk_observed_at_ms, recorded_at_ms, facts_json "
        "FROM custody_transitions WHERE order_ref = ? "
        "AND transition_kind = 'EXECUTION_COVERAGE_QUARANTINED' ORDER BY sequence ASC",
        (conflict.order_ref,),
    ).fetchall()
    observations: list[ExecutionCoverageExactProvenance] = []
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
        observations.append(
            ExecutionCoverageExactProvenance(
                exact_execution=facts.exact_execution,
                observation_transition_sequence=row["sequence"],
                clerk_observed_at_ms=row["clerk_observed_at_ms"],
                recorded_at_ms=row["recorded_at_ms"],
                recorded_transition_sequence=row["sequence"],
            )
        )
    return tuple(observations)


def unreadable_quarantine_source_ids_for_order(
    conn: sqlite3.Connection,
    *,
    order_ref: str,
) -> tuple[str, ...]:
    """Name malformed quarantine transitions so a set proof can refuse them."""
    rows = conn.execute(
        "SELECT sequence, facts_json FROM custody_transitions WHERE order_ref = ? "
        "AND transition_kind = 'EXECUTION_COVERAGE_QUARANTINED'",
        (order_ref,),
    ).fetchall()
    unreadable: list[str] = []
    for row in rows:
        try:
            facts = ExecutionCoverageQuarantinedFacts.from_facts_json(row["facts_json"])
            validate_execution_coverage_quarantined_facts(facts)
        except (KeyError, TypeError, ValueError):
            unreadable.append(f"quarantine-transition:{row['sequence']}")
    return tuple(unreadable)


def order_total_coverage_evidence(
    conn: sqlite3.Connection,
    *,
    conflict: ActiveExecutionCoverageConflict,
) -> OrderTotalCoverageEvidence | None:
    """Read the recorded totals the order-level coverage proof compares (#2346).

    ``None`` keeps the episode open without a proof attempt: unreadable
    quarantine evidence, an episode whose originating exact is not a
    quarantined, non-effective slice (a changed redelivery of an effective
    execution ID is a genuine conflict), or an exact whose side differs from
    the cumulative recovery it would be covered by.
    """
    if unreadable_quarantine_source_ids_for_order(conn, order_ref=conflict.order_ref):
        return None
    effective_ids = effective_exact_execution_ids_for_order(conn, order_ref=conflict.order_ref)
    quarantined: dict[str, ExecutionSliceFilledFacts] = {}
    for facts in quarantined_facts_for_order(conn, order_ref=conflict.order_ref):
        exact = facts.exact_execution
        if exact.execution_id in effective_ids:
            continue
        if quarantined.setdefault(exact.execution_id, exact) != exact:
            # One broker execution ID with two economics is a genuine conflict.
            return None
    if conflict.conflict_execution_id not in quarantined:
        return None
    cumulative = cumulative_recovery_fills_for_order(conn, order_ref=conflict.order_ref)
    sides = {item.side for item in cumulative} | {item.side for item in quarantined.values()}
    if not cumulative or len(sides) != 1:
        return None
    order = reads.order(conn, conflict.order_ref)
    effective_quantity, _ = reads.effective_fill_totals_for_order(conn, conflict.order_ref)
    return OrderTotalCoverageEvidence(
        broker_state=order.broker_state if order is not None else None,
        reported_filled_quantity=reads.latest_reported_filled_quantity(conn, conflict.order_ref),
        effective_quantity=effective_quantity,
        cumulative_recovery_quantity=math.fsum(item.quantity for item in cumulative),
        quarantined_exact_quantities=tuple(
            quarantined[execution_id].slice_qty for execution_id in sorted(quarantined)
        ),
    )


def quarantined_facts_for_order(
    conn: sqlite3.Connection,
    *,
    order_ref: str,
) -> tuple[ExecutionCoverageQuarantinedFacts, ...]:
    """Every readable quarantine of the order, across all of its episodes.

    Unreadable rows are skipped here; callers that prove anything from this
    read refuse first through :func:`unreadable_quarantine_source_ids_for_order`.
    """
    rows = conn.execute(
        "SELECT facts_json FROM custody_transitions WHERE order_ref = ? "
        "AND transition_kind = 'EXECUTION_COVERAGE_QUARANTINED' ORDER BY sequence ASC",
        (order_ref,),
    ).fetchall()
    quarantined: list[ExecutionCoverageQuarantinedFacts] = []
    for row in rows:
        try:
            facts = ExecutionCoverageQuarantinedFacts.from_facts_json(row["facts_json"])
            validate_execution_coverage_quarantined_facts(facts)
        except (KeyError, TypeError, ValueError):
            continue
        quarantined.append(facts)
    return tuple(quarantined)


def effective_exact_execution_ids_for_order(
    conn: sqlite3.Connection,
    *,
    order_ref: str,
) -> frozenset[str]:
    """Return currently effective exact identities without counting corrections."""
    rows = conn.execute(
        "SELECT execution_id FROM fills current_fill WHERE order_ref = ? "
        "AND execution_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM fills successor "
        "WHERE successor.superseded_execution_ref = current_fill.execution_id)",
        (order_ref,),
    ).fetchall()
    return frozenset(row["execution_id"] for row in rows)


def execution_coverage_candidate(
    *,
    identity: ExecutionCoverageIdentity,
    cumulative: tuple[CumulativeRecoveryFill, ...],
    prior: tuple[ExecutionCoverageExactProvenance, ...],
    exact: ExecutionSliceFilledFacts,
    active_episode_ids: tuple[str, ...] = (),
    effective_exact_source_ids: frozenset[str] = frozenset(),
    unreadable_source_ids: tuple[str, ...] = (),
) -> ExecutionCoverageSetCandidate:
    """Map current rows and retained exact custody evidence into set proof input."""
    return ExecutionCoverageSetCandidate(
        cumulative_recovery=tuple(
            CumulativeCoverageObservation(
                source_id=item.fill_id,
                identity=ExecutionCoverageIdentity(
                    account_id=identity.account_id,
                    authority_generation=identity.authority_generation,
                    database_identity_token=identity.database_identity_token,
                    order_ref=item.order_ref,
                    symbol=identity.symbol,
                    side=item.side,
                ),
                quantity=item.quantity,
                price=item.price,
            )
            for item in cumulative
        ),
        prior_quarantined_exact=tuple(
            ExactCoverageObservation(
                source_id=item.exact_execution.execution_id,
                identity=identity,
                quantity=item.exact_execution.slice_qty,
                price=item.exact_execution.slice_price,
                fee=item.exact_execution.fee,
            )
            for item in prior
        ),
        incoming_exact=ExactCoverageObservation(
            source_id=exact.execution_id,
            identity=identity,
            quantity=exact.slice_qty,
            price=exact.slice_price,
            fee=exact.fee,
        ),
        active_episode_ids=active_episode_ids,
        effective_exact_source_ids=effective_exact_source_ids,
        unreadable_source_ids=unreadable_source_ids,
    )
