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

Owner decision 2026-09-29 (#2622): an exit Alpaca refused while the Clerk's
own records showed an order open on the other side of its symbol (Alpaca's
wash-trade protection; the refusal records those orders, #2621) waits only
for that side to clear. It is sent again on the first pass after no such
order is open, in any session -- no settle age, no wait for the next
session. While one is still open the pass is a hold, until the settle age
has passed in the regular session, where the ordinary failure path takes
over. A re-drive refused before the settle age is a hold too, so failure
time starts where it always has and escalation keeps its timing.

#2607: an episode of a regular-session-only EXIT -- the sale at a bot's
owner-scheduled end, whose acceptance records it -- is re-driven only by the
market leg inside the regular session, the refusal above included: "in any
session" never reaches it. Outside the session it holds for the next regular
open instead of pricing an extended-hours limit, and each re-drive records
the same property, so it holds too if it meets the close.

#2854: an EXIT whose closing order an operator attested Alpaca does not have
ends with ``EXIT_STUCK`` already open, written with its ``EXIT_NOT_FLAT`` in
one transition (``unknown_order_attestation``). No read of the account shows
whether that order traded, so this watchdog never re-drives such an exit:
every pass holds it as it holds an exit whose re-drives it gave up on, and
the hold says which of the two it is.

#2854: an EXIT whose closing order was sent and could not be confirmed stays
``unknown`` with custody, possibly for days, and is work in flight on its
symbol for as long. Another bot's re-drive on that symbol is refused while it
stands, as behind any other work in flight, and escalates on the same clock.
What that bot is told names the bot whose order it is and points at that
bot's page. Its next step is its own safe flatten, which needs nothing of
that order.

ADR 0076: an episode whose position is in an option contract is known by the
symbol the episode names. It is re-driven only by the market leg inside the
regular session, as a scheduled end's is, and outside it holds for the next
open. Once the contract's last session has ended no order can close it, so
every pass holds under ``OPTION_CONTRACT_EXPIRED``: nothing is accepted, no
attempt is spent, and the episode never escalates for want of a fill that
cannot come. The first pass to hold it so also rewrites the bot's open EXIT
notices to say that the contract expired: they were written while a retry or
a flatten could still close it, and a stopped bot makes no later EXIT that
would correct them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
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
from app.broker.alpaca.clerk.sqlite.contract_terms import is_option_contract_symbol
from app.broker.alpaca.clerk.sqlite.exit import (
    ExitSubmission,
    OptionCloseTakesNoPricedLeg,
    accept_recovery_exit,
    newest_reducible_entry,
    resolve_accepted_exit,
)
from app.broker.alpaca.clerk.sqlite.exit_recovery import (
    DEFAULT_RECOVERY_INTERVAL_MS,
    RECOVERY_STOPPED_AFTER_FAILURES,
    RecoveryResult,
    latest_exit_recovery,
    record_exit_recovery,
    recovery_stopped_because,
)
from app.broker.alpaca.clerk.sqlite.exit_resolution import (
    expired_option_close,
    option_retry_waits_for_open,
    redrive_decision_prefix,
    redrive_episode_token,
    regular_session_sale_waits_for_open,
    say_the_contract_expired,
)
from app.broker.alpaca.clerk.sqlite.facts import (
    ExitAcceptedFacts,
    ExitReducingOrderCreatedFacts,
    OrderSubmitFailedFacts,
    UncertaintyRaisedFacts,
)
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.alpaca.clerk.sqlite.idempotency import DurableConflictError
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.off_loop import OffLoop, run_inline
from app.broker.alpaca.clerk.sqlite.open_replacement import replacement_ready
from app.broker.alpaca.clerk.sqlite.order_projection import OrderProjectionReadError
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
    open_exit_stuck,
    raise_uncertainty,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
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
    # What the operator is told, where it is more than the log line says, and
    # the next step of the ``EXIT_STUCK`` episode a failure escalates to, where
    # it says more than the default (#2854).
    explanation: str | None = None
    next_step: str | None = None


@dataclass(frozen=True)
class EpisodeAttempt:
    """One durable reducing order in this recovery episode."""

    order_ref: str
    submitted: bool
    failed: bool
    extended_hours: bool
    valid_until_ms: int | None
    redrive: bool
    # Its EXIT sells only inside the regular session
    # (``ExitAcceptedFacts.regular_session_only``, #2607).
    regular_session_only: bool = False


type OppositeSide = Literal["open", "ended"]
"""Where the other side of a refused exit's symbol stands (:func:`opposite_side_after_refusal`)."""


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
    # ``None`` unless the episode's order was refused behind an opposite-side
    # order (#2622), and also when the open orders could not be read.
    opposite_side: OppositeSide | None
    # The refusal has settled for the policy's re-drive age: only from here on
    # does a refused re-drive spend regular-session failure time (#2622 review).
    settled_at_ms: int
    # Why nothing is sent while ``stopped``, by the open ``EXIT_STUCK``
    # episode's cause (#2854).
    stopped_because: str = RECOVERY_STOPPED_AFTER_FAILURES

    @property
    def regular_failures(self) -> int:
        return sum(a.redrive and a.submitted and a.failed and not a.extended_hours for a in self.attempts)

    @property
    def regular_session_only(self) -> bool:
        """The episode's EXIT sells only inside the regular session, so every re-drive of it does too.

        An option position's close always does (ADR 0076), whatever raised
        its episode and whether or not any order of it was ever made.
        """
        return self.closes_an_option or any(a.regular_session_only for a in self.attempts)

    @property
    def closes_an_option(self) -> bool:
        """The position the episode names is in an option contract, read from the symbol it names."""
        return is_option_contract_symbol(self.cause.symbol.upper())


@dataclass(frozen=True)
class RecoveryEvaluation:
    stale: _StaleExit
    result: RecoveryResult
    evaluated_at_ms: int


def episode_attempts(
    repo: ClerkSqliteRepository, *, sid: str, episode: dict, episode_token: str,
) -> tuple[int, tuple[EpisodeAttempt, ...]]:
    """The single history reader for counts, session waits and send outcomes."""
    prefix = f"cmd:{sid}:{redrive_decision_prefix(episode_token)}"
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
        accepted_row = repo.first_effect_transition(effect_operation_id=order.effect_operation_id, transition_kind="EXIT_ACCEPTED")
        accepted = None if accepted_row is None else ExitAcceptedFacts.from_facts_json(accepted_row["facts_json"])
        bound = created.valid_until_ms
        if created.extended_hours and bound is None and accepted is not None:
            bound = accepted.reducing_valid_until_ms
        attempts.append(EpisodeAttempt(
            order.order_ref,
            repo.has_order_transition(order_ref=order.order_ref, transition_kind="ORDER_SUBMIT_REQUESTED"),
            effect.state == "failed", created.extended_hours, bound, redrive,
            regular_session_only=accepted is not None and accepted.regular_session_only,
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
        token = redrive_episode_token(episode["uncertainty_id"])
        redrives, attempts = episode_attempts(repo, sid=sid, episode=episode, episode_token=token)
        stuck = open_exit_stuck(repo, sid)
        stopped = stuck is not None
        opposite = opposite_side_after_refusal(
            repo, facts.evidence_refs, symbol=cause.symbol, side=reducing_side(remaining),
        )
        settled = episode["observed_at_ms"] + policy.after_ms
        send_now = opposite == "ended" or replacement_ready(repo, facts.evidence_refs)
        ready = repo.clock() if not stopped and send_now else settled
        because = RECOVERY_STOPPED_AFTER_FAILURES if stuck is None else recovery_stopped_because(stuck)
        stale.append(_StaleExit(sid, episode, cause, remaining, redrives, token, attempts, ready, stopped, opposite, settled, because))
    return stale


def reducing_side(quantity: float) -> OrderSide:
    """The side that takes a signed attributed ``quantity`` toward flat: sell a long, buy back a short."""
    return OrderSide.SELL if quantity > 0 else OrderSide.BUY


def opposite_side_after_refusal(
    repo: ClerkSqliteRepository, evidence_refs: tuple[str, ...], *, symbol: str, side: OrderSide,
) -> OppositeSide | None:
    """Whether a ``side`` exit refused behind an opposite-side order is still blocked (#2622).

    Alpaca refuses a new order while an order on the other side of its
    symbol is open in the account, whoever placed it. On such a refusal the
    Clerk records the orders its own records showed open there (#2621); an
    episode's evidence names its latest order. ``open`` while any order on
    the other side of ``side`` is open -- that one, or one opened since, which
    would refuse the re-send again; ``ended`` once none is. ``None`` when the
    episode's order was not refused behind one (any other refusal, or an
    order the broker ended), which keeps the watchdog's ordinary timing, and
    when the open orders cannot be read.
    """
    for order_ref in evidence_refs:
        row = repo.last_order_transition(order_ref=order_ref, transition_kind="EXIT_NOT_FLAT")
        if row is None or not OrderSubmitFailedFacts.from_facts_json(row["facts_json"]).opposite_open_order_refs:
            continue
        try:
            return "open" if repo.open_opposite_side_orders(symbol=symbol, side=side) else "ended"
        except OrderProjectionReadError:
            logger.exception("Clerk could not read its open orders for a refused exit; it keeps the ordinary retry timing", extra={
                "action": "exit_watchdog_opposite_orders_unreadable", "account_id": repo.account_id,
                "order_ref": order_ref, "symbol": symbol, "side": side.value,
            })
            return None
    return None


def evaluate_recovery_wait(
    stale: _StaleExit, *, now_ms: int, own_exit_working: bool,
    policy: ProgramLegPolicy, liveness: MarketLivenessFact | None,
) -> RecoveryResult | None:
    """Decide from the episode and current market evidence; never record or send."""
    if stale.closes_an_option:
        # Asked first: nothing below can change what an expired contract is.
        expired = expired_option_close(stale.cause.symbol, now_ms)
        if expired is not None:
            return RecoveryResult("hold", expired.reason_code, expired.explanation)
    if stale.stopped:
        return RecoveryResult("hold", "EXIT_STUCK", stale.stopped_because)
    if own_exit_working:
        return RecoveryResult("hold", "OWN_EXIT_WORKING", "An exit is in progress; the Clerk is waiting for its outcome.")
    verdict = reducing_send_verdict(now_ms=now_ms, extended_hours=False, valid_until_ms=None, liveness=liveness)
    if isinstance(verdict, LegRefusal):
        return RecoveryResult("hold", verdict.reason_code, verdict.explanation)
    if stale.regular_session_only and verdict != "send":
        waits = option_retry_waits_for_open(now_ms) if stale.closes_an_option else regular_session_sale_waits_for_open(now_ms)
        return RecoveryResult("hold", waits.reason_code, waits.explanation, waits.available_at_ms)
    if stale.opposite_side == "open" and (now_ms < stale.settled_at_ms or verdict != "send"):
        # Past the settle age in the regular session the ordinary failure path
        # takes over, so an order that keeps working escalates as before.
        return RecoveryResult("hold", "EXIT_OTHER_ORDER_WORKING", (
            "Alpaca refused this exit while an opposite order on this symbol was open in this account; "
            "the Clerk sends it again once no such order is open."
        ))
    if now_ms < stale.ready_at_ms:
        return RecoveryResult("hold", "RECOVERY_RETRY_WAIT", "The Clerk is allowing the previous exit's evidence to settle.",
                              next_redrive_at_ms(not_before_ms=stale.ready_at_ms, policy=policy))
    if verdict == "send":
        age_policy = reason_age_policy(EXIT_NOT_FLAT_REASON_CODE, RedriveThenEscalate)
        if stale.regular_failures >= age_policy.max_count:
            return RecoveryResult("failure", "EXIT_REDRIVES_EXHAUSTED", "Automatic regular-session exit attempts were exhausted.")
    elif stale.opposite_side is None:
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
    # A regular-session-only episode is priced as the regular session alone
    # would price it: its market leg, never an extended-hours limit (#2607).
    policy = ProgramLegPolicy.regular_only() if stale.regular_session_only else pricing.policy_for(sid)
    result = evaluate_recovery_wait(
        stale, now_ms=now_ms, own_exit_working=own_working, policy=policy,
        liveness=pricing.read_liveness(cause.symbol, now_ms),
    )
    if result is not None:
        return result
    # An EXIT owning the symbol is the strategy's own, already answered above
    # and again under intake (``_accept_admissible_redrive``).
    target = await run(lambda: newest_reducible_entry(repo, sid, cause.symbol))
    if target is None:
        return RecoveryResult("hold", "RECOVERY_ENTRY_UNAVAILABLE", "No releasable entry evidence is available for this exit.")
    # Read the instant after repository hops, immediately before quote/liveness.
    now_ms = repo.clock()
    touch = (
        PricingSnapshot(policy, None, pricing.read_liveness(cause.symbol, now_ms))
        if market_leg_sendable(now_ms) or stale.regular_session_only
        else pricing.read(cause.symbol, now_ms, strategy_instance_id=sid)
    )
    try:
        shape = touch.price(side=reducing_side(stale.remaining),
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
            symbol=cause.symbol, decision_id=f"{redrive_decision_prefix(stale.episode_token)}{stale.redrives + 1}",
            entry_order_ref=target.order_ref, confirmed_shape=shape,
            regular_session_only=stale.regular_session_only,
        )
        if accepted is None:
            return None
        if isinstance(accepted, _RedriveRefused):
            # An exit ready before its refusal settled (#2622) spends no
            # failure time early, so escalation keeps its timing.
            refused_at_ms = repo.clock()
            counts = market_leg_sendable(refused_at_ms) and refused_at_ms >= stale.settled_at_ms
            outcome = accepted.outcome if counts else "hold"
            previous = await run(lambda: latest_exit_recovery(repo, strategy_instance_id=sid, uncertainty_id=stale.episode["uncertainty_id"]))
            if outcome == "failure" and (previous is None or previous.first_failure_at_ms is None):
                logger.warning(accepted.message, extra={
                    "action": accepted.action, "account_id": repo.account_id,
                    "strategy_instance_id": sid, "symbol": cause.symbol, **accepted.facts,
                })
            return RecoveryResult(
                outcome, accepted.reason_code, accepted.explanation or accepted.message,
                next_step=accepted.next_step,
            )
        # ``_accept_admissible_redrive`` already checked the account for this
        # re-drive; the Flatten's send-time check is not its own (#2839).
        resolved = await resolve_accepted_exit(
            repo, accepted=accepted, trade=trade, pricing=pricing, read=None, off_loop=run
        )
        return await run(lambda: _redrive_result(repo, stale, resolved))
    except OptionCloseTakesNoPricedLeg as exc:
        # Refused before anything was accepted: an option position is closed by a market order only.
        return RecoveryResult("hold", exc.refusal.reason_code, exc.refusal.explanation)
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


def revalidate_recovery_evaluations(
    repo: ClerkSqliteRepository, evaluations: list[RecoveryEvaluation], *, broker_symbol: BrokerSymbolReader,
) -> None:
    """Do not spend failure time on a broker refusal cleared by the final snapshot."""
    for index, evaluation in enumerate(evaluations):
        result = evaluation.result
        if result.outcome != "failure" or result.reason_code not in {"EXIT_OTHER_ORDER_WORKING", "EXIT_BROKER_POSITION_MISMATCH"}:
            continue
        symbol = evaluation.stale.cause.symbol
        if broker_symbol(symbol).agrees and not clerk_work_in_flight(repo, symbol):
            evaluations[index] = replace(evaluation, evaluated_at_ms=repo.clock(), result=RecoveryResult(
                "hold", "RECOVERY_BROKER_REFUSAL_CLEARED", "The final broker snapshot cleared the earlier refusal; recovery will check again.",
            ))


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
        if observation is not None:
            _say_if_the_contract_expired(repo, evaluation)
        if observation is None or result.outcome != "failure":
            continue
        exhausted = stale.regular_failures >= policy.max_count
        if not exhausted and observation.failure_elapsed_ms < policy.after_ms * (policy.max_count + 1):
            continue
        _escalate_to_exit_stuck(
            repo, redrive_policy=policy, stale_exit=stale, now_ms=repo.clock(),
            deferred_since_ms=None if exhausted else observation.first_failure_at_ms,
            failure_elapsed_ms=observation.failure_elapsed_ms, failure_reason=result.explanation,
            next_step=result.next_step,
        )


def _say_if_the_contract_expired(repo: ClerkSqliteRepository, evaluation: RecoveryEvaluation) -> None:
    """Rewrite the notices of an episode this pass held because its option contract has expired (ADR 0076).

    Written with the pass's observation, so only for an episode that is
    still the bot's open one. The notices then say what is true from now
    on: the contract expired, no order can close it, and nothing more is
    sent for it. They send the operator to no flatten and promise no retry.
    A later pass finds them saying so already and writes nothing.
    """
    stale = evaluation.stale
    if not stale.closes_an_option:
        return
    expired = expired_option_close(stale.cause.symbol, evaluation.evaluated_at_ms)
    if expired is None:
        return
    if say_the_contract_expired(repo, strategy_instance_id=stale.strategy_instance_id, expired=expired):
        logger.warning("a held option contract expired; the bot's open EXIT notices now say so", extra={
            "action": "exit_notices_say_contract_expired", "account_id": repo.account_id,
            "strategy_instance_id": stale.strategy_instance_id, "symbol": stale.cause.symbol,
            "uncertainty_id": stale.episode["uncertainty_id"],
        })


def _accept_admissible_redrive(
    repo: ClerkSqliteRepository,
    *,
    broker_symbol: BrokerSymbolReader,
    strategy_instance_id: str,
    symbol: str,
    decision_id: str,
    entry_order_ref: str,
    confirmed_shape: ConfirmedRecoveryShape | None,
    regular_session_only: bool = False,
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
        unconfirmed_of = bots_with_an_unconfirmed_closing_order(repo, symbol, other_than=strategy_instance_id)
        if unconfirmed_of:
            explanation, next_step = _behind_unconfirmed_closing_orders(unconfirmed_of, symbol)
            return _RedriveRefused(
                action="exit_redrive_deferred_unconfirmed_closing_order", reason_code="EXIT_OTHER_ORDER_WORKING",
                message=(
                    "deferred a stuck-EXIT re-drive: another bot's closing order on the symbol "
                    "was sent and could not be confirmed"
                ),
                facts={"strategy_attributed_qty": remaining, "unconfirmed_closing_order_of": list(unconfirmed_of)},
                explanation=explanation, next_step=next_step,
            )
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
            side=reducing_side(remaining).value.upper(),
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
        regular_session_only=regular_session_only,
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


def bots_with_an_unconfirmed_closing_order(
    repo: ClerkSqliteRepository, symbol: str, *, other_than: str
) -> tuple[str, ...]:
    """The other bots whose closing order on ``symbol`` the Clerk sent and could not confirm (#2854).

    Work in flight (:func:`clerk_work_in_flight`) that only an operator may
    end: such an EXIT stays ``unknown`` with custody until Alpaca shows its
    order or the order is settled. Read from what the settle action itself is
    presented from (``unknown_exit_orders``), so a bot is named exactly when
    its page shows that order. The page cannot always settle it: settling is
    refused for a running bot, inside the wait after a send, and for an order
    Alpaca answered with one that names no client order id.
    """
    candidates = sorted({
        effect.strategy_instance_id
        for effect in repo.reconcilable_effect_operations()
        if effect.kind == "EXIT" and effect.state == "unknown"
        and effect.strategy_instance_id is not None and effect.strategy_instance_id != other_than
    })
    return tuple(
        sid for sid in candidates
        if any(order.symbol.upper() == symbol.upper() for order in repo.unknown_exit_orders(sid))
    )


def _behind_unconfirmed_closing_orders(bots: tuple[str, ...], symbol: str) -> tuple[str, str]:
    """What a bot is told while other bots' unconfirmed closing orders hold its re-drive: why, and what to do (#2854).

    The why is the hold's own reason and, if the hold outlasts the failure
    budget, the reason ``EXIT_STUCK`` gives. The next step is ``EXIT_STUCK``'s:
    by then the Clerk sends nothing more for this bot by itself, whatever
    becomes of those orders, so it is the operator's own flatten of this bot.

    That flatten needs nothing of those orders, and some of them no action
    settles. Both texts are written once and stay on this bot's rows after
    the orders are settled. So neither says to settle them first: each names
    the bots whose pages show them.
    """
    flatten = "Run Reconcile now and execute the presented safe flatten for this bot."
    if len(bots) == 1:
        (bot,) = bots
        return (
            f"Another bot, {bot}, has a closing order for {symbol} that the Clerk sent and could not "
            "confirm. While that order's outcome is unknown, the Clerk does not send this bot's exit "
            f"again by itself. See {bot}'s page.",
            f"{flatten} The Clerk does not send this bot's exit by itself again, even after {bot}'s "
            f"closing order is settled; see {bot}'s page.",
        )
    names = ", ".join(bots)
    return (
        f"Other bots ({names}) have closing orders for {symbol} that the Clerk sent and could not "
        "confirm. While their outcomes are unknown, the Clerk does not send this bot's exit again by "
        "itself. See those bots' pages.",
        f"{flatten} The Clerk does not send this bot's exit by itself again, even after the closing "
        f"orders of {names} are settled; see their pages.",
    )


def _escalate_to_exit_stuck(
    repo: ClerkSqliteRepository,
    *,
    redrive_policy: RedriveThenEscalate,
    stale_exit: _StaleExit,
    now_ms: int,
    deferred_since_ms: int | None,
    failure_elapsed_ms: int = 0, failure_reason: str = "",
    next_step: str | None = None,
) -> None:
    """Escalate a stuck EXIT to EXIT_STUCK: re-drives exhausted, or deferred too long.

    ``deferred_since_ms`` is ``None`` for exhausted re-drives and the first
    deferral's clock otherwise; it only changes the operator-facing wording.
    ``next_step`` is the failing result's own, where it has more to say than
    a reconciliation and a flatten: behind another bot's unconfirmed closing
    order the way on is still this bot's own flatten, and the step also says
    that the Clerk sends nothing more for this bot by itself and names the
    bot whose page shows that order (#2854).
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
                "New exposure stays on hold for this strategy and automatic "
                "re-drives stopped. Exact operator reduction remains available."
            ),
            next_step=next_step or "Run Reconcile now, then execute the presented safe flatten.",
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
