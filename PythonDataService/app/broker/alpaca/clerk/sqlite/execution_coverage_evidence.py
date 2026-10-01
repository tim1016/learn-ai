"""SQLite evidence adapters for the canonical execution-coverage set proof.

This module owns current-state reads and the conversion of immutable custody
observations into the pure proof's input vocabulary. It deliberately contains
no transition append or fold behavior.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass

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
    QuarantineObservation,
    active_execution_coverage_conflicts,
    cumulative_recovery_fills_for_order,
    first_quarantine_per_execution,
    order_total_proven_conflict_execution_ids,
    quarantine_observations_for_order,
)
from app.broker.alpaca.clerk.sqlite.facts import ExecutionSliceFilledFacts, ManualOrderAcceptedFacts
from app.broker.contract.models import BrokerOrderLeg


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
    acknowledgement. The state and the total both come from the order's one
    governing acknowledgement (:func:`reads.governing_acknowledgement`); a
    non-final governing state is refused by the proof itself.
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
    governing_ack = reads.governing_acknowledgement(conn, conflict.order_ref)
    if governing_ack is None:
        return None
    broker_state, reported_filled_quantity = governing_ack
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


#: The exact evidence a real broker chain's executions carry. A manual leg is
#: never simulated, so a ``simulated_execution`` row refuses the chain proof.
_BROKER_EXACT_EVIDENCE_SOURCES = frozenset({"websocket", "activity_recovery"})


@dataclass(frozen=True)
class ChainTotalCoveragePlan:
    """What a chain-total proof would change on one manual leg, read from current rows (#2786).

    ``exact_quantities`` are every distinct exact execution of the chain --
    effective already, or quarantined and made effective by the proof --
    sorted by execution id; ``exact_quantity`` and
    ``prior_effective_quantity`` are ``math.fsum`` totals, in shares, of
    those and of every currently effective fill.
    """

    symbol: str
    side: str
    superseded_cumulative_fill_ids: tuple[str, ...]
    effective_exact_execution_ids: tuple[str, ...]
    made_effective: tuple[ExecutionCoverageExactProvenance, ...]
    exact_quantities: tuple[float, ...]
    exact_quantity: float
    prior_effective_quantity: float
    resolved_uncertainty_id: str | None


def chain_total_coverage_plan(
    conn: sqlite3.Connection,
    *,
    order_ref: str,
) -> ChainTotalCoveragePlan | None:
    """Read one manual leg's exact executions, cumulative rows and open coverage episode (#2786).

    The one reader both the chain-total planner and its fold use, so replay
    re-derives exactly what was planned. ``None`` means there is nothing a
    chain-total proof may change, or no safe way to read it: not a manual
    leg; nothing to supersede or make effective; unreadable quarantine
    evidence; one execution id quarantined with two economics; a fill or
    exact on another side or symbol than the leg's, or from a simulated
    source; or an open coverage episode the proof does not answer -- more
    than one, or one no quarantined exact of this leg opened (the account
    activity's over-quantity refusal and a changed redelivery of an
    effective execution stay for an operator).
    """
    acceptance = conn.execute(
        "SELECT facts_json FROM custody_transitions WHERE order_ref = ? "
        "AND transition_kind = 'MANUAL_ORDER_ACCEPTED' ORDER BY sequence ASC LIMIT 1",
        (order_ref,),
    ).fetchone()
    if acceptance is None:
        return None
    leg = BrokerOrderLeg.model_validate(ManualOrderAcceptedFacts.from_facts_json(acceptance["facts_json"]).leg)
    symbol, side = leg.symbol.strip().upper(), leg.side.value.upper()
    observations = quarantine_observations_for_order(conn, order_ref=order_ref)
    if _unreadable_source_ids(observations):
        return None
    fills = conn.execute(
        "SELECT fill_id, qty, side, execution_id, evidence_source FROM fills current_fill "
        "WHERE order_ref = ? AND NOT EXISTS (SELECT 1 FROM fills successor "
        "WHERE successor.superseded_execution_ref = current_fill.execution_id) ORDER BY fill_id ASC",
        (order_ref,),
    ).fetchall()
    cumulative_ids: list[str] = []
    exact_by_id: dict[str, float] = {}
    for fill in fills:
        if fill["side"] != side:
            return None
        if fill["evidence_source"] == "cumulative_recovery":
            cumulative_ids.append(fill["fill_id"])
        elif fill["evidence_source"] in _BROKER_EXACT_EVIDENCE_SOURCES and fill["execution_id"]:
            exact_by_id[fill["execution_id"]] = float(fill["qty"])
        else:
            return None
    effective_ids = tuple(sorted(exact_by_id))
    quarantined: dict[str, ExecutionCoverageExactProvenance] = {}
    for provenance in _exact_provenance(observations):
        exact = provenance.exact_execution
        if exact.execution_id in exact_by_id:
            continue
        if (
            exact.symbol.strip().upper() != symbol
            or exact.side != side
            or exact.evidence_source not in _BROKER_EXACT_EVIDENCE_SOURCES
            or quarantined.setdefault(exact.execution_id, provenance).exact_execution != exact
        ):
            return None
    active = active_execution_coverage_conflicts(conn, order_ref=order_ref)
    if len(active) > 1 or (active and not _opened_by_quarantine(active[0], observations, quarantined)):
        return None
    if not cumulative_ids and not quarantined:
        return None
    for execution_id, provenance in quarantined.items():
        exact_by_id[execution_id] = provenance.exact_execution.slice_qty
    exact_quantities = tuple(exact_by_id[execution_id] for execution_id in sorted(exact_by_id))
    return ChainTotalCoveragePlan(
        symbol=symbol,
        side=side,
        superseded_cumulative_fill_ids=tuple(cumulative_ids),
        effective_exact_execution_ids=effective_ids,
        made_effective=tuple(quarantined[execution_id] for execution_id in sorted(quarantined)),
        exact_quantities=exact_quantities,
        exact_quantity=math.fsum(exact_quantities),
        prior_effective_quantity=math.fsum(float(fill["qty"]) for fill in fills),
        resolved_uncertainty_id=active[0].uncertainty_id if active else None,
    )


def _opened_by_quarantine(
    conflict: ActiveExecutionCoverageConflict,
    observations: tuple[QuarantineObservation, ...],
    quarantined: dict[str, ExecutionCoverageExactProvenance],
) -> bool:
    """Whether a quarantine of a not-yet-effective exact opened this coverage episode.

    An episode's id names the transition that raised it
    (``uncertainty_folds.fold_uncertainty_raised``); a quarantine that opens
    an episode carries the episode's facts and roots it at its own exact.
    """
    return conflict.conflict_execution_id in quarantined and any(
        observation.facts is not None
        and observation.facts.uncertainty is not None
        and observation.facts.exact_execution.execution_id == conflict.conflict_execution_id
        and conflict.uncertainty_id == f"uncertainty:{observation.sequence}"
        for observation in observations
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
    order_effective: tuple[float, float],
    active_episode_ids: tuple[str, ...] = (),
    effective_exact_source_ids: frozenset[str] = frozenset(),
    unreadable_source_ids: tuple[str, ...] = (),
) -> ExecutionCoverageSetCandidate:
    """Map current rows and retained exact custody evidence into set proof input.

    ``order_effective`` is the order's effective ``(quantity, gross cost)``,
    as :func:`reads.effective_fill_totals_for_order` reads it before the swap.
    """
    order_effective_quantity, order_effective_gross_cost = order_effective
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
        order_effective_quantity=order_effective_quantity,
        order_effective_gross_cost=order_effective_gross_cost,
        active_episode_ids=active_episode_ids,
        effective_exact_source_ids=effective_exact_source_ids,
        unreadable_source_ids=unreadable_source_ids,
    )
