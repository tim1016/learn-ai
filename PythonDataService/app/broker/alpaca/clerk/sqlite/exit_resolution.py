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
priced for. An EXIT whose leg can no longer go out as recorded is re-priced
from the current instant (``price_automatic_recovery_reduction``: an
extended-hours limit off the live touch in PRE or POST, the market leg inside
the regular session) unless an operator confirmed that price; when nothing
can price it, or its order already has a broker identity, nothing is sent and
the EXIT folds releasably through ``EXIT_NOT_FLAT`` — the operator-visible
episode, never a silent queue.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from typing import NamedTuple

from app.broker.alpaca.clerk.program_leg import (
    PROGRAM_EXIT_SESSION_ENDED,
    LegRefusal,
    LegShape,
    ProgramLegRefused,
    regular_session_shape,
)
from app.broker.alpaca.clerk.recovery_reduction import (
    RECOVERY_LIMIT_QUANTITY_CHANGED,
    RECOVERY_LIMIT_SESSION_ENDED,
    RECOVERY_MARKET_WAIT_ENDED,
    UNPRICEABLE_RECOVERY,
    RecoveryPricing,
    ReducingLegVerdict,
    price_automatic_recovery_reduction,
    quote_spread_bps,
    reducing_leg_verdict,
)
from app.broker.alpaca.clerk.sqlite.claimed_broker_io import ClaimedBrokerIO
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
from app.broker.contract.errors import BrokerError, BrokerUnavailable
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg, OrderSide
from app.broker.contract.ports import BrokerTradePort
from app.engine.live.order_identity import build_bot_order_namespace, build_order_ref

logger = logging.getLogger(__name__)

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
    pricing: RecoveryPricing = UNPRICEABLE_RECOVERY,
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
    is what a leg that can no longer go out as recorded is re-priced from.
    The degraded :data:`UNPRICEABLE_RECOVERY` default prices nothing, so such
    an EXIT folds for the operator instead.

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
    pricing: RecoveryPricing,
) -> tuple[OrderResource | None, ExitSubmission | None]:
    """One atomic admission-to-append run: ``(created, None)``, or
    ``(None, snapshot)`` when a precondition folded the EXIT to an early
    end — attributed-flat, the send-time rule, or a moved quantity.
    """
    remaining_qty = repo.position(state.effect.strategy_instance_id, state.symbol)
    if not position_quantity_is_nonzero(remaining_qty):
        _fold_attributed_flat(repo, effect_operation_id, _primary_entry_ref(repo, state.entries))
        return None, _snapshot(repo, effect_operation_id)
    leg = _leg_to_create(
        repo,
        effect_operation_id=effect_operation_id,
        shape=_resolved_reducing_shape(
            repo,
            effect_operation_id=effect_operation_id,
            reducing_side=OrderSide.SELL if remaining_qty > 0 else OrderSide.BUY,
        ),
        order_ref=_primary_entry_ref(repo, state.entries),
        symbol=state.symbol,
        remaining_qty=remaining_qty,
        pricing=pricing,
    )
    if leg is None:
        return None, _snapshot(repo, effect_operation_id)
    # A confirmed quantity belongs to the price it was confirmed with; a leg
    # re-priced just now was priced for the quantity attributed now.
    if not leg.repriced and not _confirmed_quantity_still_holds(
        repo,
        effect_operation_id=effect_operation_id,
        order_ref=_primary_entry_ref(repo, state.entries),
        symbol=state.symbol,
        remaining_qty=remaining_qty,
    ):
        return None, _snapshot(repo, effect_operation_id)
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
    return (
        _create_reducing_order(
            repo,
            effect_operation_id=effect_operation_id,
            symbol=state.symbol,
            quantity=remaining_qty,
            shape=leg.shape,
            valid_until_ms=leg.valid_until_ms,
        ),
        None,
    )


def _finalize_claimed_exit(
    repo: ClerkSqliteRepository,
    effect_operation_id: str,
    state: _ClaimedState,
    submitted_reducing: OrderResource,
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
        # the operator is told the position is still open, and nothing is
        # queued for the next day.
        headline = "An extended-hours exit ended unfilled; the position is still open"
        explanation = (
            f"The extended-hours limit to {created.side.lower()} {created.quantity:g} "
            f"{state.symbol} at {created.limit_price} ended {refreshed.broker_state} "
            f"without flattening the position; {final_qty:g} {state.symbol} is still held."
        )
        next_step = (
            "Flatten it with a priced limit, or let the automatic re-drive price one "
            "once a session that accepts it is open. Nothing was queued for the next open."
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
        reducing, prepared_snapshot = await run(
            lambda: _prepare_reduction(repo, effect_operation_id, state, pricing)
        )
        if prepared_snapshot is not None:
            return prepared_snapshot
        assert reducing is not None
        await _submit_reducing_order(
            repo,
            effect_operation_id=effect_operation_id,
            reducing=reducing,
            broker=broker,
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
            run=run,
        )
    return await run(
        lambda: _finalize_claimed_exit(repo, effect_operation_id, state, reducing)
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
) -> None:
    """Fail the EXIT and raise the ``EXIT_NOT_FLAT`` episode exposure is still held under.

    The episode is what flags the bot for the operator and what the stuck-EXIT
    watchdog re-drives once the regular session opens.
    """
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    fold_failed(
        repo,
        effect_operation_id=effect_operation_id,
        order_ref=order_ref,
        summary_code=summary_code,
        reason=reason,
        why=f"attributed_qty={attributed_qty} remains for {symbol!r}.",
        transition_kind="EXIT_NOT_FLAT",
    )
    raise_uncertainty(
        repo,
        strategy_instance_id=effect.strategy_instance_id,
        reason_code=EXIT_NOT_FLAT_REASON_CODE,
        headline=headline,
        explanation=explanation,
        operator_impact=(
            "New exposure is paused for this strategy; exact risk reduction "
            "and reconciliation remain available."
        ),
        next_step=next_step,
        evidence_refs=(order_ref,),
        cause_facts=ExitNotFlatCause(
            symbol=symbol.upper(),
            attributed_qty=attributed_qty,
        ).to_mapping(),
        severity="error",
    )


class _SendableLeg(NamedTuple):
    """The leg a reduction is created with now, and until when it may be sent."""

    shape: LegShape
    valid_until_ms: int | None
    # Priced at this instant rather than taken from the EXIT's acceptance.
    repriced: bool


def _leg_to_create(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    shape: LegShape,
    order_ref: str,
    symbol: str,
    remaining_qty: float,
    pricing: RecoveryPricing,
) -> _SendableLeg | None:
    """The send-time rule at creation: the leg to create now, or ``None`` once the EXIT folded.

    ``shape`` is the EXIT's recorded leg, side-reconciled. Sendable now, it is
    kept. Otherwise — a market leg after the regular close, which Alpaca would
    queue for the next open; a limit past the session it was priced for — it
    is re-priced from the current instant, since no broker identity exists yet
    to preserve (#2440, owner decision #2431): an extended-hours limit off the
    live touch in PRE or POST, the market leg inside the regular session. An
    operator's confirmed price is the one exception — it is never replaced by
    a price nobody confirmed (#2007). A refusal to price (no session open, no
    allowance, no live quote, a spread past the cap) folds the EXIT for the
    operator; the watchdog's re-drive keeps trying.
    """
    facts = _accepted_facts(repo, effect_operation_id)
    now_ms = repo.clock()
    valid_until_ms = facts.reducing_valid_until_ms if shape.extended_hours else None
    verdict = reducing_leg_verdict(
        extended_hours=shape.extended_hours, valid_until_ms=valid_until_ms, now_ms=now_ms
    )
    if verdict == "send":
        return _SendableLeg(shape, valid_until_ms, repriced=False)

    def fold(refusal: LegRefusal | None) -> None:
        _fold_unsendable_leg(
            repo,
            effect_operation_id=effect_operation_id,
            order_ref=order_ref,
            symbol=symbol,
            remaining_qty=remaining_qty,
            verdict=verdict,
            refusal=refusal,
        )

    if shape.extended_hours and _operator_priced(facts):
        fold(None)
        return None
    quote = pricing.quote_source(symbol, now_ms)
    try:
        priced = price_automatic_recovery_reduction(
            side=shape.side,
            symbol=symbol,
            quantity=remaining_qty,
            now_ms=now_ms,
            policy=pricing.policy_source(),
            quote=quote,
        )
    except ProgramLegRefused as exc:
        fold(exc.refusal)
        return None
    resent = _SendableLeg(
        regular_session_shape(shape.side) if priced is None else priced.shape,
        None if priced is None else priced.valid_until_ms,
        repriced=True,
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
            "quote_spread_bps": None if quote is None else quote_spread_bps(quote),
        },
    )
    return resent


def _created_leg_may_be_sent(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    created: ExitReducingOrderCreatedFacts,
    order_ref: str,
) -> bool:
    """The send-time rule at (re)submission: ``True`` only to send the created order now.

    The order already carries its client identity, so its leg is never
    re-priced here: a resubmission after an outage or a restart replays it
    exactly or not at all. No longer sendable, it is not sent — the EXIT folds
    releasably for the operator, or proves attributed-flat when nothing is
    left to reduce.
    """
    valid_until_ms = None
    if created.extended_hours:
        valid_until_ms = created.valid_until_ms
        if valid_until_ms is None:
            # Created before the order carried its own bound (#2440).
            valid_until_ms = _accepted_facts(repo, effect_operation_id).reducing_valid_until_ms
    verdict = reducing_leg_verdict(
        extended_hours=created.extended_hours, valid_until_ms=valid_until_ms, now_ms=repo.clock()
    )
    if verdict == "send":
        return True
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    remaining_qty = repo.position(effect.strategy_instance_id, created.symbol)
    if not position_quantity_is_nonzero(remaining_qty):
        _fold_attributed_flat(repo, effect_operation_id, order_ref)
        return False
    _fold_unsendable_leg(
        repo,
        effect_operation_id=effect_operation_id,
        order_ref=order_ref,
        symbol=created.symbol,
        remaining_qty=remaining_qty,
        verdict=verdict,
        refusal=None,
    )
    return False


def _operator_priced(facts: ExitAcceptedFacts) -> bool:
    """Did an operator confirm this EXIT's recorded price (#2007)?

    A confirmed quantity comes with every recovery price; only the Clerk's own
    stamps ``priced_by`` (#2229). A deciding program confirms no quantity.
    """
    return facts.reducing_confirmed_quantity is not None and facts.reducing_priced_by is None


def _fold_unsendable_leg(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    order_ref: str,
    symbol: str,
    remaining_qty: float,
    verdict: ReducingLegVerdict,
    refusal: LegRefusal | None,
) -> None:
    """Fold an EXIT whose leg can no longer be sent, releasably, through ``EXIT_NOT_FLAT``.

    Nothing is queued for a later session: the entry is freed, the episode is
    what the operator sees on the bot page and in the lane's attention bell,
    and it is what the stuck-EXIT watchdog re-drives — pricing a limit itself
    in an extended session (#2229). ``refusal`` is why no current-session
    price could replace the leg, when one was attempted.
    """
    logger.info(
        "an EXIT's reducing leg could not be sent in its session; folded releasably",
        extra={
            "action": "exit_leg_unsendable_folded",
            "account_id": repo.account_id,
            "effect_operation_id": effect_operation_id,
            "verdict": verdict,
            "reason_code": None if refusal is None else refusal.reason_code,
        },
    )
    if not _is_recovery_exit(repo, effect_operation_id):
        leg = "market order" if verdict == "wait" else "limit"
        _fold_exit_not_flat(
            repo,
            effect_operation_id=effect_operation_id,
            order_ref=order_ref,
            symbol=symbol,
            attributed_qty=remaining_qty,
            summary_code=PROGRAM_EXIT_SESSION_ENDED.reason_code,
            reason=PROGRAM_EXIT_SESSION_ENDED.explanation,
            headline="An exit could not be sent after its session ended; the position is still open",
            explanation=(
                f"{remaining_qty:g} {symbol} is still held: this exit's {leg} could not go "
                "out after the session it was shaped for ended, and nothing is queued "
                "for the next open."
                + ("" if refusal is None else f" {refusal.explanation}")
            ),
            next_step=PROGRAM_EXIT_SESSION_ENDED.next_step,
        )
        return
    if verdict == "wait":
        _fold_exit_not_flat(
            repo,
            effect_operation_id=effect_operation_id,
            order_ref=order_ref,
            symbol=symbol,
            attributed_qty=remaining_qty,
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
        return
    clerk_priced = _accepted_facts(repo, effect_operation_id).reducing_priced_by == "clerk"
    subject = "The Clerk-priced limit" if clerk_priced else "The limit confirmed"
    _fold_exit_not_flat(
        repo,
        effect_operation_id=effect_operation_id,
        order_ref=order_ref,
        symbol=symbol,
        attributed_qty=remaining_qty,
        summary_code=RECOVERY_LIMIT_SESSION_ENDED.reason_code,
        reason=RECOVERY_LIMIT_SESSION_ENDED.explanation,
        headline="A recovery flatten limit expired unsent",
        explanation=(
            f"{subject} for {symbol} was not sent before its session ended; "
            f"{remaining_qty:g} {symbol} remains attributed to this strategy."
        ),
        next_step=(
            f"{RECOVERY_LIMIT_SESSION_ENDED.next_step} Automatic re-drives resume "
            "at the next session open."
        ),
    )


def _confirmed_quantity_still_holds(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    order_ref: str,
    symbol: str,
    remaining_qty: float,
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


def _accepted_reducing_shape(
    repo: ClerkSqliteRepository, *, effect_operation_id: str
) -> LegShape | None:
    """The leg shape this EXIT's own acceptance recorded, if any.

    Read here rather than threaded from the caller so a reduction created on
    a later pass — the 15 s reconciliation sweep, the stuck-EXIT watchdog,
    restart recovery — carries the deciding program's (or the operator's
    confirmed) shape instead of silently falling back to a market DAY order
    the vendor queues to the next regular open.
    """
    return _accepted_facts(repo, effect_operation_id).reducing_shape()


def _resolved_reducing_shape(
    repo: ClerkSqliteRepository, *, effect_operation_id: str, reducing_side: OrderSide
) -> LegShape:
    """The leg this EXIT's reduction is created with: its recorded shape, side-reconciled.

    The shape was priced for the side its author expected to reduce.
    Cancellation can resolve to the other one; a limit priced for the wrong
    side would be unmarketable, so the regular-session leg — which is always
    executable — takes over. This is the one place a shape can meet a side
    its author did not expect, so it holds the only side reconciliation in the
    codebase (ruling R11). The result still passes the send-time rule
    (:func:`_leg_to_create`) before it is created.
    """
    shape = _accepted_reducing_shape(repo, effect_operation_id=effect_operation_id)
    resolved = regular_session_shape(reducing_side) if shape is None else shape
    if resolved.side is not reducing_side:
        logger.warning(
            "Reducing leg shape was priced for the other side; submitting a regular-session market leg instead",
            extra={
                "action": "reducing_leg_shape_side_mismatch",
                "effect_operation_id": effect_operation_id,
                "shaped_side": resolved.side.value,
                "reducing_side": reducing_side.value,
            },
        )
        resolved = regular_session_shape(reducing_side)
    return resolved


def _create_reducing_order(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    symbol: str,
    quantity: float,
    shape: LegShape,
    valid_until_ms: int | None,
) -> OrderResource:
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    order_ref = build_order_ref(
        build_bot_order_namespace(effect.strategy_instance_id),
        _deterministic_intent_id(effect_operation_id),
    )
    facts = ExitReducingOrderCreatedFacts(
        symbol=symbol,
        side=shape.side.value.upper(),
        quantity=abs(quantity),
        order_type=shape.order_type.value,
        time_in_force=shape.time_in_force.value,
        limit_price=shape.limit_price,
        extended_hours=shape.extended_hours,
        valid_until_ms=valid_until_ms,
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
            lambda: fold_order_evidence(
                repo,
                effect_operation_id=effect_operation_id,
                order=observed,
                simulated_authority=trade_port_folds_simulated_evidence(broker.trade),
            )
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
        run=run,
    )


async def _submit_reducing_order(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    reducing: OrderResource,
    broker: ClaimedBrokerIO,
    run: OffLoop,
) -> None:
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
        ):
            return None
        if _is_recovery_exit(repo, effect_operation_id) and not broker.bind_latest_recovery_bar(
            reducing.client_order_id,
            symbol=facts.symbol,
        ):
            fold_uncertain(
                repo,
                effect_operation_id=effect_operation_id,
                order_ref=reducing.order_ref,
                why=(
                    "Recovery reduction has no retained source bar in its strategy evidence "
                    "namespace; keeping the order unsubmitted."
                ),
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
            lambda: fold_failed(
                repo,
                effect_operation_id=effect_operation_id,
                order_ref=reducing.order_ref,
                summary_code="ORDER_SUBMIT_FAILED",
                reason="The reducing order did not reach the broker.",
                why=failed_why,
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


def confirmed_flatten_reference_price(repo: ClerkSqliteRepository, order_ref: str) -> float | None:
    """The bid (sell) or ask (cover) an operator-confirmed flatten's fills are measured from.

    ``None`` unless ``order_ref`` is the reducing order of an EXIT that
    recorded a confirmed limit's reference quote (#2007) — every other fill
    has no confirmed price to have slipped from.
    """
    order = repo.order(order_ref)
    if order is None or order.role != "REDUCING":
        return None
    facts = _accepted_facts(repo, order.effect_operation_id)
    confirmed = facts.reducing_shape()
    if confirmed is None:
        return None
    created = _reducing_order_facts(repo, order_ref)
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
    return facts.reference_bid if confirmed.side is OrderSide.SELL else facts.reference_ask


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


__all__ = ["cancel_and_prove_owned_entry", "confirmed_flatten_reference_price", "resolve_exit"]
