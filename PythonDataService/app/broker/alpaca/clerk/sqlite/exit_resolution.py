"""Durable cancel → prove terminal → reduce once → verify flat EXIT machine.

**The send-time rule (#2440, owner decision #2431).** Whether a reducing leg
may go to the broker is decided when it is sent, not when the EXIT was
decided, and once for every EXIT — a deciding program's, the operator's
flatten, the watchdog's re-drive — by :func:`_leg_to_create` (the reduction
is created now) and :func:`_created_leg_may_be_sent` (a created order is
submitted or resubmitted now), both through
``recovery_reduction.reducing_leg_verdict``. A market leg is sent only inside
the regular session, where Alpaca executes it; after the close Alpaca would
queue it for the next open. A limit is sent only inside the session it was
priced for. Both are judged at the latest instant the leg may reach the
broker (``send_arrival_ms``: now plus a few seconds' guard band), and on an
early-close day after-hours ends at the calendar's close. An EXIT whose leg
can no longer go out as recorded is re-priced for that instant
(``price_automatic_recovery_reduction``: an
extended-hours limit off the live touch in PRE or POST, the market leg inside
the regular session) unless an operator confirmed that price; when nothing
can price it, or its order already has a broker identity, nothing is sent and
the EXIT folds releasably through ``EXIT_NOT_FLAT`` — the operator-visible
episode, never a silent queue, carrying when the watchdog next tries. A
broker's outright refusal of the reducing order raises the same episode, and
so does a reducing order the broker still reports working well past its
session (:func:`_raise_if_working_past_session`). What it is re-priced from is the one pricing
seam the caller names (every caller passes its authority's
``recovery_pricing``); the live touch is read on the event loop, and only for
a leg that must be re-priced. A leg the Clerk priced this way records who
priced it and the quote it was priced against on its own
``EXIT_REDUCING_ORDER_CREATED`` facts.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass
from typing import ClassVar, NamedTuple

from app.broker.alpaca.clerk.program_leg import (
    LegRefusal,
    LegShape,
    ProgramLegRefused,
    regular_session_shape,
)
from app.broker.alpaca.clerk.recovery_reduction import (
    RECOVERY_LIMIT_QUANTITY_CHANGED,
    RECOVERY_LIMIT_SESSION_ENDED,
    RECOVERY_MARKET_WAIT_ENDED,
    REDUCING_ORDER_PAST_SESSION_GRACE_MS,
    ConfirmedRecoveryShape,
    PricingSnapshot,
    RecoveryPricing,
    ReducingLegVerdict,
    next_redrive_at_ms,
    reducing_leg_session_end_ms,
    reducing_leg_verdict,
    reduction_market_hold,
    reduction_touch,
)
from app.broker.alpaca.clerk.sqlite.claimed_broker_io import ClaimedBrokerIO
from app.broker.alpaca.clerk.sqlite.exit_recovery import observe_exit_recovery
from app.broker.alpaca.clerk.sqlite.facts import (
    ExitAcceptedFacts,
    ExitReducingOrderCreatedFacts,
)
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.models import (
    EffectOperationResource,
    ExitSubmission,
    OrderResource,
    TransitionInput,
)
from app.broker.alpaca.clerk.sqlite.off_loop import (
    OffLoop,
    claim_scoped,
    run_drained,
    run_inline,
)
from app.broker.alpaca.clerk.sqlite.open_replacement import cancel_at_regular_open
from app.broker.alpaca.clerk.sqlite.order_evidence import (
    UNFILLED_TERMINAL_STATES,
    entry_never_accepted_durably,
    entry_order_symbol,
    fold_entry_never_accepted,
    fold_failed,
    fold_order_evidence,
    fold_order_submission_response,
    fold_submit_absence_void,
    fold_uncertain,
    order_never_reached_broker,
    submit_absence_grace_ms,
    trade_port_folds_simulated_evidence,
)
from app.broker.alpaca.clerk.sqlite.order_projection import (
    ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES,
)
from app.broker.alpaca.clerk.sqlite.reads import NONTERMINAL_EFFECT_STATES
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    EXIT_NOT_FLAT_REASON_CODE,
    Capability,
    ReductionIntent,
    raise_uncertainty,
    require_capability,
    resolve_exit_not_flat_uncertainty,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import ExitNotFlatCause
from app.broker.alpaca.clerk.sqlite.uncertainty_policies import (
    RedriveThenEscalate,
    reason_age_policy,
)
from app.broker.contract.errors import BrokerError, BrokerUnavailable
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg, OrderSide
from app.broker.contract.ports import BrokerTradePort
from app.engine.live.order_identity import build_bot_order_namespace, build_order_ref

logger = logging.getLogger(__name__)

_REDRIVE_NEXT_STEP = (
    "Flatten with a priced limit now, or let the automatic re-drive reduce it at the "
    "next attempt shown with this notice."
)
"""What the operator can do about an EXIT that folded with exposure the watchdog re-drives.

Every ``EXIT_NOT_FLAT`` fold carries the time of that re-drive
(:func:`_fold_exit_not_flat`); this is the next step of the folds whose copy
has nothing more specific to ask — a leg the send-time rule could not send, a
broker's refusal, an extended-hours limit that ended unfilled — so it points
at the time the notice shows."""

PROGRAM_EXIT_SESSION_ENDED = LegRefusal(
    reason_code="PROGRAM_EXIT_SESSION_ENDED",
    explanation=(
        "The session this exit's order was shaped for ended before the order could go "
        "out, and nothing is queued for the next open."
    ),
    next_step=_REDRIVE_NEXT_STEP,
)
"""The deciding program's EXIT the send-time rule could not send (#2440).

Operator copy, kept beside the one fold that writes it. Whether a price was
tried in its place — and why none could be — is said by the episode's own
explanation, never by this shared text: a created order being resubmitted is
never re-priced at all."""

# The two decision-id namespaces documented on ``exit.accept_recovery_exit``
# (minted by ``safe_flatten_execution`` and ``exit_watchdog`` respectively).
# Named here, the module both namespaces classify against, so a rename or a
# third namespace has exactly one declaration to update.
RECOVERY_FLATTEN_DECISION_PREFIX = "recovery-flatten-"
EXIT_REDRIVE_DECISION_PREFIX = "exit-redrive-"
_RECOVERY_DECISION_PREFIXES = (RECOVERY_FLATTEN_DECISION_PREFIX, EXIT_REDRIVE_DECISION_PREFIX)


async def resolve_exit(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    trade: BrokerTradePort,
    pricing: RecoveryPricing,
    off_loop: OffLoop | None = None,
) -> ExitSubmission:
    """Advance one EXIT under one exclusive, attempt-scoped broker claim.

    The reducing leg's shape is never passed in: it is read from this EXIT's
    own ``EXIT_ACCEPTED`` facts (ADR 0059 D5.3), so the deciding runner's
    first pass, the reconciliation sweep's re-drive, the watchdog and
    recovery all build the same leg for the same EXIT. An EXIT that recorded
    no shape — a regular-session decision, a safe flatten or re-drive inside
    the regular session — gets the regular-session market DAY leg (ruling R5).
    Either leg then passes the send-time rule (module docstring); ``pricing``
    is what a leg that can no longer go out as recorded is re-priced from. It
    has no default: the runner, the sweep, restart recovery, the watchdog and
    the operator's flatten all name their authority's one seam
    (``SqliteAlpacaClerkFacade.recovery_pricing``), so which caller drives an
    EXIT never decides whether it is re-priced or folded. A caller that can
    price nothing passes :data:`~recovery_reduction.UNPRICEABLE_RECOVERY`, and
    such an EXIT folds for the operator instead.

    ``off_loop`` moves each synchronous repository run of the machine onto a
    worker thread (#1993); the default keeps the pre-#1993 inline behavior
    for every non-sweep caller.
    """
    run = off_loop if off_loop is not None else run_inline
    effect = await run(lambda: repo.effect_operation(effect_operation_id))
    assert effect is not None
    if effect.state in ("succeeded", "failed", "rejected"):
        return await run(lambda: _snapshot(repo, effect_operation_id))
    # Claim on the caller's thread, body claim_scoped: a cancellation can
    # neither strand a claim nor release around a still-running worker
    # (#1993 review).
    claim_token = repo.claim_before_broker_contact(effect_operation_id).token
    broker = ClaimedBrokerIO(
        repo=repo,
        effect_operation_id=effect_operation_id,
        claim_token=claim_token,
        trade=trade,
    )
    try:
        return await _resolve_claimed(
            repo,
            effect_operation_id=effect_operation_id,
            broker=broker,
            pricing=pricing,
            run=claim_scoped(run),
        )
    finally:
        # Off the loop (it takes the write lock) and drained on cancellation.
        await run_drained(
            run,
            lambda: repo.release_operation_claim(
                effect_operation_id=effect_operation_id, token=claim_token
            ),
        )


async def cancel_and_prove_owned_entry(
    repo: ClerkSqliteRepository,
    *,
    entry_order_ref: str,
    trade: BrokerTradePort,
    off_loop: OffLoop | None = None,
) -> OrderResource:
    """Cancel one exact owned ENTRY under its current durable custodian.

    ``off_loop`` moves each synchronous repository run onto a worker thread
    (#1993); the default keeps the pre-#1993 inline behavior.
    """
    run = off_loop if off_loop is not None else run_inline
    entry = await run(lambda: repo.order(entry_order_ref))
    if entry is None or entry.role != "ENTRY":
        raise ValueError(f"{entry_order_ref!r} is not an owned ENTRY order")
    active_exit = await run(lambda: repo.active_exit_for_order(entry_order_ref))
    effect_operation_id = (
        active_exit.effect_operation_id
        if active_exit is not None
        else entry.effect_operation_id
    )
    # Claim on the caller's thread, body claim_scoped (see resolve_exit).
    claim_token = repo.claim_before_broker_contact(effect_operation_id).token
    broker = ClaimedBrokerIO(
        repo=repo,
        effect_operation_id=effect_operation_id,
        claim_token=claim_token,
        trade=trade,
    )
    try:
        await _cancel_and_prove_entry(
            repo,
            effect_operation_id=effect_operation_id,
            entry=entry,
            broker=broker,
            run=claim_scoped(run),
        )
    finally:
        await run_drained(
            run,
            lambda: repo.release_operation_claim(
                effect_operation_id=effect_operation_id, token=claim_token
            ),
        )
    refreshed = await run(lambda: repo.order(entry_order_ref))
    assert refreshed is not None
    return refreshed


class _ClaimedState(NamedTuple):
    """One consistent read of the claimed EXIT's durable custody state."""

    effect: EffectOperationResource
    entries: list[OrderResource]
    reducing: OrderResource | None
    symbol: str
    reducing_fills_short: bool


def _read_exit_claim_state(
    repo: ClerkSqliteRepository, effect_operation_id: str
) -> _ClaimedState:
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    orders = repo.orders_for_effect_operation(effect_operation_id)
    entries = [order for order in orders if order.role == "ENTRY"]
    reducing = next((order for order in orders if order.role == "REDUCING"), None)
    assert entries
    return _ClaimedState(
        effect,
        entries,
        reducing,
        _single_entry_symbol(repo, entries),
        reducing is not None and repo.order_fills_short_of_broker_cumulative(reducing.order_ref),
    )


def _fold_attributed_flat_or_none(
    repo: ClerkSqliteRepository, effect_operation_id: str, state: _ClaimedState
) -> ExitSubmission | None:
    remaining_qty = repo.position(state.effect.strategy_instance_id, state.symbol)
    if state.reducing is None and not position_quantity_is_nonzero(remaining_qty):
        _fold_attributed_flat(repo, effect_operation_id, _primary_entry_ref(repo, state.entries))
        return _snapshot(repo, effect_operation_id)
    return None


def _prepare_reduction(
    repo: ClerkSqliteRepository,
    effect_operation_id: str,
    state: _ClaimedState,
    touch: PricingSnapshot | None,
    pricing: RecoveryPricing,
) -> OrderResource | ExitSubmission | _TouchNeeded:
    """One atomic admission-to-append run: the created reducing order, or
    the EXIT's snapshot when a precondition folded it to an early end —
    attributed-flat, the send-time rule, or a moved quantity — or
    :data:`_TOUCH_NEEDED`, having written nothing, when the recorded leg
    must be re-priced and ``touch`` was not read yet. ``pricing`` names the
    policy a fold reads the watchdog's next attempt from.
    """
    remaining_qty = repo.position(state.effect.strategy_instance_id, state.symbol)
    order_ref = _primary_entry_ref(repo, state.entries)
    if not position_quantity_is_nonzero(remaining_qty):
        _fold_attributed_flat(repo, effect_operation_id, order_ref)
        return _snapshot(repo, effect_operation_id)
    leg = _leg_to_create(
        repo,
        effect_operation_id=effect_operation_id,
        order_ref=order_ref,
        symbol=state.symbol,
        remaining_qty=remaining_qty,
        touch=touch,
        pricing=pricing,
    )
    if isinstance(leg, _TouchNeeded):
        return leg
    if leg is None:
        return _snapshot(repo, effect_operation_id)
    require_capability(
        repo,
        capability=Capability.REDUCE,
        strategy_instance_id=state.effect.strategy_instance_id,
        reduction_intent=ReductionIntent(
            symbol=state.symbol,
            side="SELL" if remaining_qty > 0 else "BUY",
            quantity=abs(remaining_qty),
        ),
    )
    return _create_reducing_order(
        repo,
        effect_operation_id=effect_operation_id,
        symbol=state.symbol,
        quantity=remaining_qty,
        leg=leg,
    )


def _finalize_claimed_exit(
    repo: ClerkSqliteRepository,
    effect_operation_id: str,
    state: _ClaimedState,
    submitted_reducing: OrderResource,
    pricing: RecoveryPricing,
) -> ExitSubmission:
    """Fold the driven reducing order's outcome and snapshot the EXIT."""
    refreshed = repo.order(submitted_reducing.order_ref)
    assert refreshed is not None
    if not _is_terminal(refreshed.broker_state):
        return _snapshot(repo, effect_operation_id)
    final_qty = repo.position(state.effect.strategy_instance_id, state.symbol)
    if not position_quantity_is_nonzero(final_qty):
        _fold_attributed_flat(repo, effect_operation_id, _primary_entry_ref(repo, state.entries))
        return _snapshot(repo, effect_operation_id)
    # A synchronous submit response can truthfully say the reducing order
    # is terminal (``filled``/``replaced``) while its execution slice has
    # not reached the websocket capture path yet.  That is incomplete
    # custody evidence, not proof that an EXIT failed to flatten, so hold
    # the effect unknown until an exact execution or labelled recovery
    # observation can establish the economic delta.  A ``canceled`` /
    # ``expired`` / ``rejected`` terminal snapshot with no recorded
    # execution carries no such ambiguity — it is proven unfilled
    # (ADR 0059 D5.4) and falls through to EXIT_NOT_FLAT below.
    # The same holds when the recorded fills fall short of the broker's own
    # cumulative (#2305): a slice was lost, so the remaining attribution is
    # not proof the reduction under-filled. The shortfall read subsumes the
    # no-fill clause for every acknowledgement that reports its cumulative;
    # the clause stays for acknowledgements written before #2305, whose
    # ``facts_json`` is ``{}`` and so never reads short. The shortfall is
    # bounded: the refresh above appends the broker's current cumulative as
    # the latest acknowledgement, so after one successful exact lookup the
    # order is short only while the broker itself reports unrecorded fills.
    if (
        not repo.fills_for_order(refreshed.order_ref)
        and (refreshed.broker_state or "").lower() not in UNFILLED_TERMINAL_STATES
    ) or repo.order_fills_short_of_broker_cumulative(refreshed.order_ref):
        if state.effect.state != "unknown":
            fold_uncertain(
                repo,
                effect_operation_id=effect_operation_id,
                order_ref=refreshed.order_ref,
                why=(
                    "Reducing order is terminal but its recorded execution slices do "
                    "not yet cover the broker's filled quantity; awaiting websocket "
                    "or recovery evidence."
                ),
            )
        return _snapshot(repo, effect_operation_id)
    created = _reducing_order_facts(repo, refreshed.order_ref)
    if created.extended_hours:
        # An extended-hours DAY limit the broker ended unfilled — normally at
        # the close of the after-hours session (#2440, owner decision #2431):
        # the operator is told the position is still open, and when the
        # watchdog tries again; nothing is queued for the next day.
        headline = "An extended-hours exit ended unfilled; the position is still open"
        explanation = (
            f"The extended-hours limit to {created.side.lower()} {created.quantity:g} "
            f"{state.symbol} at {created.limit_price} ended {refreshed.broker_state} "
            f"without flattening the position; {final_qty:g} {state.symbol} is still held."
        )
        next_step = (
            f"{_REDRIVE_NEXT_STEP} Nothing was queued for the next open."
        )
    else:
        headline = "A completed EXIT left attributed exposure"
        explanation = (
            f"The reducing order became terminal while {final_qty:g} {state.symbol} "
            "remained attributed to this strategy."
        )
        next_step = "Run another EXIT or reconcile until attributed exposure is flat."
    _fold_exit_not_flat(
        repo,
        effect_operation_id=effect_operation_id,
        order_ref=refreshed.order_ref,
        symbol=state.symbol,
        attributed_qty=final_qty,
        summary_code="EXIT_NOT_FLAT",
        reason="The reducing order resolved without flattening the position.",
        headline=headline,
        explanation=explanation,
        next_step=next_step,
        pricing=pricing,
    )
    return _snapshot(repo, effect_operation_id)


async def _resolve_claimed(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    broker: ClaimedBrokerIO,
    pricing: RecoveryPricing,
    run: OffLoop,
) -> ExitSubmission:
    """The claimed EXIT machine: read state, prove terminal, reduce, finalize.

    Broker sequencing stays here; every synchronous repository phase is a
    named module-level function executed through ``run`` — the claim-scoped
    off-loop seam ``resolve_exit`` wraps before handing it in (#1993).

    The send-time rule is judged only once the entry set is proven terminal:
    a working entry is cancelled whatever the session, because cancelling it
    reduces risk and the reducing quantity is not known until it is proven.

    ``pricing`` is read here, on the event loop, and only when the repository
    step answers that the recorded leg must be re-priced: the production
    quote source registers IBKR demand in the market-liveness store, which is
    not safe from the worker thread ``run`` may hop to, and a leg that goes
    out as recorded needs no quote at all (#2440 review).
    """
    state = await run(lambda: _read_exit_claim_state(repo, effect_operation_id))
    if not await _prove_entry_set_terminal(
        repo,
        effect_operation_id=effect_operation_id,
        entries=state.entries,
        reducing_exists=state.reducing is not None,
        broker=broker,
        run=run,
    ):
        return await run(lambda: _snapshot(repo, effect_operation_id))

    flat_snapshot = await run(
        lambda: _fold_attributed_flat_or_none(repo, effect_operation_id, state)
    )
    if flat_snapshot is not None:
        return flat_snapshot

    reducing = state.reducing
    if reducing is None:
        # A previous call may have proven terminal minutes ago. Refresh every
        # exact entry identity under the same claim immediately before the
        # reducing quantity is fixed.
        if not await _refresh_terminal_entries(
            repo,
            effect_operation_id=effect_operation_id,
            entries=state.entries,
            broker=broker,
            run=run,
        ):
            return await run(lambda: _snapshot(repo, effect_operation_id))
        now_ms = repo.clock()
        hold = reduction_market_hold(now_ms=now_ms, fact=pricing.read_liveness(state.symbol, now_ms))
        if hold is not None:
            await run(lambda: _fold_market_hold(
                repo, effect_operation_id, _primary_entry_ref(repo, state.entries), state.symbol, hold, pricing,
            ))
            return await run(lambda: _snapshot(repo, effect_operation_id))
        prepared = await run(
            lambda: _prepare_reduction(repo, effect_operation_id, state, None, pricing)
        )
        if isinstance(prepared, _TouchNeeded):
            touch = pricing.read(state.symbol, repo.clock())
            prepared = await run(
                lambda: _prepare_reduction(repo, effect_operation_id, state, touch, pricing)
            )
        if isinstance(prepared, ExitSubmission):
            return prepared
        assert isinstance(prepared, OrderResource)
        reducing = prepared
        await _submit_reducing_order(
            repo,
            effect_operation_id=effect_operation_id,
            reducing=reducing,
            broker=broker,
            pricing=pricing,
            run=run,
        )
    elif not _is_terminal(reducing.broker_state) or state.reducing_fills_short:
        # A terminal reducing order whose recorded fills fall short of the
        # broker's cumulative lost a slice (#2305): refresh it by exact lookup
        # so the cumulative fold closes the gap before the outcome is judged.
        await _refresh_or_resume_reducing_order(
            repo,
            effect_operation_id=effect_operation_id,
            reducing=reducing,
            broker=broker,
            pricing=pricing,
            run=run,
        )
    return await run(
        lambda: _finalize_claimed_exit(repo, effect_operation_id, state, reducing, pricing)
    )


def _fold_exit_not_flat(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    order_ref: str,
    symbol: str,
    attributed_qty: float,
    summary_code: str,
    reason: str,
    headline: str,
    explanation: str,
    next_step: str,
    pricing: RecoveryPricing,
    why: str | None = None,
) -> None:
    """Fail the EXIT and raise the ``EXIT_NOT_FLAT`` episode exposure is still held under.

    The episode is what flags the bot for the operator — on the bot page and
    in the lane's attention bell — and what the stuck-EXIT watchdog re-drives:
    the market leg inside the regular session, a limit it prices itself in a
    declared extended session (#2229). Every such episode carries when that
    re-drive is first session-eligible (#2440), computed here from ``pricing`` so no fold
    can omit or disagree on it: an ``int64 ms UTC`` value the UI renders,
    never prose. What the fold cannot know — that the watchdog has since
    escalated, an EXIT is working, or session eligibility has rolled over — the projection says
    (``projections.project_uncertainties``). ``why`` is the cause recorded on
    the failed transition beside the quantity still held.
    """
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    held = f"attributed_qty={attributed_qty} remains for {symbol!r}."
    fold_failed(
        repo,
        effect_operation_id=effect_operation_id,
        order_ref=order_ref,
        summary_code=summary_code,
        reason=reason,
        why=held if why is None else f"{why} {held}",
        transition_kind="EXIT_NOT_FLAT",
    )
    raise_uncertainty(
        repo,
        strategy_instance_id=effect.strategy_instance_id,
        reason_code=EXIT_NOT_FLAT_REASON_CODE,
        headline=headline,
        explanation=explanation,
        operator_impact=_EXIT_NOT_FLAT_OPERATOR_IMPACT,
        next_step=next_step,
        evidence_refs=(order_ref,),
        cause_facts=ExitNotFlatCause(
            symbol=symbol.upper(),
            attributed_qty=attributed_qty,
        ).to_mapping(),
        severity="error",
        next_attempt_at_ms=_next_redrive_at_ms(repo, pricing),
    )


_EXIT_NOT_FLAT_OPERATOR_IMPACT = (
    "New exposure is paused for this strategy; exact risk reduction and "
    "reconciliation remain available."
)


def _next_redrive_at_ms(repo: ClerkSqliteRepository, pricing: RecoveryPricing) -> int:
    """When the stuck-EXIT watchdog will next try to reduce what a fold leaves held (#2440).

    Not before the episode's re-drive age, and then in the first session the
    pricing seam can send in: the regular session, or a declared extended one
    when the policy carries the exit allowance. Only
    :func:`_fold_exit_not_flat` asks, so every fold agrees; no quote is read.
    """
    redrive = reason_age_policy(EXIT_NOT_FLAT_REASON_CODE, RedriveThenEscalate)
    return next_redrive_at_ms(
        not_before_ms=repo.clock() + redrive.after_ms, policy=pricing.policy_source()
    )


def _fold_observed_reducing_order(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    reducing: OrderResource,
    observed: BrokerOrder,
    simulated_authority: bool,
) -> None:
    """Fold the broker's exact answer for the reducing order, and alarm if it outlived its session.

    A later pass's exact lookup finding the order still working is the one
    proof it outlived its session, so :func:`_raise_if_working_past_session`
    runs only here: never for an order whose submit was lost or whose lookup
    failed — an unknown outcome, not a working order — and never once the
    EXIT has an outcome. An EXIT already folded for the operator (a
    resubmission the send-time rule refused, say) keeps that notice and when
    the watchdog next tries (#2440 review).
    """
    fold_order_evidence(
        repo,
        effect_operation_id=effect_operation_id,
        order=observed,
        simulated_authority=simulated_authority,
    )
    refreshed = repo.order(reducing.order_ref)
    assert refreshed is not None
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    if _is_terminal(refreshed.broker_state) or effect.state not in NONTERMINAL_EFFECT_STATES:
        return
    _raise_if_working_past_session(repo, effect=effect, reducing=refreshed)


def _raise_if_working_past_session(
    repo: ClerkSqliteRepository,
    *,
    effect: EffectOperationResource,
    reducing: OrderResource,
) -> None:
    """Tell the operator, once, that a reducing order seen working outlived its session (#2440 review).

    Acceptance criterion 4 — an exit unfilled at the end of after-hours is
    told to the operator — otherwise rests on the broker ending the DAY
    extended-hours limit at the after-hours close and saying so. The Clerk
    does not rely on it: a reducing order the broker still reports working
    :data:`~recovery_reduction.REDUCING_ORDER_PAST_SESSION_GRACE_MS` after the
    broker should have ended it (``reducing_leg_session_end_ms``: the
    after-hours close of the day an extended-hours DAY limit was sent — a
    pre-market limit keeps working through the regular session — or the
    regular close for a market leg) raises ``EXIT_NOT_FLAT`` while the EXIT
    keeps custody of the order. The
    EXIT is not failed and its entry not released — the order may still
    execute — so the watchdog finds no free entry and re-drives nothing. The
    copy holds no clock value, so a later pass re-raises it unchanged. The
    session is dated from the latest send: an order proven absent after a
    lost submit is legitimately sent again, possibly the next day.
    """
    submitted = repo.last_order_transition(
        order_ref=reducing.order_ref, transition_kind="ORDER_SUBMIT_REQUESTED"
    )
    if submitted is None:
        return  # no recorded send to date its session from
    created = _reducing_order_facts(repo, reducing.order_ref)
    session_end_ms = reducing_leg_session_end_ms(
        extended_hours=created.extended_hours,
        valid_until_ms=_created_leg_valid_until_ms(repo, effect.effect_operation_id, created),
        sent_at_ms=submitted["recorded_at_ms"],
    )
    if session_end_ms is None or repo.clock() < session_end_ms + REDUCING_ORDER_PAST_SESSION_GRACE_MS:
        return
    symbol = created.symbol
    held = repo.position(effect.strategy_instance_id, symbol)
    if not position_quantity_is_nonzero(held):
        return
    leg = "extended-hours limit" if created.extended_hours else "market order"
    outcome = raise_uncertainty(
        repo,
        strategy_instance_id=effect.strategy_instance_id,
        reason_code=EXIT_NOT_FLAT_REASON_CODE,
        headline="An exit order is still working after its session ended; the position is still open",
        explanation=(
            f"The {leg} to {created.side.lower()} {created.quantity:g} {symbol} is still "
            f"working at the broker after the last session it could trade in ended; {held:g} "
            f"{symbol} is still held."
        ),
        operator_impact=_EXIT_NOT_FLAT_OPERATOR_IMPACT,
        next_step=(
            "Check the order at the broker and cancel it there if it should not execute; "
            "the Clerk keeps custody of it until the broker reports it terminal."
        ),
        evidence_refs=(reducing.order_ref,),
        cause_facts=ExitNotFlatCause(symbol=symbol.upper(), attributed_qty=held).to_mapping(),
        severity="error",
    )
    if outcome == "raised":
        logger.error(
            "a reducing order is still working past the end of its session",
            extra={
                "action": "reducing_order_working_past_session",
                "account_id": repo.account_id,
                "strategy_instance_id": effect.strategy_instance_id,
                "effect_operation_id": effect.effect_operation_id,
                "order_ref": reducing.order_ref,
                "session_end_ms": session_end_ms,
            },
        )


class _SendableLeg(NamedTuple):
    """The leg a reduction is created with now, and until when it may be sent."""

    shape: LegShape
    valid_until_ms: int | None
    # The Clerk's own price when the leg was priced as the reduction was
    # created rather than taken from the acceptance (#2440): recorded on the
    # order with the quote it was priced against, so a fill's slippage is
    # measured from it and no copy ever calls the price confirmed.
    clerk_price: ConfirmedRecoveryShape | None


class _TouchNeeded:
    """The recorded leg must be re-priced, and no live touch was read for it yet."""


_TOUCH_NEEDED = _TouchNeeded()


def _leg_to_create(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    order_ref: str,
    symbol: str,
    remaining_qty: float,
    touch: PricingSnapshot | None,
    pricing: RecoveryPricing,
) -> _SendableLeg | _TouchNeeded | None:
    """The send-time rule at creation: the leg to create now, or ``None`` once the EXIT folded.

    The EXIT's recorded leg, side-reconciled, is kept while it can still go
    out — an operator's confirmed price only for the quantity it was
    confirmed for. Otherwise — a market leg after the regular close, which
    Alpaca would queue for the next open; a limit past the session it was
    priced for — it is re-priced for the instant it would reach the broker
    (``send_arrival_ms``), since no broker identity exists yet to preserve
    (#2440, owner decision #2431): an extended-hours limit off the live touch
    in PRE or POST, the market leg inside the regular session. Re-pricing
    needs ``touch``, which is read on the event loop and never here: without
    one this answers :data:`_TOUCH_NEEDED` before writing anything, and the
    caller reads the touch and asks again.

    An operator's confirmed price is never replaced by one nobody confirmed
    (#2007). Whether the recorded price is the operator's is read from the
    EXIT's own acceptance — who priced it — never from the caller's pricing
    seam, and never from the side-reconciled shape: a limit confirmed for the
    other side becomes a market leg here, and is still the operator's. A
    refusal to price (no session open, no allowance, no live quote, a spread
    past the cap) folds the EXIT for the operator; the watchdog's re-drive
    keeps trying.
    """
    facts = _accepted_facts(repo, effect_operation_id)
    now_ms = repo.clock()
    reducing_side = OrderSide.SELL if remaining_qty > 0 else OrderSide.BUY
    recorded = facts.reducing_shape()
    shape = _resolved_reducing_shape(recorded, reducing_side=reducing_side)
    valid_until_ms = facts.reducing_valid_until_ms if shape.extended_hours else None
    verdict = reducing_leg_verdict(
        extended_hours=shape.extended_hours, valid_until_ms=valid_until_ms, now_ms=now_ms
    )
    operator_priced = _operator_priced(facts)
    if verdict != "send" and not operator_priced and touch is None:
        return _TOUCH_NEEDED
    if recorded is not None and shape != recorded:
        logger.warning(
            "Reducing leg shape was priced for the other side; submitting a regular-session market leg instead",
            extra={
                "action": "reducing_leg_shape_side_mismatch",
                "effect_operation_id": effect_operation_id,
                "shaped_side": recorded.side.value,
                "reducing_side": reducing_side.value,
            },
        )
    if verdict == "send":
        if not _confirmed_quantity_still_holds(
            repo,
            effect_operation_id=effect_operation_id,
            order_ref=order_ref,
            symbol=symbol,
            remaining_qty=remaining_qty,
            pricing=pricing,
        ):
            return None
        return _SendableLeg(shape, valid_until_ms, clerk_price=None)

    def fold(not_repriced: _NotRepriced) -> None:
        _fold_unsendable_leg(
            repo,
            effect_operation_id=effect_operation_id,
            order_ref=order_ref,
            symbol=symbol,
            remaining_qty=remaining_qty,
            verdict=verdict,
            not_repriced=not_repriced,
            pricing=pricing,
        )

    if operator_priced:
        fold(_OperatorConfirmed(other_side=recorded is not None and shape != recorded))
        return None
    assert touch is not None  # answered _TOUCH_NEEDED above otherwise
    try:
        # Priced for the send instant: the pricing seam judges the session
        # at ``send_arrival_ms(now_ms)``, as the verdict above did.
        priced = touch.price(side=reducing_side, symbol=symbol, quantity=remaining_qty, now_ms=now_ms)
    except ProgramLegRefused as exc:
        fold(_Unpriced(exc.refusal))
        return None
    resent = (
        _SendableLeg(regular_session_shape(reducing_side), None, clerk_price=None)
        if priced is None
        else _SendableLeg(priced.shape, priced.valid_until_ms, clerk_price=priced)
    )
    logger.warning(
        "an EXIT's recorded leg could no longer be sent; re-priced it for the session open now",
        extra={
            "action": "exit_leg_repriced_at_send",
            "account_id": repo.account_id,
            "effect_operation_id": effect_operation_id,
            "verdict": verdict,
            "order_type": resent.shape.order_type.value,
            "limit_price": resent.shape.limit_price,
            "quote_spread_bps": touch.quote_spread_bps,
        },
    )
    return resent


def _created_leg_may_be_sent(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    created: ExitReducingOrderCreatedFacts,
    order_ref: str,
    pricing: RecoveryPricing,
) -> bool:
    """The send-time rule at (re)submission: ``True`` only to send the created order now.

    Nothing is sent to reduce a position that is already flat, in the session
    or out of it: a resubmission after an outage or a restart would otherwise
    sell into a flat — or short — position, so the EXIT proves attributed-flat
    instead. The proof cites the reducing order being judged: a lost submit
    left that exact identity's outcome unknown, and only proof recorded
    against it closes that episode (#2440 review). The order already carries
    its client identity, so its leg is never re-priced here: it is replayed
    exactly or not at all. No longer sendable, it is not sent — the EXIT
    folds releasably for the operator.
    """
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    remaining_qty = repo.position(effect.strategy_instance_id, created.symbol)
    if not position_quantity_is_nonzero(remaining_qty):
        _fold_attributed_flat(repo, effect_operation_id, order_ref)
        return False
    verdict = reducing_leg_verdict(
        extended_hours=created.extended_hours,
        valid_until_ms=_created_leg_valid_until_ms(repo, effect_operation_id, created),
        now_ms=repo.clock(),
    )
    if verdict == "send":
        return True
    _fold_unsendable_leg(
        repo,
        effect_operation_id=effect_operation_id,
        order_ref=order_ref,
        symbol=created.symbol,
        remaining_qty=remaining_qty,
        verdict=verdict,
        # Who priced the order being judged: the Clerk's own record when it
        # priced the leg at creation, otherwise the acceptance's.
        not_repriced=_Created(
            priced_by=(
                created.priced_by
                if created.priced_by is not None
                else _accepted_facts(repo, effect_operation_id).reducing_priced_by
            )
        ),
        pricing=pricing,
    )
    return False


def _created_leg_valid_until_ms(
    repo: ClerkSqliteRepository,
    effect_operation_id: str,
    created: ExitReducingOrderCreatedFacts,
) -> int | None:
    """Until when a created reducing leg may be sent: an extended-hours limit's bound, else ``None``.

    The order's own recorded bound, or — for an order created before it
    carried one (#2440) — the bound its EXIT's acceptance recorded.
    """
    if not created.extended_hours:
        return None
    if created.valid_until_ms is not None:
        return created.valid_until_ms
    return _accepted_facts(repo, effect_operation_id).reducing_valid_until_ms


def _operator_priced(facts: ExitAcceptedFacts) -> bool:
    """Did an operator confirm this EXIT's recorded price (#2007)?

    Read from the acceptance alone, whatever shape side reconciliation later
    makes of it. A confirmed quantity comes with every recovery price; only
    the Clerk's own stamps ``priced_by`` (#2229). A deciding program confirms
    no quantity.
    """
    return facts.reducing_confirmed_quantity is not None and facts.reducing_priced_by is None


@dataclass(frozen=True)
class _Unpriced:
    """A price for now was tried and refused; ``refusal`` says why.

    Only a leg nobody confirmed is re-priced: a deciding program's, a market
    reduction, or a limit the Clerk priced itself for an automatic re-drive.
    """

    refusal: LegRefusal
    cause: ClassVar[str] = "unpriced"


@dataclass(frozen=True)
class _OperatorConfirmed:
    """An operator confirmed the price, and the Clerk never replaces it (#2007).

    ``other_side``: the limit was priced for the side the position no longer
    needs, so side reconciliation made it a market leg, which is not sent
    outside the regular session.
    """

    other_side: bool
    cause: ClassVar[str] = "confirmed"


@dataclass(frozen=True)
class _Created:
    """The order already carries its client identity: resubmitted as created or not at all.

    ``priced_by`` is who priced that order: ``"clerk"``, or ``None`` for the
    price its EXIT's acceptance recorded (an operator's, or a deciding
    program's).
    """

    priced_by: str | None
    cause: ClassVar[str] = "created"


type _NotRepriced = _Unpriced | _OperatorConfirmed | _Created
"""Why an unsendable leg was not replaced by one priced for now: the fact its fold's copy is chosen from."""


def _fold_unsendable_leg(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    order_ref: str,
    symbol: str,
    remaining_qty: float,
    verdict: ReducingLegVerdict,
    not_repriced: _NotRepriced,
    pricing: RecoveryPricing,
) -> None:
    """Fold an EXIT whose leg can no longer be sent, releasably, through ``EXIT_NOT_FLAT``.

    Nothing is queued for a later session: the entry is freed, the episode is
    what the operator sees on the bot page and in the lane's attention bell,
    and it is what the stuck-EXIT watchdog re-drives — pricing a limit itself
    in an extended session (#2229), at the time the episode carries (owner
    decision 2026-09-25: the notice says when the sell will be tried). The
    copy is chosen from ``not_repriced`` alone, so a trader is never told a
    price was confirmed that nobody confirmed.
    """
    logger.info(
        "an EXIT's reducing leg could not be sent in its session; folded releasably",
        extra={
            "action": "exit_leg_unsendable_folded",
            "account_id": repo.account_id,
            "effect_operation_id": effect_operation_id,
            "verdict": verdict,
            "cause": not_repriced.cause,
            "reason_code": (
                not_repriced.refusal.reason_code if isinstance(not_repriced, _Unpriced) else None
            ),
        },
    )
    why_not_repriced = _why_not_repriced(not_repriced)
    held = f"{remaining_qty:g} {symbol} remains attributed to this strategy"

    def fold(*, summary_code: str, reason: str, headline: str, explanation: str, next_step: str) -> None:
        _fold_exit_not_flat(
            repo,
            effect_operation_id=effect_operation_id,
            order_ref=order_ref,
            symbol=symbol,
            attributed_qty=remaining_qty,
            summary_code=summary_code,
            reason=reason,
            headline=headline,
            explanation=f"{explanation} {why_not_repriced}".rstrip(),
            next_step=next_step,
            pricing=pricing,
        )

    if not _is_recovery_exit(repo, effect_operation_id):
        leg = "market order" if verdict == "wait" else "limit"
        fold(
            summary_code=PROGRAM_EXIT_SESSION_ENDED.reason_code,
            reason=PROGRAM_EXIT_SESSION_ENDED.explanation,
            headline="An exit could not be sent after its session ended; the position is still open",
            explanation=(
                f"{remaining_qty:g} {symbol} is still held: this exit's {leg} could not go "
                "out after the session it was shaped for ended, and nothing is queued "
                "for the next open."
            ),
            next_step=PROGRAM_EXIT_SESSION_ENDED.next_step,
        )
        return

    def fold_expired_limit(subject: str, reason: str) -> None:
        fold(
            summary_code=RECOVERY_LIMIT_SESSION_ENDED.reason_code,
            reason=reason,
            headline="A recovery flatten limit expired unsent",
            explanation=f"{subject} for {symbol} was not sent before its session ended; {held}.",
            next_step=RECOVERY_LIMIT_SESSION_ENDED.next_step,
        )

    match not_repriced:
        case _OperatorConfirmed(other_side=True):
            fold(
                summary_code=RECOVERY_MARKET_WAIT_ENDED.reason_code,
                reason=(
                    "The confirmed limit was priced for the other side of the position; outside "
                    "the regular session the market reduction it became is not sent."
                ),
                headline="A confirmed flatten limit no longer fits the position",
                explanation=(
                    f"The limit confirmed for {symbol} was priced for the other side of the "
                    "position, and outside the regular session the market reduction it became "
                    f"is not sent; {held}, and nothing is queued for the next open."
                ),
                next_step=RECOVERY_LIMIT_QUANTITY_CHANGED.next_step,
            )
        case _ if verdict == "wait":
            fold(
                summary_code=RECOVERY_MARKET_WAIT_ENDED.reason_code,
                reason=RECOVERY_MARKET_WAIT_ENDED.explanation,
                headline="An unpriced recovery reduction waited past the regular session",
                explanation=(
                    f"The regular session ended while {remaining_qty:g} {symbol} waited "
                    "under a market reduction nothing had priced; it is not queued to "
                    "the next open."
                ),
                next_step=RECOVERY_MARKET_WAIT_ENDED.next_step,
            )
        case _Created(priced_by="clerk") | _Unpriced():
            # A recovery limit re-priced, and refused, was one nobody confirmed:
            # the Clerk's own automatic re-drive price.
            fold_expired_limit(
                "The Clerk-priced limit",
                "The session this limit was priced in ended before it could be sent; a "
                "price is never carried into another session.",
            )
        case _OperatorConfirmed() | _Created():
            fold_expired_limit("The limit confirmed", RECOVERY_LIMIT_SESSION_ENDED.explanation)


def _why_not_repriced(not_repriced: _NotRepriced) -> str:
    """The sentence an unsendable leg's episode ends with: why nothing priced for now replaced it.

    Empty where the rest of the copy already says it (an operator's price is
    never replaced). A refusal is the pricing seam's own words, which are the
    truth at the send instant (``recovery_reduction._tradeable_state``).
    """
    match not_repriced:
        case _Created():
            return (
                "Its order was already created, and a created order is sent as it was "
                "created or not at all."
            )
        case _OperatorConfirmed():
            return ""
        case _Unpriced(refusal):
            return refusal.explanation


def _confirmed_quantity_still_holds(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    order_ref: str,
    symbol: str,
    remaining_qty: float,
    pricing: RecoveryPricing,
) -> bool:
    """``True`` unless the reduction is no longer the one the operator priced (#2007).

    An operator confirms a price for a quantity they can see. Cancellation
    resolves the real reducing quantity several steps later, and a late entry
    fill in between would otherwise send their price for a position they never
    reviewed. Their confirmation covers exactly what it was given, so a
    different quantity fails the EXIT instead — loudly, with the entry left
    free to price again.
    """
    facts = _accepted_facts(repo, effect_operation_id)
    confirmed = facts.reducing_confirmed_quantity
    if confirmed is None or confirmed == abs(remaining_qty):
        return True
    if facts.reducing_priced_by == "clerk":
        # PR #2230 review, blocker 1: nobody confirmed this price — the
        # Clerk computed it for an automatic re-drive. The fold is the same
        # custody fact, but the copy asks no operator for anything: the next
        # automatic re-drive prices the new quantity afresh.
        _fold_exit_not_flat(
            repo,
            effect_operation_id=effect_operation_id,
            order_ref=order_ref,
            symbol=symbol,
            attributed_qty=remaining_qty,
            summary_code=RECOVERY_LIMIT_QUANTITY_CHANGED.reason_code,
            reason=RECOVERY_LIMIT_QUANTITY_CHANGED.explanation,
            headline="The flatten's quantity changed after the Clerk priced it",
            explanation=(
                f"A price the Clerk computed would have reduced {confirmed:g} {symbol}, "
                f"but {abs(remaining_qty):g} is attributed now, so it was never sent."
            ),
            next_step=(
                "No action needed: the next automatic re-drive prices the new "
                "quantity afresh, or the operator can flatten it themselves."
            ),
            pricing=pricing,
        )
        return False
    _fold_exit_not_flat(
        repo,
        effect_operation_id=effect_operation_id,
        order_ref=order_ref,
        symbol=symbol,
        attributed_qty=remaining_qty,
        summary_code=RECOVERY_LIMIT_QUANTITY_CHANGED.reason_code,
        reason=RECOVERY_LIMIT_QUANTITY_CHANGED.explanation,
        headline="The flatten's quantity changed after its price was confirmed",
        explanation=(
            f"A price was confirmed to reduce {confirmed:g} {symbol}, but "
            f"{abs(remaining_qty):g} is attributed now; the confirmation does not cover it."
        ),
        next_step=RECOVERY_LIMIT_QUANTITY_CHANGED.next_step,
        pricing=pricing,
    )
    return False


def _single_entry_symbol(repo: ClerkSqliteRepository, entries: list[OrderResource]) -> str:
    symbols = {entry_order_symbol(repo, order.order_ref) for order in entries}
    if len(symbols) != 1:
        raise AssertionError("one EXIT may only link entry orders for one symbol")
    return next(iter(symbols))


async def _prove_entry_set_terminal(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    entries: list[OrderResource],
    reducing_exists: bool,
    broker: ClaimedBrokerIO,
    run: OffLoop,
) -> bool:
    for entry in entries:
        if reducing_exists and _is_terminal(entry.broker_state):
            continue
        if not await _cancel_and_prove_entry(
            repo,
            effect_operation_id=effect_operation_id,
            entry=entry,
            broker=broker,
            run=run,
        ):
            return False
    return True


async def _cancel_and_prove_entry(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    entry: OrderResource,
    broker: ClaimedBrokerIO,
    run: OffLoop,
) -> bool:
    if await run(
        lambda: _prove_never_accepted_durably(
            repo, effect_operation_id=effect_operation_id, entry=entry
        )
    ):
        return True
    cancel_error: BrokerError | None = None
    if not _is_terminal(entry.broker_state) and entry.broker_order_id is not None:
        await run(
            lambda: _append_order_phase_once(
                repo, effect_operation_id, entry, "ORDER_CANCEL_REQUESTED"
            )
        )
        try:
            # Reissuing cancel for the same broker identity is a retry of the
            # durable phase, not a second intent. This makes a lost cancel
            # response recoverable when the exact poll still shows working.
            await broker.cancel(entry.broker_order_id, order_ref=entry.order_ref)
        except BrokerUnavailable as exc:
            unavailable_why = str(exc)
            await run(
                lambda: fold_uncertain(
                    repo,
                    effect_operation_id=effect_operation_id,
                    order_ref=entry.order_ref,
                    why=unavailable_why,
                    transition_kind="ORDER_CANCEL_UNCERTAIN",
                )
            )
            return False
        except BrokerError as exc:
            # The exact lookup below, not the cancel response, proves state.
            cancel_error = exc

    observed = await broker.observe_exact(entry.client_order_id)
    if await run(
        lambda: _prove_never_accepted_if_absent(
            repo, effect_operation_id=effect_operation_id, entry=entry, observed=observed
        )
    ):
        return True
    if isinstance(observed, BrokerError) or observed is None:

        def _fold_cancel_uncertain() -> None:
            fold_uncertain(
                repo,
                effect_operation_id=effect_operation_id,
                order_ref=entry.order_ref,
                why=(
                    str(observed)
                    if isinstance(observed, BrokerError)
                    else str(cancel_error)
                    if cancel_error is not None
                    else "No exact order evidence yet."
                ),
                transition_kind="ORDER_CANCEL_UNCERTAIN",
            )

        await run(_fold_cancel_uncertain)
        return False

    def _fold_observed_entry() -> bool:
        fold_order_evidence(
            repo,
            effect_operation_id=effect_operation_id,
            order=observed,
            simulated_authority=trade_port_folds_simulated_evidence(broker.trade),
        )
        refreshed = repo.order(entry.order_ref)
        assert refreshed is not None
        if not _is_terminal(refreshed.broker_state):
            return False
        _append_order_phase_once(repo, effect_operation_id, refreshed, "ENTRY_TERMINAL_CONFIRMED")
        return True

    return await run(_fold_observed_entry)


def _prove_never_accepted_durably(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    entry: OrderResource,
) -> bool:
    """Is this entry already provably never-accepted, without asking the broker?"""
    if not entry_never_accepted_durably(repo, entry):
        return False
    _prove_never_accepted(
        repo,
        effect_operation_id=effect_operation_id,
        entry=entry,
        why="The owning ENTER voided this exact order as definitively absent at the broker.",
    )
    return True


def _prove_never_accepted_if_absent(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    entry: OrderResource,
    observed: BrokerOrder | BrokerError | None,
) -> bool:
    """Did this pass's exact lookup prove the entry never reached the broker?

    Only a definitively-absent answer counts — a lost lookup (a
    ``BrokerError``) says nothing about the order — and absence itself is
    only terminal under :func:`order_never_reached_broker`.
    """
    if observed is not None or not order_never_reached_broker(repo, entry):
        return False
    _prove_never_accepted(
        repo,
        effect_operation_id=effect_operation_id,
        entry=entry,
        why=(
            "The exact broker lookup is definitively absent past the "
            "submit-absence grace window, and the order carries no broker identity."
        ),
    )
    return True


def _prove_never_accepted(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    entry: OrderResource,
    why: str,
) -> None:
    """Close the entry's own ENTER, then confirm its end state to this EXIT.

    Both halves matter. The unknown-outcome episode is keyed by
    ``(effect_operation_id, order_ref)``, so proof recorded only against the
    EXIT would leave the ENTER's identity — and the outstanding intent the
    admission gate counts — open after the EXIT finished. Voiding the ENTER
    uses the same producer the submit resolver uses, so an EXIT that reaches
    the proof first writes exactly the evidence a later sweep would have.

    The EXIT-side confirmation is appended once per order: it is what
    :func:`entry_never_accepted_durably` reads on every later pass, so a
    repeated reconciliation cannot re-append it.
    """
    owner = repo.effect_operation(entry.effect_operation_id)
    if owner is not None and owner.state not in ("succeeded", "failed", "rejected"):
        fold_submit_absence_void(
            repo,
            effect_operation_id=entry.effect_operation_id,
            order_ref=entry.order_ref,
        )
    if repo.has_order_transition(order_ref=entry.order_ref, transition_kind="ENTRY_NEVER_ACCEPTED"):
        return
    fold_entry_never_accepted(
        repo,
        effect_operation_id=effect_operation_id,
        order_ref=entry.order_ref,
        why=why,
    )


async def _refresh_terminal_entries(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    entries: list[OrderResource],
    broker: ClaimedBrokerIO,
    run: OffLoop,
) -> bool:
    for entry in entries:
        # Nothing to refresh for an order the broker never accepted: it holds
        # no exposure and cannot change state.
        if await run(
            lambda entry=entry: _prove_never_accepted_durably(
                repo, effect_operation_id=effect_operation_id, entry=entry
            )
        ):
            continue
        observed = await broker.observe_exact(entry.client_order_id)
        if await run(
            lambda entry=entry, observed=observed: _prove_never_accepted_if_absent(
                repo, effect_operation_id=effect_operation_id, entry=entry, observed=observed
            )
        ):
            continue
        if isinstance(observed, BrokerError) or observed is None:
            await run(
                lambda entry=entry, observed=observed: fold_uncertain(
                    repo,
                    effect_operation_id=effect_operation_id,
                    order_ref=entry.order_ref,
                    why=(
                        str(observed)
                        if isinstance(observed, BrokerError)
                        else "Terminal entry evidence could not be refreshed."
                    ),
                    transition_kind="ORDER_CANCEL_UNCERTAIN",
                )
            )
            return False

        def _fold_refreshed_entry(entry: OrderResource = entry, observed: BrokerOrder | BrokerError | None = observed) -> bool:
            fold_order_evidence(
                repo,
                effect_operation_id=effect_operation_id,
                order=observed,
                simulated_authority=trade_port_folds_simulated_evidence(broker.trade),
            )
            current = repo.order(entry.order_ref)
            assert current is not None
            return _is_terminal(current.broker_state)

        if not await run(_fold_refreshed_entry):
            return False
    return True


def _accepted_facts(repo: ClerkSqliteRepository, effect_operation_id: str) -> ExitAcceptedFacts:
    """This EXIT's own ``EXIT_ACCEPTED`` facts; every EXIT has exactly one."""
    acceptance = repo.first_effect_transition(
        effect_operation_id=effect_operation_id,
        transition_kind="EXIT_ACCEPTED",
    )
    if acceptance is None:
        raise AssertionError(f"EXIT effect {effect_operation_id!r} has no acceptance transition")
    return ExitAcceptedFacts.from_facts_json(acceptance["facts_json"])


def _resolved_reducing_shape(recorded: LegShape | None, *, reducing_side: OrderSide) -> LegShape:
    """The leg this EXIT's reduction is created with: its recorded shape, side-reconciled.

    ``recorded`` is the shape this EXIT's own acceptance recorded, never one
    its driver supplies, so a reduction created on a later pass — the 15 s
    reconciliation sweep, the stuck-EXIT watchdog, restart recovery — carries
    the deciding program's (or the operator's confirmed) shape; an EXIT that
    recorded none gets the regular-session leg (ruling R5).

    The shape was priced for the side its author expected to reduce.
    Cancellation can resolve to the other one; a limit priced for the wrong
    side would be unmarketable, so the regular-session leg — which is always
    executable — takes over. This is the one place a shape can meet a side
    its author did not expect, so it holds the only side reconciliation in the
    codebase (ruling R11). The result still passes the send-time rule
    (:func:`_leg_to_create`) before it is created.
    """
    if recorded is None or recorded.side is not reducing_side:
        return regular_session_shape(reducing_side)
    return recorded


def _create_reducing_order(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    symbol: str,
    quantity: float,
    leg: _SendableLeg,
) -> OrderResource:
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    order_ref = build_order_ref(
        build_bot_order_namespace(effect.strategy_instance_id),
        _deterministic_intent_id(effect_operation_id),
    )
    shape = leg.shape
    clerk_price = leg.clerk_price
    quote = None if clerk_price is None else clerk_price.reference_quote
    facts = ExitReducingOrderCreatedFacts(
        symbol=symbol,
        side=shape.side.value.upper(),
        quantity=abs(quantity),
        order_type=shape.order_type.value,
        time_in_force=shape.time_in_force.value,
        limit_price=shape.limit_price,
        extended_hours=shape.extended_hours,
        valid_until_ms=leg.valid_until_ms,
        priced_by=None if clerk_price is None else clerk_price.priced_by,
        reference_bid=None if quote is None else quote.bid,
        reference_ask=None if quote is None else quote.ask,
        reference_quote_observed_at_ms=None if quote is None else quote.observed_at_ms,
    )
    repo.append_transition(
        TransitionInput(
            strategy_instance_id=effect.strategy_instance_id,
            run_id=effect.run_id,
            command_id=effect.command_id,
            effect_operation_id=effect_operation_id,
            order_ref=order_ref,
            transition_kind="EXIT_REDUCING_ORDER_CREATED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="in_progress",
            clerk_observed_at_ms=repo.clock(),
            summary_code="EXIT_REDUCING_ORDER_CREATED",
            facts_json=facts.to_facts_json(),
        )
    )
    created = repo.order(order_ref)
    assert created is not None
    return created


async def _refresh_or_resume_reducing_order(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    reducing: OrderResource,
    broker: ClaimedBrokerIO,
    pricing: RecoveryPricing,
    run: OffLoop,
) -> None:
    observed = await broker.observe_exact(reducing.client_order_id)
    if isinstance(observed, BrokerError):
        await run(
            lambda: fold_uncertain(
                repo,
                effect_operation_id=effect_operation_id,
                order_ref=reducing.order_ref,
                why=str(observed),
            )
        )
        return
    if observed is not None:
        await run(
            lambda: _fold_observed_reducing_order(
                repo,
                effect_operation_id=effect_operation_id,
                reducing=reducing,
                observed=observed,
                simulated_authority=trade_port_folds_simulated_evidence(broker.trade),
            )
        )
        refreshed, accepted, created = await run(lambda: (
            repo.order(reducing.order_ref), _accepted_facts(repo, effect_operation_id),
            _reducing_order_facts(repo, reducing.order_ref),
        ))
        assert refreshed is not None
        await cancel_at_regular_open(
            repo, broker=broker, order=refreshed, accepted=accepted, created=created, run=run,
        )
        return

    def _absence_folds_uncertain() -> bool:
        if reducing.broker_order_id is not None or not _absence_grace_elapsed(
            repo, reducing.order_ref
        ):
            fold_uncertain(
                repo,
                effect_operation_id=effect_operation_id,
                order_ref=reducing.order_ref,
                why="No exact reducing-order evidence yet; retaining custody.",
            )
            return True
        return False

    if await run(_absence_folds_uncertain):
        return
    # A reducing order whose submission was uncertain is only ever resumed —
    # or released by the wait fold below it — on conclusive identity evidence
    # (PR #2230 review): the request may have reached Alpaca, and a release
    # without proof would let a fresh EXIT mint a *different* client id while
    # the original might still execute, over-reducing the account.
    # ``order_never_reached_broker`` is the one predicate that reads an absent
    # lookup as an answer: no broker identity, no acknowledgement, no recorded
    # fill, and the R4 grace closed. Anything less retains custody as an
    # unknown, exactly as before the fold existed.
    def _unproven_absence_folds_uncertain() -> bool:
        if not order_never_reached_broker(repo, reducing):
            fold_uncertain(
                repo,
                effect_operation_id=effect_operation_id,
                order_ref=reducing.order_ref,
                why=(
                    "The reducing order's broker identity is not conclusively "
                    "absent; retaining custody rather than resuming or releasing it."
                ),
            )
            return True
        return False

    if await run(_unproven_absence_folds_uncertain):
        return
    await _submit_reducing_order(
        repo,
        effect_operation_id=effect_operation_id,
        reducing=reducing,
        broker=broker,
        pricing=pricing,
        run=run,
    )


async def _submit_reducing_order(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    reducing: OrderResource,
    broker: ClaimedBrokerIO,
    pricing: RecoveryPricing,
    run: OffLoop,
) -> None:
    created = await run(lambda: _reducing_order_facts(repo, reducing.order_ref))
    now_ms = repo.clock()
    hold = reduction_market_hold(now_ms=now_ms, fact=pricing.read_liveness(created.symbol, now_ms))
    if hold is not None:
        await run(lambda: _fold_market_hold(
            repo, effect_operation_id, reducing.order_ref, created.symbol, hold, pricing,
        ))
        return

    def _prepare_submit() -> BrokerOrderLeg | None:
        facts = _reducing_order_facts(repo, reducing.order_ref)
        leg = BrokerOrderLeg(
            symbol=facts.symbol,
            side=facts.side.lower(),
            quantity=facts.quantity,
            order_type=facts.order_type,
            time_in_force=facts.time_in_force,
            limit_price=facts.limit_price,
            extended_hours=facts.extended_hours,
        )
        # A resubmission after an outage or a restart replays the leg created
        # earlier; the clock may have moved past where it could be sent.
        if not _created_leg_may_be_sent(
            repo,
            effect_operation_id=effect_operation_id,
            created=facts,
            order_ref=reducing.order_ref,
            pricing=pricing,
        ):
            return None
        if _is_recovery_exit(repo, effect_operation_id) and not broker.bind_latest_recovery_bar(
            reducing.client_order_id,
            symbol=facts.symbol,
        ):
            _fold_submit_refused(
                repo,
                effect_operation_id=effect_operation_id,
                reducing=reducing,
                why="Shadow recovery has no retained source bar in its send session; no order was sent.",
                pricing=pricing,
            )
            return None
        _append_order_phase(repo, effect_operation_id, reducing, "ORDER_SUBMIT_REQUESTED")
        return leg

    leg = await run(_prepare_submit)
    if leg is None:
        return
    try:
        observed = await broker.submit(leg, client_order_id=reducing.client_order_id)
    except BrokerUnavailable as exc:
        unavailable_why = str(exc)
        await run(
            lambda: fold_uncertain(
                repo,
                effect_operation_id=effect_operation_id,
                order_ref=reducing.order_ref,
                why=unavailable_why,
            )
        )
        return
    except BrokerError as exc:
        failed_why = str(exc)
        await run(
            lambda: _fold_submit_refused(
                repo,
                effect_operation_id=effect_operation_id,
                reducing=reducing,
                why=failed_why,
                pricing=pricing,
            )
        )
        return
    if observed.client_order_id != reducing.client_order_id:
        await run(
            lambda: fold_uncertain(
                repo,
                effect_operation_id=effect_operation_id,
                order_ref=reducing.order_ref,
                why=(
                    f"broker returned client_order_id={observed.client_order_id!r}, expected {reducing.client_order_id!r}"
                ),
            )
        )
        return
    await run(
        lambda: fold_order_submission_response(
            repo,
            effect_operation_id=effect_operation_id,
            order=observed,
            trade=broker.trade,
        )
    )


def _fold_market_hold(
    repo: ClerkSqliteRepository, effect_operation_id: str, order_ref: str,
    symbol: str, hold: LegRefusal, pricing: RecoveryPricing,
) -> None:
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    _fold_exit_not_flat(
        repo, effect_operation_id=effect_operation_id, order_ref=order_ref,
        symbol=symbol, attributed_qty=repo.position(effect.strategy_instance_id, symbol),
        summary_code=hold.reason_code, reason=hold.explanation,
        headline="Exit on hold; the position is still open", explanation=hold.explanation,
        next_step=hold.next_step, pricing=pricing,
    )
    episode = repo.active_uncertainty(
        scope="CUSTODY_SUBJECT", reason_code=EXIT_NOT_FLAT_REASON_CODE,
        strategy_instance_id=effect.strategy_instance_id,
    )
    assert episode is not None
    observe_exit_recovery(
        repo, strategy_instance_id=effect.strategy_instance_id,
        uncertainty_id=episode["uncertainty_id"], outcome="hold", reason_code=hold.reason_code,
    )


def _fold_submit_refused(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    reducing: OrderResource,
    why: str,
    pricing: RecoveryPricing,
) -> None:
    """The broker refused the reducing order outright (a 4xx): fail the EXIT, and tell the operator.

    The refusal is definitive — nothing reached the book — so the EXIT fails
    releasably as before. While exposure is still held that is the
    ``EXIT_NOT_FLAT`` episode (#2440 review): a bare ``ORDER_SUBMIT_FAILED``
    raised no notice and no bell, and left the position open silently.
    """
    created = _reducing_order_facts(repo, reducing.order_ref)
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    held = repo.position(effect.strategy_instance_id, created.symbol)
    reason = "The reducing order did not reach the broker."
    if not position_quantity_is_nonzero(held):
        fold_failed(
            repo,
            effect_operation_id=effect_operation_id,
            order_ref=reducing.order_ref,
            summary_code="ORDER_SUBMIT_FAILED",
            reason=reason,
            why=why,
        )
        return
    _fold_exit_not_flat(
        repo,
        effect_operation_id=effect_operation_id,
        order_ref=reducing.order_ref,
        symbol=created.symbol,
        attributed_qty=held,
        summary_code="ORDER_SUBMIT_FAILED",
        reason=reason,
        why=why,
        headline="The broker refused this exit's order; the position is still open",
        explanation=(
            f"The broker refused the order to {created.side.lower()} {created.quantity:g} "
            f"{created.symbol}, so nothing was sent; {held:g} {created.symbol} is still "
            "held. The broker's reason is on the order's evidence."
        ),
        next_step=_REDRIVE_NEXT_STEP,
        pricing=pricing,
    )


def priced_reduction_reference_price(repo: ClerkSqliteRepository, order_ref: str) -> float | None:
    """The bid (sell) or ask (cover) a priced reducing leg's fills are measured from.

    ``None`` unless ``order_ref`` is a reducing order priced against a live
    quote: a leg the Clerk priced as the reduction was created (#2440), whose
    own facts record that quote, or the recorded limit of an EXIT whose
    acceptance recorded one — an operator's confirmed flatten (#2007) or the
    watchdog's Clerk-priced re-drive (#2229). Every other fill has no quoted
    price to have slipped from.
    """
    order = repo.order(order_ref)
    if order is None or order.role != "REDUCING":
        return None
    created = _reducing_order_facts(repo, order_ref)
    if created.priced_by is not None:
        return reduction_touch(
            OrderSide(created.side.lower()), bid=created.reference_bid, ask=created.reference_ask
        )
    facts = _accepted_facts(repo, order.effect_operation_id)
    confirmed = facts.reducing_shape()
    if confirmed is None:
        return None
    # The leg that actually went out is not always the confirmed one: side
    # reconciliation (R11) replaces a limit priced for the wrong side with a
    # regular-session market leg. That leg was never priced from this quote,
    # so it has no reference to have slipped from (Codex review 2026-09-19).
    if (
        created.side.lower() != confirmed.side.value.lower()
        or created.limit_price != confirmed.limit_price
        or created.extended_hours != confirmed.extended_hours
    ):
        return None
    return reduction_touch(confirmed.side, bid=facts.reference_bid, ask=facts.reference_ask)


def _is_recovery_exit(repo: ClerkSqliteRepository, effect_operation_id: str) -> bool:
    decision_id = _accepted_facts(repo, effect_operation_id).decision_id
    return decision_id.startswith(_RECOVERY_DECISION_PREFIXES)


def _append_order_phase(
    repo: ClerkSqliteRepository,
    effect_operation_id: str,
    order: OrderResource,
    transition_kind: str,
) -> None:
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    repo.append_transition(
        TransitionInput(
            strategy_instance_id=effect.strategy_instance_id,
            run_id=effect.run_id,
            command_id=effect.command_id,
            effect_operation_id=effect_operation_id,
            order_ref=order.order_ref,
            broker_order_id=order.broker_order_id,
            broker_state=order.broker_state,
            transition_kind=transition_kind,
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state=effect.state,
            clerk_observed_at_ms=repo.clock(),
            summary_code=transition_kind,
            facts_json=canonicalize({}),
        )
    )


def _append_order_phase_once(
    repo: ClerkSqliteRepository,
    effect_operation_id: str,
    order: OrderResource,
    transition_kind: str,
) -> None:
    if not repo.has_order_transition(order_ref=order.order_ref, transition_kind=transition_kind):
        _append_order_phase(repo, effect_operation_id, order, transition_kind)


def _fold_attributed_flat(repo: ClerkSqliteRepository, effect_operation_id: str, order_ref: str) -> None:
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    repo.append_transition(
        TransitionInput(
            strategy_instance_id=effect.strategy_instance_id,
            run_id=effect.run_id,
            command_id=effect.command_id,
            effect_operation_id=effect_operation_id,
            order_ref=order_ref,
            transition_kind="EXIT_ATTRIBUTED_FLAT",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=repo.clock(),
            summary_code="STOPPED_AND_ATTRIBUTED_FLAT",
            facts_json=canonicalize({}),
        )
    )
    resolve_exit_not_flat_uncertainty(
        repo,
        strategy_instance_id=effect.strategy_instance_id,
        evidence_refs=(order_ref,),
    )


def _primary_entry_ref(repo: ClerkSqliteRepository, entries: list[OrderResource]) -> str:
    for entry in entries:
        transition = repo.first_order_transition(order_ref=entry.order_ref, transition_kind="EXIT_ACCEPTED")
        if transition is not None:
            return ExitAcceptedFacts.from_facts_json(transition["facts_json"]).entry_order_ref
    return entries[0].order_ref


def _reducing_order_facts(repo: ClerkSqliteRepository, order_ref: str) -> ExitReducingOrderCreatedFacts:
    transition = repo.first_order_transition(order_ref=order_ref, transition_kind="EXIT_REDUCING_ORDER_CREATED")
    if transition is None:
        raise AssertionError(f"no reducing-order creation fact for {order_ref!r}")
    return ExitReducingOrderCreatedFacts.from_facts_json(transition["facts_json"])


def _absence_grace_elapsed(repo: ClerkSqliteRepository, order_ref: str) -> bool:
    # The greatest recorded uncertainty is the anchor when there is one — the
    # maximum, not the last by sequence: ``recorded_at_ms`` is wall time, so a
    # host clock that steps backwards and rebounds can leave the newest row
    # holding an older timestamp, and a grace window must not shorten because
    # of that. SQLite computes it over the index; the former ``max()`` did it
    # after reading the order's whole history in Python (#1942).
    anchor_ms = repo.max_order_transition_recorded_at_ms(
        order_ref=order_ref, transition_kind="ORDER_SUBMIT_UNCERTAIN"
    )
    if anchor_ms is None:
        created = repo.first_order_transition(
            order_ref=order_ref, transition_kind="EXIT_REDUCING_ORDER_CREATED"
        )
        if created is None:
            raise AssertionError(f"no reducing-order creation transition for {order_ref!r}")
        anchor_ms = created["recorded_at_ms"]
    return repo.clock() - anchor_ms >= submit_absence_grace_ms()


def _deterministic_intent_id(effect_operation_id: str) -> str:
    digest = hashlib.sha256(effect_operation_id.encode("utf-8")).digest()[:16]
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _is_terminal(broker_state: str | None) -> bool:
    return broker_state is not None and broker_state.lower() in ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES


def _snapshot(repo: ClerkSqliteRepository, effect_operation_id: str) -> ExitSubmission:
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    command = repo.get_command(effect.command_id)
    assert command is not None
    orders = repo.orders_for_effect_operation(effect_operation_id)
    entries = [order for order in orders if order.role == "ENTRY"]
    reducing = next((order for order in orders if order.role == "REDUCING"), None)
    return ExitSubmission(
        command=command,
        effect_operation_id=effect_operation_id,
        entry_order_ref=_primary_entry_ref(repo, entries),
        reducing_order_ref=reducing.order_ref if reducing is not None else None,
        created=True,
    )


__all__ = ["cancel_and_prove_owned_entry", "priced_reduction_reference_price", "resolve_exit"]
