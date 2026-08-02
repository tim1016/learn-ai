"""Pure, typed first-Start admission policy for broker bot runs.

Projection endpoints and mutation paths must call :func:`evaluate_run_admission`
with authority-authored facts. The policy does not read files, call a broker,
or mutate a run, which keeps the displayed decision identical to the execution
gate.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.models import ClerkCustodySnapshot
from app.schemas.run_admission import (
    RunAdmissionDecision,
    RunAdmissionFactAges,
    StartRunFacts,
)

AUTHORITY_FACT_MAX_AGE_MS = 5_000


def _decision(
    bot: StartRunFacts,
    clerk: ClerkCustodySnapshot,
    *,
    evaluated_at_ms: int,
    fact_ages_ms: RunAdmissionFactAges,
    allowed: bool,
    reason_code: str,
    explanation: str,
    next_step: str | None,
) -> RunAdmissionDecision:
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
        evidence_refs=(
            f"bot-process-registry:{bot.process.registry_generation}",
            f"market-data-feed:{bot.market_data.feed_id or 'unknown'}:{bot.market_data.observed_at_ms}",
            *clerk.evidence_refs,
        ),
    )


def evaluate_run_admission(
    bot: StartRunFacts,
    clerk: ClerkCustodySnapshot,
    *,
    evaluated_at_ms: int,
) -> RunAdmissionDecision:
    """Decide Start from bot and Clerk facts only; unknown always blocks."""
    fact_ages_ms = RunAdmissionFactAges(
        runtime=evaluated_at_ms - bot.runtime.observed_at_ms,
        process=evaluated_at_ms - bot.process.observed_at_ms,
        market_data=evaluated_at_ms - bot.market_data.observed_at_ms,
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
            next_step="Refresh process, market-data, and Clerk evidence before Start.",
        )
    stale_authority = next(
        (authority for authority, age_ms in fact_ages.items() if age_ms > AUTHORITY_FACT_MAX_AGE_MS),
        None,
    )
    if stale_authority is not None:
        return decide(
            allowed=False,
            reason_code="AUTHORITY_FACT_STALE",
            explanation=(f"The {stale_authority} fact is older than the 5-second Start boundary."),
            next_step="Refresh process, market-data, and Clerk evidence before Start.",
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
            next_step="Recover the bot runner registry before Start.",
        )
    if bot.process.state != "ABSENT":
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
            next_step="Restore fresh market data before Start.",
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
            next_step=clerk.freeze.next_step or "Resolve the Clerk freeze before Start.",
        )
    if clerk.hold.active:
        return decide(
            allowed=False,
            reason_code=clerk.hold.reason_code or "CLERK_HOLD_ACTIVE",
            explanation=clerk.hold.reason or "The Clerk is holding new exposure.",
            next_step="Resolve the Clerk hold before Start.",
        )
    if clerk.exposure.state == "unknown":
        return decide(
            allowed=False,
            reason_code="CLERK_EXPOSURE_UNKNOWN",
            explanation="The Clerk cannot prove the instance exposure state.",
            next_step="Reconcile exposure through the Clerk before Start.",
        )
    if clerk.exposure.state == "non_zero":
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
            next_step="Reconcile all order and effect work before Start.",
        )
    remaining = next((label for label, fact in unresolved if fact.state == "non_zero"), None)
    if remaining is not None:
        return decide(
            allowed=False,
            reason_code="CLERK_WORK_REMAINS",
            explanation=f"The Clerk proves that unresolved {remaining} remain.",
            next_step="Resolve the remaining Clerk work before Start.",
        )
    return decide(
        allowed=True,
        reason_code="START_ADMITTED",
        explanation="The process slot is absent, market data is ready, and the Clerk proves flat custody.",
        next_step=None,
    )
