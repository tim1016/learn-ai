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

One EXIT is outside the send-time rule: a Dry Run's run-end close
(``DRY_RUN_CLOSE_DECISION_PREFIX``, minted by ``dry_run_close``). It fills in
the simulation at the last price its run saw, not at a market that could hold
it, so it always sends (:func:`_send_verdict`), and only a simulation can bind
its price.

One kind of EXIT is narrower than the rule: one whose acceptance records
``regular_session_only`` -- the sale at a bot's owner-scheduled end
(``SCHEDULED_END_DECISION_PREFIX``, minted by ``scheduled_end``, #2607) and
every watchdog re-drive of it. It goes out only as a market order inside the
regular session; outside it, it holds -- never re-priced as an
extended-hours limit -- and sells at the next open (owner default, grill
2026-09-29: "never after hours").

One kind of EXIT passes a second check as it is sent (#2839): an operator's
Flatten (``RECOVERY_FLATTEN_DECISION_PREFIX``) going to a real broker. Its
order is sized from the Clerk's own record, so just before it is submitted --
first or resumed, by the Flatten itself or by the sweep that carries on a
deferred one -- the Clerk reads the account's open orders, then its
positions, and sends only a sale the account covers (``flatten_cover``).
Otherwise nothing is sent and the EXIT folds as a broker's refusal does. A
resumed send that finds its own order among those open orders is neither sent
again nor refused: custody is retained until the order's exact lookup answers.

A reducing order whose send was lost is sent again, under the same client
order id, once an exact lookup has read it absent a whole submit-absence wait
after its latest send (#2845). Each send made again waits longer than the
last (:func:`resend_wait_ms`, #2854), and they go on only while the order may
still be sent (:func:`_created_leg_may_be_sent`).

An order sent to a real broker is never sent again once the session it was
sent in has ended, and its EXIT does not fail (#2854, owner decision
2026-10-03). The earlier send may have reached the broker with no read
showing it, and a failed EXIT would release the position: the bot's next exit
decision and the sale at a scheduled end send with no check of the account,
and would sell it again under a new client order id. So the EXIT stays
``unknown`` with custody kept (:func:`_hold_past_its_session`): past the
order's own session, on any later day, and for a scheduled-end sale too,
which is not sent again at the next open. The operator is told once, each
pass still looks the order up and folds it if the broker shows it, and an
operator who has looked at Alpaca settles it
(``unknown_order_attestation``). An operator's Flatten that the account does
not cover when it would be sent again keeps custody the same way
(:func:`_hold_uncovered_resend`, #2839). Evidence that the bot is flat still
comes first and ends the EXIT. An order never sent folds as before -- it
fails releasably, or a scheduled-end sale holds for the next open -- and so
does every order of a simulated port, which fills inside the send and
reaches no broker: a Dry Run must never end the day holding.

Three things guard a send made again. Whether
the order ever reached the broker is read again in the run that records the
send (:func:`_resend_still_proven_absent`). No refusal of a send made again
fails the EXIT: an earlier send may have arrived, so custody is retained as an
unknown outcome (:func:`_fold_send_refused`). And an order whose latest send
the broker answered under another client order id is never sent again: the
same client order id cannot protect a second send from an order that may exist
under a different one (:func:`_retain_answered_as_another_order`). In both of
those the broker answered, so the open unknown-outcome notice says what it
answered instead of that a response was lost (#2854). No notice outlasts a
later send whose own answer is lost: after a refusal, and after the account
check held a send, the notice then says a response was lost again
(:func:`_lost_answer_after_a_notice`).
"""

from __future__ import annotations

import base64
import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Literal, NamedTuple, TypedDict

from app.broker.alpaca.clerk.account_money import holdings_text
from app.broker.alpaca.clerk.option_effects import OptionEffectRefusal
from app.broker.alpaca.clerk.program_leg import (
    LegRefusal,
    LegShape,
    ProgramLegPolicy,
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
    reducing_send_verdict,
    reduction_touch,
    send_arrival_ms,
)
from app.broker.alpaca.clerk.sqlite.claimed_broker_io import ClaimedBrokerIO
from app.broker.alpaca.clerk.sqlite.contract_terms import is_option_contract_symbol
from app.broker.alpaca.clerk.sqlite.facts import (
    ExitAcceptedFacts,
    ExitReducingOrderCreatedFacts,
    OrderCancelRequestedFacts,
    OrderOutcomeAttestedFacts,
    OrderSubmitFailedFacts,
    OrderSubmitUncertainFacts,
    UncertaintyRaisedFacts,
    UnknownOutcomeNotice,
)
from app.broker.alpaca.clerk.sqlite.flatten_cover import (
    FLATTEN_COVER_HEADLINES,
    FLATTEN_COVER_NEXT_STEPS,
    account_lists_order,
    flatten_cover_refusal,
    held_text,
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
from app.broker.alpaca.clerk.sqlite.open_replacement import OPEN_REPLACEMENT, replacement_due
from app.broker.alpaca.clerk.sqlite.option_close import (
    captured_positions,
    close_held_options,
    order_reductions,
    recorded_option_close,
)
from app.broker.alpaca.clerk.sqlite.option_entry import MultiLegOptionEntryFacts, option_entry_of_order
from app.broker.alpaca.clerk.sqlite.option_event_books import event_share_order_symbol
from app.broker.alpaca.clerk.sqlite.order_evidence import (
    UNFILLED_TERMINAL_STATES,
    BrokerRefusal,
    broker_refusal,
    entry_never_accepted_durably,
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
from app.broker.alpaca.clerk.sqlite.reads import CANCELLABLE_ENTRY_BROKER_STATES, NONTERMINAL_EFFECT_STATES
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository, OperationClaimError
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    EXIT_NOT_FLAT_REASON_CODE,
    Capability,
    ReductionIntent,
    raise_uncertainty,
    require_capability,
    resolve_exit_not_flat_uncertainty,
    uncertainty_envelope,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXIT_STUCK_REASON_CODE,
    ExitNotFlatCause,
    ExitStuckCause,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_folds import LOST_ANSWER_NOTICE
from app.broker.alpaca.clerk.sqlite.uncertainty_policies import RedriveThenEscalate, reason_age_policy
from app.broker.alpaca.clerk.sqlite.unknown_exit_orders import send_wait_from_ms
from app.broker.alpaca.config import BROKER_ID
from app.broker.contract.errors import BrokerError, BrokerUnavailable
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg, OrderSide
from app.broker.contract.options import OptionOrderRequest, SingleOptionOrderRequest
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort
from app.engine.live.order_identity import build_bot_order_namespace, build_order_ref
from app.options.contract import OptionContract
from app.options.deadlines import past_last_session
from app.schemas.market_liveness import MarketLivenessFact

logger = logging.getLogger(__name__)

_REDRIVE_NEXT_STEP = (
    "Flatten with a priced limit now, or let the Clerk retry after a fresh recovery check."
)
"""What the operator can do about an EXIT that folded with exposure the watchdog re-drives.

A leg that could not be sent, a broker refusal, or an unfilled extended-hours
limit stays visible. The separate observed recovery status explains current
eligibility; the failed order itself promises no future send time."""

_OPTION_CLOSE_RETRIED = "The Clerk tries the close again in regular hours after a fresh recovery check."


def _option_close_not_taken_next_step(repo: ClerkSqliteRepository) -> str:
    """What the operator can do about an option position a refused close left open (ADR 0076).

    Never "flatten with a priced limit": an option position is closed by a
    market order in the regular session, and takes no limit. A Dry Run's
    close and a Live Shadow's are priced from a quote kept for the
    contract, so their Flatten waits for one with something bid. A real
    account's close is a market order the broker fills, and waits for no
    quote of ours.
    """
    if repo.account_id.startswith(("sim:", "shadow:")):
        return (
            f"{_OPTION_CLOSE_RETRIED} Or flatten it yourself once a quote of the contract with something bid "
            "has been kept."
        )
    return f"{_OPTION_CLOSE_RETRIED} Or flatten it yourself from the bot's page, in regular hours."

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

# The recovery decision-id namespaces documented on
# ``exit.accept_recovery_exit`` (minted by ``safe_flatten_execution``,
# ``exit_watchdog`` and ``scheduled_end`` respectively -- the last the sale at
# a bot's owner-scheduled end, #2607, whose acceptance records
# ``regular_session_only``). Named here, the module every namespace
# classifies against, so a rename or another namespace has exactly one
# declaration to update.
RECOVERY_FLATTEN_DECISION_PREFIX = "recovery-flatten-"
EXIT_REDRIVE_DECISION_PREFIX = "exit-redrive-"
SCHEDULED_END_DECISION_PREFIX = "scheduled-end-"
# The Clerk's own scheduled close of an option position (``scheduled_close``,
# ADR 0076): on its contract's expiry day, or before a session's close for a
# structure with a sold leg. Its acceptance records which close and when due.
SCHEDULED_CLOSE_DECISION_PREFIX = "scheduled-close-"
_RECOVERY_DECISION_PREFIXES = (
    RECOVERY_FLATTEN_DECISION_PREFIX,
    EXIT_REDRIVE_DECISION_PREFIX,
    SCHEDULED_END_DECISION_PREFIX,
    SCHEDULED_CLOSE_DECISION_PREFIX,
)
# The panel's flatten-and-stop decides its EXIT under this namespace (it is
# colon-bearing, so the facade stores it encoded). With the safe flatten's,
# the two namespaces an owner's own flatten is recorded under (#2574).
PANEL_FLATTEN_DECISION_PREFIX = "panel-flatten:"
OWNER_FLATTEN_DECISION_PREFIXES = (RECOVERY_FLATTEN_DECISION_PREFIX, PANEL_FLATTEN_DECISION_PREFIX)


def redrive_episode_token(uncertainty_id: str) -> str:
    """The token a stuck-EXIT episode's re-drives are decided under (``redrive_decision_prefix``).

    It names which episode -- and so which stuck EXIT -- a re-drive carries on.
    """
    return hashlib.sha256(uncertainty_id.encode("utf-8")).hexdigest()[:12]


def redrive_decision_prefix(episode_token: str) -> str:
    """Every re-drive of one episode is decided as this prefix and its attempt number, from 1."""
    return f"{EXIT_REDRIVE_DECISION_PREFIX}{episode_token}-"


def redriven_episode_token(decision_id: str) -> str | None:
    """The episode token a re-drive's decision names (``redrive_decision_prefix``), or ``None`` for any other."""
    if not decision_id.startswith(EXIT_REDRIVE_DECISION_PREFIX):
        return None
    token, _, _attempt = decision_id.removeprefix(EXIT_REDRIVE_DECISION_PREFIX).rpartition("-")
    return token or None
#: Why an entry is cancelled when nothing says otherwise: an EXIT, or a stopped
#: run's cleanup, must prove the entry ended before anything else is done.
ENTRY_TERMINAL_PROOF = "EXIT_ENTRY_TERMINAL_PROOF"
#: Why a resting option entry is cancelled: the owner's entry wait ran out (ADR 0076).
ENTRY_WAIT_ENDED = "ENTRY_WAIT_ENDED"
#: Why a resting option entry is cancelled whatever is left of its wait: its Clerk closed (ADR 0076).
ENTRY_CLERK_CLOSED = "ENTRY_CLERK_CLOSED"
#: Why an entry in shares is cancelled while its run is still ACTIVE: its own
#: ENTER ended in failure, and the broker later showed the order working (#2961).
ENTRY_ENTER_FAILED = "ENTRY_ENTER_FAILED"

# A Dry Run's run-end close (``dry_run_close``, owner decision 2026-09-29): the
# simulation closing what an ended run left, at the last price the run saw.
DRY_RUN_CLOSE_DECISION_PREFIX = "dry-run-close-"

#: The hold of an EXIT that sells only inside the regular session, met
#: outside it (``ExitAcceptedFacts.regular_session_only``, #2607).
SCHEDULED_END_WAITS_FOR_OPEN = "SCHEDULED_END_WAITS_FOR_OPEN"


#: The hold of an EXIT that closes an option contract, met outside the regular
#: session (``ExitAcceptedFacts.option_contract``, ADR 0076). Its own code: it
#: is no bot's scheduled end, and must not be told as one.
OPTION_CLOSE_WAITS_FOR_OPEN = "OPTION_CLOSE_WAITS_FOR_OPEN"


def option_close_waits_for_open(now_ms: int) -> LegRefusal:
    """Why an order that closes an option position is held, and when it can go out: the next regular open.

    A hold, not a failure, exactly as a scheduled end's sale is held: the
    EXIT keeps custody and the pass re-drives it until the session opens.

    The words are about this one order. They do not say that nothing closes
    an option position after hours: a Dry Run's simulation closes what an
    ended run left at whatever hour the run ended, as it does for shares
    (``_send_verdict``).
    """
    return LegRefusal(
        reason_code=OPTION_CLOSE_WAITS_FOR_OPEN,
        explanation=(
            "This order closes a position in an option contract, and the market is closed. It waits for the "
            "next regular session and goes out at the open as a market order."
        ),
        next_step="Nothing to do: the Clerk sends it at the next regular open.",
        available_at_ms=next_redrive_at_ms(not_before_ms=now_ms, policy=ProgramLegPolicy.regular_only()),
    )


def option_retry_waits_for_open(now_ms: int) -> LegRefusal:
    """Why the Clerk does not try again, now, to close an option position a failed exit left open.

    What the watchdog records while the market is closed. It speaks of the
    Clerk's own retry only: an ended Dry Run's simulation may still close
    the position before the open, and the words must stay true when it does.
    """
    return LegRefusal(
        reason_code=OPTION_CLOSE_WAITS_FOR_OPEN,
        explanation=(
            "This position is in an option contract, and the market is closed. The Clerk tries its close "
            "again only in regular hours: at the next open, as a market order. A Dry Run's simulation may "
            "close what an ended run left before then, at the newest quote kept for the contract."
        ),
        next_step="Nothing to do: the Clerk tries again at the next regular open.",
        available_at_ms=next_redrive_at_ms(not_before_ms=now_ms, policy=ProgramLegPolicy.regular_only()),
    )


def option_flatten_waits_for_open(now_ms: int) -> LegRefusal:
    """Why an operator's Flatten of an option position is not sent outside the regular session.

    Refused before any EXIT is accepted, as a Flatten in shares is refused
    when no session is open, so nothing waits on the books to be sent later.
    """
    return LegRefusal(
        reason_code=OPTION_CLOSE_WAITS_FOR_OPEN,
        explanation=(
            "A position in an option contract is closed by a market order in regular hours, and the market "
            "is closed, so nothing was sent."
        ),
        next_step="Flatten again once the regular session opens.",
        available_at_ms=next_redrive_at_ms(not_before_ms=now_ms, policy=ProgramLegPolicy.regular_only()),
    )


#: A simulated close of an option contract that nothing kept can price (ADR 0076).
OPTION_EXIT_UNPRICED = OptionEffectRefusal.EXIT_UNPRICED.value


def option_close_unpriced(symbol: str, *, side: str) -> LegRefusal:
    """Why a Dry Run's Flatten of an option position cannot be priced: nothing kept for the contract can.

    A simulated close is priced from the newest quote kept for its contract:
    the bid for a sale. No quote kept, or a newest one with nothing on that
    side, prices nothing, and the simulation invents no price.
    """
    wanted = "bid" if side == "sell" else "offered"
    return LegRefusal(
        reason_code=OPTION_EXIT_UNPRICED,
        explanation=(
            f"No quote kept for {symbol.upper()} has anything {wanted}, so this Dry Run's simulated close "
            "of it cannot be priced honestly. Nothing was sold or bought."
        ),
        next_step=(
            "The contract is simulated and holds no real money. It can be closed once a quote of it with "
            f"something {wanted} is handed to this Dry Run's account Clerk; flatten again then."
        ),
    )


#: No order can close this: the option contract's last session has ended
#: (ADR 0076). The entry refuses a contract in that state under the same code.
OPTION_CONTRACT_EXPIRED = OptionEffectRefusal.CONTRACT_EXPIRED.value

_EXPIRED_CONTRACT_HEADLINE = "An option contract expired while the bot still held it"

_EXPIRED_CONTRACT_NEXT_STEP = (
    "No order can close an expired contract, so there is nothing to send. The Clerk books the "
    "contract's end by itself once it is known, and the position leaves this bot's books then."
)

_EXPIRED_CONTRACT_OPERATOR_IMPACT = (
    "New exposure is on hold for this strategy. No order can reduce this position; reconciliation "
    "remains available."
)
"""What an expired contract's notice says the owner can still do. Never "exact risk reduction remains
available", as every other ``EXIT_NOT_FLAT`` notice says: nothing reduces an expired contract."""


def expired_option_close(symbol: str, now_ms: int) -> LegRefusal | None:
    """Why no order can close a position in ``symbol``, or ``None`` when one still can.

    A refusal only for an option contract whose last session is over when an
    order sent now would reach the market (``send_arrival_ms``, as the
    session rule is judged). The last session is the calendar's, on the
    contract's expiry date (``app.options.deadlines``). From then on nothing
    trades in the contract: what it is worth is its exercise value, which no
    kept quote states, so nothing is priced from one. Any other symbol, and
    a contract still trading, is ``None``.

    The position stays on the books until its end is booked (ADR 0076
    decision 22): from the broker's own activity on a real account, from the
    simulation's settlement on a Dry Run, or at the owner's word when
    neither will come. The words are true of each.
    """
    contract_symbol = symbol.upper()
    if not is_option_contract_symbol(contract_symbol):
        return None
    expiry = OptionContract.from_occ_symbol(contract_symbol).expiry_date
    if not past_last_session(expiry, send_arrival_ms(now_ms)):
        return None
    return LegRefusal(
        reason_code=OPTION_CONTRACT_EXPIRED,
        explanation=(
            f"{contract_symbol} expired on {expiry.isoformat()}. Its last session is over, or ends within "
            "seconds, so no order can close it and none was sent. The position is still on the books "
            "until the contract's end is settled there."
        ),
        next_step=_EXPIRED_CONTRACT_NEXT_STEP,
    )


def regular_session_hold(facts: ExitAcceptedFacts, now_ms: int) -> LegRefusal:
    """The hold of a regular-session-only EXIT met outside the session, in the words of what it is."""
    if facts.option_contract is not None:
        return option_close_waits_for_open(now_ms)
    return regular_session_sale_waits_for_open(now_ms)


def regular_session_sale_waits_for_open(now_ms: int) -> LegRefusal:
    """Why a regular-session-only sale is held, and when it can go out: the next regular open.

    A hold, not a failure: the EXIT keeps custody and the pass re-drives it
    until the regular session opens (#2607).
    """
    return LegRefusal(
        reason_code=SCHEDULED_END_WAITS_FOR_OPEN,
        explanation=(
            "This sale is for a bot's scheduled end, which sells only in regular hours, and the market "
            "is closed. It waits for the next regular session and goes out at the open as a market order; "
            "it never goes out after hours."
        ),
        next_step="Nothing to do: the Clerk sends the sale when the market opens.",
        available_at_ms=next_redrive_at_ms(not_before_ms=now_ms, policy=ProgramLegPolicy.regular_only()),
    )


async def resolve_exit(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    trade: BrokerTradePort,
    pricing: RecoveryPricing,
    read: BrokerReadPort | None,
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

    ``read`` is the port an operator's Flatten is checked against the account
    with as it is sent (#2839, module docstring). It has no default either:
    the Flatten and the reconciliation sweep, which carries on a Flatten that
    deferred, pass the authority's read port; every caller that drives only
    its own kind of EXIT -- a deciding program's, the watchdog's re-drive, an
    ended run's close -- says ``None``. A Flatten going to a real broker with
    no read port is refused, never sent unchecked.

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
            read=read,
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
    reason: str = ENTRY_TERMINAL_PROOF,
    say_once: bool = False,
) -> OrderResource:
    """Cancel one exact owned ENTRY under its current durable custodian.

    ``off_loop`` moves each synchronous repository run onto a worker thread
    (#1993); the default keeps the pre-#1993 inline behavior.

    ``reason`` is why the cancel is asked for, recorded on the order's
    ``ORDER_CANCEL_REQUESTED`` the first time one is written for it.

    ``say_once`` is for a caller that asks again on a short timer (the
    deadline keeper, ADR 0076): a cancel still uncertain for the reason the
    books already hold is not recorded again (:func:`_cancel_already_uncertain`).
    Every other caller records each uncertain attempt, as before.
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
        await _cancel_and_prove_order(
            repo,
            effect_operation_id=effect_operation_id,
            order=entry,
            reason=reason,
            say_once=say_once,
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


async def cancel_each_entry_as_a_pass_step(
    repo: ClerkSqliteRepository,
    entries: Sequence[OrderResource],
    *,
    trade: BrokerTradePort,
    off_loop: OffLoop | None = None,
    reason: str,
    about: str,
    action: str,
) -> None:
    """Cancel and exact-prove each entry of ``entries``, one after another, each under its own order's claim.

    The loop every reconciliation-pass canceller of owned entries runs
    (``stopped_run_entries``, ``failed_enter_entries``): a held operation
    claim means another owner is acting on that order right now, so the entry
    is deferred to the next pass; a cancel whose exact proof still shows the
    order working is re-driven by the next pass; anything else propagates and
    the pass records itself incomplete. Broker failures are folded by
    :func:`cancel_and_prove_owned_entry` itself (``ORDER_CANCEL_UNCERTAIN``),
    as for every other caller.

    ``reason`` is why the cancel is asked for, recorded on the order's first
    ``ORDER_CANCEL_REQUESTED``. ``about`` names the entries as an operator
    reads them, and ``action`` prefixes the three actions logged, so each
    step's records stay greppable as its own.
    """
    run = off_loop if off_loop is not None else run_inline
    for entry in entries:
        try:
            resolved = await cancel_and_prove_owned_entry(
                repo,
                entry_order_ref=entry.order_ref,
                trade=trade,
                off_loop=run,
                reason=reason,
            )
        except OperationClaimError:
            logger.info(
                f"deferred the cancel of {about}: its operation is claimed",
                extra={
                    "action": f"{action}_deferred",
                    "account_id": repo.account_id,
                    "order_ref": entry.order_ref,
                },
            )
            continue
        if (resolved.broker_state or "").lower() in CANCELLABLE_ENTRY_BROKER_STATES:
            logger.warning(
                f"{about} is still working after its cancel; the next pass re-drives it",
                extra={
                    "action": f"{action}_unconfirmed",
                    "account_id": repo.account_id,
                    "order_ref": entry.order_ref,
                    "broker_state": resolved.broker_state,
                },
            )
            continue
        logger.warning(
            f"cancelled {about}",
            extra={
                "action": f"{action}_cancelled",
                "account_id": repo.account_id,
                "order_ref": entry.order_ref,
                "broker_state": resolved.broker_state,
            },
        )


class _ClaimedState(NamedTuple):
    """One consistent read of the claimed EXIT's durable custody state."""

    effect: EffectOperationResource
    entries: list[OrderResource]
    reducing: OrderResource | None
    symbol: str
    reducing_needs_evidence: bool


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
        reducing is not None and (
            repo.order_fills_short_of_broker_cumulative(reducing.order_ref)
            or repo.order_has_unproven_legs(reducing.order_ref)
        ),
    )


def _fold_attributed_flat_or_none(
    repo: ClerkSqliteRepository, effect_operation_id: str, state: _ClaimedState
) -> ExitSubmission | None:
    held = captured_positions(repo, state.effect.strategy_instance_id, state.entries)
    if state.reducing is None and not any(position_quantity_is_nonzero(qty) for qty in held.values()):
        _fold_attributed_flat(repo, effect_operation_id, _primary_entry_ref(repo, state.entries))
        return _snapshot(repo, effect_operation_id)
    return None


def _prepare_reduction(
    repo: ClerkSqliteRepository,
    effect_operation_id: str,
    state: _ClaimedState,
    touch: PricingSnapshot | None,
    liveness: MarketLivenessFact | None,
) -> OrderResource | ExitSubmission | _TouchNeeded:
    """One atomic admission-to-append run: the created reducing order, or
    the EXIT's snapshot when a precondition folded it to an early end —
    attributed-flat, the send-time rule, or a moved quantity — or
    :data:`_TOUCH_NEEDED`, having written nothing, when the recorded leg
    must be re-priced and ``touch`` was not read yet.
    """
    positions = captured_positions(repo, state.effect.strategy_instance_id, state.entries)
    remaining_qty = next((qty for qty in positions.values() if position_quantity_is_nonzero(qty)), 0)
    symbol = next((symbol for symbol, qty in positions.items() if position_quantity_is_nonzero(qty)), state.symbol)
    order_ref = _primary_entry_ref(repo, state.entries)
    if not position_quantity_is_nonzero(remaining_qty):
        _fold_attributed_flat(repo, effect_operation_id, order_ref)
        return _snapshot(repo, effect_operation_id)
    leg = _leg_to_create(
        repo,
        effect_operation_id=effect_operation_id,
        order_ref=order_ref,
        symbol=symbol,
        remaining_qty=remaining_qty,
        touch=touch, liveness=liveness,
    )
    if isinstance(leg, _TouchNeeded):
        return leg
    if leg is None:
        return _snapshot(repo, effect_operation_id)
    multi = any(isinstance(option_entry_of_order(repo, entry.order_ref), MultiLegOptionEntryFacts) for entry in state.entries)
    close = None
    leg_ids = None
    if multi:
        for contract, qty in positions.items():
            if not position_quantity_is_nonzero(qty):
                continue
            if _fold_expired_option_close(
                repo, _accepted_facts(repo, effect_operation_id), effect_operation_id=effect_operation_id,
                order_ref=order_ref, symbol=contract, remaining_qty=qty, now_ms=repo.clock(),
            ):
                return _snapshot(repo, effect_operation_id)
            require_capability(
                repo, capability=Capability.REDUCE, strategy_instance_id=state.effect.strategy_instance_id,
                reduction_intent=ReductionIntent(symbol=contract, side="SELL" if qty > 0 else "BUY", quantity=abs(qty)),
            )
        close, leg_ids = close_held_options(repo, state.effect.strategy_instance_id, state.entries)
    require_capability(
        repo,
        capability=Capability.REDUCE,
        strategy_instance_id=state.effect.strategy_instance_id,
        reduction_intent=ReductionIntent(
            symbol=symbol,
            side="SELL" if remaining_qty > 0 else "BUY",
            quantity=abs(remaining_qty),
        ),
    )
    return _create_reducing_order(
        repo,
        effect_operation_id=effect_operation_id,
        symbol=symbol,
        quantity=remaining_qty,
        leg=leg,
        option_order=close,
        option_leg_ids=leg_ids,
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
    if repo.order_has_unproven_legs(refreshed.order_ref):
        if state.effect.state != "unknown":
            fold_uncertain(
                repo, effect_operation_id=effect_operation_id, order_ref=refreshed.order_ref,
                why="The parent ended while an option leg still needs terminal or execution evidence; retaining custody.",
            )
        return _snapshot(repo, effect_operation_id)
    held = captured_positions(repo, state.effect.strategy_instance_id, state.entries)
    final_qty = next((qty for qty in held.values() if position_quantity_is_nonzero(qty)), 0)
    final_symbol = next((symbol for symbol, qty in held.items() if position_quantity_is_nonzero(qty)), state.symbol)
    if not any(position_quantity_is_nonzero(qty) for qty in held.values()):
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
        not any(repo.fills_for_order(leg.order_ref) for leg in repo.order_legs(refreshed.order_ref))
        and not repo.fills_for_order(refreshed.order_ref)
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
            f"The reducing order became terminal while {holdings_text({symbol: qty for symbol, qty in held.items() if qty})} "
            "remained attributed to this strategy."
        )
        next_step = "Run another EXIT or reconcile until attributed exposure is flat."
    _fold_exit_not_flat(
        repo,
        effect_operation_id=effect_operation_id,
        order_ref=refreshed.order_ref,
        symbol=final_symbol,
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
    read: BrokerReadPort | None,
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
    # The entries the broker knows. Shares an option event left were booked on an order that
    # was never at the broker (ADR 0076 decision 22): there is nothing of it to cancel or to
    # look up, and a lookup would only find it absent.
    entries_at_the_broker = await run(lambda: _entries_the_broker_knows(repo, state.entries))
    if not await _prove_entry_set_terminal(
        repo,
        effect_operation_id=effect_operation_id,
        entries=entries_at_the_broker,
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
            entries=entries_at_the_broker,
            broker=broker,
            run=run,
        ):
            return await run(lambda: _snapshot(repo, effect_operation_id))
        now_ms = repo.clock()
        liveness = pricing.read_liveness(state.symbol, now_ms)
        prepared = await run(
            lambda: _prepare_reduction(repo, effect_operation_id, state, None,  liveness)
        )
        if isinstance(prepared, _TouchNeeded):
            touch = pricing.read(state.symbol, repo.clock(), strategy_instance_id=state.effect.strategy_instance_id)
            prepared = await run(
                lambda: _prepare_reduction(repo, effect_operation_id, state, touch,  touch.market_liveness)
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
            read=read,
            run=run,
        )
    elif not _is_terminal(reducing.broker_state) or state.reducing_needs_evidence:
        # A terminal reducing order whose recorded fills fall short of the
        # broker's cumulative lost a slice (#2305): refresh it by exact lookup
        # so the cumulative fold closes the gap before the outcome is judged.
        await _refresh_or_resume_reducing_order(
            repo,
            effect_operation_id=effect_operation_id,
            reducing=reducing,
            broker=broker,
            pricing=pricing,
            read=read,
            run=run,
        )
    refreshed, accepted, created = await run(lambda: (
        repo.order(reducing.order_ref), _accepted_facts(repo, effect_operation_id),
        _reducing_order_facts(repo, reducing.order_ref),
    ))
    if refreshed is not None and replacement_due(
        order=refreshed, accepted=accepted, created=created, now_ms=repo.clock(),
    ):
        await _cancel_and_prove_order(
            repo, effect_operation_id=effect_operation_id, order=refreshed,
            reason=OPEN_REPLACEMENT, broker=broker, run=run,
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
    why: str | None = None,
    refusal: BrokerRefusal | None = None,
    operator_impact: str | None = None,
) -> None:
    """Fail the EXIT and raise the ``EXIT_NOT_FLAT`` episode exposure is still held under.

    The episode is what flags the bot for the operator — on the bot page and
    in the lane's attention bell — and what the stuck-EXIT watchdog re-drives:
    the market leg inside the regular session, a limit it prices itself in a
    declared extended session (#2229). Recovery eligibility is recorded only
    after a successful reconciliation pass, then projected from that check.
    This fold retains custody and the failure cause without guessing a retry
    time. ``why`` is recorded beside the quantity still held, and a broker
    ``refusal``'s evidence beside it.
    """
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    held = f"attributed_qty={attributed_qty} remains for {symbol!r}."
    # The notice is written before the failure (#2854). Each is its own
    # write, and a process that stops between them must not leave an EXIT
    # that failed and released its entry with nothing telling the operator
    # or the watchdog: no pass visits a failed EXIT again. Stopped after the
    # notice alone, the EXIT is still unfinished, so the next pass drives it
    # here again; the notice it then raises is the one already open.
    raise_uncertainty(
        repo,
        strategy_instance_id=effect.strategy_instance_id,
        **_exit_not_flat_notice(
            order_ref=order_ref,
            symbol=symbol,
            attributed_qty=attributed_qty,
            headline=headline,
            explanation=explanation,
            next_step=next_step,
            operator_impact=operator_impact,
        ),
    )
    fold_failed(
        repo,
        effect_operation_id=effect_operation_id,
        order_ref=order_ref,
        summary_code=summary_code,
        reason=reason,
        why=held if why is None else f"{why} {held}",
        transition_kind="EXIT_NOT_FLAT",
        refusal=refusal,
    )


_EXIT_NOT_FLAT_OPERATOR_IMPACT = (
    "New exposure is on hold for this strategy; exact risk reduction and "
    "reconciliation remain available."
)


class _ExitNotFlatNotice(TypedDict):
    """What every ``EXIT_NOT_FLAT`` episode an EXIT ends under is raised with."""

    reason_code: str
    headline: str
    explanation: str
    operator_impact: str
    next_step: str
    evidence_refs: tuple[str, ...]
    cause_facts: dict[str, Any]
    severity: str


def _exit_not_flat_notice(
    *,
    order_ref: str,
    symbol: str,
    attributed_qty: float,
    headline: str,
    explanation: str,
    next_step: str,
    operator_impact: str | None = None,
) -> _ExitNotFlatNotice:
    """The ``EXIT_NOT_FLAT`` notice of an EXIT that ended with ``attributed_qty`` of ``symbol`` still held.

    One shape for the episode raised as its own transition
    (:func:`_fold_exit_not_flat`) and the one an operator's attestation
    opens in the write that ends the EXIT
    (:func:`attested_absent_exit_episodes`): the watchdog reads both the same.
    """
    return {
        "reason_code": EXIT_NOT_FLAT_REASON_CODE,
        "headline": headline,
        "explanation": explanation,
        "operator_impact": _EXIT_NOT_FLAT_OPERATOR_IMPACT if operator_impact is None else operator_impact,
        "next_step": next_step,
        "evidence_refs": (order_ref,),
        "cause_facts": ExitNotFlatCause(symbol=symbol.upper(), attributed_qty=attributed_qty).to_mapping(),
        "severity": "error",
    }


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
    lost submit is legitimately sent again while its session is open.
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
    liveness: MarketLivenessFact | None,
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
    if _fold_expired_option_close(
        repo, facts, effect_operation_id=effect_operation_id, order_ref=order_ref, symbol=symbol,
        remaining_qty=remaining_qty, now_ms=now_ms,
    ):
        return None
    reducing_side = OrderSide.SELL if remaining_qty > 0 else OrderSide.BUY
    recorded = facts.reducing_shape()
    shape = _resolved_reducing_shape(recorded, reducing_side=reducing_side)
    valid_until_ms = facts.reducing_valid_until_ms if shape.extended_hours else None
    verdict = _send_verdict(
        facts, extended_hours=shape.extended_hours, valid_until_ms=valid_until_ms, now_ms=now_ms,
        liveness=liveness,
    )
    if isinstance(verdict, LegRefusal):
        _fold_market_hold(repo, effect_operation_id, order_ref, symbol, verdict)
        return None
    operator_priced = facts.operator_priced
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


def _fold_expired_option_close(
    repo: ClerkSqliteRepository,
    facts: ExitAcceptedFacts,
    *,
    effect_operation_id: str,
    order_ref: str,
    symbol: str,
    remaining_qty: float,
    now_ms: int,
) -> bool:
    """End an EXIT whose option contract has expired, with nothing sent; ``False`` for every other EXIT.

    Asked before the session rule and before anything is priced, by whatever
    made the EXIT: a deciding program, an ended run's close, the watchdog's
    re-drive, an operator's Flatten, or an EXIT held since before the
    contract expired. The EXIT fails under the refusal's own code and the
    ``EXIT_NOT_FLAT`` notice names the contract and says that it expired.
    The watchdog then holds it: it sends nothing for a contract that can
    never trade again (``exit_watchdog.evaluate_recovery_wait``).
    """
    if facts.option_contract is None:
        return False
    expired = expired_option_close(symbol, now_ms)
    if expired is None:
        return False
    logger.warning(
        "an EXIT was not sent: the option contract it closes has expired",
        extra={
            "action": "exit_option_contract_expired",
            "account_id": repo.account_id,
            "effect_operation_id": effect_operation_id,
            "symbol": symbol,
            "attributed_qty": remaining_qty,
        },
    )
    _fold_exit_not_flat(
        repo,
        effect_operation_id=effect_operation_id,
        order_ref=order_ref,
        symbol=symbol,
        attributed_qty=remaining_qty,
        summary_code=expired.reason_code,
        reason=expired.explanation,
        headline=_EXPIRED_CONTRACT_HEADLINE,
        explanation=expired.explanation,
        next_step=expired.next_step,
        operator_impact=_EXPIRED_CONTRACT_OPERATOR_IMPACT,
    )
    return True


def note_expired_option_position(
    repo: ClerkSqliteRepository,
    *,
    strategy_instance_id: str,
    entry_order_ref: str,
    symbol: str,
    attributed_qty: float,
    expired: LegRefusal,
) -> None:
    """Tell the operator that a bot holds an expired option contract, without making an EXIT of it.

    For a close refused before anything is accepted. The notice is the one
    an EXIT ended by :func:`_fold_expired_option_close` raises, so the bot's
    page reads the same whichever met the expiry first, and raising it again
    writes nothing.
    """
    raise_uncertainty(
        repo,
        strategy_instance_id=strategy_instance_id,
        **_exit_not_flat_notice(
            order_ref=entry_order_ref,
            symbol=symbol,
            attributed_qty=attributed_qty,
            headline=_EXPIRED_CONTRACT_HEADLINE,
            explanation=expired.explanation,
            next_step=expired.next_step,
            operator_impact=_EXPIRED_CONTRACT_OPERATOR_IMPACT,
        ),
    )


class ExpiredContractNotice(NamedTuple):
    """What a notice of a held option contract says once the contract has expired."""

    headline: str
    explanation: str
    operator_impact: str
    next_step: str


def expired_contract_notice(expired: LegRefusal) -> ExpiredContractNotice:
    """The one wording of a held contract that has expired (ADR 0076), wherever it is said.

    On the notice an EXIT raises when it meets the expiry, on the notices
    the Clerk's pass rewrites (:func:`say_the_contract_expired`), and on
    the bot's page, which reads an open notice in these words from the
    moment of expiry without waiting for a pass (``projections``).
    """
    return ExpiredContractNotice(
        headline=_EXPIRED_CONTRACT_HEADLINE,
        explanation=expired.explanation,
        operator_impact=_EXPIRED_CONTRACT_OPERATOR_IMPACT,
        next_step=expired.next_step,
    )


def say_the_contract_expired(
    repo: ClerkSqliteRepository, *, strategy_instance_id: str, expired: LegRefusal
) -> bool:
    """Say on this bot's open EXIT notices that its option contract expired; whether anything was written.

    A notice raised while the contract still traded tells the operator what
    could be done then: wait for the Clerk's retry, or flatten. Once the
    contract's last session is over neither can happen, and a stopped bot
    makes no later EXIT that would say so. So the Clerk's own pass says it
    (``exit_watchdog.commit_recovery_evaluations``), on the
    ``EXIT_NOT_FLAT`` notice and on the ``EXIT_STUCK`` one beside it, in the
    words an EXIT ended by :func:`_fold_expired_option_close` raises
    (:func:`expired_contract_notice`).

    Each notice keeps what it names and its evidence: the watchdog still
    reads the episode's orders from them. The words do not change with the
    time, so a notice that already says it is left as it is and nothing is
    written: once per notice, however many passes follow.
    """
    written = False
    for reason_code in (EXIT_NOT_FLAT_REASON_CODE, EXIT_STUCK_REASON_CODE):
        episode = repo.active_uncertainty(
            scope="CUSTODY_SUBJECT", reason_code=reason_code, strategy_instance_id=strategy_instance_id
        )
        if episode is None:
            continue
        recorded = UncertaintyRaisedFacts.from_facts_json(episode["facts_json"])
        outcome = raise_uncertainty(
            repo,
            strategy_instance_id=strategy_instance_id,
            reason_code=reason_code,
            **expired_contract_notice(expired)._asdict(),
            evidence_refs=tuple(recorded.evidence_refs),
            cause_facts=recorded.cause_facts,
            severity=recorded.severity,
        )
        written = written or outcome != "unchanged"
    return written


def _send_verdict(
    facts: ExitAcceptedFacts, *, extended_hours: bool, valid_until_ms: int | None, now_ms: int,
    liveness: MarketLivenessFact | None,
) -> ReducingLegVerdict | LegRefusal:
    """The send-time rule for this EXIT's leg; a Dry Run's run-end close always sends.

    That close fills at a price its run already saw, never at the market, so
    no session, halt or missing quote can hold it (owner decision 2026-09-29).
    A run that ended holding an option contract is closed the same way, at
    the newest quote kept for the contract and at whatever hour the run
    ended (ADR 0076). Only the contract's expiry stops that close, and it is
    asked before this rule (:func:`_fold_expired_option_close`).
    A regular-session-only EXIT (the sale at a scheduled end, and its
    re-drives) sends only its market leg: where any other EXIT would be
    re-priced for the session open now, it holds for the next regular
    session instead (#2607).
    """
    if _exit_intent(facts) == "run_end_close":
        return "send"
    verdict = reducing_send_verdict(
        extended_hours=extended_hours, valid_until_ms=valid_until_ms, now_ms=now_ms, liveness=liveness,
    )
    if facts.regular_session_only and verdict == "wait":
        return regular_session_hold(facts, now_ms)
    return verdict


def _created_leg_may_be_sent(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    created: ExitReducingOrderCreatedFacts,
    order_ref: str,
    liveness: MarketLivenessFact | None,
    sent_to_broker: bool,
) -> bool:
    """The send-time rule at (re)submission: ``True`` only to send the created order now.

    Nothing is sent to reduce a position that is already flat, in the session
    or out of it: a resubmission after an outage or a restart would otherwise
    sell into a flat — or short — position, so the EXIT proves attributed-flat
    instead. The proof cites the reducing order being judged: a lost submit
    left that exact identity's outcome unknown, and only proof recorded
    against it closes that episode (#2440 review). The order already carries
    its client identity, so its leg is never re-priced here: it is replayed
    exactly or not at all.

    No longer sendable, it is not sent, and what becomes of its EXIT turns on
    ``sent_to_broker``: whether the order was sent before to a real broker.
    One that was may be there, so its EXIT keeps custody as an unknown
    outcome (:func:`_hold_past_its_session`, #2854). One never sent, or sent
    only to a simulated port, folds releasably for the operator, or holds for
    the next open when it sells only in the regular session.
    """
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    entries = [order for order in repo.orders_for_effect_operation(effect_operation_id) if order.role == "ENTRY"]
    held = captured_positions(repo, effect.strategy_instance_id, entries)
    remaining_qty = next((qty for qty in held.values() if position_quantity_is_nonzero(qty)), 0)
    if not position_quantity_is_nonzero(remaining_qty):
        _fold_attributed_flat(repo, effect_operation_id, order_ref)
        return False
    request = created.the_option_order
    if request is not None:
        for symbol, side, quantity in order_reductions(request):
            confirmed = held.get(symbol, 0)
            if confirmed != (quantity if side is OrderSide.SELL else -quantity):
                refusal = BrokerRefusal(why=f"The confirmed holding of {symbol} changed after this close was created; no stale close was sent.")
                reducing = repo.order(order_ref)
                assert reducing is not None
                if sent_to_broker:
                    fold_uncertain(repo, effect_operation_id=effect_operation_id, order_ref=order_ref, why=refusal.why)
                else:
                    _fold_submit_refused(repo, effect_operation_id=effect_operation_id, reducing=reducing, refusal=refusal)
                return False
            if not sent_to_broker and _fold_expired_option_close(
                repo, _accepted_facts(repo, effect_operation_id), effect_operation_id=effect_operation_id,
                order_ref=order_ref, symbol=symbol, remaining_qty=confirmed, now_ms=repo.clock(),
            ):
                return False
    valid_until_ms = _created_leg_valid_until_ms(repo, effect_operation_id, created)
    now_ms = repo.clock()
    accepted = _accepted_facts(repo, effect_operation_id)
    # An order sent before to a real broker may be there: its EXIT keeps custody below, expired contract or not.
    if not sent_to_broker and _fold_expired_option_close(
        repo, accepted, effect_operation_id=effect_operation_id, order_ref=order_ref, symbol=created.symbol,
        remaining_qty=remaining_qty, now_ms=now_ms,
    ):
        return False
    verdict = _send_verdict(
        accepted,
        extended_hours=created.extended_hours,
        valid_until_ms=valid_until_ms,
        now_ms=now_ms, liveness=liveness,
    )
    if sent_to_broker and _past_its_session(
        repo, order_ref=order_ref, created=created, valid_until_ms=valid_until_ms,
        now_ms=now_ms, verdict=verdict,
    ):
        _hold_past_its_session(
            repo, effect_operation_id=effect_operation_id, order_ref=order_ref, created=created
        )
        return False
    if isinstance(verdict, LegRefusal):
        _fold_market_hold(repo, effect_operation_id, order_ref, created.symbol, verdict)
        return False
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


def _past_its_session(
    repo: ClerkSqliteRepository,
    *,
    order_ref: str,
    created: ExitReducingOrderCreatedFacts,
    valid_until_ms: int | None,
    now_ms: int,
    verdict: ReducingLegVerdict | LegRefusal,
) -> bool:
    """Whether a reducing order sent before may no longer be sent again: the session it was sent in is over (#2854).

    Over when the send-time rule would not send it now: a market leg outside
    the regular session, a limit past its bound, a regular-session sale that
    would wait for the next open. Over, too, in any later session in which
    that rule would send it: an order sent in an earlier session is looked
    up, never sent again (owner decision 2026-10-03). The session is the one
    of the order's latest send, read from the canonical calendar
    (``reducing_leg_session_end_ms``). A send whose session the calendar
    cannot name is not shown to be inside an open one, so it is over.

    A halt or an emergency close inside that session is not its end: the
    order holds as before, and is sent again when the hold lifts.
    """
    if verdict in ("wait", "expired"):
        return True
    if isinstance(verdict, LegRefusal) and verdict.reason_code in (
        SCHEDULED_END_WAITS_FOR_OPEN, OPTION_CLOSE_WAITS_FOR_OPEN
    ):
        return True
    latest = _latest_send(repo, order_ref)
    if latest is None:
        raise AssertionError(f"no recorded send for {order_ref!r}")
    session_end_ms = reducing_leg_session_end_ms(
        extended_hours=created.extended_hours,
        valid_until_ms=valid_until_ms,
        sent_at_ms=latest.requested["recorded_at_ms"],
    )
    return session_end_ms is None or now_ms >= session_end_ms


# The settle action's label is spelled out here: ``recovery_policy``, which names it, imports this module.
SETTLE_THE_ORDER_NEXT_STEP = (
    "Look at this account's orders and position at Alpaca, then settle the order from the "
    'bot\'s page ("Settle the uncertain order"). A bot that is still running is stopped first.'
)
"""What an operator does about a closing order the Clerk holds and will not settle by itself (#2854)."""


def _what_is_held(created: ExitReducingOrderCreatedFacts) -> str:
    """What a reducing order would have closed, as a notice names it: a sale's shares, a cover's short."""
    return "the shares" if created.side.upper() == "SELL" else "the short position"


def _hold_sent_order(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    order_ref: str,
    why: str,
    notice: UnknownOutcomeNotice,
) -> bool:
    """Keep custody of a reducing order that was sent and is not sent now, telling the operator once (#2854).

    The EXIT stays ``unknown``: it is not failed and its entry is not
    released. The passes come every few seconds and this can last overnight,
    so the record is written when it changes what the operator is told, and
    not again: a pass that finds the EXIT ``unknown`` and the order's newest
    notice already this one writes nothing. Returns whether it wrote.
    """
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    told = repo.last_order_notice_transition(order_ref=order_ref)
    if (
        effect.state == "unknown"
        and told is not None
        and OrderSubmitUncertainFacts.from_facts_json(told["facts_json"]).notice == notice
    ):
        return False
    fold_uncertain(
        repo, effect_operation_id=effect_operation_id, order_ref=order_ref, why=why, notice=notice
    )
    return True


def _hold_past_its_session(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    order_ref: str,
    created: ExitReducingOrderCreatedFacts,
) -> None:
    """A reducing order sent to a real broker outlived its session with its outcome unknown: custody is kept (#2854).

    Before this the EXIT failed here on the absence proof alone and released
    the position (:func:`_fold_unsendable_leg`), or -- a scheduled-end sale --
    held for the next open and was sent again then. Either way an order that
    had reached the broker unseen could be followed by a second sale: the
    bot's next exit decision and the sale at a scheduled end send with no
    check of the account. Now nothing more is sent for the bot. The EXIT
    stays ``unknown``, so no other EXIT is accepted for its entry, the
    watchdog has no failed EXIT to re-drive, and the order itself is never
    sent again, in this session or a later one. Each pass still looks it up
    and folds it if the broker shows it. Otherwise it waits for an operator,
    who looks at Alpaca and settles it (``unknown_order_attestation``).

    The open notice says so in place of whatever it said while the order
    could still go out, and it is recorded once (:func:`_hold_sent_order`).
    It says nothing of a session having ended. :func:`_past_its_session` also
    holds an order whose own bound for sending has passed while the broker
    would still work it: a pre-market limit, past 09:30, works at the broker
    until the after-hours close if it is there.
    """
    wrote = _hold_sent_order(
        repo,
        effect_operation_id=effect_operation_id,
        order_ref=order_ref,
        why=(
            "This reducing order was sent to the broker, its outcome is unknown, and it may no "
            "longer be sent again. An earlier send may have reached the broker, so the EXIT is not "
            "failed: custody is kept until the order is found or an operator settles it."
        ),
        notice=UnknownOutcomeNotice(
            explanation=(
                f"The Clerk sent an order to {_the_reduction(created)} and could not confirm it. "
                "The Clerk will not send it again. The order may be at Alpaca, so the Clerk holds "
                f"{_what_is_held(created)} and sends no other order for this bot."
            ),
            next_step=SETTLE_THE_ORDER_NEXT_STEP,
        ),
    )
    if wrote:
        logger.warning(
            "a reducing order sent to the broker may no longer be sent again and its outcome is unknown; custody is kept",
            extra={
                "action": "reducing_order_held_past_session",
                "account_id": repo.account_id,
                "effect_operation_id": effect_operation_id,
                "order_ref": order_ref,
                "symbol": created.symbol,
            },
        )


def _hold_uncovered_resend(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    reducing: OrderResource,
    refusal: LegRefusal,
) -> None:
    """An operator's Flatten about to be sent again is not covered by the account: custody is kept (#2839, #2854).

    A first send the account does not cover fails the EXIT
    (:func:`_fold_flatten_uncovered`): nothing was sent before, so nothing of
    it can be at the broker. A send made again is different. Its earlier
    send's answer was lost, and what the account no longer holds may be what
    that send closed. Failing the EXIT would release a position its own order
    may have closed. So nothing is sent, and the EXIT stays ``unknown`` with
    custody kept. While the order's session is open the account is read again
    for each send the wait allows, and the order goes out if the account
    covers it; past its session it is held as any sent order is
    (:func:`_hold_past_its_session`).
    """
    created = _reducing_order_facts(repo, reducing.order_ref)
    wrote = _hold_sent_order(
        repo,
        effect_operation_id=effect_operation_id,
        order_ref=reducing.order_ref,
        why=(
            "The account check made before sending this reducing order again refused it. An "
            "earlier send of it may have reached the broker, so the refusal does not prove "
            f"nothing is working there, and custody is kept. The check's answer: {refusal.explanation}"
        ),
        notice=UnknownOutcomeNotice(
            explanation=(
                f"The Clerk sent an order to {_the_reduction(created)} and could not confirm it. "
                f"Before sending it again the Clerk checked the account: {refusal.explanation} "
                f"The earlier send may have reached Alpaca, so the Clerk holds {_what_is_held(created)} "
                "and sends no other order for this bot."
            ),
            next_step=(
                f"{SETTLE_THE_ORDER_NEXT_STEP} While the order's session is open the Clerk also "
                "checks the account again by itself, and sends the order once the account covers it."
            ),
        ),
    )
    if wrote:
        logger.warning(
            "an operator's flatten was not sent again: the account check refused it; custody is kept",
            extra={
                "action": "flatten_resend_refused_by_account_check",
                "account_id": repo.account_id,
                "effect_operation_id": effect_operation_id,
                "order_ref": reducing.order_ref,
                "symbol": created.symbol,
                "reason_code": refusal.reason_code,
            },
        )


def _lost_answer_after_a_notice(repo: ClerkSqliteRepository, order_ref: str) -> UnknownOutcomeNotice | None:
    """What the open notice says once a send made after a notice is lost too, or ``None`` to leave it as it is (#2854).

    A lost answer records no notice: the notice it opens, or finds open,
    already says a broker response was lost. That is not so once a notice has
    been recorded for the order: that the account check held a send made
    again (:func:`_hold_uncovered_resend`), or that Alpaca refused one
    (:func:`_retain_refused_resend`). Those words are about the order before
    this send. Left standing, they told the operator that nothing was sent,
    and that the order goes out once the account covers it, after it had gone
    out. So this send's lost answer records the lost-answer words as its own
    notice. Once, as every notice is: ``None`` for an order with no notice,
    and for one whose newest notice already says this.
    """
    told = repo.last_order_notice_transition(order_ref=order_ref)
    if told is None:
        return None
    if OrderSubmitUncertainFacts.from_facts_json(told["facts_json"]).notice == LOST_ANSWER_NOTICE:
        return None
    return LOST_ANSWER_NOTICE


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
) -> None:
    """Fold an EXIT whose leg can no longer be sent, releasably, through ``EXIT_NOT_FLAT``.

    Nothing is queued for a later session: the entry is freed, the episode is
    what the operator sees on the bot page and in the lane's attention bell,
    and it is what the stuck-EXIT watchdog re-drives — pricing a limit itself
    in an extended session (#2229), after a fresh eligibility check. The
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
        )

    if _exit_intent(_accepted_facts(repo, effect_operation_id)) != "recovery":
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
    from app.broker.alpaca.clerk.sqlite.order_evidence import entry_order_symbols

    symbols = tuple(dict.fromkeys(symbol for entry in entries for symbol in entry_order_symbols(repo, entry.order_ref)))
    if len(symbols) != 1 and not all(option_entry_of_order(repo, order.order_ref) is not None for order in entries):
        raise AssertionError("one EXIT may only link entry orders for one symbol")
    return symbols[0]


def _entries_the_broker_knows(repo: ClerkSqliteRepository, entries: list[OrderResource]) -> list[OrderResource]:
    """``entries`` without the orders that are shares an option event left. Every other entry is kept."""
    with repo.write_fence() as conn:
        return [entry for entry in entries if event_share_order_symbol(conn, entry.order_ref) is None]


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
        if reducing_exists and _is_terminal(entry.broker_state) and not await run(
            lambda entry=entry: repo.order_has_unproven_legs(entry.order_ref)
            or repo.order_fills_short_of_broker_cumulative(entry.order_ref)
        ):
            continue
        if not await _cancel_and_prove_order(
            repo,
            effect_operation_id=effect_operation_id,
            order=entry,
            broker=broker,
            run=run,
        ):
            return False
    return True


async def _cancel_and_prove_order(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    order: OrderResource,
    reason: str = ENTRY_TERMINAL_PROOF,
    say_once: bool = False,
    broker: ClaimedBrokerIO,
    run: OffLoop,
) -> bool:
    if order.role == "ENTRY" and await run(
        lambda: _prove_never_accepted_durably(
            repo, effect_operation_id=effect_operation_id, entry=order
        )
    ):
        return True
    if order.broker_state == "pending_cancel":
        await run(lambda: _append_order_phase_once(
            repo, effect_operation_id, order, "ORDER_CANCEL_REQUESTED", cancel_reason=reason,
        ))
    cancel_error: BrokerError | None = None
    if not _is_terminal(order.broker_state) and order.broker_order_id is not None and order.broker_state != "pending_cancel":
        await run(
            lambda: _append_order_phase_once(
                repo, effect_operation_id, order, "ORDER_CANCEL_REQUESTED", cancel_reason=reason
            )
        )
        try:
            # Reissuing cancel for the same broker identity is a retry of the
            # durable phase, not a second intent. This makes a lost cancel
            # response recoverable when the exact poll still shows working.
            await broker.cancel(order.broker_order_id, order_ref=order.order_ref)
        except BrokerError as exc:
            # The exact lookup below, not the cancel response, proves state.
            cancel_error = exc

    observed = await broker.observe_exact(order.client_order_id)
    if order.role == "ENTRY" and await run(
        lambda: _prove_never_accepted_if_absent(
            repo, effect_operation_id=effect_operation_id, entry=order, observed=observed
        )
    ):
        return True
    if isinstance(observed, BrokerError) or observed is None:

        def _fold_cancel_uncertain() -> None:
            if say_once and _cancel_already_uncertain(repo, order, looked_up=None):
                return
            fold_uncertain(
                repo,
                effect_operation_id=effect_operation_id,
                order_ref=order.order_ref,
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
        refreshed = repo.order(order.order_ref)
        assert refreshed is not None
        if not _is_terminal(refreshed.broker_state) or repo.order_has_unproven_legs(order.order_ref):
            if cancel_error is not None and not (
                say_once and _cancel_already_uncertain(repo, order, looked_up=refreshed)
            ):
                fold_uncertain(repo, effect_operation_id=effect_operation_id, order_ref=order.order_ref,
                               why=str(cancel_error), transition_kind="ORDER_CANCEL_UNCERTAIN")
            return False
        if order.role == "ENTRY":
            _append_order_phase_once(repo, effect_operation_id, refreshed, "ENTRY_TERMINAL_CONFIRMED")
        return not repo.order_fills_short_of_broker_cumulative(order.order_ref)

    return await run(_fold_observed_entry)


_UNCERTAIN_KINDS = frozenset({"ORDER_CANCEL_UNCERTAIN", "ORDER_SUBMIT_UNCERTAIN"})


def _cancel_already_uncertain(
    repo: ClerkSqliteRepository, order: OrderResource, *, looked_up: OrderResource | None
) -> bool:
    """Whether the books already say this order's cancel is uncertain, and this attempt adds nothing to that.

    ``order`` is the order as it stood before the attempt. ``looked_up`` is
    the order after this attempt's lookup was folded, or ``None`` when the
    lookup itself failed.

    A cancel asked for again every second would otherwise write a record
    every second for as long as the broker refused it or could not be
    reached. Nothing is lost by saying it once: the first record stands, and
    the order stays on the worklist until a lookup shows it ended.

    - The lookup failed: nothing new when the last thing recorded of the
      order is already that its outcome is uncertain.
    - The lookup answered and the cancel was refused: nothing new when a
      cancel of this order was already recorded uncertain and the broker
      still shows the state it showed before.
    """
    if not repo.has_order_transition(order_ref=order.order_ref, transition_kind="ORDER_CANCEL_UNCERTAIN"):
        return False
    if looked_up is not None:
        return looked_up.broker_state == order.broker_state
    last = repo.last_order_transition(order_ref=order.order_ref)
    return last is not None and last["transition_kind"] in _UNCERTAIN_KINDS


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
            return (
                _is_terminal(current.broker_state)
                and not repo.order_has_unproven_legs(current.order_ref)
                and not repo.order_fills_short_of_broker_cumulative(current.order_ref)
            )

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
    option_order: OptionOrderRequest | None = None,
    option_leg_ids: tuple[str, ...] | None = None,
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
        option_order=None if option_order is None else option_order.model_dump(mode="json"),
        option_leg_ids=None if option_leg_ids is None else list(option_leg_ids),
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
    read: BrokerReadPort | None,
    run: OffLoop,
) -> None:
    observed = await broker.observe_exact(reducing.client_order_id)
    if observed is None and reducing.broker_order_id is not None:
        observed = await _observe_by_broker_order_id(broker, reducing)
        if observed is None:
            # The broker answered that it has no order under either id. A
            # read that failed is an error above, and never reaches here.
            observed = await run(lambda: _attested_order_as_recorded(repo, reducing))
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
        return

    def _absence_folds_uncertain() -> bool:
        # Answered under another client order id (#2845): the absence of the
        # one that was sent is never leave to send it again.
        if _retain_answered_as_another_order(
            repo, effect_operation_id=effect_operation_id, reducing=reducing
        ):
            return True
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
    # or released by the wait fold below it, which since #2854 releases no
    # order that was sent to a real broker — on conclusive identity evidence
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
        read=read,
        run=run,
    )


async def _observe_by_broker_order_id(
    broker: ClaimedBrokerIO, reducing: OrderResource
) -> BrokerOrder | BrokerError | None:
    """The reducing order read by the broker order id the Clerk recorded for it (#2854).

    Asked only when the order's exact lookup by client order id reads absent
    while the Clerk holds a broker order id for it: the broker acknowledged
    the order, or an operator pointed the Clerk at it
    (:func:`fold_exit_attested_order`), so "absent" is a lookup that does not
    show an order the broker has. Without this the order, working or filled,
    folded back to ``unknown`` on every pass.

    The answer is evidence only for the order it names: one that carries
    another client order id is an error, never this order's state.
    """
    assert reducing.broker_order_id is not None
    observed = await broker.observe_broker_order(reducing.broker_order_id, order_ref=reducing.order_ref)
    if isinstance(observed, BrokerOrder) and observed.client_order_id != reducing.client_order_id:
        return BrokerError(
            f"broker order {reducing.broker_order_id!r} carries "
            f"client_order_id={observed.client_order_id!r}, expected {reducing.client_order_id!r}"
        )
    return observed


def _attested_order_as_recorded(
    repo: ClerkSqliteRepository, reducing: OrderResource
) -> BrokerOrder | None:
    """The reducing order as the Clerk read it for an operator's attestation, when that read was its end (#2854).

    For an order taken up by its broker order id
    (:func:`fold_exit_attested_order`) whose evidence was not all folded
    before the process stopped, once the broker answers that it has no order
    under that id. The attestation's own row records what the Clerk read of
    the order: its state, its cumulative filled quantity, the average price
    of that, and the broker's time for it. An order that had ended does
    nothing more, so that read is its whole outcome, and it is folded from
    the row exactly as it would have been from the answer: the fill is
    credited by its difference from what is already recorded, so never
    twice.

    ``None``, and the order stays ``unknown`` for the next pass to ask the
    broker about again, for an order no attestation took up under this
    broker order id, for one that was still working when it was read -- what
    it did since is not on the row -- and for a row that records a fill and
    no price for it.

    Asked only after the broker answered that it has no such order. A read
    that failed is not that answer, and is retried.
    """
    row = repo.last_order_transition(
        order_ref=reducing.order_ref, transition_kind="ORDER_OUTCOME_ATTESTED"
    )
    if row is None:
        return None
    facts = OrderOutcomeAttestedFacts.from_facts_json(row["facts_json"])
    if facts.attestation != "exists" or facts.broker_order_id != reducing.broker_order_id:
        return None
    assert facts.broker_state is not None and facts.broker_filled_quantity is not None
    if not _is_terminal(facts.broker_state):
        return None
    if facts.broker_filled_quantity > 0 and facts.broker_filled_avg_price is None:
        return None
    created = _reducing_order_facts(repo, reducing.order_ref)
    return BrokerOrder(
        broker=BROKER_ID,
        order_id=facts.broker_order_id,
        client_order_id=reducing.client_order_id,
        symbol=facts.symbol,
        asset_class=None,
        side=facts.side.lower(),
        order_type=created.order_type,
        time_in_force=created.time_in_force,
        quantity=facts.quantity,
        filled_quantity=facts.broker_filled_quantity,
        limit_price=created.limit_price,
        stop_price=None,
        extended_hours=created.extended_hours,
        filled_avg_price=facts.broker_filled_avg_price,
        status=facts.broker_state,
        submitted_at_ms=None,
        created_at_ms=None,
        updated_at_ms=facts.broker_updated_at_ms,
        filled_at_ms=None,
        canceled_at_ms=None,
        expired_at_ms=None,
        observed_at_ms=facts.broker_observed_at_ms,
    )


async def _submit_reducing_order(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    reducing: OrderResource,
    broker: ClaimedBrokerIO,
    pricing: RecoveryPricing,
    read: BrokerReadPort | None,
    run: OffLoop,
) -> None:
    created, checked = await run(lambda: (
        _reducing_order_facts(repo, reducing.order_ref),
        _send_is_checked_against_the_account(
            _accepted_facts(repo, effect_operation_id), broker.trade
        ),
    ))
    uncovered: LegRefusal | None = None
    if checked:
        # Read here, on the event loop and outside intake, where the submit
        # below also runs: the guarded read port refuses contact under the
        # fence. With no read port nothing is read, and the rule refuses: a
        # Flatten never goes to a real broker unchecked (#2839).
        account = None if read is None else await broker.observe_open_orders_then_positions(read)
        # Its own order is looked for among the open orders first, whatever
        # the positions read after them answered (#2839).
        if account_lists_order(account, client_order_id=reducing.client_order_id):
            await run(
                lambda: _retain_flatten_already_at_broker(
                    repo, effect_operation_id=effect_operation_id, reducing=reducing
                )
            )
            return
        # The verdict is applied inside the one run that records the send.
        reductions = (
            order_reductions(created.the_option_order) if created.the_option_order is not None
            else ((created.symbol, OrderSide(created.side.lower()), created.quantity),)
        )
        uncovered = next((
            refusal for symbol, side, quantity in reductions
            if (refusal := flatten_cover_refusal(symbol=symbol, side=side, quantity=quantity, observed=account)) is not None
        ), None)
    now_ms = repo.clock()
    liveness = pricing.read_liveness(created.symbol, now_ms)

    def _prepare_submit() -> BrokerOrderLeg | OptionOrderRequest | None:
        facts = _reducing_order_facts(repo, reducing.order_ref)
        try:
            option_close = recorded_option_close(repo, effect_operation_id, facts)
        except ValueError as exc:
            # The books do not hold what one closing order can close: nothing is sent.
            _fold_submit_refused(
                repo, effect_operation_id=effect_operation_id, reducing=reducing,
                refusal=BrokerRefusal(why=f"The Clerk could not make the order that closes this option position: {exc}"),
            )
            return None
        leg: BrokerOrderLeg | OptionOrderRequest = (
            option_close.request
            if option_close is not None
            else BrokerOrderLeg(
                symbol=facts.symbol,
                side=facts.side.lower(),
                quantity=facts.quantity,
                order_type=facts.order_type,
                time_in_force=facts.time_in_force,
                limit_price=facts.limit_price,
                extended_hours=facts.extended_hours,
            )
        )
        # Sent before (#2845): the lookup that read it absent was made before
        # the account was read, so its absence is read again here, first --
        # before any rule below can fold the EXIT over an order that arrived.
        # Whether its latest send was answered under another client order id
        # is read here too, as the pass that found it absent read it.
        sent_before = repo.has_order_transition(
            order_ref=reducing.order_ref, transition_kind="ORDER_SUBMIT_REQUESTED"
        )
        if sent_before and (
            _retain_answered_as_another_order(
                repo, effect_operation_id=effect_operation_id, reducing=reducing
            )
            or not _resend_still_proven_absent(
                repo, effect_operation_id=effect_operation_id, reducing=reducing
            )
        ):
            return None
        # Sent before to a real broker, the order may be there: where the
        # rules below would fail the EXIT, they keep its custody (#2854). A
        # simulated port fills inside the send, so its order is judged as one
        # never sent is.
        sent_to_broker = sent_before and not trade_port_folds_simulated_evidence(broker.trade)
        # A resubmission after an outage or a restart replays the leg created
        # earlier; the clock may have moved past where it could be sent.
        if not _created_leg_may_be_sent(
            repo,
            effect_operation_id=effect_operation_id,
            created=facts,
            order_ref=reducing.order_ref,
            liveness=liveness,
            sent_to_broker=sent_to_broker,
        ):
            return None
        # The account does not cover an operator's Flatten (#2839). Refused
        # only once the send-time rule would send it, so a flat position or an
        # ended session is still told as itself.
        if uncovered is not None:
            if sent_to_broker:
                _hold_uncovered_resend(
                    repo, effect_operation_id=effect_operation_id, reducing=reducing, refusal=uncovered
                )
            else:
                _fold_flatten_uncovered(
                    repo, effect_operation_id=effect_operation_id, reducing=reducing, refusal=uncovered
                )
            return None
        # An EXIT with no decision bar is priced by the port before it is sent:
        # a no-submit world binds the price it fills at, or nothing goes out.
        # No order sent before to a real broker is refused here: a real port
        # binds every recovery, and refuses a run-end close on its first send.
        if option_close is not None and not trade_port_folds_simulated_evidence(broker.trade):
            # A real account's close is a market order the broker fills: it needs no
            # quote of ours, as a stock decision's sale needs no bar (ADR 0076).
            bound, why = True, ""
        elif option_close is not None:
            # A simulated close of an option contract is priced from a quote, never
            # a bar, whatever made the EXIT: the newest kept for the contract.
            bound = all(
                broker.bind_option_close_quotes(
                    reducing.client_order_id, symbol=symbol, side=side.value,
                    quotes=option_close.quotes.get(symbol, ()), usable_generation=option_close.usable_generation,
                )
                for symbol, side, quantity in order_reductions(option_close.request)
            )
            why = (
                f"{OPTION_EXIT_UNPRICED}: the simulation would not price a close of {facts.symbol}. Nothing "
                "kept for it by now can be closed at; no order was sent."
            )
        else:
            match _exit_intent(_accepted_facts(repo, effect_operation_id)):
                case "run_end_close":
                    bound = broker.bind_run_end_close_bar(reducing.client_order_id, symbol=facts.symbol)
                    why = "The simulation has no price its run saw to close this position at; no order was sent."
                case "recovery":
                    assert isinstance(leg, BrokerOrderLeg)
                    bound = broker.bind_latest_recovery_bar(
                        reducing.client_order_id, symbol=facts.symbol, side=leg.side,
                    )
                    why = "The simulation had no current price to fill this recovery at; no order was sent."
                case "decision":
                    bound, why = True, ""
        if not bound:
            _fold_submit_refused(
                repo, effect_operation_id=effect_operation_id, reducing=reducing, refusal=BrokerRefusal(why=why),
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
                notice=_lost_answer_after_a_notice(repo, reducing.order_ref),
            )
        )
        return
    except BrokerError as exc:
        refused = exc
        await run(
            lambda: _fold_send_refused(
                repo,
                effect_operation_id=effect_operation_id,
                reducing=reducing,
                leg=leg,
                error=refused,
            )
        )
        return
    if observed.client_order_id != reducing.client_order_id:
        await run(
            lambda: _fold_answered_as_another_order(
                repo, effect_operation_id=effect_operation_id, reducing=reducing, answer=observed
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


def _send_is_checked_against_the_account(facts: ExitAcceptedFacts, trade: BrokerTradePort) -> bool:
    """Whether this EXIT's reducing order is set against the broker's account before it is sent (#2839).

    Only an operator's Flatten -- the safe flatten, its confirmed limit and
    each leg of a cohort Flatten, all decided under
    ``RECOVERY_FLATTEN_DECISION_PREFIX`` -- going to a real broker. A Dry
    Run's or a Shadow's port fills inside ``submit`` and reaches no account,
    so it sends as before with no account read. Every other EXIT is outside
    the check: a deciding program's, a bot's scheduled end, a Dry Run's close,
    and the watchdog's re-drive, which is admitted by its own check of the
    same account (``exit_watchdog``).
    """
    return facts.decision_id.startswith(
        RECOVERY_FLATTEN_DECISION_PREFIX
    ) and not trade_port_folds_simulated_evidence(trade)


def _retain_flatten_already_at_broker(
    repo: ClerkSqliteRepository, *, effect_operation_id: str, reducing: OrderResource
) -> None:
    """A Flatten about to be sent again found its own order among the account's open orders (#2839).

    The exact lookup answered that the order never reached the broker, and
    the account read made for the send then listed it: it arrived in between.
    It is not sent a second time, and it is not a competing sale the Flatten
    is refused behind -- that fold would tell the operator nothing was sent
    while the order works. Custody is retained as an unknown outcome, so the
    next pass's exact lookup folds the order as any acknowledged one.
    """
    logger.warning(
        "an operator's flatten found its own order at the broker as it was about to be sent again",
        extra={
            "action": "flatten_resend_found_own_order",
            "account_id": repo.account_id,
            "effect_operation_id": effect_operation_id,
            "order_ref": reducing.order_ref,
        },
    )
    fold_uncertain(
        repo,
        effect_operation_id=effect_operation_id,
        order_ref=reducing.order_ref,
        why=(
            "The account's open orders, read before sending, list this reducing order; "
            "retaining custody until its exact lookup answers."
        ),
    )


def _fold_flatten_uncovered(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    reducing: OrderResource,
    refusal: LegRefusal,
) -> None:
    """The Clerk would not send an operator's Flatten the broker's account does not cover (#2839).

    Folded as a broker's own refusal is (:func:`_fold_submit_refused`; owner
    decision #2839): nothing reached the book, so the EXIT fails releasably
    through ``EXIT_NOT_FLAT`` -- the notice, the bell, and the episode the
    watchdog re-drives under its own check of the account. The fold records
    the refusal's own code and words, never a broker's: the broker was not
    asked. :func:`flatten_send_refusal` reads them back for the operator.
    """
    created = _reducing_order_facts(repo, reducing.order_ref)
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    held = repo.position(effect.strategy_instance_id, created.symbol)
    logger.warning(
        "an operator's flatten was not sent: the account check before the send refused it",
        extra={
            "action": "flatten_send_refused_by_account_check",
            "account_id": repo.account_id,
            "effect_operation_id": effect_operation_id,
            "order_ref": reducing.order_ref,
            "symbol": created.symbol,
            "reason_code": refusal.reason_code,
            "explanation": refusal.explanation,
            "attributed_qty": held,
        },
    )
    _fold_exit_not_flat(
        repo,
        effect_operation_id=effect_operation_id,
        order_ref=reducing.order_ref,
        symbol=created.symbol,
        attributed_qty=held,
        summary_code=refusal.reason_code,
        reason=refusal.explanation,
        headline=FLATTEN_COVER_HEADLINES[refusal.reason_code],
        explanation=(
            f"{refusal.explanation} The Clerk still attributes {held:g} {created.symbol} to this bot."
        ),
        next_step=refusal.next_step,
    )


def flatten_send_refusal(repo: ClerkSqliteRepository, effect_operation_id: str) -> LegRefusal | None:
    """The account check's refusal a failed Flatten recorded, or ``None`` for an EXIT that ended any other way (#2839).

    Read from the EXIT's own fold (:func:`_fold_flatten_uncovered`), so the
    Flatten that was refused and a retry of it are told the same thing.
    """
    row = repo.first_effect_transition(
        effect_operation_id=effect_operation_id, transition_kind="EXIT_NOT_FLAT"
    )
    if row is None or row["summary_code"] not in FLATTEN_COVER_NEXT_STEPS:
        return None
    return LegRefusal(
        reason_code=row["summary_code"],
        explanation=OrderSubmitFailedFacts.from_facts_json(row["facts_json"]).reason,
        next_step=FLATTEN_COVER_NEXT_STEPS[row["summary_code"]],
    )


def option_close_not_taken(
    repo: ClerkSqliteRepository, effect_operation_id: str, *, symbol: str
) -> LegRefusal | None:
    """Why a failed EXIT's close of an option contract was not taken, or ``None`` for a reduction in shares (ADR 0076).

    Read from the EXIT's own fold, as :func:`flatten_send_refusal` is, so
    the Flatten that failed and a retry of it are told the same thing. The
    Clerk itself, a Dry Run's simulation or a Paper account's broker may
    have refused it, and the recorded reason is in the refuser's own words.

    A contract that expired keeps its own code and its own next step,
    which sends the operator to no retry: none can work. Any other is
    told under the fold's code. Its next step does not promise that
    flattening again will work: a Flatten is offered again only when one
    can go out, and the bot's page says why when none can.
    """
    contract_symbol = symbol.upper()
    if not is_option_contract_symbol(contract_symbol):
        return None
    row = repo.first_effect_transition(
        effect_operation_id=effect_operation_id, transition_kind="EXIT_NOT_FLAT"
    )
    if row is None:
        return None
    recorded = OrderSubmitFailedFacts.from_facts_json(row["facts_json"])
    if row["summary_code"] == OPTION_CONTRACT_EXPIRED:
        return LegRefusal(
            reason_code=OPTION_CONTRACT_EXPIRED, explanation=recorded.reason, next_step=_EXPIRED_CONTRACT_NEXT_STEP
        )
    return LegRefusal(
        reason_code=row["summary_code"],
        explanation=(
            f"The order to close {contract_symbol} was not taken, so nothing was sold or bought and the "
            f"position is still open. {recorded.why}"
        ),
        next_step=(
            "Run Reconcile now. A Flatten is offered again only when one can go out, and the bot's page "
            "says why when none can."
        ),
    )


def _fold_market_hold(
    repo: ClerkSqliteRepository, effect_operation_id: str, order_ref: str,
    symbol: str, hold: LegRefusal,
) -> None:
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    previous = repo.last_strategy_transition(strategy_instance_id=effect.strategy_instance_id, transition_kind="EXIT_MARKET_HOLD")
    if previous is not None and previous["effect_operation_id"] == effect_operation_id and previous["summary_code"] == hold.reason_code:
        return
    repo.append_transition(TransitionInput(
        strategy_instance_id=effect.strategy_instance_id, run_id=effect.run_id,
        command_id=effect.command_id, effect_operation_id=effect_operation_id, order_ref=order_ref,
        transition_kind="EXIT_MARKET_HOLD", custody_owner="ACCOUNT_CLERK", execution_authority="ACCOUNT_CLERK",
        operation_state="in_progress", clerk_observed_at_ms=repo.clock(), summary_code=hold.reason_code,
        facts_json=canonicalize({"symbol": symbol, "explanation": hold.explanation, "next_step": hold.next_step}),
    ))


def _resend_still_proven_absent(
    repo: ClerkSqliteRepository, *, effect_operation_id: str, reducing: OrderResource
) -> bool:
    """Whether a reducing order about to be sent again still reads as never having reached the broker (#2845).

    The exact lookup answered that it never did, but the send is recorded in
    a later repository run: an operator's Flatten reads the account in
    between (#2839), and the stream may acknowledge or fill the order
    meanwhile. So the order is read again -- its broker identity is not taken
    from the caller's earlier read -- through ``order_never_reached_broker``,
    the one predicate that reads absence as an answer. When that no longer
    holds, nothing is recorded and nothing is sent: the EXIT is left as it
    is, and the next pass folds the order.
    """
    current = repo.order(reducing.order_ref)
    assert current is not None
    if order_never_reached_broker(repo, current):
        return True
    logger.warning(
        "a reducing order about to be sent again is no longer proven absent from the broker; it is not sent",
        extra={
            "action": "reducing_resend_withheld",
            "account_id": repo.account_id,
            "effect_operation_id": effect_operation_id,
            "order_ref": reducing.order_ref,
            "broker_order_id": current.broker_order_id,
        },
    )
    return False


def _was_sent_before_this_attempt(repo: ClerkSqliteRepository, order_ref: str) -> bool:
    """Whether the send just recorded for this reducing order was not its first (#2845).

    Every send records its own ``ORDER_SUBMIT_REQUESTED`` before it leaves,
    so an order sent before carries an earlier one than this attempt's.
    """
    first = repo.first_order_transition(order_ref=order_ref, transition_kind="ORDER_SUBMIT_REQUESTED")
    last = repo.last_order_transition(order_ref=order_ref, transition_kind="ORDER_SUBMIT_REQUESTED")
    if first is None or last is None:
        raise AssertionError(f"no recorded send for {order_ref!r}")
    return first["sequence"] != last["sequence"]


def _retain_refused_resend(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    reducing: OrderResource,
    error: BrokerError,
) -> None:
    """The broker refused a reducing order that had been sent before: custody is kept and the EXIT is not failed (#2845).

    No refusal of a send made again proves the earlier send never arrived.
    That send's answer was lost, and the exact lookup that then read the order
    absent may not show an order the broker has: the broker answers a client
    order id it already has with a refusal, and it refuses a sale whose shares
    an order already working holds. So this is not the refusal
    :func:`_fold_submit_refused` records, whatever its kind -- an invalid
    request, a conflict, an order not permitted, rejected credentials, a
    throttle. That fold fails the EXIT and releases the position, and the
    next EXIT -- the watchdog's re-drive, the bot's next decision -- would
    sell it again under a new client order id while this order works. Custody
    is retained as an unknown outcome instead. The next pass's exact lookup
    folds the order once the broker shows it; while it stays absent, the wait
    runs again from this answer, longer each time (:func:`resend_wait_ms`),
    and the same order is sent again while its session is open.

    What a first send's refusal records is not recorded here: the orders open
    on the other side of the symbol that the watchdog's re-drive waits behind
    (#2622), and the episode it re-drives. The order still goes out once that
    side clears, on the first send after it.

    The open notice says so, in Alpaca's own words (#2854): it was opened as
    a lost answer, and this send was answered.
    """
    created = _reducing_order_facts(repo, reducing.order_ref)
    logger.warning(
        "the broker refused a reducing order sent again; an earlier send may have arrived, so custody is kept",
        extra={
            "action": "reducing_resend_refused_custody_kept",
            "account_id": repo.account_id,
            "effect_operation_id": effect_operation_id,
            "order_ref": reducing.order_ref,
            "broker_error": type(error).__name__,
            "broker_error_code": error.code,
        },
    )
    fold_uncertain(
        repo,
        effect_operation_id=effect_operation_id,
        order_ref=reducing.order_ref,
        why=(
            "The broker refused this reducing order when it was sent again. An earlier send of "
            "it may have reached the broker, so the refusal does not prove nothing is working "
            "there. Custody is kept, and the order is sent again under the same client order id "
            f"while its exact lookup still reads it absent. The broker's answer: {error}"
        ),
        notice=UnknownOutcomeNotice(
            explanation=(
                f"Alpaca refused the order to {_the_reduction(created)} when the Clerk sent it again. "
                "An earlier send of this order may have reached Alpaca, so while the order's "
                "session is open the Clerk keeps custody of the position and sends the same order "
                "again, under the same client order id, for as long as Alpaca shows no order under "
                f"that id. Alpaca's answer: {error}"
            ),
            next_step=(
                "Look at this account's orders and position at Alpaca. While the order's session "
                "is open the Clerk sends the order again by itself, waiting longer after each "
                f"refusal (up to {RESEND_WAIT_CEILING_MS // 60_000} minutes), and takes it up as "
                "soon as Alpaca shows it."
            ),
        ),
    )


def _fold_send_refused(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    reducing: OrderResource,
    leg: BrokerOrderLeg | SingleOptionOrderRequest,
    error: BrokerError,
) -> None:
    """Fold the broker's outright refusal of a reducing order's send, by whether it was sent before (#2845).

    A first send's refusal is definitive: nothing was sent before, so no
    answer to it can be about an earlier order. The refusal of an order sent
    again never is (:func:`_retain_refused_resend`).
    """
    if _was_sent_before_this_attempt(repo, reducing.order_ref):
        _retain_refused_resend(
            repo, effect_operation_id=effect_operation_id, reducing=reducing, error=error
        )
        return
    _fold_submit_refused(
        repo,
        effect_operation_id=effect_operation_id,
        reducing=reducing,
        refusal=broker_refusal(repo, leg=leg, error=error),
    )


class _LatestSend(NamedTuple):
    """A reducing order's latest send, and what was first recorded of its answer."""

    requested: dict
    answer: dict | None


def _latest_send(repo: ClerkSqliteRepository, order_ref: str) -> _LatestSend | None:
    """This reducing order's latest send and its answer, or ``None`` for an order never sent (#2845).

    The send is its latest ``ORDER_SUBMIT_REQUESTED``, recorded before every
    send. The answer is the first ``ORDER_SUBMIT_UNCERTAIN`` after it: that
    send's lost or unusable answer, or the first pass to find the order
    absent when the Clerk stopped before it recorded one; ``None`` while none
    is recorded. Each read is one row off the order's index (#1942).
    """
    requested = repo.last_order_transition(
        order_ref=order_ref, transition_kind="ORDER_SUBMIT_REQUESTED"
    )
    if requested is None:
        return None
    answer = repo.first_order_transition(
        order_ref=order_ref,
        transition_kind="ORDER_SUBMIT_UNCERTAIN",
        after_sequence=requested["sequence"],
    )
    return _LatestSend(requested, answer)


def _answered_as_another_order_why(*, sent: str, answered_as: str) -> str:
    """Why an order answered under another client order id is not sent again, naming both ids (#2845)."""
    named = f"client order id {answered_as!r}" if answered_as else "no client order id"
    return (
        f"The broker answered the send of client order id {sent!r} with an order under {named}. "
        "An order may be at the broker under an identity other than the one sent, which sending "
        "the same client order id again could not guard against; this order is not sent again, "
        "and custody is kept."
    )


def _fold_answered_as_another_order(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    reducing: OrderResource,
    answer: BrokerOrder,
) -> None:
    """The broker answered this reducing order's send with an order that is not under its client order id (#2845).

    That answer is evidence an order may exist at the broker under another
    identity, so it is not folded as a lost answer is. A lost order is sent
    again once the id that was sent has read absent for the wait, and that is
    safe only because the broker accepts one client order id at most once:
    two different ids could both execute. So the
    answer is recorded on the uncertainty's own facts, where
    :func:`_retain_answered_as_another_order` reads it on every later pass,
    and the order is never sent again. The answer's broker order id is named
    and never taken as this order's: whose order it is is not known. An
    answer that names no client order id at all is recorded as the empty
    string: it does not name the one that was sent either.

    The open notice says the order will not be sent again, and names the
    order Alpaca answered with (#2854): the only thing an operator can do
    about it today is look at that order at Alpaca.
    """
    answered_as = answer.client_order_id or ""
    created = _reducing_order_facts(repo, reducing.order_ref)
    answered_with = (
        f"client order id {answered_as!r}" if answered_as else "no client order id"
    ) + (f", Alpaca order id {answer.order_id!r}" if answer.order_id.strip() else "")
    logger.error(
        "the broker answered a reducing order's send under another client order id; the order will not be sent again",
        extra={
            "action": "reducing_send_answered_as_another_order",
            "account_id": repo.account_id,
            "effect_operation_id": effect_operation_id,
            "order_ref": reducing.order_ref,
            "answered_as_client_order_id": answered_as,
            "answered_broker_order_id": answer.order_id,
        },
    )
    fold_uncertain(
        repo,
        effect_operation_id=effect_operation_id,
        order_ref=reducing.order_ref,
        why=(
            f"{_answered_as_another_order_why(sent=reducing.client_order_id, answered_as=answered_as)} "
            f"The answer's broker order id, {answer.order_id!r}, is not taken as this order's."
        ),
        answered_as_client_order_id=answered_as,
        notice=UnknownOutcomeNotice(
            explanation=(
                f"The Clerk sent the order to {_the_reduction(created)} under client order id "
                f"{reducing.client_order_id!r}, and Alpaca answered with an order under "
                f"{answered_with}. An order may be at Alpaca under that other identity, and "
                "sending the same client order id again could not guard against it, so the Clerk "
                "will not send this order again. It keeps custody of the position."
            ),
            next_step=(
                "Look at this account's orders at Alpaca for the order it answered with. The "
                "Clerk takes this order up by itself only if Alpaca shows one under the client "
                "order id that was sent."
            ),
        ),
    )


def _retain_answered_as_another_order(
    repo: ClerkSqliteRepository, *, effect_operation_id: str, reducing: OrderResource
) -> bool:
    """Keep custody of a reducing order whose latest send was answered under another client order id (#2845).

    Returns whether it was, having recorded that the order is still not
    sent. Both places that could send the order again ask this first -- the
    pass that finds it absent, and the run that would record the send -- so
    the two cannot disagree. Its exact lookup still folds the order if the
    broker ever shows it under the id that was sent.
    """
    latest = _latest_send(repo, reducing.order_ref)
    if latest is None or latest.answer is None:
        return False
    answered_as = OrderSubmitUncertainFacts.from_facts_json(
        latest.answer["facts_json"]
    ).answered_as_client_order_id
    if answered_as is None:
        return False
    fold_uncertain(
        repo,
        effect_operation_id=effect_operation_id,
        order_ref=reducing.order_ref,
        why=_answered_as_another_order_why(sent=reducing.client_order_id, answered_as=answered_as),
    )
    return True


def _fold_submit_refused(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    reducing: OrderResource,
    refusal: BrokerRefusal,
) -> None:
    """The broker refused the reducing order outright (a 4xx): fail the EXIT, and tell the operator.

    The refusal is definitive — nothing reached the book — so the EXIT fails
    releasably as before. While exposure is still held that is the
    ``EXIT_NOT_FLAT`` episode (#2440 review): a bare ``ORDER_SUBMIT_FAILED``
    raised no notice and no bell, and left the position open silently.
    A refusal that is not definitive never reaches this fold
    (:func:`_fold_send_refused`).
    """
    created = _reducing_order_facts(repo, reducing.order_ref)
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    held = repo.position(effect.strategy_instance_id, created.symbol)
    closes_an_option = is_option_contract_symbol(created.symbol.upper())
    # Who refused an option order is not named here: the Clerk itself, a simulation, or a
    # Paper account's broker (ADR 0076). The recorded reason says which, in the refuser's words.
    reason = "The closing order was not taken." if closes_an_option else "The reducing order did not reach the broker."
    if not position_quantity_is_nonzero(held):
        fold_failed(
            repo,
            effect_operation_id=effect_operation_id,
            order_ref=reducing.order_ref,
            summary_code="ORDER_SUBMIT_FAILED",
            reason=reason,
            why=refusal.why,
            refusal=refusal,
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
        why=refusal.why,
        refusal=refusal,
        headline=(
            "An order to close an option position was not taken; the position is still open"
            if closes_an_option
            else "The broker refused this exit's order; the position is still open"
        ),
        explanation=(
            (
                f"The order to {created.side.lower()} {created.quantity:g} {created.symbol} was not taken, so "
                f"nothing was closed; {held:g} {created.symbol} is still held. {refusal.why}"
            )
            if closes_an_option
            else (
                f"The broker refused the order to {created.side.lower()} {created.quantity:g} "
                f"{created.symbol}, so nothing was sent; {held:g} {created.symbol} is still "
                "held. The broker's reason is on the order's evidence."
            )
        ),
        next_step=_option_close_not_taken_next_step(repo) if closes_an_option else _REDRIVE_NEXT_STEP,
    )


ORDER_ATTESTED_ABSENT = "ORDER_ATTESTED_ABSENT"
"""How an EXIT ended whose closing order an operator attested Alpaca does not have (#2854)."""


class AttestedAbsentEpisodes(NamedTuple):
    """The two episodes an EXIT ends under when an operator attested Alpaca does not have its closing order (#2854)."""

    exit_not_flat: UncertaintyRaisedFacts
    exit_stuck: UncertaintyRaisedFacts


def attested_absent_exit_episodes(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    order_ref: str,
    broker_qty: float,
    account_attributed_qty: float,
    observed_at_ms: int,
) -> AttestedAbsentEpisodes | None:
    """The episodes an EXIT ends under when an operator attested Alpaca does not have its closing order (#2854).

    The caller has read the broker itself and found no such order and none
    working on the symbol (``unknown_order_attestation``), and records both
    envelopes on the one transition that ends the EXIT: the order is ended
    as not sent and the EXIT fails releasably (owner decision on #2854), so
    its entry is freed. Nothing is written here.

    No read of the account shows whether the order traded, so the position
    is left to the operator. ``exit_not_flat`` says the position is still
    open. ``exit_stuck`` is the episode the watchdog escalates to once its
    re-drives have failed (``exit_watchdog``), opened here with no re-drive
    tried: under it the Clerk sends no order for the position by itself.
    The operator sells the position, with the Flatten's own check of the
    account, or writes it off; either leaves the bot attributed-flat, which
    resolves both episodes as it resolves any stuck exit.

    The attributed quantity is never changed by this. ``broker_qty`` and
    ``account_attributed_qty`` -- the broker's position in the symbol and the
    account-wide attribution, as the caller read them -- are told, and are
    no proof of anything. Each quantity is said by its own sign, a short as a
    short, and what to do is said by the order's side: a closing sale is
    made again by selling, a cover by buying back.

    ``None`` for an EXIT whose bot holds nothing in the symbol: it has
    nothing left to exit and ends attributed-flat instead, citing the same
    order, as a resubmission does (:func:`_created_leg_may_be_sent`).
    """
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    created = _reducing_order_facts(repo, order_ref)
    held = repo.position(effect.strategy_instance_id, created.symbol)
    if not position_quantity_is_nonzero(held):
        return None
    symbol = created.symbol
    covers = created.side.upper() == "BUY"
    still = f"{held_text(held, symbol)} is still {'held' if held > 0 else 'open'}"
    sends_nothing = f"sends no order for {'it' if covers else 'them'} by itself"
    cure = (
        "Buy it back or write it off from the bot's page."
        if covers
        else "Sell them or write them off from the bot's page."
    )
    exit_not_flat = uncertainty_envelope(
        **_exit_not_flat_notice(
            order_ref=order_ref,
            symbol=symbol,
            attributed_qty=held,
            headline="A closing order was settled as not sent; the position is still open",
            explanation=(
                f"The Clerk could not confirm the order to {_the_reduction(created)}. An operator "
                "said Alpaca has no such order, the Clerk's own reads of Alpaca found no order under "
                f"its client order id and none working on {symbol}, and the exit was ended as not "
                f"sent; {still}. When the Clerk read the account, Alpaca held "
                f"{held_text(broker_qty, symbol)} and the Clerk's records said "
                f"{held_text(account_attributed_qty, symbol)}; that does not show whether the order "
                f"traded. The Clerk {sends_nothing}."
            ),
            next_step=(
                "Look at this account's position at Alpaca. "
                + (
                    "If Alpaca is not short these shares, write off the short the Clerk records; "
                    "otherwise buy back what is short. Both are on the bot's page."
                    if covers
                    else "If Alpaca does not hold these shares, write off the missing shares; "
                    "otherwise sell what is held. Both are on the bot's page."
                )
            ),
        )
    )
    stuck_reason_code = reason_age_policy(EXIT_NOT_FLAT_REASON_CODE, RedriveThenEscalate).escalate_to
    carried = _open_exit_episode(repo, effect.strategy_instance_id, EXIT_NOT_FLAT_REASON_CODE)
    standing = _standing_stuck_cause(repo, effect.strategy_instance_id, stuck_reason_code)
    exit_stuck = uncertainty_envelope(
        reason_code=stuck_reason_code,
        headline="A closing order was settled as not sent; the Clerk sends no order for the position by itself",
        explanation=(
            f"An operator said Alpaca does not have this bot's order to {_the_reduction(created)}, "
            f"and the Clerk ended this exit as not sent. It holds {held_text(held, symbol)} for the "
            f"bot and {sends_nothing}."
        ),
        operator_impact=(
            "New exposure stays on hold for this strategy, and the Clerk sends no order for this "
            "position by itself. Exact operator reduction and the write-off remain available."
        ),
        next_step=cure,
        evidence_refs=(order_ref,),
        cause_facts=ExitStuckCause(
            symbol=symbol.upper(),
            attributed_qty=held,
            redrive_count=0 if standing is None else standing.redrive_count,
            first_observed_at_ms=(
                standing.first_observed_at_ms
                if standing is not None
                else observed_at_ms if carried is None else carried["observed_at_ms"]
            ),
            attested_absent_order_ref=order_ref,
        ).to_mapping(),
        severity="error",
    )
    return AttestedAbsentEpisodes(exit_not_flat=exit_not_flat, exit_stuck=exit_stuck)


def _open_exit_episode(
    repo: ClerkSqliteRepository, strategy_instance_id: str, reason_code: str
) -> dict[str, Any] | None:
    return repo.active_uncertainty(
        scope="CUSTODY_SUBJECT", reason_code=reason_code, strategy_instance_id=strategy_instance_id
    )


def _standing_stuck_cause(
    repo: ClerkSqliteRepository, strategy_instance_id: str, stuck_reason_code: str
) -> ExitStuckCause | None:
    """The cause of the bot's open stuck-EXIT episode, when it has one this build can read.

    An attestation refreshes an episode that is already open: how many
    re-drives it had seen, and since when, stay as recorded.
    """
    open_stuck = _open_exit_episode(repo, strategy_instance_id, stuck_reason_code)
    if open_stuck is None:
        return None
    try:
        return ExitStuckCause.from_mapping(
            UncertaintyRaisedFacts.from_facts_json(open_stuck["facts_json"]).cause_facts
        )
    except (TypeError, ValueError, KeyError):
        # An episode this build cannot read is replaced whole.
        return None


def fold_exit_attested_order(
    repo: ClerkSqliteRepository, *, effect_operation_id: str, observed: BrokerOrder
) -> None:
    """Take up a closing order an operator pointed the Clerk at by its Alpaca order id (#2854).

    The caller read the order by that id and checked it is this EXIT's own:
    it carries the reducing order's client order id, symbol, side and
    quantity (``unknown_order_attestation``). From here it is folded exactly
    as the answer of the order's own exact lookup is
    (:func:`_fold_observed_reducing_order`), and the EXIT is finalized on it.
    A working order is followed. A fill is credited from the order's
    cumulative total, by its difference from what is already recorded, so
    never twice. An order that ended unfilled fails the EXIT through
    ``EXIT_NOT_FLAT``.

    The attestation's own transition already recorded the order's broker
    order id on the order, before any of this is written. So should the
    process stop between the writes made here, each of which commits alone,
    the next pass reads the order by that id while its client-id lookup still
    reads absent (:func:`_observe_by_broker_order_id`) and folds what is
    left; nothing here is lost with it. Should the broker answer that pass
    that it has no order under the id, an order that had ended when the
    attestation read it is finished from the attestation's own record
    (:func:`_attested_order_as_recorded`).
    """
    reducing = _read_exit_claim_state(repo, effect_operation_id).reducing
    if reducing is None or observed.client_order_id != reducing.client_order_id:
        raise AssertionError(
            f"broker order {observed.order_id!r} is not EXIT {effect_operation_id!r}'s reducing order"
        )
    _fold_observed_reducing_order(
        repo,
        effect_operation_id=effect_operation_id,
        reducing=reducing,
        observed=observed,
        simulated_authority=False,
    )
    # Read again: the fold above settled the unknown outcome this EXIT was in.
    _finalize_claimed_exit(
        repo, effect_operation_id, _read_exit_claim_state(repo, effect_operation_id), reducing
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


type _ExitIntent = Literal["decision", "recovery", "run_end_close"]


def _exit_intent(facts: ExitAcceptedFacts) -> _ExitIntent:
    """What an EXIT is, read once from the decision id it was accepted under.

    ``decision`` -- a deciding program's or the panel's own EXIT, bound to its
    decision bar. ``recovery`` -- the operator's safe flatten, the watchdog's
    re-drive or the Clerk's sale at a bot's owner-scheduled end, priced from
    the live quote (the last in the regular session only, as its acceptance
    records). ``run_end_close`` -- a Dry Run's simulation closing what an
    ended run left, at the last price it saw. The last two are recovery EXITs
    (``accept_recovery_exit``).
    """
    if facts.decision_id.startswith(DRY_RUN_CLOSE_DECISION_PREFIX):
        return "run_end_close"
    if facts.decision_id.startswith(_RECOVERY_DECISION_PREFIXES):
        return "recovery"
    return "decision"


def _append_order_phase(
    repo: ClerkSqliteRepository,
    effect_operation_id: str,
    order: OrderResource,
    transition_kind: str,
    *, cancel_reason: str | None = None,
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
            summary_code=cancel_reason or transition_kind,
            facts_json=OrderCancelRequestedFacts(cancel_reason).to_facts_json() if cancel_reason else canonicalize({}),
        )
    )


def _append_order_phase_once(
    repo: ClerkSqliteRepository,
    effect_operation_id: str,
    order: OrderResource,
    transition_kind: str,
    *, cancel_reason: str | None = None,
) -> None:
    if not repo.has_order_transition(order_ref=order.order_ref, transition_kind=transition_kind):
        _append_order_phase(repo, effect_operation_id, order, transition_kind, cancel_reason=cancel_reason)


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


def _the_reduction(created: ExitReducingOrderCreatedFacts) -> str:
    """A reducing order as a notice names it: its side, quantity and symbol."""
    return f"{created.side.lower()} {created.quantity:g} {created.symbol}"


RESEND_WAITS_AFTER_A_RESEND_MS: tuple[int, ...] = (60_000, 120_000)
"""How long a reducing order waits to be sent again after its second send, then after its third (#2854)."""

RESEND_WAIT_CEILING_MS = 300_000
"""How long it waits after its fourth send, and after every later one (#2854)."""

# Sends are counted this far and no further: every count from here on waits the ceiling.
_SENDS_COUNTED = len(RESEND_WAITS_AFTER_A_RESEND_MS) + 2


def resend_wait_ms(sends: int) -> int:
    """How long a reducing order sent ``sends`` times waits, from its latest send, before it may be sent again (#2854).

    The submit-absence wait itself after its first send -- and, for an order
    never sent, from its creation -- so an order whose one send was lost goes
    out again as soon as it did before the waits grew. Each later send waits
    longer: :data:`RESEND_WAITS_AFTER_A_RESEND_MS`, then
    :data:`RESEND_WAIT_CEILING_MS` every time.

    A send made again can be refused for as long as its cause stands, and no
    number of refusals ends the sends. Only the send-time rule does
    (:func:`_created_leg_may_be_sent`): past the order's session nothing is
    sent again and custody is kept for an operator to settle
    (:func:`_hold_past_its_session`). So the sends back off instead of asking
    the broker twice a minute until then. Never shorter than the
    submit-absence wait, whatever that is set to.
    """
    base_ms = submit_absence_grace_ms()
    if sends <= 1:
        return base_ms
    later = sends - 2
    return max(
        base_ms,
        RESEND_WAITS_AFTER_A_RESEND_MS[later]
        if later < len(RESEND_WAITS_AFTER_A_RESEND_MS)
        else RESEND_WAIT_CEILING_MS,
    )


def _absence_grace_elapsed(repo: ClerkSqliteRepository, order_ref: str) -> bool:
    """Whether the wait since this reducing order's latest send has passed (#2845).

    An order whose submit answer was lost may have reached the broker and not
    yet show in an exact lookup; sent again inside that window, it could sell
    twice. So the wait runs from the latest send: from its
    ``ORDER_SUBMIT_REQUESTED``, recorded before every send, or from the first
    ``ORDER_SUBMIT_UNCERTAIN`` after it -- that send's lost answer, or the
    first pass to find the order absent when the Clerk stopped before it
    recorded one -- whichever was recorded with the later time.
    ``recorded_at_ms`` is wall time, so the later of the two is not always
    the later by sequence: a host clock that steps backwards between them
    must not shorten the wait. An order never sent waits from its creation.

    How long the wait is depends on how many times the order has been sent
    (:func:`resend_wait_ms`, #2854). That is the count of its
    ``ORDER_SUBMIT_REQUESTED`` records, not a clock reading, so a clock step
    cannot shorten the wait that way either.

    No later ``ORDER_SUBMIT_UNCERTAIN`` moves the wait. Those are this
    machine's own records that the order is still absent: each pass writes
    one, and the passes come more often than the wait is long, so a wait
    measured from the newest never ended and the order was never sent again
    while the passes kept running. Each read is one row off the order's
    index, and the count stops at the few sends it tells apart (#1942).
    """
    latest = _latest_send(repo, order_ref)
    if latest is None:
        created = repo.first_order_transition(
            order_ref=order_ref, transition_kind="EXIT_REDUCING_ORDER_CREATED"
        )
        if created is None:
            raise AssertionError(f"no reducing-order creation transition for {order_ref!r}")
        anchor_ms = created["recorded_at_ms"]
        sends = 0
    else:
        anchor_ms = send_wait_from_ms(
            sent_at_ms=latest.requested["recorded_at_ms"],
            answer_recorded_at_ms=None if latest.answer is None else latest.answer["recorded_at_ms"],
        )
        sends = repo.count_order_transitions(
            order_ref=order_ref, transition_kind="ORDER_SUBMIT_REQUESTED", at_most=_SENDS_COUNTED
        )
    return repo.clock() - anchor_ms >= resend_wait_ms(sends)


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


__all__ = [
    "ENTRY_CLERK_CLOSED",
    "ENTRY_TERMINAL_PROOF",
    "ENTRY_WAIT_ENDED",
    "attested_absent_exit_episodes",
    "cancel_and_prove_owned_entry",
    "flatten_send_refusal",
    "fold_exit_attested_order",
    "priced_reduction_reference_price",
    "resolve_exit",
]
