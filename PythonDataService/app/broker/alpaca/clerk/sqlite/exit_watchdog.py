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
refused re-drive never parks an EXIT. A deferral is logged once per episode,
and an episode still deferring after the policy's escalation age (the time
its re-drives would have been exhausted in) escalates to ``EXIT_STUCK``
exactly as exhausted re-drives do.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from typing import NamedTuple
from weakref import WeakKeyDictionary

from app.broker.alpaca.clerk.program_leg import ProgramLegRefused
from app.broker.alpaca.clerk.recovery_reduction import (
    UNPRICEABLE_RECOVERY,
    ConfirmedRecoveryShape,
    RecoveryPricing,
    price_automatic_recovery_reduction,
    quote_spread_bps,
    regular_session_open,
)
from app.broker.alpaca.clerk.sqlite.exit import (
    ExitSubmission,
    accept_recovery_exit,
    resolve_accepted_exit,
)
from app.broker.alpaca.clerk.sqlite.exit_resolution import EXIT_REDRIVE_DECISION_PREFIX
from app.broker.alpaca.clerk.sqlite.facts import UncertaintyRaisedFacts
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.alpaca.clerk.sqlite.idempotency import DurableConflictError
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.models import OrderResource
from app.broker.alpaca.clerk.sqlite.off_loop import OffLoop, run_inline
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

# First pre-acceptance deferral per EXIT_NOT_FLAT episode (uncertainty id ->
# clock ms), per repository. It both logs a deferral once per episode rather
# than every 15 s pass and anchors the deferral escalation age. Process
# memory on purpose: a restart re-logs once and restarts the deferral clock,
# which only delays an escalation, never sends an order.
_FIRST_DEFERRAL_MS: WeakKeyDictionary[ClerkSqliteRepository, dict[str, int]] = WeakKeyDictionary()


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


class _StaleExit(NamedTuple):
    """One age-qualified EXIT_NOT_FLAT episode and its redrive count."""

    strategy_instance_id: str
    episode: dict
    cause: ExitNotFlatCause
    remaining: float
    redrives: int
    episode_token: str


async def redrive_or_escalate_stale_exits(
    repo: ClerkSqliteRepository,
    *,
    trade: BrokerTradePort,
    intake: ReentrantAsyncLock,
    broker_symbol: BrokerSymbolReader,
    pricing: RecoveryPricing = UNPRICEABLE_RECOVERY,
    off_loop: OffLoop | None = None,
) -> None:
    """Age-gate active EXIT_NOT_FLAT episodes: bounded re-drive, then escalate.

    ``broker_symbol`` reads one symbol from this pass's broker snapshot
    against the Clerk's current attribution (#2343). It has no default: a
    caller without a broker snapshot cannot re-drive.

    ``pricing`` is what an extended-hours re-drive prices from (#2229); the
    degraded :data:`UNPRICEABLE_RECOVERY` default defers outside the regular
    session rather than guessing a price, so a caller with no declared window
    — a paper authority, a test — behaves exactly as before.

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
    pricing_policy = pricing.policy_source()

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
            if episode is None or now_ms - episode["observed_at_ms"] < redrive_policy.after_ms:
                continue
            if (
                repo.active_uncertainty(
                    scope="CUSTODY_SUBJECT",
                    reason_code=EXIT_STUCK_REASON_CODE,
                    strategy_instance_id=sid,
                )
                is not None
            ):
                continue  # already escalated: automatic re-drives stopped
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
            while (
                repo.get_command(
                    f"cmd:{sid}:{EXIT_REDRIVE_DECISION_PREFIX}{episode_token}-{redrives + 1}"
                )
                is not None
            ):
                redrives += 1
            stale.append(_StaleExit(sid, episode, cause, remaining, redrives, episode_token))
        return now_ms, stale

    now_ms, stale = await run(_scan_stale_exits)
    for stale_exit in stale:
        sid = stale_exit.strategy_instance_id
        episode = stale_exit.episode
        cause = stale_exit.cause
        remaining = stale_exit.remaining
        redrives = stale_exit.redrives
        if redrives >= redrive_policy.max_count:
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
            continue
        confirmed_shape = None
        quote_spread = None
        if not regular_session_open(now_ms):
            # Owner decision 2026-09-19 (evening, #2229): an extended-hours
            # re-drive prices a limit itself instead of waiting for the open.
            # A refusal (no session, no allowance, no live quote, or a spread
            # past the cap) defers — the episode stays raised and the entry
            # stays free.
            quote = pricing.quote_source(cause.symbol, now_ms)
            try:
                priced = price_automatic_recovery_reduction(
                    side=OrderSide.SELL if remaining > 0 else OrderSide.BUY,
                    symbol=cause.symbol,
                    quantity=remaining,
                    now_ms=now_ms,
                    policy=pricing_policy,
                    quote=quote,
                )
            except ProgramLegRefused as exc:
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
                        "quote_spread_bps": None
                        if quote is None
                        else quote_spread_bps(quote),
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
            quote_spread = quote_spread_bps(quote) if quote is not None else None
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
            _FIRST_DEFERRAL_MS.get(repo, {}).pop(episode["uncertainty_id"], None)
            await resolve_accepted_exit(
                repo, accepted=accepted, trade=trade, pricing=pricing, off_loop=run
            )
        except (OperationClaimError, AdmissionBlockedError, DurableConflictError):
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
                "deferred a stuck-EXIT re-drive: this pass's broker snapshot does not "
                "agree with the attributed position, or an order on it is working"
            ),
            facts={
                "broker_qty": view.broker_qty,
                "attributed_qty": view.attributed_qty,
                "strategy_attributed_qty": remaining,
                "broker_agrees": view.agrees,
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
    """Log a pre-acceptance deferral once per episode; escalate one that outlasts the policy.

    A deferral burns no attempt, so without this bound a permanently flat
    broker would never reach ``EXIT_STUCK``. The escalation age is the time
    the policy's re-drives would have been exhausted in:
    ``after_ms × (max_count + 1)`` counted from the episode's first deferral.
    """
    uncertainty_id = stale_exit.episode["uncertainty_id"]
    first_deferrals = _FIRST_DEFERRAL_MS.setdefault(repo, {})
    first_ms = first_deferrals.get(uncertainty_id)
    if first_ms is None:
        first_deferrals[uncertainty_id] = now_ms
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
        return
    if now_ms - first_ms < redrive_policy.after_ms * (redrive_policy.max_count + 1):
        return
    await _escalate_to_exit_stuck(
        repo,
        intake=intake,
        redrive_policy=redrive_policy,
        stale_exit=stale_exit,
        now_ms=now_ms,
        deferred_since_ms=first_ms,
    )


async def _escalate_to_exit_stuck(
    repo: ClerkSqliteRepository,
    *,
    intake: ReentrantAsyncLock,
    redrive_policy: RedriveThenEscalate,
    stale_exit: _StaleExit,
    now_ms: int,
    deferred_since_ms: int | None,
) -> None:
    """Escalate a stuck EXIT to EXIT_STUCK: re-drives exhausted, or deferred too long.

    ``deferred_since_ms`` is ``None`` for exhausted re-drives and the first
    deferral's clock otherwise; it only changes the operator-facing wording.
    """
    sid = stale_exit.strategy_instance_id
    cause = stale_exit.cause
    redrives = stale_exit.redrives
    if deferred_since_ms is None:
        headline = "A stuck EXIT exhausted automatic re-drives"
        why = f"after {redrives} automatic EXIT re-drives."
    else:
        headline = "A stuck EXIT could not be safely re-driven"
        why = (
            f"after automatic re-drives were deferred for {now_ms - deferred_since_ms} ms: "
            "the broker's position did not agree with the Clerk's, or an order on the "
            "symbol was still working."
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
        if episode_now is None:
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
