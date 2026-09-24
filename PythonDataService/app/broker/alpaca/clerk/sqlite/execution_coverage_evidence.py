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
    FINAL_CUMULATIVE_BROKER_STATES,
    ActiveExecutionCoverageConflict,
    CumulativeCoverageObservation,
    CumulativeRecoveryFill,
    ExactCoverageObservation,
    ExecutionCoverageExactProvenance,
    ExecutionCoverageIdentity,
    ExecutionCoverageSetCandidate,
    OrderTotalCoverageEvidence,
    QuarantineObservation,
    cumulative_recovery_fills_for_order,
    first_quarantine_per_execution,
    order_total_proven_conflict_execution_ids,
    quarantine_observations_for_order,
)
from app.broker.alpaca.clerk.sqlite.facts import ExecutionSliceFilledFacts


def quarantined_exact_provenance_for_conflict(
    conn: sqlite3.Connection,
    *,
    conflict: ActiveExecutionCoverageConflict,
) -> tuple[ExecutionCoverageExactProvenance, ...]:
    """Read each retained exact together with its immutable observation clocks."""
    return _exact_provenance(
        first_quarantine_per_execution(
            quarantine_observations_for_order(conn, order_ref=conflict.order_ref),
            conflict_execution_ids=frozenset({conflict.conflict_execution_id}),
        )
    )


def order_total_retained_exact_provenance(
    conn: sqlite3.Connection,
    *,
    order_ref: str,
) -> tuple[ExecutionCoverageExactProvenance, ...]:
    """Quarantined exacts an order-total proof accounted for that are still not effective (#2346).

    The order-level proof closes an episode without moving a fill, so its
    exacts stay quarantined behind the cumulative-recovery row. A later exact
    of the same order is proven together with them through the canonical set
    proof, exactly as the accumulated episode proof would have done. Sorted by
    execution ID, the order the supersession fold compares.
    """
    episode_execution_ids = order_total_proven_conflict_execution_ids(conn, order_ref=order_ref)
    if not episode_execution_ids:
        return ()
    effective_ids = effective_exact_execution_ids_for_order(conn, order_ref=order_ref)
    retained = _exact_provenance(
        first_quarantine_per_execution(
            quarantine_observations_for_order(conn, order_ref=order_ref),
            conflict_execution_ids=episode_execution_ids,
        )
    )
    return tuple(
        sorted(
            (item for item in retained if item.exact_execution.execution_id not in effective_ids),
            key=lambda item: item.exact_execution.execution_id,
        )
    )


def _exact_provenance(
    observations: tuple[QuarantineObservation, ...],
) -> tuple[ExecutionCoverageExactProvenance, ...]:
    return tuple(
        ExecutionCoverageExactProvenance(
            exact_execution=observation.facts.exact_execution,
            observation_transition_sequence=observation.sequence,
            clerk_observed_at_ms=observation.clerk_observed_at_ms,
            recorded_at_ms=observation.recorded_at_ms,
            recorded_transition_sequence=observation.sequence,
        )
        for observation in observations
        if observation.facts is not None
    )


def unreadable_quarantine_source_ids_for_order(
    conn: sqlite3.Connection,
    *,
    order_ref: str,
) -> tuple[str, ...]:
    """Name malformed quarantine transitions so a set proof can refuse them."""
    return _unreadable_source_ids(quarantine_observations_for_order(conn, order_ref=order_ref))


def _unreadable_source_ids(observations: tuple[QuarantineObservation, ...]) -> tuple[str, ...]:
    return tuple(
        f"quarantine-transition:{observation.sequence}"
        for observation in observations
        if observation.facts is None
    )


def order_total_coverage_evidence(
    conn: sqlite3.Connection,
    *,
    conflict: ActiveExecutionCoverageConflict,
) -> OrderTotalCoverageEvidence | None:
    """Read the recorded totals the order-level coverage proof compares (#2346).

    ``None`` keeps the episode open without a proof attempt: unreadable
    quarantine evidence, an episode whose originating exact is not a
    quarantined, non-effective slice (a changed redelivery of an effective
    execution ID is a genuine conflict), an exact whose side differs from
    the cumulative recovery it would be covered by, or an order with no
    acknowledgement in a final state. The state and the total both come from
    that one final acknowledgement (see
    :func:`reads.latest_acknowledgement_in_states`).
    """
    observations = quarantine_observations_for_order(conn, order_ref=conflict.order_ref)
    if _unreadable_source_ids(observations):
        return None
    effective_ids = effective_exact_execution_ids_for_order(conn, order_ref=conflict.order_ref)
    quarantined: dict[str, ExecutionSliceFilledFacts] = {}
    for facts in (observation.facts for observation in observations if observation.facts is not None):
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
    final_ack = reads.latest_acknowledgement_in_states(
        conn, conflict.order_ref, FINAL_CUMULATIVE_BROKER_STATES
    )
    if final_ack is None:
        return None
    broker_state, reported_filled_quantity = final_ack
    effective_quantity, _ = reads.effective_fill_totals_for_order(conn, conflict.order_ref)
    return OrderTotalCoverageEvidence(
        broker_state=broker_state,
        reported_filled_quantity=reported_filled_quantity,
        effective_quantity=effective_quantity,
        cumulative_recovery_quantity=math.fsum(item.quantity for item in cumulative),
        quarantined_exact_quantities=tuple(
            quarantined[execution_id].slice_qty for execution_id in sorted(quarantined)
        ),
    )


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
