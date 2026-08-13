"""Atomic evidence-bound resolution of quarantined execution coverage.

This focused mixin keeps the repository spine responsible for leases, mirror
fencing, and the raw append while keeping the closed recovery proof vocabulary
auditable in one small module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.broker.alpaca.clerk.sqlite import reads
from app.broker.alpaca.clerk.sqlite.execution_coverage import (
    active_execution_coverage_conflicts,
    execution_coverage_proof,
)
from app.broker.alpaca.clerk.sqlite.facts import (
    ExecutionCoverageResolvedFacts,
    validate_execution_coverage_resolved_facts,
)
from app.broker.alpaca.clerk.sqlite.models import (
    ExecutionCoverageResolutionReceipt,
    TransitionInput,
)

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository


class ExecutionCoverageResolutionUnavailable(Exception):
    """The current immutable evidence does not prove a safe replacement."""


class ClerkSqliteRepositoryExecutionCoverageApi:
    """Focused atomic mutations mixed into ``ClerkSqliteRepository``."""

    def resolve_execution_coverage_conflict(
        self: ClerkSqliteRepository,
        *,
        uncertainty_id: str,
        expected_authority_generation: int,
        expected_db_identity_token: str,
        expected_control_revision: int,
    ) -> ExecutionCoverageResolutionReceipt:
        """Replace one fully-covered cumulative fold with exact evidence once."""
        with self._write_lock:
            self._assert_not_poisoned()
            self._renew_execution_lease()
            existing = self._existing_coverage_resolution(uncertainty_id)
            if existing is not None:
                return existing
            meta = reads.control_meta_snapshot(self._conn)
            if (
                meta.authority_generation != expected_authority_generation
                or meta.db_identity_token != expected_db_identity_token
                or meta.control_revision != expected_control_revision
            ):
                raise ExecutionCoverageResolutionUnavailable(
                    "The Clerk authority or control revision changed; refresh the recovery evidence."
                )
            active = active_execution_coverage_conflicts(
                self._conn,
                uncertainty_id=uncertainty_id,
            )
            if len(active) != 1:
                raise ExecutionCoverageResolutionUnavailable(
                    "The selected execution-coverage conflict is no longer active."
                )
            conflict = active[0]
            proof = execution_coverage_proof(self._conn, conflict=conflict)
            if not proof.proof_available or proof.exact_execution is None or proof.cumulative is None:
                raise ExecutionCoverageResolutionUnavailable(
                    proof.unavailable_reason
                    or "The exact execution does not fully prove replacement of the cumulative recovery total."
                )
            exact = proof.exact_execution
            cumulative = proof.cumulative
            facts = ExecutionCoverageResolvedFacts(
                uncertainty_id=uncertainty_id,
                account_id=self.account_id,
                authority_generation=meta.authority_generation,
                db_identity_token=meta.db_identity_token,
                expected_control_revision=meta.control_revision,
                order_ref=conflict.order_ref,
                resolution_kind="EXACT_REPLACES_CUMULATIVE",
                replaced_cumulative_fill_id=cumulative.fill_id,
                exact_execution=exact,
                evidence_refs=sorted(
                    {conflict.order_ref, *proof.execution_ids, cumulative.fill_id}
                ),
            )
            validate_execution_coverage_resolved_facts(facts)
            committed = self.append_transition(
                TransitionInput(
                    strategy_instance_id=conflict.strategy_instance_id,
                    transition_kind="EXECUTION_COVERAGE_RESOLVED",
                    custody_owner="ACCOUNT_CLERK",
                    execution_authority="ACCOUNT_CLERK",
                    operation_state="succeeded",
                    order_ref=conflict.order_ref,
                    clerk_observed_at_ms=self._clock(),
                    source_event_at_ms=exact.source_event_at_ms,
                    summary_code="EXECUTION_COVERAGE_RESOLVED",
                    facts_json=facts.to_facts_json(),
                )
            )
            recorded_at_ms = self._conn.execute(
                "SELECT recorded_at_ms FROM custody_transitions WHERE sequence = ?",
                (committed.sequence,),
            ).fetchone()["recorded_at_ms"]
            return ExecutionCoverageResolutionReceipt(
                uncertainty_id=uncertainty_id,
                order_ref=conflict.order_ref,
                execution_id=exact.execution_id,
                receipt_id=f"coverage-resolution:{committed.sequence}",
                recorded_at_ms=recorded_at_ms,
                applied=True,
            )

    def execution_coverage_resolution_receipt(
        self: ClerkSqliteRepository,
        uncertainty_id: str,
    ) -> ExecutionCoverageResolutionReceipt | None:
        """Read an already-committed resolution for safe HTTP replay only."""
        with self._write_lock:
            return self._existing_coverage_resolution(uncertainty_id)

    def _existing_coverage_resolution(
        self: ClerkSqliteRepository,
        uncertainty_id: str,
    ) -> ExecutionCoverageResolutionReceipt | None:
        rows = self._conn.execute(
            "SELECT sequence, facts_json, recorded_at_ms FROM custody_transitions "
            "WHERE transition_kind = 'EXECUTION_COVERAGE_RESOLVED' ORDER BY sequence ASC"
        ).fetchall()
        for row in rows:
            try:
                facts = ExecutionCoverageResolvedFacts.from_facts_json(row["facts_json"])
                exact = validate_execution_coverage_resolved_facts(facts)
            except (TypeError, ValueError):
                continue
            if facts.uncertainty_id == uncertainty_id:
                return ExecutionCoverageResolutionReceipt(
                    uncertainty_id=uncertainty_id,
                    order_ref=facts.order_ref,
                    execution_id=exact.execution_id,
                    receipt_id=f"coverage-resolution:{row['sequence']}",
                    recorded_at_ms=row["recorded_at_ms"],
                    applied=False,
                )
        return None
