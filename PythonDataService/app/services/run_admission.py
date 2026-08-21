"""Pure, typed Start and Resume admission policy for broker bot runs.

Projection endpoints and mutation paths must call :func:`evaluate_run_admission`
with authority-authored facts. The policy does not read files, call a broker,
or mutate a run, which keeps the displayed decision identical to the execution
gate.
"""

from __future__ import annotations

import logging
import math

from app.broker.alpaca.clerk.models import ClerkCustodySnapshot
from app.schemas.run_admission import (
    ResumeRunFacts,
    RunAdmissionDecision,
    RunAdmissionFactAges,
    RunAdmissionFacts,
)

AUTHORITY_FACT_MAX_AGE_MS = 5_000
_QTY_TOLERANCE_ULPS = 4

logger = logging.getLogger(__name__)


def _exposure_matches(
    checkpoint: dict[str, float],
    observed: dict[str, float] | None,
) -> bool:
    """Compare custody quantities without rejecting JSON float round-off.

    Custody contracts currently do not carry a broker minimum-increment field,
    so the narrow safe tolerance is four IEEE-754 ULPs at each quantity. This
    accepts serialization noise only; it cannot hide a tradable quantity move.
    """
    if observed is None or checkpoint.keys() != observed.keys():
        return False
    return all(
        math.isclose(
            checkpoint[symbol],
            observed[symbol],
            rel_tol=0.0,
            abs_tol=max(
                math.ulp(checkpoint[symbol]),
                math.ulp(observed[symbol]),
            )
            * _QTY_TOLERANCE_ULPS,
        )
        for symbol in checkpoint
    )


def _decision(
    bot: RunAdmissionFacts,
    clerk: ClerkCustodySnapshot,
    *,
    evaluated_at_ms: int,
    fact_ages_ms: RunAdmissionFactAges,
    allowed: bool,
    reason_code: str,
    explanation: str,
    next_step: str | None,
) -> RunAdmissionDecision:
    evidence_refs = [
        f"bot-process-registry:{bot.process.registry_generation}",
        f"market-data-feed:{bot.market_data.feed_id or 'unknown'}:{bot.market_data.observed_at_ms}",
        (
            "market-liveness-clock:"
            f"{bot.market_liveness.market_clock.source}:"
            f"{bot.market_liveness.market_clock.observed_at_ms}"
        ),
        *clerk.evidence_refs,
    ]
    if bot.market_liveness.symbol_status is not None:
        evidence_refs.append(
            "market-liveness-symbol:"
            f"{bot.market_liveness.symbol_status.source}:"
            f"{bot.market_liveness.symbol_status.observed_at_ms}"
        )
    if isinstance(bot, ResumeRunFacts) and bot.checkpoint is not None:
        evidence_refs.append(bot.checkpoint.evidence_ref)
    return RunAdmissionDecision(
        operation=bot.operation,
        allowed=allowed,
        reason_code=reason_code,
        explanation=explanation,
        next_step=next_step,
        strategy_instance_id=bot.strategy_instance_id,
        proposed_run_id=bot.proposed_run_id,
        configuration_hash=bot.configuration_hash,
        account_id=clerk.account_id,
        evaluated_at_ms=evaluated_at_ms,
        fact_ages_ms=fact_ages_ms,
        evidence_refs=tuple(evidence_refs),
    )


def evaluate_run_admission(
    bot: RunAdmissionFacts,
    clerk: ClerkCustodySnapshot,
    *,
    evaluated_at_ms: int,
) -> RunAdmissionDecision:
    """Decide Start or Resume from bot and Clerk facts; unknown blocks."""
    fact_ages_ms = RunAdmissionFactAges(
        runtime=evaluated_at_ms - bot.runtime.observed_at_ms,
        process=evaluated_at_ms - bot.process.observed_at_ms,
        market_data=evaluated_at_ms - bot.market_data.observed_at_ms,
        market_liveness=evaluated_at_ms - bot.market_liveness.observed_at_ms,
        clerk=evaluated_at_ms - clerk.observed_at_ms,
    )

    def decide(
        *,
        allowed: bool,
        reason_code: str,
        explanation: str,
        next_step: str | None,
    ) -> RunAdmissionDecision:
        return _decision(
            bot,
            clerk,
            evaluated_at_ms=evaluated_at_ms,
            fact_ages_ms=fact_ages_ms,
            allowed=allowed,
            reason_code=reason_code,
            explanation=explanation,
            next_step=next_step,
        )

    fact_ages = fact_ages_ms.model_dump()
    if any(age < 0 for age in fact_ages.values()):
        return decide(
            allowed=False,
            reason_code="AUTHORITY_CLOCK_INVALID",
            explanation="An authority fact is dated after this admission evaluation.",
            next_step=f"Refresh process, market-data, and Clerk evidence before {bot.operation.title()}.",
        )
    stale_authority = next(
        (authority for authority, age_ms in fact_ages.items() if age_ms > AUTHORITY_FACT_MAX_AGE_MS),
        None,
    )
    if stale_authority is not None:
        return decide(
            allowed=False,
            reason_code="AUTHORITY_FACT_STALE",
            explanation=(
                f"The {stale_authority} fact is older than the 5-second "
                f"{bot.operation.title()} boundary."
            ),
            next_step=f"Refresh process, market-data, and Clerk evidence before {bot.operation.title()}.",
        )
    if clerk.strategy_instance_id != bot.strategy_instance_id:
        return decide(
            allowed=False,
            reason_code="CUSTODY_INSTANCE_MISMATCH",
            explanation="The Clerk custody proof belongs to a different strategy instance.",
            next_step="Refresh the selected instance from the Clerk.",
        )
    if bot.runtime.state != "READY":
        return decide(
            allowed=False,
            reason_code=bot.runtime.state,
            explanation=bot.runtime.explanation,
            next_step=bot.runtime.next_step,
        )
    if bot.process.state == "UNKNOWN":
        return decide(
            allowed=False,
            reason_code="PROCESS_STATE_UNKNOWN",
            explanation="The process registry cannot prove whether this instance is already running.",
            next_step=f"Recover the bot runner registry before {bot.operation.title()}.",
        )
    if bot.operation == "START" and bot.process.state != "ABSENT":
        active = bot.process.state in {"STARTING", "RUNNING", "STOPPING"}
        return decide(
            allowed=False,
            reason_code=("RUN_ALREADY_ACTIVE" if active else "STRATEGY_INSTANCE_ALREADY_EXISTS"),
            explanation=(
                "This strategy instance already has an active process-owned run."
                if active
                else "This strategy instance already exists and cannot be started as a new instance."
            ),
            next_step=(
                "Use the existing run controls."
                if active
                else "Use Resume for the unchanged instance, or create a new instance ID."
            ),
        )
    if isinstance(bot, ResumeRunFacts):
        if bot.process.state in {"STARTING", "RUNNING", "STOPPING"}:
            return decide(
                allowed=False,
                reason_code="RUN_ALREADY_ACTIVE",
                explanation="This strategy instance already has an active process-owned run.",
                next_step="Use the current run controls instead of creating another run.",
            )
        if bot.process.state != "EXITED" or bot.process.run_id != bot.prior_run_id:
            return decide(
                allowed=False,
                reason_code="RESUME_PROCESS_NOT_TERMINAL",
                explanation="The process registry cannot prove the prior run exited terminally.",
                next_step="Recover terminal process evidence before Resume.",
            )
        if bot.phase == "RETIRED":
            return decide(
                allowed=False,
                reason_code="BOT_RETIRED",
                explanation="A retired strategy instance cannot create another run.",
                next_step="Deploy a replacement strategy instance with a new ID.",
            )
        if bot.phase != "OFF_DUTY" or bot.desired_state != "STOPPED":
            return decide(
                allowed=False,
                reason_code="RESUME_REQUIRES_STOPPED_INSTANCE",
                explanation="Resume requires an off-duty instance with durable STOPPED intent.",
                next_step="Resolve the prior lifecycle transition before Resume.",
            )
    if bot.market_data.state != "AVAILABLE":
        reason_codes = {
            "STALE": "MARKET_DATA_STALE",
            "UNAVAILABLE": "MARKET_DATA_UNAVAILABLE",
            "UNKNOWN": "MARKET_DATA_UNKNOWN",
        }
        return decide(
            allowed=False,
            reason_code=reason_codes[bot.market_data.state],
            explanation="The required market-data feed is not proven ready for this run.",
            next_step=f"Restore fresh market data before {bot.operation.title()}.",
        )
    # #1702: everything from here down is either an execution-channel gate or
    # a custody/evidence gate — Dry Run makes no broker contact, holds no
    # custody, and never carries exposure, so none of it applies to Dry Run.
    # The guard is additive-only: trade/paper (and log_only) fall through
    # unchanged, byte-for-byte, to the exact checks that existed before this
    # mode ever existed.
    if bot.mode != "dry_run":
        if bot.market_liveness.state != "TRADABLE":
            reason_codes = {
                "HALTED": "MARKET_LIVENESS_HALTED",
                "CLOSED": "MARKET_LIVENESS_CLOSED",
                "UNKNOWN": "MARKET_LIVENESS_UNKNOWN",
            }
            return decide(
                allowed=False,
                reason_code=reason_codes[bot.market_liveness.state],
                explanation=bot.market_liveness.reason,
                next_step=(
                    f"Wait for fresh market-wide and symbol trading-status evidence before {bot.operation.title()}."
                ),
            )
        if clerk.reconciliation_state != "clean" or not clerk.reconciliation_fresh:
            return decide(
                allowed=False,
                reason_code=clerk.reason_code,
                explanation="The Clerk cannot currently prove reconciled custody for this instance.",
                next_step=clerk.next_step or "Reconcile the account through the Clerk.",
            )
        if clerk.freeze.active:
            return decide(
                allowed=False,
                reason_code=clerk.freeze.category or "CLERK_FREEZE_ACTIVE",
                explanation=clerk.freeze.explanation or "The Clerk has frozen new exposure.",
                next_step=clerk.freeze.next_step or f"Resolve the Clerk freeze before {bot.operation.title()}.",
            )
        if clerk.hold.active:
            return decide(
                allowed=False,
                reason_code=clerk.hold.reason_code or "CLERK_HOLD_ACTIVE",
                explanation=clerk.hold.reason or "The Clerk is holding new exposure.",
                next_step=f"Resolve the Clerk hold before {bot.operation.title()}.",
            )
        if clerk.exposure.state == "unknown":
            return decide(
                allowed=False,
                reason_code="CLERK_EXPOSURE_UNKNOWN",
                explanation="The Clerk cannot prove the instance exposure state.",
                next_step=f"Reconcile exposure through the Clerk before {bot.operation.title()}.",
            )
        if clerk.exposure.state == "non_zero" and bot.operation == "START":
            return decide(
                allowed=False,
                reason_code="START_REQUIRES_FLAT_CUSTODY",
                explanation="The Clerk proves that this instance already has attributed exposure.",
                next_step="Use Resume for approved carryover, or flatten through the Clerk.",
            )

        unresolved = (
            ("working orders", clerk.working_orders),
            ("pending orders", clerk.pending_orders),
            ("effects", clerk.unresolved_effects),
        )
        unknown = next((label for label, fact in unresolved if fact.state == "unknown"), None)
        if unknown is not None:
            return decide(
                allowed=False,
                reason_code="CLERK_WORK_STATE_UNKNOWN",
                explanation=f"The Clerk cannot prove the state of {unknown}.",
                next_step=f"Reconcile all order and effect work before {bot.operation.title()}.",
            )
        remaining = next((label for label, fact in unresolved if fact.state == "non_zero"), None)
        if remaining is not None:
            return decide(
                allowed=False,
                reason_code="CLERK_WORK_REMAINS",
                explanation=f"The Clerk proves that unresolved {remaining} remain.",
                next_step=f"Resolve the remaining Clerk work before {bot.operation.title()}.",
            )
        if isinstance(bot, ResumeRunFacts) and clerk.exposure.state == "non_zero":
            if not bot.exposure_carryover_supported:
                return decide(
                    allowed=False,
                    reason_code="RESUME_CARRYOVER_UNSUPPORTED",
                    explanation="This strategy cannot safely restore its prior open-position lifecycle.",
                    next_step="Flatten the exact Clerk-attributed exposure before Resume.",
                )
            if bot.carryover_policy != "ALLOW" or not bot.carryover_account_policy_enabled:
                return decide(
                    allowed=False,
                    reason_code="RESUME_CARRYOVER_NOT_ALLOWED",
                    explanation=(
                        "The stopped exposure is not approved by both the immutable instance and account policy."
                    ),
                    next_step="Flatten the exact Clerk-attributed exposure before Resume.",
                )
            checkpoint = bot.checkpoint
            if checkpoint is None or not checkpoint.approved:
                return decide(
                    allowed=False,
                    reason_code="RESUME_CHECKPOINT_MISSING",
                    explanation="No approved terminal STOP checkpoint proves this carried exposure.",
                    next_step="Flatten the attributed exposure or restore the approved STOP checkpoint.",
                )
            matches = (
                checkpoint.account_id == clerk.account_id
                and checkpoint.stopped_run_id == bot.prior_run_id
                and checkpoint.configuration_hash == bot.configuration_hash
                and _exposure_matches(checkpoint.exposure, clerk.exposure.positions)
            )
            if not matches:
                logger.warning(
                    "Resume checkpoint custody does not match current Clerk exposure",
                    extra={
                        "action": "resume_checkpoint_mismatch",
                        "strategy_instance_id": bot.strategy_instance_id,
                        "checkpoint_exposure": checkpoint.exposure,
                        "clerk_exposure": clerk.exposure.positions,
                    },
                )
                return decide(
                    allowed=False,
                    reason_code="RESUME_CHECKPOINT_MISMATCH",
                    explanation=(
                        "The carryover custody proof changed: account, prior run, "
                        "configuration, or Clerk-attributed exposure no longer matches STOP."
                    ),
                    next_step="Reconcile and flatten rather than adopting changed custody.",
                )
    return decide(
        allowed=True,
        reason_code=f"{bot.operation}_ADMITTED",
        explanation=(
            "The process slot is absent, market data is ready, and the Clerk proves flat custody."
            if bot.operation == "START"
            else "The prior run is terminal, market data is ready, and the Clerk proves resumable custody."
        ),
        next_step=None,
    )
