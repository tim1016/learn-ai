"""Pure, typed Deploy admission policy for broker bot runs.

Projection endpoints and mutation paths must call :func:`evaluate_run_admission`
with authority-authored facts. The policy does not read files, call a broker,
or mutate a run, which keeps the displayed decision identical to the execution
gate.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.broker.alpaca.clerk.models import CLERK_CUSTODY_BROKER_UNREACHABLE, ClerkCustodySnapshot
from app.schemas.market_liveness import MarketLivenessFact
from app.schemas.run_admission import (
    CORPUS_UNCOVERED_NEXT_STEP,
    MarketDataAdmissionFact,
    RunAdmissionDecision,
    RunAdmissionFactAges,
    RunAdmissionFacts,
)
from app.services.canary_admission import canary_gate_applies, canary_pairing_admitted
from app.services.deploy_window import start_window_next_step

AUTHORITY_FACT_MAX_AGE_MS = 5_000
CORPUS_UNCOVERED_ADMITTED_NOTE = (
    "Corpus coverage is UNCOVERED: Dry Run admits this exploratory configuration without broker orders. "
    "It is not citable as qualification evidence."
)

@dataclass(frozen=True)
class _MarketSide:
    """One symbol a Start needs live, and how a refusal about it names it."""

    market_data: MarketDataAdmissionFact
    market_liveness: MarketLivenessFact
    #: ``None`` for a bot that trades the symbol it listens to: it has one
    #: symbol, and a refusal about it needs no name.
    named: str | None

    def say(self, sentence: str, *, true_of_every_symbol: bool) -> str:
        """``sentence`` under this symbol's name, unless it is as true of the bot's other symbol.

        A closed market or a lost connection refuses both symbols for one
        cause, and naming one of them would blame it.
        """
        return sentence if self.named is None or true_of_every_symbol else f"{self.named}: {sentence}"


def _market_sides(bot: RunAdmissionFacts) -> tuple[_MarketSide, ...]:
    """The symbols this Start needs live: the one the bot listens to, then the
    asset it trades when that is another one (#2912). The same two the running
    bot checks before every ENTER, so Start refuses what every ENTER would."""
    traded = bot.traded_asset
    if traded is None:
        return (_MarketSide(bot.market_data, bot.market_liveness, named=None),)
    return (
        _MarketSide(
            bot.market_data,
            bot.market_liveness,
            named=f"{bot.market_liveness.symbol}, which this bot listens to",
        ),
        _MarketSide(traded.market_data, traded.market_liveness, named=f"{traded.symbol}, which this bot trades"),
    )


def _options_can_wait_for_session(bot: RunAdmissionFacts, side: _MarketSide, now_ms: int) -> bool:
    """ADR 0079 schedules a fresh Options bot while closed; entries still need live quotes."""
    liveness = side.market_liveness
    clock = liveness.market_clock
    subscription = liveness.market_data
    return (
        bot.operation == "START" and bot.schedules_options_sessions
        and side.market_data.scheduled_phase == "CLOSED" and side.market_data.connected is True
        and clock.state == "CLOSED" and 0 <= now_ms - clock.observed_at_ms <= AUTHORITY_FACT_MAX_AGE_MS
        and subscription is not None and subscription.symbol == liveness.symbol
        and 0 <= now_ms - subscription.observed_at_ms <= AUTHORITY_FACT_MAX_AGE_MS
        and subscription.state in {"STARTING", "RECOVERING"}
        and liveness.reason_code == f"MARKET_DATA_{subscription.state}"
    )


def _worst_age(ages_ms: Iterable[int]) -> int:
    """One age for an authority that gave several facts: a future-dated one if
    there is any, else the oldest. Either is the one that refuses the Start."""
    ages = tuple(ages_ms)
    return min(ages) if min(ages) < 0 else max(ages)


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
        *bot.program_build.evidence_refs,
        *bot.validation.evidence_refs,
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
    traded = bot.traded_asset
    if traded is not None:
        evidence_refs.append(
            f"traded-market-data-feed:{traded.symbol}:"
            f"{traded.market_data.feed_id or 'unknown'}:{traded.market_data.observed_at_ms}"
        )
        if traded.market_liveness.symbol_status is not None:
            evidence_refs.append(
                f"traded-market-liveness-symbol:{traded.symbol}:"
                f"{traded.market_liveness.symbol_status.source}:"
                f"{traded.market_liveness.symbol_status.observed_at_ms}"
            )
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
    """Decide a fresh deployment from bot and Clerk facts; unknown blocks."""
    sides = _market_sides(bot)
    fact_ages_ms = RunAdmissionFactAges(
        program_build=evaluated_at_ms - bot.program_build.verified_at_ms,
        runtime=evaluated_at_ms - bot.runtime.observed_at_ms,
        process=evaluated_at_ms - bot.process.observed_at_ms,
        market_data=_worst_age(evaluated_at_ms - side.market_data.observed_at_ms for side in sides),
        market_liveness=_worst_age(evaluated_at_ms - side.market_liveness.observed_at_ms for side in sides),
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
    if bot.sealed_account_id != clerk.account_id:
        return decide(
            allowed=False,
            reason_code="SEALED_ACCOUNT_MISMATCH",
            explanation="The immutable run binding names a different custody account than this Clerk snapshot.",
            next_step="Deploy against the selected account with a fresh identity.",
        )
    # #1729 AC4: the guarded canary path additionally requires one exact
    # (program, account) pairing an operator has explicitly enabled. Every
    # other canary proof (seal, build, provider, validation, replay, Clerk
    # custody) is already composed by the gates below (and, for Clerk
    # custody, further below in the non-dry-run block) — `canary_gate_applies`
    # only turns this check on once the build is already PROVEN, so it never
    # pre-empts the PROGRAM_BUILD_UNPROVEN gate's own reason code.
    if not bot.schedules_options_sessions and canary_gate_applies(
        mode=bot.mode, program_build_state=bot.program_build.state
    ) and not canary_pairing_admitted(
        program_key=bot.program_build.program_key, account_id=bot.sealed_account_id
    ):
        return decide(
            allowed=False,
            reason_code="CANARY_PAIRING_NOT_ALLOWLISTED",
            explanation=(
                "This exact program/account pairing is not on the closed canary allowlist. "
                "The guarded Alpaca Paper path admits only pairings an operator has "
                "explicitly enabled after reviewing the full composed proof."
            ),
            next_step=(
                "Review and confirm this exact pairing with the broker-free canary "
                "activation command; activation never starts the bot by itself."
            ),
        )
    if bot.program_build.state == "UNPROVEN":
        return decide(
            allowed=False,
            reason_code="PROGRAM_BUILD_UNPROVEN",
            explanation=bot.program_build.explanation,
            next_step=bot.program_build.next_step
            or "Re-run the program qualification job and deploy a new sealed instance.",
        )
    # Corpus qualification is independent of human evidence acceptance and
    # account access. Only the explicit no-order Dry Run path is exempt.
    if bot.program_build.corpus_coverage == "UNCOVERED" and bot.mode != "dry_run":
        return decide(
            allowed=False,
            reason_code="PROGRAM_CORPUS_UNCOVERED",
            explanation=(
                "This exact symbol and resolved parameter configuration is outside the qualification corpus. "
                "Paper, Shadow, and Live require a covered configuration."
            ),
            next_step=CORPUS_UNCOVERED_NEXT_STEP,
        )
    if bot.validation.state != "VERIFIED":
        return decide(
            allowed=False,
            reason_code=(
                "STRATEGY_VALIDATION_UNREADABLE"
                if bot.validation.state == "UNREADABLE"
                else "STRATEGY_VALIDATION_UNVERIFIED"
            ),
            explanation=bot.validation.explanation,
            next_step=bot.validation.next_step
            or "Restore current validation evidence and revalidate before deployment.",
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
                else "Open Deploy again to review a fresh instance."
            ),
        )
    for side in sides:
        if side.market_data.state != "AVAILABLE":
            reason_codes = {
                "STALE": "MARKET_DATA_STALE",
                "UNAVAILABLE": "MARKET_DATA_UNAVAILABLE",
                "UNKNOWN": "MARKET_DATA_UNKNOWN",
            }
            return decide(
                allowed=False,
                reason_code=reason_codes[side.market_data.state],
                explanation=side.say(
                    "The required market-data feed is not proven ready for this run.",
                    true_of_every_symbol=all(other.market_data.state == side.market_data.state for other in sides),
                ),
                next_step=f"Restore fresh market data before {bot.operation.title()}.",
            )
    # One rule, no mode carve-out: whatever the Clerk will refuse per decision,
    # the gate refuses up front. A Dry Run routes every intent through the same
    # `shape_program_leg` on the synthetic authority, so admitting one with
    # unset allowances started a run that then rejected every extended decision
    # it made. The refusals are `program_leg.py`'s named values, so the gate and
    # the receipt say the same thing.
    extended_refusal = bot.extended_hours.refusal

    if extended_refusal is not None:
        return decide(
            allowed=False,
            reason_code=extended_refusal.reason_code,
            explanation=extended_refusal.explanation,
            next_step=extended_refusal.next_step,
        )
    # #1702: everything from here down is either an execution-channel gate or
    # a custody/evidence gate — Dry Run makes no broker contact, holds no
    # custody, and never carries exposure, so none of it applies to Dry Run.
    # The guard is additive-only: trade/paper (and log_only) fall through
    # unchanged, byte-for-byte, to the exact checks that existed before this
    # mode ever existed.
    if bot.mode != "dry_run":
        window = bot.start_window
        if window is not None and window.state == "CLOSED" and not bot.schedules_options_sessions:
            return decide(allowed=False, reason_code="DEPLOY_WINDOW_CLOSED",
                          explanation="Regular-session bots may start from pre-market open through the regular close.",
                          next_step=start_window_next_step(window, now_ms=evaluated_at_ms))
        permitted_liveness = (
            {"TRADABLE", "CLOSED"}
            if bot.schedules_options_sessions or (window is not None and window.state == "PREMARKET")
            else {"TRADABLE"}
        )
        for side in sides:
            liveness = side.market_liveness
            symbol_halted = liveness.symbol_status is not None and liveness.symbol_status.state == "HALTED"
            waiting = _options_can_wait_for_session(bot, side, evaluated_at_ms)
            if (liveness.state not in permitted_liveness and not waiting) or symbol_halted:
                reason_codes = {
                    "HALTED": "MARKET_LIVENESS_HALTED",
                    "CLOSED": "MARKET_LIVENESS_CLOSED",
                    "UNKNOWN": "MARKET_LIVENESS_UNKNOWN",
                }
                return decide(
                    allowed=False,
                    reason_code=reason_codes["HALTED" if symbol_halted else liveness.state],
                    explanation=side.say(
                        liveness.reason,
                        # The same code can carry a sentence about one symbol only,
                        # so the sentence has to be the same one too.
                        true_of_every_symbol=all(
                            (other.market_liveness.reason_code, other.market_liveness.reason)
                            == (liveness.reason_code, liveness.reason)
                            for other in sides
                        ),
                    ),
                    next_step=(
                        f"Wait for fresh market-wide and symbol trading-status evidence before {bot.operation.title()}."
                    ),
                )
        if clerk.reconciliation_state != "clean" or not clerk.reconciliation_fresh:
            if clerk.reason_code == CLERK_CUSTODY_BROKER_UNREACHABLE:
                # #2901: the pass could not READ Alpaca, so nothing observed
                # contradicts the account. The plain sentence a trader can
                # act on, with the claim limited to what this refusal proves:
                # the operation was refused, so nothing was started or sent.
                unstarted = "Your bot has not started." if bot.operation == "START" else "No resume was sent."
                return decide(
                    allowed=False,
                    reason_code=CLERK_CUSTODY_BROKER_UNREACHABLE,
                    explanation=(
                        "Alpaca could not be reached to check this account's orders, "
                        f"so the Clerk cannot prove reconciled custody. {unstarted}"
                    ),
                    next_step=clerk.next_step or "Check again when the Alpaca connection recovers.",
                )
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
                next_step="Flatten the exact Clerk-attributed exposure before starting another run.",
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
    return decide(
        allowed=True,
        reason_code=f"{bot.operation}_ADMITTED",
        explanation=_admitted_explanation(bot),
        next_step=None,
    )


def _admitted_explanation(bot: RunAdmissionFacts) -> str:
    """The admitted sentence with the corpus-coverage notice when it applies."""
    admitted = "The process slot is absent, market data is ready, and the Clerk proves flat custody."
    if bot.validation.state == "VERIFIED" and (bot.validation.event_id or "").startswith("golden-validation:"):
        admitted = f"{admitted} {bot.validation.explanation}"
    if bot.mode == "dry_run" and bot.program_build.corpus_coverage == "UNCOVERED":
        admitted = f"{admitted} {CORPUS_UNCOVERED_ADMITTED_NOTE}"
    return admitted
