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
from functools import partial
from typing import Literal, NamedTuple

from app.broker.alpaca.clerk.program_leg import ProgramLegRefused
from app.broker.alpaca.clerk.recovery_reduction import (
    ConfirmedRecoveryShape,
    RecoveryPricing,
    market_leg_sendable,
    next_redrive_at_ms,
    reduction_market_hold,
)
from app.broker.alpaca.clerk.sqlite.exit import (
    ExitSubmission,
    accept_recovery_exit,
    resolve_accepted_exit,
)
from app.broker.alpaca.clerk.sqlite.exit_recovery import defer_recovery_escalation, observe_exit_recovery
from app.broker.alpaca.clerk.sqlite.exit_resolution import EXIT_REDRIVE_DECISION_PREFIX
from app.broker.alpaca.clerk.sqlite.facts import ExitReducingOrderCreatedFacts, UncertaintyRaisedFacts
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.alpaca.clerk.sqlite.idempotency import DurableConflictError
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.models import OrderResource
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

logger = logging.getLogger(__name__)


class BrokerSymbolView(NamedTuple):
    """One symbol as this pass's broker snapshot and the Clerk's books see it.

    ``attributed_qty`` is the account-wide attributed total across every
    strategy instance — the quantity the broker's signed position must equal.
    ``working`` is whether an order for the symbol is working in the snapshot.
    ``agrees`` is that equality with no order working.
    """

    broker_qty: float
    attributed_qty: float
    working: bool
    agrees: bool


type BrokerSymbolReader = Callable[[str], BrokerSymbolView]


class _RedriveRefused(NamedTuple):
    """A pre-acceptance refusal: logged, nothing sent, nothing accepted."""

    action: str
    message: str
    facts: dict[str, object]
    outcome: Literal["failure", "hold"] = "failure"


class _StaleExit(NamedTuple):
    """One age-qualified EXIT_NOT_FLAT episode and its redrive count."""

    strategy_instance_id: str
    episode: dict
    cause: ExitNotFlatCause
    remaining: float
    redrives: int
    episode_token: str
    regular_failures: int
    ready_at_ms: int
    stopped: bool


async def redrive_or_escalate_stale_exits(
    repo: ClerkSqliteRepository,
    *,
    trade: BrokerTradePort,
    intake: ReentrantAsyncLock,
    broker_symbol: BrokerSymbolReader,
    pricing: RecoveryPricing,
    off_loop: OffLoop | None = None,
) -> None:
    """Age-gate active EXIT_NOT_FLAT episodes: bounded re-drive, then escalate.

    ``broker_symbol`` reads one symbol from this pass's broker snapshot
    against the Clerk's current attribution (#2343). It has no default: a
    caller without a broker snapshot cannot re-drive.

    ``pricing`` is what an extended-hours re-drive prices from (#2229): the
    authority's own ``recovery_pricing``. It has no default either — the
    re-drive and the notice's next-attempt time must come from the same
    seam; a caller with nothing to price from names
    :data:`~recovery_reduction.UNPRICEABLE_RECOVERY`, which defers outside
    the regular session rather than guessing a price.

    ``off_loop`` moves the episode/entry scans onto a worker thread and the
    escalation/acceptance folds through the fence's sanctioned hop (#1993);
    the default keeps the pre-#1993 inline behavior for non-sweep callers.
    """
    run = off_loop if off_loop is not None else run_inline
    # The single declared age policy (ADR 0048 Decision 1) — replaces the
    # former EXIT_NOT_FLAT_REDRIVE_AFTER_MS / EXIT_NOT_FLAT_MAX_REDRIVES
    # module constants; the watchdog keeps its execution logic and loses
    # its policy.
    redrive_policy = reason_age_policy(EXIT_NOT_FLAT_REASON_CODE, RedriveThenEscalate)

    def _scan_stale_exits() -> tuple[int, list[_StaleExit]]:
        now_ms = repo.clock()
        stale: list[_StaleExit] = []
        for instance in repo.strategy_instances():
            sid = instance["strategy_instance_id"]
            episode = repo.active_uncertainty(
                scope="CUSTODY_SUBJECT",
                reason_code=EXIT_NOT_FLAT_REASON_CODE,
                strategy_instance_id=sid,
            )
            if episode is None:
                continue
            stopped = (
                repo.active_uncertainty(
                    scope="CUSTODY_SUBJECT",
                    reason_code=EXIT_STUCK_REASON_CODE,
                    strategy_instance_id=sid,
                )
                is not None
            )
            try:
                facts = UncertaintyRaisedFacts.from_facts_json(episode["facts_json"])
                cause = ExitNotFlatCause.from_mapping(facts.cause_facts)
            except (TypeError, ValueError, KeyError):
                logger.error(
                    "stale EXIT_NOT_FLAT episode carries unreadable cause facts",
                    extra={
                        "action": "exit_watchdog_unreadable_cause",
                        "account_id": repo.account_id,
                        "strategy_instance_id": sid,
                        "uncertainty_id": episode["uncertainty_id"],
                    },
                )
                continue
            ready_at_ms = (
                now_ms if replacement_ready(repo, facts.evidence_refs)
                else episode["observed_at_ms"] + redrive_policy.after_ms
            )
            remaining = repo.position(sid, cause.symbol)
            if not position_quantity_is_nonzero(remaining):
                continue  # the flat fence resolver clears this episode in this pass
            # Episode-scoped redrive count. `_exit_identity` keys idempotency on
            # (strategy_instance_id, decision_id) only, and a completed-but-non-flat
            # redrive REFRESHES this EXIT_NOT_FLAT episode (exit_resolution re-raises
            # with the new reducing order_ref), which overwrites observed_at_ms. A
            # time-anchored count would therefore reset to zero every cycle and the
            # watchdog would loop at attempt 1 forever, never escalating. Count by
            # the stable per-episode redrive namespace instead, minted from the
            # immutable uncertainty id (colon-bearing "uncertainty:<seq>" hashed to
            # a colon-free hex token) so successive episodes never collide.
            episode_token = hashlib.sha256(
                episode["uncertainty_id"].encode("utf-8")
            ).hexdigest()[:12]
            redrives = 0
            regular_failures = 0
            while (command := repo.get_command(
                    f"cmd:{sid}:{EXIT_REDRIVE_DECISION_PREFIX}{episode_token}-{redrives + 1}"
                )) is not None:
                if command.effect_operation_id is not None:
                    effect = repo.effect_operation(command.effect_operation_id)
                    created = repo.first_effect_transition(
                        effect_operation_id=command.effect_operation_id,
                        transition_kind="EXIT_REDUCING_ORDER_CREATED",
                    )
                    if effect is not None and effect.state == "failed" and created is not None:
                        leg = ExitReducingOrderCreatedFacts.from_facts_json(created["facts_json"])
                        if not leg.extended_hours and repo.has_order_transition(
                            order_ref=created["order_ref"], transition_kind="ORDER_SUBMIT_REQUESTED",
                        ):
                            regular_failures += 1
                redrives += 1
            stale.append(_StaleExit(sid, episode, cause, remaining, redrives, episode_token, regular_failures, ready_at_ms, stopped))
        return now_ms, stale

    now_ms, stale = await run(_scan_stale_exits)
    for stale_exit in stale:
        sid = stale_exit.strategy_instance_id
        episode = stale_exit.episode
        cause = stale_exit.cause
        remaining = stale_exit.remaining
        redrives = stale_exit.redrives
        if stale_exit.stopped:
            await intake.off_loop(
                observe_exit_recovery, repo, strategy_instance_id=sid,
                uncertainty_id=episode["uncertainty_id"], outcome="hold", reason_code="EXIT_STUCK",
                explanation="Automatic recovery stopped after repeated regular-session failures.",
            )
            continue
        if await run(lambda sid=sid: repo.active_exit_for_strategy(sid)) is not None:
            await intake.off_loop(
                observe_exit_recovery, repo, strategy_instance_id=sid,
                uncertainty_id=episode["uncertainty_id"], outcome="hold",
                reason_code="OWN_EXIT_WORKING",
            )
            continue
        if hold := reduction_market_hold(
            now_ms=now_ms, fact=pricing.read_liveness(cause.symbol, now_ms),
        ):
            await intake.off_loop(
                observe_exit_recovery, repo, strategy_instance_id=sid,
                uncertainty_id=episode["uncertainty_id"], outcome="hold",
                reason_code=hold.reason_code,
                explanation=hold.explanation,
            )
            continue
        if now_ms < stale_exit.ready_at_ms:
            await intake.off_loop(
                observe_exit_recovery, repo, strategy_instance_id=sid,
                uncertainty_id=episode["uncertainty_id"], outcome="hold",
                reason_code="RECOVERY_RETRY_WAIT", allowed_from_ms=next_redrive_at_ms(
                    not_before_ms=stale_exit.ready_at_ms, policy=pricing.policy_source(),
                ),
                explanation="The Clerk is allowing the previous exit's evidence to settle.",
            )
            continue
        if stale_exit.regular_failures >= redrive_policy.max_count and market_leg_sendable(now_ms):
            await _escalate_to_exit_stuck(
                repo,
                intake=intake,
                redrive_policy=redrive_policy,
                stale_exit=stale_exit,
                now_ms=now_ms,
                deferred_since_ms=None,
            )
            continue

        def _candidate_entries(sid: str = sid, cause: ExitNotFlatCause = cause) -> list[OrderResource]:
            return [
                order
                for order in repo.entry_orders_for_strategy(sid)
                if entry_order_symbol(repo, order.order_ref).upper() == cause.symbol
                and repo.active_exit_for_order(order.order_ref) is None
            ]

        entries = await run(_candidate_entries)
        if not entries:
            await intake.off_loop(
                observe_exit_recovery, repo, strategy_instance_id=sid,
                uncertainty_id=episode["uncertainty_id"], outcome="hold",
                reason_code="RECOVERY_ENTRY_UNAVAILABLE",
                explanation="No releasable entry evidence is available for this exit.",
            )
            continue
        confirmed_shape = None
        quote_spread = None
        if not market_leg_sendable(now_ms):
            waiting_until = await run(lambda stale_exit=stale_exit: _extended_attempt_wait_until(repo, stale_exit, now_ms))
            if waiting_until is not None:
                await intake.off_loop(
                    observe_exit_recovery, repo, strategy_instance_id=sid,
                    uncertainty_id=episode["uncertainty_id"], outcome="hold",
                    reason_code="EXTENDED_EXIT_WAIT", allowed_from_ms=waiting_until,
                    explanation="The extended-hours exit ended; the Clerk will retry in the next session.",
                )
                continue
            # Owner decision 2026-09-19 (evening, #2229): an extended-hours
            # re-drive prices a limit itself instead of waiting for the open.
            # A refusal (no session, no allowance, no live quote, or a spread
            # past the cap) defers — the episode stays raised and the entry
            # stays free. Read here, on the event loop (#2440 review). Both
            # this choice and the price are judged at the send instant, as
            # the send-time rule judges the leg, so a re-drive the rule would
            # refuse is refused here, before an attempt is burned.
            touch = pricing.read(cause.symbol, now_ms, strategy_instance_id=sid)
            try:
                priced = touch.price(
                    side=OrderSide.SELL if remaining > 0 else OrderSide.BUY,
                    symbol=cause.symbol,
                    quantity=remaining,
                    now_ms=now_ms,
                )
            except ProgramLegRefused as exc:
                await intake.off_loop(
                    observe_exit_recovery, repo, strategy_instance_id=sid,
                    uncertainty_id=episode["uncertainty_id"], outcome="hold",
                    reason_code=exc.reason_code,
                    explanation=exc.refusal.explanation,
                    allowed_from_ms=exc.refusal.available_at_ms,
                )
                logger.info(
                    "deferred an extended-hours stuck-EXIT re-drive: no reduction can be priced",
                    extra={
                        "action": "exit_redrive_unpriceable",
                        "account_id": repo.account_id,
                        "strategy_instance_id": sid,
                        "symbol": cause.symbol,
                        "reason_code": exc.reason_code,
                        "available_at_ms": exc.refusal.available_at_ms,
                        # The spread beside every refusal is the series an
                        # operator tunes ALPACA_LIVE_XH_EXIT_SPREAD_CAP_BPS
                        # from — a too-tight gate should be visible in data.
                        "quote_spread_bps": touch.quote_spread_bps,
                    },
                )
                continue
            if priced is None:
                # Unreachable while the two session notions agree — outside
                # the regular session the answer is a shape or a refusal —
                # but an AssertionError here would abort the whole account's
                # reconciliation pass, not just this instance. Loud, contained,
                # and the episode stays raised either way.
                logger.error(
                    "an extended-hours re-drive priced a market leg outside the "
                    "regular session; deferring the instance for re-examination",
                    extra={
                        "action": "exit_redrive_priced_market_leg",
                        "account_id": repo.account_id,
                        "strategy_instance_id": sid,
                        "symbol": cause.symbol,
                    },
                )
                continue
            confirmed_shape = priced
            quote_spread = touch.quote_spread_bps
        try:
            accepted = await intake.off_loop(
                _accept_admissible_redrive,
                repo,
                broker_symbol=broker_symbol,
                strategy_instance_id=sid,
                symbol=cause.symbol,
                decision_id=(
                    f"{EXIT_REDRIVE_DECISION_PREFIX}{stale_exit.episode_token}-{redrives + 1}"
                ),
                entry_order_ref=entries[-1].order_ref,
                confirmed_shape=confirmed_shape,
            )
            if accepted is None:
                continue  # attributed-flat since the scan; the flat fence resolver clears it
            if isinstance(accepted, _RedriveRefused):
                await _defer_or_escalate(
                    repo,
                    intake=intake,
                    redrive_policy=redrive_policy,
                    stale_exit=stale_exit,
                    now_ms=now_ms,
                    refusal=accepted,
                )
                continue
            resolved = await resolve_accepted_exit(
                repo, accepted=accepted, trade=trade, pricing=pricing, off_loop=run
            )
            await intake.off_loop(
                _record_redrive_result, repo, stale_exit, resolved,
            )
        except (OperationClaimError, AdmissionBlockedError, DurableConflictError):
            await intake.off_loop(
                observe_exit_recovery, repo, strategy_instance_id=sid,
                uncertainty_id=episode["uncertainty_id"], outcome="hold",
                reason_code="RECOVERY_EVALUATION_CONTENDED",
            )
            logger.info(
                "deferred a contended or policy-blocked stuck-EXIT re-drive",
                extra={
                    "action": "exit_redrive_deferred",
                    "account_id": repo.account_id,
                    "strategy_instance_id": sid,
                },
            )
            continue
        logger.warning(
            "re-drove a stale EXIT_NOT_FLAT episode with a fresh recovery EXIT",
            extra={
                "action": "exit_redrive_submitted",
                "account_id": repo.account_id,
                "strategy_instance_id": sid,
                "symbol": cause.symbol,
                "attempt": redrives + 1,
                # The spread at every priced send is the series the cap is
                # tuned from (#2229).
                "quote_spread_bps": quote_spread,
            },
        )


def _record_redrive_result(repo: ClerkSqliteRepository, stale: _StaleExit, result: ExitSubmission) -> None:
    """Acceptance alone is not a send; a new hold must preserve the failure budget."""
    effect = repo.effect_operation(result.effect_operation_id)
    assert effect is not None
    sent = result.reducing_order_ref is not None and repo.has_order_transition(
        order_ref=result.reducing_order_ref, transition_kind="ORDER_SUBMIT_REQUESTED",
    )
    outcome: Literal["failure", "hold", "accepted"]
    if sent and effect.state == "failed":
        outcome, reason = "failure", "EXIT_REDRIVE_NOT_FLAT"
        explanation = "The recovery order ended while attributed exposure remained."
    elif sent:
        outcome, reason = "accepted", "RECOVERY_EXIT_ACCEPTED"
        explanation = "The Clerk submitted a recovery exit and is checking its outcome."
    else:
        # The resolver may already have recorded a specific halt. Preserve it.
        if effect.state == "failed":
            return
        outcome, reason = "hold", "OWN_EXIT_WORKING"
        explanation = "The accepted exit is waiting for custody evidence before submission."
    observe_exit_recovery(
        repo, strategy_instance_id=stale.strategy_instance_id,
        uncertainty_id=stale.episode["uncertainty_id"], outcome=outcome,
        reason_code=reason, explanation=explanation,
    )


def _extended_attempt_wait_until(repo: ClerkSqliteRepository, stale: _StaleExit, now_ms: int) -> int | None:
    """One submitted automatic limit per extended session; no repeated chasing."""
    episode_facts = UncertaintyRaisedFacts.from_facts_json(stale.episode["facts_json"])
    command_prefix = f"cmd:{stale.strategy_instance_id}:{EXIT_REDRIVE_DECISION_PREFIX}{stale.episode_token}-"
    for order in reversed(repo.orders_for_strategy(stale.strategy_instance_id)):
        if order.role != "REDUCING" or not repo.has_order_transition(
            order_ref=order.order_ref, transition_kind="ORDER_SUBMIT_REQUESTED",
        ):
            continue
        effect = repo.effect_operation(order.effect_operation_id)
        if order.order_ref not in episode_facts.evidence_refs and (
            effect is None or not effect.command_id.startswith(command_prefix)
        ):
            continue  # A completed earlier obligation cannot hold a new one.
        row = repo.first_order_transition(
            order_ref=order.order_ref, transition_kind="EXIT_REDUCING_ORDER_CREATED",
        )
        if row is None:
            continue
        created = ExitReducingOrderCreatedFacts.from_facts_json(row["facts_json"])
        if created.extended_hours:
            bound = created.valid_until_ms
            if bound is None:
                accepted = repo.first_effect_transition(
                    effect_operation_id=order.effect_operation_id, transition_kind="EXIT_ACCEPTED",
                )
                if accepted is not None:
                    from app.broker.alpaca.clerk.sqlite.facts import ExitAcceptedFacts
                    bound = ExitAcceptedFacts.from_facts_json(accepted["facts_json"]).reducing_valid_until_ms
            if bound is not None and now_ms < bound:
                return bound
    return None


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
            action="OWN_EXIT_WORKING",
            message="An EXIT for this strategy is already working.",
            facts={}, outcome="hold",
        )
    if clerk_work_in_flight(repo, symbol):
        return _RedriveRefused(
            action="exit_redrive_deferred_work_in_flight",
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
            action="exit_redrive_deferred",
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


async def _defer_or_escalate(
    repo: ClerkSqliteRepository,
    *,
    intake: ReentrantAsyncLock,
    redrive_policy: RedriveThenEscalate,
    stale_exit: _StaleExit,
    now_ms: int,
    refusal: _RedriveRefused,
) -> None:
    """Escalate only the durable budget of observed regular-session failures."""
    observation = await intake.off_loop(
        observe_exit_recovery, repo,
        strategy_instance_id=stale_exit.strategy_instance_id,
        uncertainty_id=stale_exit.episode["uncertainty_id"], outcome=refusal.outcome,
        reason_code=str(refusal.facts.get("reason_code", refusal.action)),
        explanation=refusal.message,
    )
    if observation is None or observation.outcome != "failure":
        return
    if observation.first_failure_at_ms == observation.last_checked_at_ms:
        logger.warning(
            refusal.message,
            extra={
                "action": refusal.action,
                "account_id": repo.account_id,
                "strategy_instance_id": stale_exit.strategy_instance_id,
                "symbol": stale_exit.cause.symbol,
                **refusal.facts,
            },
        )
    if observation.failure_elapsed_ms < redrive_policy.after_ms * (redrive_policy.max_count + 1):
        return
    await _escalate_to_exit_stuck(
        repo,
        intake=intake,
        redrive_policy=redrive_policy,
        stale_exit=stale_exit,
        now_ms=now_ms,
        deferred_since_ms=observation.first_failure_at_ms,
        failure_elapsed_ms=observation.failure_elapsed_ms, failure_reason=refusal.message,
    )


async def _escalate_to_exit_stuck(
    repo: ClerkSqliteRepository,
    *,
    intake: ReentrantAsyncLock,
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
    if defer_recovery_escalation(repo, partial(
        _escalate_to_exit_stuck, repo, intake=intake, redrive_policy=redrive_policy,
        stale_exit=stale_exit, now_ms=now_ms, deferred_since_ms=deferred_since_ms,
        failure_elapsed_ms=failure_elapsed_ms, failure_reason=failure_reason,
    )):
        return
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

    escalated = await intake.off_loop(_escalate_if_still_stuck)
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
