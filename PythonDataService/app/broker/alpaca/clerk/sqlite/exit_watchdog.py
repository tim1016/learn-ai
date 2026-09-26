"""Stuck-EXIT watchdog: age-gate, bounded re-drive, then durable escalation.

A terminal ``EXIT_NOT_FLAT`` folds its effect to ``failed``, which
``reconcilable_effect_operations`` never re-selects, and
``_resolve_flat_exit_fences`` clears the episode only if exposure happens to
reach flat — without this step a stuck EXIT is re-driven never, forever
(research directions 2026-08-24, Direction 1 RQ2). This runs as one step of
the account reconciliation pass (``reconcile._reconcile_account_serialized``).

Owner decision 2026-09-19 (evening, #2229) supersedes "re-drives only inside
the regular session": inside 09:30–16:00 a re-drive is the market DAY leg as
before; in the broker's declared PRE or POST window it is an extended-hours
DAY limit the Clerk prices itself from the live quote and the sealed exit
allowance (``recovery_reduction.price_automatic_recovery_reduction``) — never
a market order the vendor would queue to the next open. When no priceable
reduction exists (no session, no allowance, no live quote) the re-drive
defers: the ``EXIT_NOT_FLAT`` episode stays raised and visible for the
operator's own priced flatten, and the entry stays free for it.

#2343: the re-drive is sized from the Clerk's *attributed* position, so it is
sent only when this pass's broker snapshot equals the account-wide
attribution for the symbol and nothing — at the broker or in the Clerk — is
still working on that symbol. Equality alone rules out an overshoot: the
reduction moves broker and attribution together. Otherwise (a flat account, a
partial broker position, a working order that could fill under it) it
defers: no order, no EXIT effect, no burned attempt, the episode stays
raised. Every refusal is decided before ``accept_recovery_exit``, so a
refused re-drive never parks an EXIT. #2504 measures persistent refusals in
observed regular-session failure time. Holds and unobserved time never spend
that durable budget, and the strategy's own working EXIT is always a hold.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, NamedTuple

from app.broker.alpaca.clerk.program_leg import LegRefusal, ProgramLegPolicy, ProgramLegRefused
from app.broker.alpaca.clerk.recovery_reduction import (
    ConfirmedRecoveryShape,
    PricingSnapshot,
    RecoveryPricing,
    market_leg_sendable,
    next_redrive_at_ms,
    reducing_send_verdict,
)
from app.broker.alpaca.clerk.sqlite.exit import (
    ExitSubmission,
    accept_recovery_exit,
    resolve_accepted_exit,
)
from app.broker.alpaca.clerk.sqlite.exit_recovery import (
    DEFAULT_RECOVERY_INTERVAL_MS,
    RecoveryResult,
    latest_exit_recovery,
    record_exit_recovery,
)
from app.broker.alpaca.clerk.sqlite.exit_resolution import EXIT_REDRIVE_DECISION_PREFIX
from app.broker.alpaca.clerk.sqlite.facts import (
    ExitAcceptedFacts,
    ExitReducingOrderCreatedFacts,
    UncertaintyRaisedFacts,
)
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.alpaca.clerk.sqlite.idempotency import DurableConflictError
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.off_loop import OffLoop, run_inline
from app.broker.alpaca.clerk.sqlite.open_replacement import replacement_ready
from app.broker.alpaca.clerk.sqlite.order_evidence import entry_order_symbol
from app.broker.alpaca.clerk.sqlite.repository import (
    ClerkSqliteRepository,
    OperationClaimError,
)
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    EXIT_NOT_FLAT_REASON_CODE,
    AdmissionBlockedError,
    Capability,
    ReductionIntent,
    decide_capability,
    raise_uncertainty,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXIT_STUCK_REASON_CODE,
    ExitNotFlatCause,
    ExitStuckCause,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_policies import (
    RedriveThenEscalate,
    reason_age_policy,
)
from app.broker.contract.models import OrderSide
from app.broker.contract.ports import BrokerTradePort
from app.schemas.market_liveness import MarketLivenessFact

logger = logging.getLogger(__name__)


class BrokerSymbolView(NamedTuple):
    """Fresh broker quantity, account attribution, and work in flight for a symbol."""

    broker_qty: float
    attributed_qty: float
    working: bool
    agrees: bool


type BrokerSymbolReader = Callable[[str], BrokerSymbolView]


class _RedriveRefused(NamedTuple):
    action: str
    reason_code: str
    message: str
    facts: dict[str, object]
    outcome: Literal["failure", "hold"] = "failure"


@dataclass(frozen=True)
class EpisodeAttempt:
    """One durable reducing order in this recovery episode."""

    order_ref: str
    submitted: bool
    failed: bool
    extended_hours: bool
    valid_until_ms: int | None
    redrive: bool


@dataclass(frozen=True)
class _StaleExit:
    strategy_instance_id: str
    episode: dict
    cause: ExitNotFlatCause
    remaining: float
    redrives: int
    episode_token: str
    attempts: tuple[EpisodeAttempt, ...]
    ready_at_ms: int
    stopped: bool

    @property
    def regular_failures(self) -> int:
        return sum(a.redrive and a.submitted and a.failed and not a.extended_hours for a in self.attempts)


@dataclass(frozen=True)
class RecoveryEvaluation:
    stale: _StaleExit
    result: RecoveryResult
    evaluated_at_ms: int


def episode_attempts(
    repo: ClerkSqliteRepository, *, sid: str, episode: dict, episode_token: str,
) -> tuple[int, tuple[EpisodeAttempt, ...]]:
    """The single history reader for counts, session waits and send outcomes."""
    prefix = f"cmd:{sid}:{EXIT_REDRIVE_DECISION_PREFIX}{episode_token}-"
    redrives = 0
    while repo.get_command(f"{prefix}{redrives + 1}") is not None:
        redrives += 1
    refs = UncertaintyRaisedFacts.from_facts_json(episode["facts_json"]).evidence_refs
    attempts = []
    for order in repo.orders_for_strategy(sid):
        if order.role != "REDUCING":
            continue
        effect = repo.effect_operation(order.effect_operation_id)
        if effect is None:
            continue
        redrive = effect.command_id.startswith(prefix)
        if order.order_ref not in refs and not redrive:
            continue
        row = repo.first_order_transition(order_ref=order.order_ref, transition_kind="EXIT_REDUCING_ORDER_CREATED")
        if row is None:
            continue
        created = ExitReducingOrderCreatedFacts.from_facts_json(row["facts_json"])
        bound = created.valid_until_ms
        if created.extended_hours and bound is None:
            accepted = repo.first_effect_transition(effect_operation_id=order.effect_operation_id, transition_kind="EXIT_ACCEPTED")
            if accepted is not None:
                bound = ExitAcceptedFacts.from_facts_json(accepted["facts_json"]).reducing_valid_until_ms
        attempts.append(EpisodeAttempt(
            order.order_ref,
            repo.has_order_transition(order_ref=order.order_ref, transition_kind="ORDER_SUBMIT_REQUESTED"),
            effect.state == "failed", created.extended_hours, bound, redrive,
        ))
    return redrives, tuple(attempts)


def _scan_stale_exits(repo: ClerkSqliteRepository) -> list[_StaleExit]:
    policy = reason_age_policy(EXIT_NOT_FLAT_REASON_CODE, RedriveThenEscalate)
    stale = []
    for instance in repo.strategy_instances():
        sid = instance["strategy_instance_id"]
        episode = repo.active_uncertainty(scope="CUSTODY_SUBJECT", reason_code=EXIT_NOT_FLAT_REASON_CODE, strategy_instance_id=sid)
        if episode is None:
            continue
        try:
            facts = UncertaintyRaisedFacts.from_facts_json(episode["facts_json"])
            cause = ExitNotFlatCause.from_mapping(facts.cause_facts)
        except (TypeError, ValueError, KeyError):
            logger.error("stale EXIT_NOT_FLAT episode carries unreadable cause facts", extra={
                "action": "exit_watchdog_unreadable_cause", "account_id": repo.account_id,
                "strategy_instance_id": sid, "uncertainty_id": episode["uncertainty_id"],
            })
            continue
        remaining = repo.position(sid, cause.symbol)
        if not position_quantity_is_nonzero(remaining):
            continue
        token = hashlib.sha256(episode["uncertainty_id"].encode("utf-8")).hexdigest()[:12]
        redrives, attempts = episode_attempts(repo, sid=sid, episode=episode, episode_token=token)
        stopped = repo.active_uncertainty(scope="CUSTODY_SUBJECT", reason_code=EXIT_STUCK_REASON_CODE, strategy_instance_id=sid) is not None
        ready = repo.clock() if not stopped and replacement_ready(repo, facts.evidence_refs) else episode["observed_at_ms"] + policy.after_ms
        stale.append(_StaleExit(sid, episode, cause, remaining, redrives, token, attempts, ready, stopped))
    return stale


def evaluate_recovery_wait(
    stale: _StaleExit, *, now_ms: int, own_exit_working: bool,
    policy: ProgramLegPolicy, liveness: MarketLivenessFact | None,
) -> RecoveryResult | None:
    """Decide from the episode and current market evidence; never record or send."""
    if stale.stopped:
        return RecoveryResult("hold", "EXIT_STUCK", "Automatic recovery stopped after repeated regular-session failures.")
    if own_exit_working:
        return RecoveryResult("hold", "OWN_EXIT_WORKING", "An exit is in progress; the Clerk is waiting for its outcome.")
    verdict = reducing_send_verdict(now_ms=now_ms, extended_hours=False, valid_until_ms=None, liveness=liveness)
    if isinstance(verdict, LegRefusal):
        return RecoveryResult("hold", verdict.reason_code, verdict.explanation)
    if now_ms < stale.ready_at_ms:
        return RecoveryResult("hold", "RECOVERY_RETRY_WAIT", "The Clerk is allowing the previous exit's evidence to settle.",
                              next_redrive_at_ms(not_before_ms=stale.ready_at_ms, policy=policy))
    if verdict == "send":
        age_policy = reason_age_policy(EXIT_NOT_FLAT_REASON_CODE, RedriveThenEscalate)
        if stale.regular_failures >= age_policy.max_count:
            return RecoveryResult("failure", "EXIT_REDRIVES_EXHAUSTED", "Automatic regular-session exit attempts were exhausted.")
    else:
        bound = max((a.valid_until_ms for a in stale.attempts if a.submitted and a.extended_hours
                     and a.valid_until_ms is not None and a.valid_until_ms > now_ms), default=None)
        if bound is not None:
            return RecoveryResult("hold", "EXTENDED_EXIT_WAIT", "The extended-hours exit ended; the Clerk will retry in the next session.",
                                  next_redrive_at_ms(not_before_ms=bound, policy=policy))
    return None


async def redrive_or_escalate_stale_exits(
    repo: ClerkSqliteRepository, *, trade: BrokerTradePort, intake: ReentrantAsyncLock,
    broker_symbol: BrokerSymbolReader, pricing: RecoveryPricing, off_loop: OffLoop | None = None,
) -> list[RecoveryEvaluation]:
    """Perform recoveries and return evaluations for an explicit successful-pass commit."""
    run = off_loop if off_loop is not None else run_inline
    evaluations = []
    for stale in await run(lambda: _scan_stale_exits(repo)):
        result = await _recover_stale_exit(repo, stale, trade=trade, intake=intake, broker_symbol=broker_symbol, pricing=pricing, run=run)
        if result is not None:
            evaluations.append(RecoveryEvaluation(stale, result, repo.clock()))
    return evaluations


async def _recover_stale_exit(
    repo: ClerkSqliteRepository, stale: _StaleExit, *, trade: BrokerTradePort,
    intake: ReentrantAsyncLock, broker_symbol: BrokerSymbolReader, pricing: RecoveryPricing, run: OffLoop,
) -> RecoveryResult | None:
    sid, cause = stale.strategy_instance_id, stale.cause
    own_working = await run(lambda: repo.active_exit_for_strategy(sid) is not None)
    now_ms = repo.clock()
    result = evaluate_recovery_wait(
        stale, now_ms=now_ms, own_exit_working=own_working, policy=pricing.policy_for(sid),
        liveness=pricing.read_liveness(cause.symbol, now_ms),
    )
    if result is not None:
        return result
    entries = await run(lambda: [order for order in repo.entry_orders_for_strategy(sid)
                                if entry_order_symbol(repo, order.order_ref).upper() == cause.symbol
                                and repo.active_exit_for_order(order.order_ref) is None])
    if not entries:
        return RecoveryResult("hold", "RECOVERY_ENTRY_UNAVAILABLE", "No releasable entry evidence is available for this exit.")
    # Read the instant after repository hops, immediately before quote/liveness.
    now_ms = repo.clock()
    touch = (
        PricingSnapshot(pricing.policy_for(sid), None, pricing.read_liveness(cause.symbol, now_ms))
        if market_leg_sendable(now_ms) else pricing.read(cause.symbol, now_ms, strategy_instance_id=sid)
    )
    try:
        shape = touch.price(side=OrderSide.SELL if stale.remaining > 0 else OrderSide.BUY,
                            symbol=cause.symbol, quantity=stale.remaining, now_ms=now_ms)
    except ProgramLegRefused as exc:
        logger.info("deferred a stuck-EXIT re-drive: no reduction can be priced", extra={
            "action": "exit_redrive_unpriceable", "account_id": repo.account_id,
            "strategy_instance_id": sid, "reason_code": exc.reason_code,
            "available_at_ms": exc.refusal.available_at_ms, "quote_spread_bps": touch.quote_spread_bps,
        })
        return RecoveryResult("hold", exc.reason_code, exc.refusal.explanation, exc.refusal.available_at_ms)
    try:
        accepted = await intake.off_loop(
            _accept_admissible_redrive, repo, broker_symbol=broker_symbol, strategy_instance_id=sid,
            symbol=cause.symbol, decision_id=f"{EXIT_REDRIVE_DECISION_PREFIX}{stale.episode_token}-{stale.redrives + 1}",
            entry_order_ref=entries[-1].order_ref, confirmed_shape=shape,
        )
        if accepted is None:
            return None
        if isinstance(accepted, _RedriveRefused):
            outcome = accepted.outcome if market_leg_sendable(repo.clock()) else "hold"
            previous = await run(lambda: latest_exit_recovery(repo, strategy_instance_id=sid, uncertainty_id=stale.episode["uncertainty_id"]))
            if outcome == "failure" and (previous is None or previous.first_failure_at_ms is None):
                logger.warning(accepted.message, extra={
                    "action": accepted.action, "account_id": repo.account_id,
                    "strategy_instance_id": sid, "symbol": cause.symbol, **accepted.facts,
                })
            return RecoveryResult(outcome, accepted.reason_code, accepted.message)
        resolved = await resolve_accepted_exit(repo, accepted=accepted, trade=trade, pricing=pricing, off_loop=run)
        return await run(lambda: _redrive_result(repo, stale, resolved))
    except (OperationClaimError, AdmissionBlockedError, DurableConflictError):
        return RecoveryResult("hold", "RECOVERY_CUSTODY_BUSY", "Another custody operation owns this exit; the Clerk will check again.")


def _redrive_result(repo: ClerkSqliteRepository, stale: _StaleExit, result: ExitSubmission) -> RecoveryResult:
    _, attempts = episode_attempts(repo, sid=stale.strategy_instance_id, episode=stale.episode, episode_token=stale.episode_token)
    attempt = next((a for a in attempts if a.order_ref == result.reducing_order_ref), None)
    if attempt is not None and attempt.submitted:
        if attempt.failed:
            outcome = "failure" if market_leg_sendable(repo.clock()) else "hold"
            return RecoveryResult(outcome, "EXIT_REDRIVE_NOT_FLAT", "The recovery order ended while attributed exposure remained.")
        return RecoveryResult("accepted", "RECOVERY_EXIT_ACCEPTED", "The Clerk submitted a recovery exit and is checking its outcome.")
    row = repo.first_effect_transition(effect_operation_id=result.effect_operation_id, transition_kind="EXIT_NOT_FLAT")
    if row is not None:
        return RecoveryResult("hold", row["summary_code"], "The exit could not be submitted; the Clerk will check again.")
    return RecoveryResult("hold", "OWN_EXIT_WORKING", "The accepted exit is waiting for custody evidence before submission.")


def commit_recovery_evaluations(
    repo: ClerkSqliteRepository, evaluations: list[RecoveryEvaluation], *, pass_started_at_ms: int,
    interval_ms: int = DEFAULT_RECOVERY_INTERVAL_MS,
) -> None:
    """Apply the whole pass's observations and escalation only after its verdict."""
    policy = reason_age_policy(EXIT_NOT_FLAT_REASON_CODE, RedriveThenEscalate)
    for evaluation in evaluations:
        stale, result = evaluation.stale, evaluation.result
        observation = record_exit_recovery(
            repo, strategy_instance_id=stale.strategy_instance_id, uncertainty_id=stale.episode["uncertainty_id"],
            result=result, evaluated_at_ms=evaluation.evaluated_at_ms,
            pass_started_at_ms=pass_started_at_ms, interval_ms=interval_ms,
        )
        if observation is None or result.outcome != "failure":
            continue
        exhausted = stale.regular_failures >= policy.max_count
        if not exhausted and observation.failure_elapsed_ms < policy.after_ms * (policy.max_count + 1):
            continue
        _escalate_to_exit_stuck(
            repo, redrive_policy=policy, stale_exit=stale, now_ms=repo.clock(),
            deferred_since_ms=None if exhausted else observation.first_failure_at_ms,
            failure_elapsed_ms=observation.failure_elapsed_ms, failure_reason=result.explanation,
        )


def _accept_admissible_redrive(
    repo: ClerkSqliteRepository,
    *,
    broker_symbol: BrokerSymbolReader,
    strategy_instance_id: str,
    symbol: str,
    decision_id: str,
    entry_order_ref: str,
    confirmed_shape: ConfirmedRecoveryShape | None,
) -> ExitSubmission | _RedriveRefused | None:
    """Accept the re-drive EXIT only when the broker and the REDUCE gate admit it.

    One fenced fold, so the attribution checked here is the attribution the
    acceptance commits against. Every refusal returns before
    ``accept_recovery_exit``: a refused re-drive creates no EXIT effect, burns
    no attempt, and leaves the entry free. (#2343 P3: the REDUCE refusal used
    to raise from ``resolve_accepted_exit`` after the EXIT was committed,
    parking it in ``accepted``, armed to sell once the hold cleared.) ``None``
    means the strategy became attributed-flat since the scan.
    """
    remaining = repo.position(strategy_instance_id, symbol)
    if not position_quantity_is_nonzero(remaining):
        return None
    if repo.active_exit_for_strategy(strategy_instance_id) is not None:
        return _RedriveRefused(
            action="OWN_EXIT_WORKING", reason_code="OWN_EXIT_WORKING",
            message="An EXIT for this strategy is already working.",
            facts={}, outcome="hold",
        )
    if clerk_work_in_flight(repo, symbol):
        return _RedriveRefused(
            action="exit_redrive_deferred_work_in_flight", reason_code="EXIT_OTHER_ORDER_WORKING",
            message=(
                "deferred a stuck-EXIT re-drive: the Clerk still has work in flight "
                "that could fill under it"
            ),
            facts={"strategy_attributed_qty": remaining},
        )
    # Equality with the account-wide attribution is the whole proof: the
    # reduction moves broker and attribution by the same amount, so they stay
    # equal and the broker cannot be carried past what the Clerk holds. A
    # netted account (A +10, B -10, broker 0) re-drives A's SELL 10 correctly.
    view = broker_symbol(symbol)
    if not view.agrees:
        return _RedriveRefused(
            action="exit_redrive_deferred_broker_disagrees",
            reason_code="EXIT_OTHER_ORDER_WORKING" if view.working else "EXIT_BROKER_POSITION_MISMATCH",
            message=(
                "Another broker order is still working on this symbol." if view.working
                else "The broker position differs from the account's attributed position."
            ),
            facts={
                "broker_qty": view.broker_qty,
                "attributed_qty": view.attributed_qty,
                "strategy_attributed_qty": remaining,
                "broker_agrees": view.agrees,
                "reason_code": "EXIT_OTHER_ORDER_WORKING" if view.working else "EXIT_BROKER_POSITION_MISMATCH",
            },
        )
    decision = decide_capability(
        repo,
        capability=Capability.REDUCE,
        strategy_instance_id=strategy_instance_id,
        reduction_intent=ReductionIntent(
            symbol=symbol,
            side="SELL" if remaining > 0 else "BUY",
            quantity=abs(remaining),
        ),
    )
    if not decision.allowed:
        return _RedriveRefused(
            action="exit_redrive_deferred", reason_code="RECOVERY_REDUCTION_BLOCKED",
            message="deferred a policy-blocked stuck-EXIT re-drive before accepting it",
            facts={"reason_code": decision.reason_code},
        )
    return accept_recovery_exit(
        repo,
        account_id=repo.account_id,
        strategy_instance_id=strategy_instance_id,
        decision_id=decision_id,
        entry_order_ref=entry_order_ref,
        confirmed_shape=confirmed_shape,
    )


def clerk_work_in_flight(repo: ClerkSqliteRepository, symbol: str) -> bool:
    """Whether the Clerk has broker intent on ``symbol`` the snapshot may not show yet.

    Any nonterminal effect of a strategy instance on the symbol (an ENTER in
    flight, another EXIT, a recovery flatten) or any nonterminal manual order
    (manual tickets are account-wide custody, so conservatively any symbol).
    """
    instance_symbols = {
        instance["strategy_instance_id"]: str(instance["symbol"]).upper()
        for instance in repo.strategy_instances()
    }
    return repo.has_nonterminal_manual_order() or any(
        instance_symbols.get(effect.strategy_instance_id or "") == symbol.upper()
        for effect in repo.reconcilable_effect_operations()
    )


def _escalate_to_exit_stuck(
    repo: ClerkSqliteRepository,
    *,
    redrive_policy: RedriveThenEscalate,
    stale_exit: _StaleExit,
    now_ms: int,
    deferred_since_ms: int | None,
    failure_elapsed_ms: int = 0, failure_reason: str = "",
) -> None:
    """Escalate a stuck EXIT to EXIT_STUCK: re-drives exhausted, or deferred too long.

    ``deferred_since_ms`` is ``None`` for exhausted re-drives and the first
    deferral's clock otherwise; it only changes the operator-facing wording.
    """
    sid = stale_exit.strategy_instance_id
    cause = stale_exit.cause
    redrives = stale_exit.regular_failures
    if deferred_since_ms is None:
        headline = "A stuck EXIT exhausted automatic re-drives"
        why = f"after {redrives} automatic EXIT re-drives."
    else:
        headline = "A stuck EXIT could not be safely re-driven"
        why = (
            f"after {failure_elapsed_ms} ms of observed regular-session failures: "
            f"{failure_reason}"
        )

    def _escalate_if_still_stuck() -> str | None:
        """Raise EXIT_STUCK from a fresh read, never the scan's cache.

        The scan ran unfenced on a worker; websocket evidence can
        resolve the episode or flatten the position between then and
        now. Escalating either from obsolete data would leave an
        already-flat strategy paused behind a false operator-visible
        error (#1993 review).
        """
        episode_now = repo.active_uncertainty(
            scope="CUSTODY_SUBJECT",
            reason_code=EXIT_NOT_FLAT_REASON_CODE,
            strategy_instance_id=sid,
        )
        if episode_now is None or episode_now["uncertainty_id"] != stale_exit.episode["uncertainty_id"]:
            return None
        if repo.active_exit_for_strategy(sid) is not None:
            return None
        remaining_now = repo.position(sid, cause.symbol)
        if not position_quantity_is_nonzero(remaining_now):
            return None
        return raise_uncertainty(
            repo,
            strategy_instance_id=sid,
            reason_code=redrive_policy.escalate_to,
            headline=headline,
            explanation=f"{remaining_now:g} {cause.symbol} remains attributed {why}",
            operator_impact=(
                "New exposure stays paused for this strategy and automatic "
                "re-drives stopped. Exact operator reduction remains available."
            ),
            next_step="Run Reconcile now, then execute the presented safe flatten.",
            evidence_refs=(episode_now["uncertainty_id"],),
            cause_facts=ExitStuckCause(
                symbol=cause.symbol,
                attributed_qty=remaining_now,
                redrive_count=redrives,
                first_observed_at_ms=episode_now["observed_at_ms"],
            ).to_mapping(),
            severity="error",
        )

    escalated = _escalate_if_still_stuck()
    if escalated is not None and escalated != "unchanged":
        logger.error(
            "stale EXIT escalated to a durable operator-visible EXIT_STUCK episode",
            extra={
                "action": "exit_stuck_escalated",
                "account_id": repo.account_id,
                "strategy_instance_id": sid,
                "symbol": cause.symbol,
                "redrive_count": redrives,
                "age_ms": now_ms - stale_exit.episode["observed_at_ms"],
                "deferred_since_ms": deferred_since_ms,
            },
        )
