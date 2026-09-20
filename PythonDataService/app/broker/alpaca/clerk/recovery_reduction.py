"""Shape an operator's recovery reduction from the current instant (#2007).

A program leg is shaped from its decision bar
(``program_leg.shape_program_leg``). An operator's safe flatten has no deciding
bar, so it is shaped from *now*:

* inside the regular session it is the market DAY leg every EXIT has always
  been;
* inside the broker's declared PRE or POST window it is an extended-hours DAY
  limit whose price the operator confirms against IBKR's live bid and ask;
* anywhere else it is refused, and the refusal names when the next session
  opens.

Nothing here builds a market order for the vendor to queue to the next open
(owner decision 2026-09-19): outside a session no reduction is sent at all.
The suggested price is only a suggestion — the operator may edit it — so what
reaches the broker is the confirmed price, never one recomputed here.

Formula (the suggested limit, and the band a confirmed limit must stay inside):
    suggested sell: floor_tick( bid × (1 − exit_bps / 10⁴) )
    suggested buy:  ceil_tick(  ask × (1 + exit_bps / 10⁴) )
    band sell:      limit ≥ floor_tick( bid × (1 − k · exit_bps / 10⁴) )
    band buy:       limit ≤ ceil_tick(  ask × (1 + k · exit_bps / 10⁴) )
    where k is the deploy-time band multiple — ALPACA_LIVE_XH_EXIT_BAND_MULTIPLE,
    default RECOVERY_BAND_ALLOWANCE_MULTIPLE — and bid/ask are the Clerk's
    live quote at send.
Formula (realized slippage of a fill, positive = worse than the reference):
    sell: (reference_bid − fill_price) / reference_bid × 10⁴ bps
    buy:  (fill_price − reference_ask) / reference_ask × 10⁴ bps
    cost: the same numerator in dollars × |quantity|
Formula (what a proposed limit does against the touch — bid to sell, ask to cover):
    through the book: (touch − limit) / touch × 10⁴ bps for a sell,
                      (limit − touch) / touch × 10⁴ bps to cover
                      (negative rests behind the touch instead of reaching through)
    worst case:       max(0, touch − limit) × |quantity| for a sell,
                      max(0, limit − touch) × |quantity| to cover
    spread:           (ask − bid) / mid × 10⁴ bps
Reference: docs/references/alpaca-extended-hours.md § "Operator flatten outside
    the regular session"; owner decisions 2026-09-19 on #2007.
Canonical implementation: this file for the session rule;
    ``app/broker/alpaca/marketable_limit.py::marketable_limit_price`` for the
    price.
Validated against: tests/broker/alpaca/clerk/test_recovery_reduction.py
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from pydantic import ValidationError

from app.broker.alpaca.clerk.program_leg import (
    EXTENDED_HOURS_ALLOWANCE_UNSET,
    LegRefusal,
    LegShape,
    ProgramLegPolicy,
    ProgramLegRefused,
)
from app.broker.alpaca.marketable_limit import marketable_limit_price
from app.broker.contract.models import OrderSide, OrderType, TimeInForce
from app.schemas.market_liveness import TopOfBookQuote
from app.services.session_authority import (
    TRADEABLE_EXTENDED_PHASES,
    SessionAuthorityState,
    session_state_at_ms,
)

# Where the live bid/ask comes from: ``(symbol, now_ms) -> quote or None``.
# Injected so the pricing seam is a pure function and a test can state the
# quote; production reads the market-liveness store's IBKR top of book.
type QuoteSource = Callable[[str, int], TopOfBookQuote | None]

RECOVERY_QUOTE_MAX_AGE_MS = 10_000
"""How old the quote an operator confirmed against may be when they send.

Owner decision 2026-09-19: send the confirmed price, but ask for a refresh if
the bid/ask the operator looked at is more than about ten seconds old.
"""

RECOVERY_BAND_ALLOWANCE_MULTIPLE = Decimal(2)
"""The declared default band multiple: how far through the book a confirmed
limit may go, in multiples of the sealed exit allowance.

Owner decision 2026-09-19: refuse a price more than twice the allowance past
the bid (sell) or ask (cover), so a typo cannot sweep a thin after-hours book.
Deploy-time configurable since #2229 — ``ALPACA_LIVE_XH_EXIT_BAND_MULTIPLE``,
carried on ``ExtendedHoursAllowances.exit_band_multiple`` — and this constant
is the value every unset deployment prices the band at.
"""

RECOVERY_SPREAD_WARNING_BPS = 50
"""A bid-ask spread wider than this, in bps of the mid, is flagged before sending.

Owner decision 2026-09-19. Presentation only: the Clerk sends it to the
operator's ticket rather than the browser holding its own number.
"""

RECOVERY_QUOTE_UNAVAILABLE = LegRefusal(
    reason_code="RECOVERY_QUOTE_UNAVAILABLE",
    explanation="No live IBKR bid and ask is available for this symbol, so an extended-hours limit cannot be priced.",
    next_step="Wait a few seconds for the IBKR subscription, then prepare the flatten again.",
)

RECOVERY_QUOTE_UNPRICEABLE = LegRefusal(
    reason_code="RECOVERY_QUOTE_UNPRICEABLE",
    explanation=(
        "The live quote and the sealed exit allowance price this reduction at or "
        "below zero, which is not a submittable limit."
    ),
    next_step="Wait for the regular session, when the reduction is a market order.",
)

RECOVERY_QUOTE_STALE = LegRefusal(
    reason_code="RECOVERY_QUOTE_STALE",
    explanation="The bid and ask you confirmed against are more than ten seconds old.",
    next_step="Refresh the quote, check the limit price, and confirm again.",
)

RECOVERY_LIMIT_REQUIRED = LegRefusal(
    reason_code="RECOVERY_LIMIT_REQUIRED",
    explanation=(
        "Outside the regular session a flatten is a limit order, and no confirmed limit price came with this request."
    ),
    next_step="Prepare the flatten, confirm a limit price, and send it again.",
)

RECOVERY_LIMIT_PRICE_INVALID = LegRefusal(
    reason_code="RECOVERY_LIMIT_PRICE_INVALID",
    explanation=(
        "The confirmed limit price is not one Alpaca accepts: it must be positive, "
        "with at most 2 decimal places at or above $1 and 4 below."
    ),
    next_step="Correct the limit price and confirm again.",
)

RECOVERY_SESSION_CHANGED = LegRefusal(
    reason_code="RECOVERY_SESSION_CHANGED",
    explanation=(
        "The regular session opened after this limit was confirmed; a regular-session flatten is a market order."
    ),
    next_step="Prepare the flatten again.",
)


def no_session_open(opens_at_ms: int | None) -> LegRefusal:
    """No session the active authority trades is open now."""
    return LegRefusal(
        reason_code="NO_SESSION_OPEN",
        explanation="No trading session is open now, so no reduction can be sent.",
        next_step="Flatten again once the next session opens.",
        available_at_ms=opens_at_ms,
    )


RECOVERY_LIMIT_OUTSIDE_BAND = LegRefusal(
    reason_code="RECOVERY_LIMIT_OUTSIDE_BAND",
    explanation=(
        "The limit is further through the book than twice the sealed exit allowance "
        "from the live bid (sell) or ask (cover)."
    ),
    next_step="Check the price against the live quote and confirm again.",
)

RECOVERY_LIMIT_SESSION_ENDED = LegRefusal(
    reason_code="RECOVERY_LIMIT_SESSION_ENDED",
    explanation=(
        "The session this limit was confirmed in ended before it could be sent; a "
        "confirmed price is never carried into another session."
    ),
    next_step="Flatten again in the current session.",
)

RECOVERY_LIMIT_QUANTITY_CHANGED = LegRefusal(
    reason_code="RECOVERY_LIMIT_QUANTITY_CHANGED",
    explanation=(
        "The position changed after this price was confirmed, so the reduction is no "
        "longer the one the operator reviewed."
    ),
    next_step="Review the new quantity and confirm a price for it.",
)

RECOVERY_MARKET_WAIT_ENDED = LegRefusal(
    reason_code="RECOVERY_MARKET_WAIT_ENDED",
    explanation=(
        "The regular session ended while this unpriced market reduction waited; it is "
        "not queued to the next open, and the entry it held is released for a priced "
        "reduction."
    ),
    next_step=(
        "Send the operator's priced flatten in the extended session, or wait for the "
        "automatic re-drive, which prices a limit itself."
    ),
)

type ExtendedPhase = Literal["PRE", "POST"]
type RecoveryLegVerdict = Literal["send", "wait", "expired"]
"""May a recovery EXIT's reducing leg go to the broker now?

``send`` — yes. ``wait`` — a market leg outside the regular session: hold it,
the next pass inside the regular session sends it. ``expired`` — an
operator-confirmed limit past the end of the session it was priced in: it is
never sent, and the EXIT fails so the operator can price again.
"""


@dataclass(frozen=True)
class RegularSessionReduction:
    """Inside the regular session: the market DAY leg; no quote is needed."""


@dataclass(frozen=True)
class ExtendedLimitProposal:
    """What the operator is shown before confirming an extended-hours flatten."""

    phase: ExtendedPhase
    side: OrderSide
    quote: TopOfBookQuote
    exit_allowance_bps: Decimal
    suggested_limit_price: Decimal
    # The furthest-through-the-book price the Clerk accepts against this quote.
    band_limit_price: Decimal


@dataclass(frozen=True)
class ProposedLimitEvaluation:
    """What a specific price the operator proposes would do against the Clerk's quote."""

    limit_price: Decimal
    # Positive reaches through the touch; negative rests behind it.
    through_book_bps: float
    # Every share filling at the limit, measured against the touch.
    worst_case_cost: float
    outside_band: bool
    # The quantity is larger than the size showing at the touch.
    thin_book: bool
    resting: bool


type RecoveryReductionPricing = RegularSessionReduction | ExtendedLimitProposal


@dataclass(frozen=True)
class ConfirmedRecoveryLimit:
    """The limit price the operator confirmed, and the quote they confirmed it against."""

    limit_price: Decimal
    quote_observed_at_ms: int


@dataclass(frozen=True)
class ConfirmedRecoveryShape:
    """The operator's confirmed extended-hours leg, and when it stops being sendable.

    ``valid_until_ms`` is the end of the session the price was confirmed in:
    09:30 for a pre-market confirmation, the declared close for after-hours.
    """

    shape: LegShape
    valid_until_ms: int
    # The Clerk's live quote at send: the reference realized slippage is
    # measured from, at most ten seconds newer than the one the operator saw.
    reference_quote: TopOfBookQuote
    # The reduction this price was confirmed for. Cancellation resolves the
    # real quantity later; a different one is not what the operator reviewed.
    quantity: float


def regular_session_open(now_ms: int) -> bool:
    """Is ``now_ms`` inside the canonical calendar's regular session (half-days included)?"""
    return session_state_at_ms(now_ms=now_ms).phase == "RTH"


def flatten_session(*, now_ms: int, policy: ProgramLegPolicy) -> SessionAuthorityState:
    """The session an operator's flatten would go out in at ``now_ms``.

    The calendar's regular session widened by the window the broker declares
    -- the same window an extended-session program leg is shaped against.
    """
    return session_state_at_ms(now_ms=now_ms, extended_window=policy.window)


def price_recovery_reduction(
    *,
    side: OrderSide,
    now_ms: int,
    policy: ProgramLegPolicy,
    quote: TopOfBookQuote | None,
) -> RecoveryReductionPricing:
    """How a reduction of ``side`` would go out right now, for the operator to confirm.

    Raises ``ProgramLegRefused`` when none can: no open session, no allowance
    to suggest a price from, no live quote, or a quote that prices to zero.
    """
    state = _tradeable_state(now_ms=now_ms, policy=policy)
    if state.phase == "RTH":
        return RegularSessionReduction()
    if policy.allowances is None:
        raise ProgramLegRefused(EXTENDED_HOURS_ALLOWANCE_UNSET)
    if quote is None:
        raise ProgramLegRefused(RECOVERY_QUOTE_UNAVAILABLE)
    return ExtendedLimitProposal(
        phase=_extended_phase(state),
        side=side,
        quote=quote,
        exit_allowance_bps=policy.allowances.exit_bps,
        suggested_limit_price=_through_the_book(
            side, quote, policy.allowances.exit_bps
        ),
        band_limit_price=_through_the_book(
            side, quote, policy.allowances.exit_bps * policy.allowances.exit_band_multiple
        ),
    )


def recovery_reduction_shape(
    *,
    side: OrderSide,
    symbol: str,
    quantity: float,
    now_ms: int,
    policy: ProgramLegPolicy,
    confirmed: ConfirmedRecoveryLimit | None,
    current_quote: TopOfBookQuote | None,
) -> ConfirmedRecoveryShape | None:
    """The shape the operator's confirmed flatten reduces with.

    ``None`` is the regular-session market DAY leg — the shape a recovery EXIT
    with no recorded shape already builds. An extended-session shape is the
    operator's own price, checked here, before any EXIT is accepted, against
    the clock, the live quote the Clerk holds now, and Alpaca's precision rule.

    The confirmed quote must be one the Clerk could have served: no later
    than the quote it holds now, and no more than ten seconds old. A client
    cannot vouch for freshness the server cannot see.
    """
    state = _tradeable_state(now_ms=now_ms, policy=policy)
    if state.phase == "RTH":
        if confirmed is not None:
            raise ProgramLegRefused(RECOVERY_SESSION_CHANGED)
        return None
    if confirmed is None:
        raise ProgramLegRefused(RECOVERY_LIMIT_REQUIRED)
    if policy.allowances is None:
        raise ProgramLegRefused(EXTENDED_HOURS_ALLOWANCE_UNSET)
    if current_quote is None:
        raise ProgramLegRefused(RECOVERY_QUOTE_UNAVAILABLE)
    if (
        confirmed.quote_observed_at_ms > current_quote.observed_at_ms
        or now_ms - confirmed.quote_observed_at_ms > RECOVERY_QUOTE_MAX_AGE_MS
    ):
        raise ProgramLegRefused(RECOVERY_QUOTE_STALE)
    if state.next_transition_ms is None:
        # A declared window always names its next transition; without one there
        # is no session end to bound the confirmed price by.
        raise ProgramLegRefused(no_session_open(None))
    shape = LegShape(
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        limit_price=float(confirmed.limit_price),
        extended_hours=True,
        side=side,
    )
    try:
        # The contract model owns Alpaca's precision rule; building the leg the
        # EXIT will submit is how that rule is asked, rather than restated.
        shape.apply(symbol=symbol, quantity=quantity)
    except ValidationError as exc:
        raise ProgramLegRefused(RECOVERY_LIMIT_PRICE_INVALID) from exc
    band = _through_the_book(
        side,
        current_quote,
        policy.allowances.exit_bps * policy.allowances.exit_band_multiple,
    )
    past_band = (
        confirmed.limit_price < band if side is OrderSide.SELL else confirmed.limit_price > band
    )
    if past_band:
        raise ProgramLegRefused(RECOVERY_LIMIT_OUTSIDE_BAND)
    return ConfirmedRecoveryShape(
        shape=shape,
        valid_until_ms=state.next_transition_ms,
        reference_quote=current_quote,
        quantity=abs(quantity),
    )


def price_automatic_recovery_reduction(
    *,
    side: OrderSide,
    symbol: str,
    quantity: float,
    now_ms: int,
    policy: ProgramLegPolicy,
    quote: TopOfBookQuote | None,
) -> ConfirmedRecoveryShape:
    """Price an automatic recovery reduction from the current instant (#2229).

    Owner decision 2026-09-19 (evening), superseding "automatic re-drives wait
    for the operator outside the regular session": in an extended session the
    stuck-EXIT watchdog's re-drive is a limit the Clerk prices itself — the
    same marketable-limit formula the operator's suggested price uses, at
    exactly one sealed exit allowance through the touch, never a market order
    the vendor would queue to the next open. The shape is durable on the
    EXIT's acceptance exactly as an operator-confirmed one is, so every later
    pass of that EXIT rebuilds the same leg.

    Raises ``ProgramLegRefused`` when no priceable reduction exists: no open
    session, no allowance, no live or fresh quote, or a price that quantises
    below what Alpaca accepts. The caller defers — the episode stays raised
    and the entry stays free for the operator's own priced flatten.
    """
    state = _tradeable_state(now_ms=now_ms, policy=policy)
    if state.phase == "RTH":
        # Inside the regular session the re-drive is the market DAY leg; a
        # price computed here would be a different product than the one sent.
        raise ProgramLegRefused(RECOVERY_SESSION_CHANGED)
    if policy.allowances is None:
        raise ProgramLegRefused(EXTENDED_HOURS_ALLOWANCE_UNSET)
    if quote is None:
        raise ProgramLegRefused(RECOVERY_QUOTE_UNAVAILABLE)
    if now_ms - quote.observed_at_ms > RECOVERY_QUOTE_MAX_AGE_MS:
        # The Clerk's own quote can go stale with the feed: a price restated
        # against a dead book is as unpriceable as none.
        raise ProgramLegRefused(RECOVERY_QUOTE_STALE)
    if state.next_transition_ms is None:
        raise ProgramLegRefused(no_session_open(None))
    shape = LegShape(
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        limit_price=float(
            _through_the_book(side, quote, policy.allowances.exit_bps)
        ),
        extended_hours=True,
        side=side,
    )
    try:
        # The contract model owns Alpaca's precision rule; building the leg the
        # EXIT will submit is how that rule is asked, rather than restated.
        # The quantity is absolute: a cover arrives as a negative attributed
        # position, and the leg reduces it by its size, never by its sign.
        shape.apply(symbol=symbol, quantity=abs(quantity))
    except ValidationError as exc:
        raise ProgramLegRefused(RECOVERY_LIMIT_PRICE_INVALID) from exc
    return ConfirmedRecoveryShape(
        shape=shape,
        valid_until_ms=state.next_transition_ms,
        reference_quote=quote,
        quantity=abs(quantity),
    )


def recovery_leg_verdict(
    *, extended_hours: bool, valid_until_ms: int | None, now_ms: int
) -> RecoveryLegVerdict:
    """The one rule every recovery EXIT's reducing leg passes before the broker sees it.

    Applied to the leg actually about to be sent — after the side
    reconciliation that can turn a confirmed limit into a market leg (R11),
    and on every resubmission — so no path reaches the broker around it.
    """
    if extended_hours:
        return "send" if valid_until_ms is not None and now_ms < valid_until_ms else "expired"
    return "send" if regular_session_open(now_ms) else "wait"


def realized_slippage_bps(*, side: OrderSide, reference_price: float, fill_price: float) -> float:
    """How much worse than the reference a reducing fill came in, in bps (positive = worse).

    The reference is the bid for a sell and the ask for a cover, taken from the
    quote the Clerk priced the operator's limit against.
    """
    if reference_price <= 0:
        raise ValueError(f"reference_price must be positive; got {reference_price}")
    return _signed_against_reference(side, reference_price, fill_price) / reference_price * 10_000


def realized_slippage_cost(
    *, side: OrderSide, reference_price: float, fill_price: float, quantity: float
) -> float:
    """What :func:`realized_slippage_bps` cost in dollars over ``quantity`` shares."""
    if reference_price <= 0:
        raise ValueError(f"reference_price must be positive; got {reference_price}")
    return _signed_against_reference(side, reference_price, fill_price) * abs(quantity)


def quote_spread_bps(quote: TopOfBookQuote) -> float:
    """The bid-ask spread as bps of the mid — wide means a fill costs more to reach."""
    mid = (quote.bid + quote.ask) / 2
    if mid <= 0:
        raise ValueError(f"quote mid must be positive; got {mid}")
    return (quote.ask - quote.bid) / mid * 10_000


def evaluate_proposed_limit(
    *, proposal: ExtendedLimitProposal, limit_price: Decimal, quantity: float
) -> ProposedLimitEvaluation:
    """What the operator's own price would do against the quote the Clerk holds.

    The one authority for the numbers an operator reads before confirming an
    extended-hours flatten: how far the price reaches through the touch, and
    the most that reach can cost. The browser renders these; it never
    recomputes them (AGENTS.md § "Python owns all math").
    """
    side = proposal.side
    touch = Decimal(str(proposal.quote.bid if side is OrderSide.SELL else proposal.quote.ask))
    if touch <= 0:
        raise ValueError(f"quote touch must be positive; got {touch}")
    through = touch - limit_price if side is OrderSide.SELL else limit_price - touch
    touch_size = proposal.quote.bid_size if side is OrderSide.SELL else proposal.quote.ask_size
    outside_band = (
        limit_price < proposal.band_limit_price
        if side is OrderSide.SELL
        else limit_price > proposal.band_limit_price
    )
    return ProposedLimitEvaluation(
        limit_price=limit_price,
        through_book_bps=float(through / touch) * 10_000,
        worst_case_cost=float(max(through, Decimal(0))) * abs(quantity),
        outside_band=outside_band,
        thin_book=touch_size is not None and abs(quantity) > touch_size,
        resting=through < 0,
    )


def _signed_against_reference(side: OrderSide, reference_price: float, fill_price: float) -> float:
    """Dollars per share worse than the reference: the bid for a sell, the ask for a cover."""
    if side is OrderSide.SELL:
        return reference_price - fill_price
    return fill_price - reference_price


def _through_the_book(side: OrderSide, quote: TopOfBookQuote, allowance_bps: Decimal) -> Decimal:
    """The price ``allowance_bps`` past the bid (sell) or ask (cover), marketably rounded."""
    anchor = quote.bid if side is OrderSide.SELL else quote.ask
    try:
        return marketable_limit_price(
            side=side, anchor=Decimal(str(anchor)), allowance_bps=allowance_bps
        )
    except ValueError as exc:
        raise ProgramLegRefused(RECOVERY_QUOTE_UNPRICEABLE) from exc


def _tradeable_state(*, now_ms: int, policy: ProgramLegPolicy) -> SessionAuthorityState:
    """The session now, or a refusal naming the next open when none is tradeable."""
    state = flatten_session(now_ms=now_ms, policy=policy)
    if state.phase != "RTH" and state.phase not in TRADEABLE_EXTENDED_PHASES:
        raise ProgramLegRefused(no_session_open(state.next_transition_ms))
    return state


def _extended_phase(state: SessionAuthorityState) -> ExtendedPhase:
    return "PRE" if state.phase == "PRE" else "POST"


__all__ = [
    "RECOVERY_BAND_ALLOWANCE_MULTIPLE",
    "RECOVERY_LIMIT_OUTSIDE_BAND",
    "RECOVERY_LIMIT_SESSION_ENDED",
    "RECOVERY_MARKET_WAIT_ENDED",
    "RECOVERY_QUOTE_MAX_AGE_MS",
    "RECOVERY_SPREAD_WARNING_BPS",
    "ConfirmedRecoveryLimit",
    "ConfirmedRecoveryShape",
    "ExtendedLimitProposal",
    "ExtendedPhase",
    "QuoteSource",
    "RecoveryLegVerdict",
    "RecoveryReductionPricing",
    "RegularSessionReduction",
    "flatten_session",
    "no_session_open",
    "price_automatic_recovery_reduction",
    "price_recovery_reduction",
    "realized_slippage_bps",
    "recovery_leg_verdict",
    "recovery_reduction_shape",
    "regular_session_open",
]
