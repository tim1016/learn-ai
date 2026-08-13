"""Atomic evidence-bound resolution of quarantined execution coverage.

This focused mixin keeps the repository spine responsible for leases, mirror
fencing, and the raw append while keeping the closed recovery proof vocabulary
auditable in one small module.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

from app.broker.alpaca.clerk.sqlite import reads
from app.broker.alpaca.clerk.sqlite.facts import (
    ExecutionCoverageQuarantinedFacts,
    ExecutionCoverageResolvedFacts,
    ExecutionSliceFilledFacts,
    UncertaintyRaisedFacts,
    validate_execution_coverage_quarantined_facts,
    validate_execution_coverage_resolved_facts,
)
from app.broker.alpaca.clerk.sqlite.folds import FILL_QTY_EPSILON
from app.broker.alpaca.clerk.sqlite.models import (
    ExecutionCoverageResolutionReceipt,
    TransitionInput,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
    ExecutionCoverageConflictCause,
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
            active = self._conn.execute(
                "SELECT uncertainty_id, strategy_instance_id, facts_json FROM uncertainties "
                "WHERE uncertainty_id = ? AND reason_code = ? AND resolved_at_ms IS NULL",
                (uncertainty_id, EXECUTION_COVERAGE_CONFLICT_REASON_CODE),
            ).fetchone()
            if active is None:
                raise ExecutionCoverageResolutionUnavailable(
                    "The selected execution-coverage conflict is no longer active."
                )
            raised = UncertaintyRaisedFacts.from_facts_json(active["facts_json"])
            cause = ExecutionCoverageConflictCause.from_mapping(raised.cause_facts)
            quarantined = self._quarantined_execution(
                order_ref=cause.order_ref,
                execution_id=cause.execution_id,
            )
            if quarantined is None:
                raise ExecutionCoverageResolutionUnavailable(
                    "The exact execution quarantine is missing; no safe resolution can be proven."
                )
            exact = validate_execution_coverage_quarantined_facts(quarantined)
            cumulative_ids = quarantined.conflicting_cumulative_fill_ids
            if len(cumulative_ids) != 1:
                raise ExecutionCoverageResolutionUnavailable(
                    "The exact execution overlaps multiple cumulative recovery rows; fresh per-slice evidence is required."
                )
            cumulative = self._conn.execute(
                "SELECT fill_id, order_ref, qty, price, side, evidence_source FROM fills WHERE fill_id = ?",
                (cumulative_ids[0],),
            ).fetchone()
            if not _exact_replaces_cumulative(
                exact=exact,
                order_ref=cause.order_ref,
                cumulative=cumulative,
            ):
                raise ExecutionCoverageResolutionUnavailable(
                    "The exact execution does not fully prove replacement of the cumulative recovery total."
                )
            facts = ExecutionCoverageResolvedFacts(
                uncertainty_id=uncertainty_id,
                account_id=self.account_id,
                authority_generation=meta.authority_generation,
                db_identity_token=meta.db_identity_token,
                expected_control_revision=meta.control_revision,
                order_ref=cause.order_ref,
                resolution_kind="EXACT_REPLACES_CUMULATIVE",
                replaced_cumulative_fill_id=cumulative_ids[0],
                exact_execution=json.loads(exact.to_facts_json()),
                evidence_refs=sorted(
                    {cause.order_ref, cause.execution_id, cumulative_ids[0]}
                ),
            )
            validate_execution_coverage_resolved_facts(facts)
            committed = self.append_transition(
                TransitionInput(
                    strategy_instance_id=active["strategy_instance_id"],
                    transition_kind="EXECUTION_COVERAGE_RESOLVED",
                    custody_owner="ACCOUNT_CLERK",
                    execution_authority="ACCOUNT_CLERK",
                    operation_state="succeeded",
                    order_ref=cause.order_ref,
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
                order_ref=cause.order_ref,
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

    def _quarantined_execution(
        self: ClerkSqliteRepository,
        *,
        order_ref: str,
        execution_id: str,
    ) -> ExecutionCoverageQuarantinedFacts | None:
        rows = self._conn.execute(
            "SELECT facts_json FROM custody_transitions WHERE order_ref = ? "
            "AND transition_kind = 'EXECUTION_COVERAGE_QUARANTINED' ORDER BY sequence DESC",
            (order_ref,),
        ).fetchall()
        for row in rows:
            try:
                facts = ExecutionCoverageQuarantinedFacts.from_facts_json(row["facts_json"])
                exact = validate_execution_coverage_quarantined_facts(facts)
            except (TypeError, ValueError):
                continue
            if exact.execution_id == execution_id:
                return facts
        return None


def _exact_replaces_cumulative(
    *,
    exact: ExecutionSliceFilledFacts,
    order_ref: str,
    cumulative: sqlite3.Row | None,
) -> bool:
    """Closed proof vocabulary for S0: one equal aggregate is one slice."""
    return (
        cumulative is not None
        and cumulative["order_ref"] == order_ref
        and cumulative["evidence_source"] == "cumulative_recovery"
        and cumulative["side"] == exact.side
        and abs(float(cumulative["qty"]) - exact.slice_qty) < FILL_QTY_EPSILON
        and abs(float(cumulative["price"]) - exact.slice_price) < FILL_QTY_EPSILON
    )
