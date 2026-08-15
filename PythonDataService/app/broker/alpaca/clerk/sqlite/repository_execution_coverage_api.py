"""Atomic evidence-bound resolution of quarantined execution coverage.

This focused mixin keeps the repository spine responsible for leases, mirror
fencing, and the raw append while keeping the closed recovery proof vocabulary
auditable in one small module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn

from app.broker.alpaca.clerk.sqlite import reads
from app.broker.alpaca.clerk.sqlite.execution_coverage import (
    PRICE_ATOL,
    QTY_ATOL,
    CumulativeRecoveryFill,
    ExecutionCoverageIdentity,
    ExecutionCoverageSetProofSuccess,
    ExecutionCoverageSupersededFacts,
    active_execution_coverage_conflicts,
    cumulative_recovery_fills_for_order,
    direct_cumulative_recovery_fill_for_order,
    direct_execution_coverage_candidate,
    exact_replaces_cumulative,
    execution_coverage_proof,
    execution_is_quarantined,
    prove_execution_coverage_set,
    validate_execution_coverage_superseded_facts,
)
from app.broker.alpaca.clerk.sqlite.facts import (
    ExecutionCoverageResolvedFacts,
    ExecutionSliceFilledFacts,
    validate_execution_coverage_resolved_facts,
)
from app.broker.alpaca.clerk.sqlite.models import (
    ExecutionCoverageResolutionReceipt,
    OrderResource,
    TransitionInput,
)

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository


class ExecutionCoverageResolutionUnavailable(Exception):
    """The current immutable evidence does not prove a safe replacement."""


@dataclass(frozen=True)
class HistoricalExecutionRecoveryTarget:
    """One bot-owned historical conflict whose exact slice is absent."""

    uncertainty_id: str
    strategy_instance_id: str
    order_ref: str
    execution_id: str
    observed_at_ms: int
    symbol: str
    exact_is_quarantined: bool
    cumulative: CumulativeRecoveryFill
    order: OrderResource


class ClerkSqliteRepositoryExecutionCoverageApi:
    """Focused atomic mutations mixed into ``ClerkSqliteRepository``."""

    def _direct_execution_coverage_supersession_transition(
        self: ClerkSqliteRepository,
        *,
        exact_transition: TransitionInput,
        facts: ExecutionSliceFilledFacts,
    ) -> TransitionInput | None:
        """Build the narrow #1554 automatic replacement when proof is closed.

        This method reads the complete direct evidence set under the caller's
        repository write coordinator. The fold repeats the same proof inside
        the append transaction before it mutates ``fills``; both checks are
        required because a planned transition is immutable once mirrored.
        """
        if exact_transition.order_ref is None:
            raise ValueError("direct coverage supersession requires an order reference")
        if active_execution_coverage_conflicts(self._conn, order_ref=exact_transition.order_ref):
            return None
        cumulative = direct_cumulative_recovery_fill_for_order(
            self._conn,
            order_ref=exact_transition.order_ref,
        )
        if cumulative is None:
            return None
        meta = reads.control_meta_snapshot(self._conn)
        identity = ExecutionCoverageIdentity(
            account_id=meta.account_id,
            authority_generation=meta.authority_generation,
            database_identity_token=meta.db_identity_token,
            order_ref=exact_transition.order_ref,
            symbol=facts.symbol,
            side=facts.side,
        )
        proof = prove_execution_coverage_set(
            direct_execution_coverage_candidate(
                identity=identity,
                cumulative=cumulative,
                exact=facts,
            )
        )
        if not isinstance(proof, ExecutionCoverageSetProofSuccess):
            return None
        superseded_facts = ExecutionCoverageSupersededFacts(
            actor="AUTOMATIC",
            account_id=meta.account_id,
            authority_generation=meta.authority_generation,
            db_identity_token=meta.db_identity_token,
            expected_control_revision=meta.control_revision,
            order_ref=exact_transition.order_ref,
            symbol=facts.symbol,
            side=facts.side,
            superseded_cumulative_fill_ids=[cumulative.fill_id],
            exact_execution=facts,
            exact_quantity=proof.exact.quantity,
            exact_gross_cost=proof.exact.gross_cost,
            cumulative_quantity=proof.cumulative.quantity,
            cumulative_gross_cost=proof.cumulative.gross_cost,
            quantity_tolerance=QTY_ATOL,
            gross_cost_tolerance=max(proof.exact.quantity, proof.cumulative.quantity) * PRICE_ATOL,
            resolved_uncertainty_id=None,
            evidence_refs=sorted(
                {
                    f"execution:{facts.execution_id}",
                    f"fill:{cumulative.fill_id}",
                    f"order:{exact_transition.order_ref}",
                }
            ),
        )
        validate_execution_coverage_superseded_facts(superseded_facts)
        return TransitionInput(
            strategy_instance_id=exact_transition.strategy_instance_id,
            run_id=exact_transition.run_id,
            command_id=exact_transition.command_id,
            effect_operation_id=exact_transition.effect_operation_id,
            order_ref=exact_transition.order_ref,
            broker_order_id=exact_transition.broker_order_id,
            transition_kind="EXECUTION_COVERAGE_SUPERSEDED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="in_progress",
            proof_reference=exact_transition.proof_reference,
            source_event_at_ms=facts.source_event_at_ms,
            clerk_observed_at_ms=exact_transition.clerk_observed_at_ms,
            summary_code="EXECUTION_COVERAGE_SUPERSEDED",
            facts_json=superseded_facts.to_facts_json(),
        )

    def historical_execution_recovery_target(
        self: ClerkSqliteRepository,
        uncertainty_id: str,
    ) -> HistoricalExecutionRecoveryTarget:
        """Read one closed historical-recovery target without writing custody."""
        with self._write_lock:
            active = active_execution_coverage_conflicts(
                self._conn,
                uncertainty_id=uncertainty_id,
            )
            if len(active) != 1:
                raise ExecutionCoverageResolutionUnavailable(
                    "The selected execution-coverage conflict is no longer active."
                )
            conflict = active[0]
            if conflict.strategy_instance_id is None:
                raise ExecutionCoverageResolutionUnavailable(
                    "Historical exact-evidence recovery requires a bot-owned conflict."
                )
            exact_is_quarantined = execution_is_quarantined(
                self._conn,
                conflict=conflict,
                execution_id=conflict.conflict_execution_id,
            )
            cumulative = cumulative_recovery_fills_for_order(
                self._conn,
                order_ref=conflict.order_ref,
            )
            if len(cumulative) != 1:
                raise ExecutionCoverageResolutionUnavailable(
                    "Historical exact-evidence recovery requires exactly one cumulative recovery fill."
                )
            order = reads.order(self._conn, conflict.order_ref)
            if order is None:
                raise ExecutionCoverageResolutionUnavailable(
                    "The conflict no longer has its Clerk-owned order identity."
                )
            strategy = reads.strategy_instance(
                self._conn,
                conflict.strategy_instance_id,
            )
            uncertainty = self._conn.execute(
                "SELECT observed_at_ms FROM uncertainties WHERE uncertainty_id = ?",
                (uncertainty_id,),
            ).fetchone()
            if strategy is None or uncertainty is None:
                raise ExecutionCoverageResolutionUnavailable(
                    "The selected historical conflict no longer has complete Clerk ownership evidence."
                )
            return HistoricalExecutionRecoveryTarget(
                uncertainty_id=uncertainty_id,
                strategy_instance_id=conflict.strategy_instance_id,
                order_ref=conflict.order_ref,
                execution_id=conflict.conflict_execution_id,
                observed_at_ms=uncertainty["observed_at_ms"],
                symbol=strategy["symbol"],
                exact_is_quarantined=exact_is_quarantined,
                cumulative=cumulative[0],
                order=order,
            )

    def recover_historical_execution_coverage(
        self: ClerkSqliteRepository,
        *,
        uncertainty_id: str,
        strategy_instance_id: str,
        expected_authority_generation: int,
        expected_db_identity_token: str,
        expected_control_revision: int,
        expected_order_ref: str,
        expected_broker_order_id: str,
        expected_cumulative_fill_id: str,
        exact: ExecutionSliceFilledFacts,
    ) -> ExecutionCoverageResolutionReceipt:
        """Append missing exact evidence and resolve its closed no-delta proof.

        The repository writer remains held for both append-only transitions, so
        no other Clerk action can observe or mutate the intermediate quarantine.
        Each transition still uses the pinned mirror-fenced commit primitive.
        """
        with self._write_lock:
            self._assert_not_poisoned()
            self._renew_execution_lease()
            existing = self._existing_coverage_resolution(uncertainty_id)
            if existing is not None:
                return existing
            target = self.historical_execution_recovery_target(uncertainty_id)
            meta = reads.control_meta_snapshot(self._conn)
            revision_matches_plan = meta.control_revision == expected_control_revision
            revision_is_only_quarantine = (
                meta.control_revision == expected_control_revision + 1
                and target.exact_is_quarantined
            )
            if (
                meta.authority_generation != expected_authority_generation
                or meta.db_identity_token != expected_db_identity_token
                or not (revision_matches_plan or revision_is_only_quarantine)
            ):
                raise ExecutionCoverageResolutionUnavailable(
                    "The Clerk authority or control revision changed; refresh the recovery evidence."
                )
            if (
                target.strategy_instance_id != strategy_instance_id
                or target.order_ref != expected_order_ref
                or target.order.broker_order_id != expected_broker_order_id
                or target.cumulative.fill_id != expected_cumulative_fill_id
                or target.execution_id != exact.execution_id
                or target.symbol.upper() != exact.symbol.upper()
            ):
                raise ExecutionCoverageResolutionUnavailable(
                    "The signed recovery plan no longer matches the selected Clerk evidence."
                )
            if not exact_replaces_cumulative(exact=exact, cumulative=target.cumulative):
                raise ExecutionCoverageResolutionUnavailable(
                    "The signed exact execution no longer matches the cumulative recovery evidence."
                )
            effect = reads.effect_operation(
                self._conn,
                target.order.effect_operation_id,
            )
            if effect is None or effect.strategy_instance_id != strategy_instance_id:
                raise ExecutionCoverageResolutionUnavailable(
                    "The Clerk order no longer has its bot-owned effect identity."
                )
            outcome = self.append_execution_slice_if_absent(
                execution_id=exact.execution_id,
                order_ref=target.order_ref,
                build_transition=lambda: TransitionInput(
                    strategy_instance_id=effect.strategy_instance_id,
                    run_id=effect.run_id,
                    command_id=effect.command_id,
                    effect_operation_id=effect.effect_operation_id,
                    order_ref=target.order_ref,
                    broker_order_id=target.order.broker_order_id,
                    transition_kind="EXECUTION_SLICE_FILLED",
                    custody_owner="ACCOUNT_CLERK",
                    execution_authority="ACCOUNT_CLERK",
                    operation_state="in_progress",
                    source_event_at_ms=exact.source_event_at_ms,
                    clerk_observed_at_ms=self._clock(),
                    summary_code="EXECUTION_SLICE_FILLED",
                    facts_json=exact.to_facts_json(),
                ),
                build_coverage_conflict=lambda: _unexpected_historical_conflict(),
            )
            if outcome not in {
                "coverage_conflict_quarantined",
                "coverage_conflict_already_raised",
            }:
                raise ExecutionCoverageResolutionUnavailable(
                    "The Clerk could not retain the recovered exact execution safely."
                )
            current = reads.control_meta_snapshot(self._conn)
            return self.resolve_execution_coverage_conflict(
                uncertainty_id=uncertainty_id,
                expected_authority_generation=current.authority_generation,
                expected_db_identity_token=current.db_identity_token,
                expected_control_revision=current.control_revision,
            )

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


def _unexpected_historical_conflict() -> NoReturn:
    """Fail closed if a recovery plan lost the already-active conflict race."""
    raise ExecutionCoverageResolutionUnavailable(
        "The historical conflict changed while exact evidence was being retained."
    )
