"""Pure operator decisions for the closed exact-execution coverage proof."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.broker.alpaca.clerk.sqlite.projection_models import (
    ProjectedExecutionCoverageConflict,
    RecoveryEvidence,
)


@dataclass(frozen=True)
class ExecutionCoverageRecoveryDecision:
    """Policy output adapted by the general recovery catalog."""

    available: bool
    reason_code: str | None
    reason: str | None
    freshness: Literal["fresh", "stale", "not_required", "unavailable"]
    evidence: tuple[RecoveryEvidence, ...]
    next_step: str
    token_facts: object
    execution_ref: str | None = None


def coverage_resolution_decision(
    *,
    control_revision: int,
    conflicts: tuple[ProjectedExecutionCoverageConflict, ...],
) -> ExecutionCoverageRecoveryDecision:
    """Expose only the closed one-exact-for-one-cumulative replacement proof."""
    if not conflicts:
        return ExecutionCoverageRecoveryDecision(
            available=False,
            reason_code="NO_EXECUTION_COVERAGE_CONFLICT",
            reason="No active execution-coverage conflict has a Clerk-owned resolution path.",
            freshness="unavailable",
            evidence=(),
            next_step="No coverage resolution is required.",
            token_facts=(),
        )
    if len(conflicts) != 1:
        return ExecutionCoverageRecoveryDecision(
            available=False,
            reason_code="COVERAGE_RESOLUTION_SELECTION_REQUIRED",
            reason="More than one coverage conflict is active; inspect each custody timeline before resolving one.",
            freshness="unavailable",
            evidence=tuple(
                RecoveryEvidence(
                    reference=f"order:{conflict.order_ref}",
                    label="Conflicting order",
                    observed_at_ms=None,
                    age_ms=None,
                    freshness="unavailable",
                )
                for conflict in conflicts
            ),
            next_step="Open the relevant bot custody timeline and present one conflict at a time.",
            token_facts=[conflict.uncertainty_id for conflict in conflicts],
        )
    conflict = conflicts[0]
    execution_ids = conflict.execution_ids or (conflict.execution_id,)
    evidence = tuple(
        RecoveryEvidence(
            reference=reference,
            label=label,
            observed_at_ms=None,
            age_ms=None,
            freshness="fresh" if conflict.proof_available else "unavailable",
        )
        for reference, label in (
            [(f"order:{conflict.order_ref}", "Clerk order reference")]
            + [
                (f"execution:{execution_id}", "Quarantined exact execution")
                for execution_id in execution_ids
            ]
            + [
                (
                    f"fill:{conflict.cumulative_fill_id or 'missing'}",
                    "Cumulative recovery fill",
                )
            ]
        )
    )
    return ExecutionCoverageRecoveryDecision(
        available=conflict.proof_available,
        reason_code=None if conflict.proof_available else "COVERAGE_PROOF_INSUFFICIENT",
        reason=(
            None
            if conflict.proof_available
            else conflict.unavailable_reason
            or "The Clerk cannot prove that the exact execution replaces the cumulative total."
        ),
        freshness="not_required" if conflict.proof_available else "unavailable",
        evidence=evidence,
        next_step=(
            "Confirm the exact replacement; no broker order will be submitted."
            if conflict.proof_available
            else "Keep the exposure blocked and obtain exact broker execution coverage; no generic override is available."
        ),
        token_facts=(
            control_revision,
            conflict.uncertainty_id,
            conflict.order_ref,
            conflict.execution_id,
            conflict.execution_ids,
            conflict.cumulative_fill_id,
            conflict.exact_qty,
            conflict.exact_price,
            conflict.exact_side,
            conflict.cumulative_qty,
            conflict.cumulative_price,
            conflict.cumulative_side,
            conflict.proof_available,
        ),
        execution_ref=conflict.uncertainty_id,
    )


def historical_exact_recovery_decision(
    *,
    strategy_instance_id: str | None,
    control_revision: int,
    conflicts: tuple[ProjectedExecutionCoverageConflict, ...],
) -> ExecutionCoverageRecoveryDecision:
    """Offer read-only broker recovery only for a bot's missing exact slice."""
    if not conflicts:
        return ExecutionCoverageRecoveryDecision(
            available=False,
            reason_code="NO_EXECUTION_COVERAGE_CONFLICT",
            reason="No active execution-coverage conflict requires historical evidence recovery.",
            freshness="unavailable",
            evidence=(),
            next_step="No historical execution recovery is required.",
            token_facts=(),
        )
    if strategy_instance_id is None:
        return ExecutionCoverageRecoveryDecision(
            available=False,
            reason_code="BOT_SCOPE_REQUIRED",
            reason="Historical exact-execution recovery must be bound to one affected bot.",
            freshness="unavailable",
            evidence=tuple(
                RecoveryEvidence(
                    reference=f"order:{conflict.order_ref}",
                    label="Conflicting bot order",
                    observed_at_ms=None,
                    age_ms=None,
                    freshness="unavailable",
                )
                for conflict in conflicts
            ),
            next_step="Open the affected bot Operator panel and prepare one historical recovery.",
            token_facts=[conflict.uncertainty_id for conflict in conflicts],
        )
    if len(conflicts) != 1:
        return ExecutionCoverageRecoveryDecision(
            available=False,
            reason_code="COVERAGE_RECOVERY_SELECTION_REQUIRED",
            reason="More than one coverage conflict is active for this bot.",
            freshness="unavailable",
            evidence=(),
            next_step="Inspect the bot custody timeline and select one conflict at a time.",
            token_facts=[conflict.uncertainty_id for conflict in conflicts],
        )
    conflict = conflicts[0]
    available = not conflict.execution_ids and conflict.cumulative_fill_id is not None
    if available:
        reason_code = None
        reason = None
        next_step = "Prepare read-only Alpaca paper execution evidence for this conflict."
    elif conflict.proof_available:
        reason_code = "EXACT_EVIDENCE_ALREADY_AVAILABLE"
        reason = "Exact execution evidence is already quarantined; use Resolve execution coverage."
        next_step = "Resolve the already-proven exact coverage replacement."
    else:
        reason_code = "HISTORICAL_RECOVERY_PROOF_UNAVAILABLE"
        reason = conflict.unavailable_reason or "The Clerk does not have a recoverable one-fill historical conflict."
        next_step = "Keep the exposure blocked and inspect the exact custody evidence."
    return ExecutionCoverageRecoveryDecision(
        available=available,
        reason_code=reason_code,
        reason=reason,
        freshness="not_required" if available else "unavailable",
        evidence=(
            RecoveryEvidence(
                reference=f"order:{conflict.order_ref}",
                label="Clerk order reference",
                observed_at_ms=None,
                age_ms=None,
                freshness="fresh" if available else "unavailable",
            ),
            RecoveryEvidence(
                reference=f"execution:{conflict.execution_id}",
                label="Retained Alpaca execution identity",
                observed_at_ms=None,
                age_ms=None,
                freshness="fresh" if available else "unavailable",
            ),
            RecoveryEvidence(
                reference=f"fill:{conflict.cumulative_fill_id or 'missing'}",
                label="Cumulative recovery fill",
                observed_at_ms=None,
                age_ms=None,
                freshness="fresh" if available else "unavailable",
            ),
        ),
        next_step=next_step,
        token_facts=(
            control_revision,
            conflict.uncertainty_id,
            conflict.order_ref,
            conflict.execution_id,
            conflict.execution_ids,
            conflict.cumulative_fill_id,
            conflict.cumulative_qty,
            conflict.cumulative_price,
            conflict.cumulative_side,
        ),
        execution_ref=conflict.uncertainty_id,
    )
