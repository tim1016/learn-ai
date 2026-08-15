"""Replayable projection fold for proven execution-coverage supersession.

Formula: ``Δposition = 0``. The current cumulative contribution already owns
the attributed quantity, so this fold swaps only its rebuildable ``fills``
representation after rerunning the canonical set proof.
Reference: PRD #1543 stories 19 and 33; issues #1554 and #1557.
Canonical implementation: execution_coverage.prove_execution_coverage_set.
Validated against: PythonDataService/tests/broker/alpaca/clerk/sqlite/
  test_folds_execution.py::test_many_to_many_coverage_supersession_preserves_exact_provenance_and_replay.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any, Protocol

from app.broker.alpaca.clerk.sqlite import reads
from app.broker.alpaca.clerk.sqlite.execution_coverage import (
    PRICE_ATOL,
    QTY_ATOL,
    ExecutionCoverageIdentity,
    ExecutionCoverageSetProofSuccess,
    ExecutionCoverageSupersededFacts,
    active_execution_coverage_conflicts,
    cumulative_recovery_fills_for_order,
    prove_execution_coverage_set,
    validate_execution_coverage_superseded_facts,
)
from app.broker.alpaca.clerk.sqlite.execution_coverage_evidence import (
    effective_exact_execution_ids_for_order,
    execution_coverage_candidate,
    quarantined_exact_provenance_for_conflict,
    unreadable_quarantine_source_ids_for_order,
)
from app.broker.alpaca.clerk.sqlite.facts import ExecutionSliceFilledFacts


class ManualOrderCompleter(Protocol):
    """The lifecycle consequence shared with the fold registry."""

    def __call__(
        self,
        conn: sqlite3.Connection,
        *,
        payload: dict[str, Any],
        order_ref: str,
    ) -> None: ...


def fold_execution_coverage_superseded(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    *,
    transition_sequence: Callable[[sqlite3.Connection], int],
    complete_manual_order: ManualOrderCompleter,
) -> None:
    """Replace proven cumulative coverage with preserved exact evidence."""
    facts = ExecutionCoverageSupersededFacts.from_facts_json(payload["facts_json"])
    exact = validate_execution_coverage_superseded_facts(facts)
    if (
        payload["order_ref"] != facts.order_ref
        or payload["source_event_at_ms"] != exact.source_event_at_ms
        or payload["authority_generation"] != facts.authority_generation
    ):
        raise ValueError("coverage supersession transition does not match its typed evidence")
    meta = reads.control_meta_snapshot(conn)
    if (
        facts.account_id != meta.account_id
        or facts.authority_generation != meta.authority_generation
        or facts.db_identity_token != meta.db_identity_token
        or facts.expected_control_revision != meta.control_revision
    ):
        raise ValueError("coverage supersession binding does not match the current Clerk authority")
    active = active_execution_coverage_conflicts(conn, order_ref=facts.order_ref)
    if facts.resolved_uncertainty_id is None:
        if active or facts.prior_exact_observations:
            raise ValueError("unconflicted coverage supersession cannot bypass accumulated evidence")
    else:
        if (
            len(active) != 1
            or active[0].uncertainty_id != facts.resolved_uncertainty_id
            or active[0].order_ref != facts.order_ref
        ):
            raise ValueError("coverage supersession requires its selected active uncertainty")
        prior = quarantined_exact_provenance_for_conflict(conn, conflict=active[0])
        if tuple(sorted(prior, key=lambda item: item.exact_execution.execution_id)) != tuple(
            facts.prior_exact_observations
        ):
            raise ValueError("coverage supersession prior exact evidence is stale or unreadable")
    owner = conn.execute(
        "SELECT candidate_effect.command_id, candidate_effect.strategy_instance_id, "
        "COALESCE(strategy.symbol, json_extract(manual_acceptance.facts_json, '$.leg.symbol')) AS symbol "
        "FROM orders order_row JOIN effect_operations order_effect "
        "ON order_effect.effect_operation_id = order_row.effect_operation_id "
        "JOIN effect_operations candidate_effect ON candidate_effect.effect_operation_id = ? "
        "LEFT JOIN strategy_instances strategy "
        "ON strategy.strategy_instance_id = order_effect.strategy_instance_id "
        "LEFT JOIN custody_transitions manual_acceptance ON manual_acceptance.sequence = ("
        "SELECT MIN(acceptance.sequence) FROM custody_transitions acceptance "
        "WHERE acceptance.order_ref = order_row.order_ref "
        "AND acceptance.effect_operation_id = order_effect.effect_operation_id "
        "AND acceptance.transition_kind = 'MANUAL_ORDER_ACCEPTED') "
        "WHERE order_row.order_ref = ? AND ("
        "order_row.effect_operation_id = candidate_effect.effect_operation_id OR EXISTS ("
        "SELECT 1 FROM operation_order_links link WHERE link.order_ref = order_row.order_ref "
        "AND link.effect_operation_id = candidate_effect.effect_operation_id))",
        (payload["effect_operation_id"], facts.order_ref),
    ).fetchone()
    if (
        owner is None
        or owner["command_id"] != payload["command_id"]
        or owner["strategy_instance_id"] != payload["strategy_instance_id"]
        or not isinstance(owner["symbol"], str)
        or owner["symbol"].upper() != facts.symbol.upper()
    ):
        raise ValueError("coverage supersession does not match the durable order owner")
    cumulative_for_proof = cumulative_recovery_fills_for_order(conn, order_ref=facts.order_ref)
    current_cumulative_ids = [item.fill_id for item in cumulative_for_proof]
    if current_cumulative_ids != facts.superseded_cumulative_fill_ids:
        raise ValueError("coverage supersession cumulative source is stale or broadened")
    proof = prove_execution_coverage_set(
        execution_coverage_candidate(
            identity=ExecutionCoverageIdentity(
                account_id=meta.account_id,
                authority_generation=meta.authority_generation,
                database_identity_token=meta.db_identity_token,
                order_ref=facts.order_ref,
                symbol=facts.symbol,
                side=facts.side,
            ),
            cumulative=cumulative_for_proof,
            prior=tuple(facts.prior_exact_observations),
            exact=exact,
            active_episode_ids=tuple(item.uncertainty_id for item in active),
            effective_exact_source_ids=effective_exact_execution_ids_for_order(conn, order_ref=facts.order_ref),
            unreadable_source_ids=unreadable_quarantine_source_ids_for_order(conn, order_ref=facts.order_ref),
        )
    )
    if not isinstance(proof, ExecutionCoverageSetProofSuccess):
        raise ValueError("coverage supersession proof no longer matches immutable evidence")
    accepted_cost_tolerances = {proof.gross_cost_tolerance}
    if facts.resolved_uncertainty_id is None:
        # #1554 records used the then-pinned direct-only envelope. Preserve
        # their replayability while accumulated records use #1557's propagated
        # quantity-plus-VWAP tolerance.
        accepted_cost_tolerances.add(max(proof.exact.quantity, proof.cumulative.quantity) * PRICE_ATOL)
    if (
        facts.quantity_tolerance != QTY_ATOL
        or facts.gross_cost_tolerance not in accepted_cost_tolerances
        or facts.exact_quantity != proof.exact.quantity
        or facts.exact_gross_cost != proof.exact.gross_cost
        or facts.cumulative_quantity != proof.cumulative.quantity
        or facts.cumulative_gross_cost != proof.cumulative.gross_cost
    ):
        raise ValueError("coverage supersession aggregates do not match its canonical proof")
    retained_exact = (*facts.prior_exact_observations,)
    execution_ids = [item.exact_execution.execution_id for item in retained_exact] + [exact.execution_id]
    if len(execution_ids) != len(set(execution_ids)) or any(
        conn.execute("SELECT 1 FROM fills WHERE execution_id = ?", (execution_id,)).fetchone() is not None
        for execution_id in execution_ids
    ):
        raise ValueError("coverage supersession exact execution already exists")
    conn.executemany(
        "DELETE FROM fills WHERE fill_id = ?",
        ((fill_id,) for fill_id in facts.superseded_cumulative_fill_ids),
    )
    for item in retained_exact:
        _insert_coverage_superseded_exact_fill(
            conn,
            order_ref=facts.order_ref,
            exact=item.exact_execution,
            clerk_observed_at_ms=item.clerk_observed_at_ms,
            recorded_at_ms=item.recorded_at_ms,
            recorded_transition_sequence=item.recorded_transition_sequence,
        )
    _insert_coverage_superseded_exact_fill(
        conn,
        order_ref=facts.order_ref,
        exact=exact,
        clerk_observed_at_ms=payload["clerk_observed_at_ms"],
        recorded_at_ms=payload["recorded_at_ms"],
        recorded_transition_sequence=transition_sequence(conn),
    )
    if facts.resolved_uncertainty_id is not None:
        conn.execute(
            "UPDATE uncertainties SET resolved_at_ms = ? WHERE uncertainty_id = ? "
            "AND reason_code = 'EXECUTION_COVERAGE_CONFLICT' AND resolved_at_ms IS NULL",
            (payload["recorded_at_ms"], facts.resolved_uncertainty_id),
        )
    complete_manual_order(conn, payload=payload, order_ref=facts.order_ref)


def _insert_coverage_superseded_exact_fill(
    conn: sqlite3.Connection,
    *,
    order_ref: str,
    exact: ExecutionSliceFilledFacts,
    clerk_observed_at_ms: int,
    recorded_at_ms: int,
    recorded_transition_sequence: int,
) -> None:
    """Restore one exact effective row with its immutable source clocks."""
    conn.execute(
        "INSERT INTO fills (fill_id, order_ref, qty, price, side, is_correction, execution_id, "
        "evidence_source, event_kind, superseded_execution_ref, fee, fee_fidelity, "
        "source_event_at_ms, clerk_observed_at_ms, recorded_at_ms, recorded_transition_sequence) "
        "VALUES (?, ?, ?, ?, ?, 0, ?, ?, 'fill', NULL, ?, ?, ?, ?, ?, ?)",
        (
            exact.execution_id,
            order_ref,
            exact.slice_qty,
            exact.slice_price,
            exact.side,
            exact.execution_id,
            exact.evidence_source,
            exact.fee,
            exact.fee_fidelity,
            exact.source_event_at_ms,
            clerk_observed_at_ms,
            recorded_at_ms,
            recorded_transition_sequence,
        ),
    )
