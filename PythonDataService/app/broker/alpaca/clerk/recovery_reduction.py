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
    where k is the owning bot's sealed band multiple, and bid/ask are the
    Clerk's live quote at send. An operator may explicitly acknowledge a price
    beyond that band; the acknowledgement is durably recorded with the EXIT.
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
from app.broker.alpaca.marketable_limit import (
    DEFAULT_EXIT_BAND_MULTIPLE,
    DEFAULT_EXIT_SPREAD_CAP_BPS,
    marketable_limit_price,
)
from app.broker.contract.models import OrderSide, OrderType, TimeInForce
from app.schemas.market_liveness import MarketLivenessFact, TopOfBookQuote
from app.services.market_liveness import compose_market_liveness
from app.services.session_authority import (
    TRADEABLE_EXTENDED_PHASES,
    SessionAuthorityState,
    order_session_state_at_ms,
    scheduled_exchange_phase_at_ms,
    scheduled_extended_session_bounds,
    session_state_at_ms,
)
from app.utils.session_anchors import et_date_at_ms

# Where the live bid/ask comes from: ``(symbol, now_ms) -> quote or None``.
# Injected so the pricing seam is a pure function and a test can state the
# quote; production reads the market-liveness store's IBKR top of book.
type QuoteSource = Callable[[str, int], TopOfBookQuote | None]
type LivenessSource = Callable[[str, int], MarketLivenessFact | None]

RECOVERY_QUOTE_MAX_AGE_MS = 10_000
"""How old the quote an operator confirmed against may be when they send.

Owner decision 2026-09-19: send the confirmed price, but ask for a refresh if
the bid/ask the operator looked at is more than about ten seconds old.
"""

EXIT_SEND_GUARD_BAND_MS = 5_000
"""Close-side transport guard for a reducing leg (#2504).

A leg must be eligible now and remain eligible when it reaches Alpaca five
seconds later. The guard never advances a session open: pre-market begins
at its actual calendar boundary, and market exits wait for the regular open.
"""

REDUCING_ORDER_PAST_SESSION_GRACE_MS = 300_000
"""How long a reducing order may still be working past its session before the operator is told.

Alpaca ends a DAY extended-hours limit at the end of after-hours, and a market
leg sent in the regular session executes before its close — the Clerk does not
rely on either (#2440 review). A reducing order the broker still reports
working this long after its session ended raises ``EXIT_NOT_FLAT`` once.
Minutes, not seconds: the broker's own expiry and its trade-update evidence
take a few seconds to land, and a false alarm on every close would teach the
operator to ignore the real one.
"""


def send_arrival_ms(now_ms: int) -> int:
    """The latest instant a leg sent at ``now_ms`` may reach the broker (:data:`EXIT_SEND_GUARD_BAND_MS`)."""
    return now_ms + EXIT_SEND_GUARD_BAND_MS


RECOVERY_BAND_ALLOWANCE_MULTIPLE = DEFAULT_EXIT_BAND_MULTIPLE
"""The declared default band multiple: how far through the book a confirmed
limit may go, in multiples of the sealed exit allowance.

Owner decision 2026-09-19: refuse a price more than twice the allowance past
the bid (sell) or ask (cover), so a typo cannot sweep a thin after-hours book.
The deploy form pre-fills this value; each bot seals its explicit choice.
Runtime recovery reads that immutable seal. The legacy environment override
is consulted only during the one-time upgrade of existing registrations.
"""

RECOVERY_SPREAD_WARNING_BPS = int(DEFAULT_EXIT_SPREAD_CAP_BPS)
"""A bid-ask spread wider than this, in bps of the mid, is flagged before sending.

Owner decision 2026-09-19. Presentation only: the Clerk sends it to the
operator's ticket rather than the browser holding its own number. It is also
the automatic re-drive gate's default cap — an ``int`` alias of
``marketable_limit.DEFAULT_EXIT_SPREAD_CAP_BPS``, the dataclass default's own
number, so the human's warning and the Clerk's enforcement cannot drift apart
(#2229).
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

# The session refusals below are judged where the order would reach the broker
# (:func:`send_arrival_ms`), not at the request instant, so their words name
# that moment: at 15:59:56 the session is still regular, at 19:59:57 after-hours
# is still open, at 09:29:56 the regular session has not opened (#2440 review).
RECOVERY_LIMIT_REQUIRED = LegRefusal(
    reason_code="RECOVERY_LIMIT_REQUIRED",
    explanation=(
        "An order sent now would reach the broker outside the regular session, where a "
        "flatten is a limit order, and no confirmed limit price came with this request."
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
        "An order sent now would reach the broker in the regular session, where a flatten "
        "is a market order, not the limit confirmed for the extended session."
    ),
    next_step="Prepare the flatten again.",
)


def no_session_open(opens_at_ms: int | None) -> LegRefusal:
    """No session the active authority trades is open when a leg sent now reaches the broker."""
    return LegRefusal(
        reason_code="NO_SESSION_OPEN",
        explanation=(
            "No trading session would be open when an order sent now reaches the broker, "
            "so no reduction can be sent."
        ),
        next_step="Flatten again once the next session opens.",
        available_at_ms=opens_at_ms,
    )


def extended_hours_pricing_unavailable(regular_open_ms: int | None) -> LegRefusal:
    """A leg sent now reaches pre-market or after-hours, but the policy it is judged under has no window.

    The degraded automatic pricing (:data:`UNPRICEABLE_RECOVERY`, which a
    ``sim:`` authority's re-drive uses although its broker declares a window)
    or a broker that declares no extended-hours window trades only the
    regular session. Outside it the calendar may still say PRE or POST, so
    "no session is open" would be false; and since the broker may declare a
    window the operator's own flatten can price in, the copy claims only that
    the Clerk cannot price a limit automatically (#2440 review).
    """
    return LegRefusal(
        reason_code="EXTENDED_HOURS_PRICING_UNAVAILABLE",
        explanation=(
            "An order sent now would reach the broker in an extended-hours session, and this "
            "Clerk cannot price an extended-hours limit automatically, so no reduction can be sent."
        ),
        next_step="Flatten again once the regular session opens.",
        available_at_ms=regular_open_ms,
    )


RECOVERY_LIMIT_OUTSIDE_BAND = LegRefusal(
    reason_code="RECOVERY_LIMIT_OUTSIDE_BAND",
    explanation=(
        "The limit is further through the book than the accepted band — the "
        "sealed exit allowance times the deploy-time band multiple "
        "(the bot’s sealed band multiple) — from the live bid (sell) or ask "
        "(cover)."
    ),
    next_step="Check the price against the live quote and confirm again.",
)

RECOVERY_SPREAD_TOO_WIDE = LegRefusal(
    reason_code="RECOVERY_SPREAD_TOO_WIDE",
    explanation=(
        "The live spread is wider than the configured cap, so an automatic "
        "price would reach through a broken book."
    ),
    next_step=(
        "Review the logged "
        "spreads, or send the operator's priced flatten — its ticket shows the "
        "wide spread and can override it."
    ),
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
type PricingProvenance = Literal["operator", "clerk"]
"""Who priced a recovery limit: the operator confirmed it, or the Clerk
computed it for an automatic re-drive (#2229). The quantity-guard and expiry
copy select their words from this — a trader must never be told to confirm a
price nobody confirmed."""
type ReducingLegVerdict = Literal["send", "wait", "expired"]
"""May an EXIT's reducing leg go to the broker now?

``send`` — yes. ``wait`` — a market leg outside the regular session, which
Alpaca would queue for the next open: it is not sent now. ``expired`` — a limit
past the end of the session it was priced in: it is never sent into another
session. What happens instead is ``exit_resolution``'s send-time rule.
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
    # The furthest-through-the-book price the Clerk accepts against this quote,
    # or ``None`` when the configured band is effectively unbounded — a sell
    # band past 100 % of the touch floors to zero or below, which is not a
    # price; any positive limit is then inside the band (PR #2230 review).
    band_limit_price: Decimal | None
    band_cap_bps: Decimal | None = None


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
    band_override: bool = False


@dataclass(frozen=True)
class ConfirmedRecoveryShape:
    """The operator's confirmed extended-hours leg, and when it stops being sendable.

    ``valid_until_ms`` is the end of the session the price was confirmed in:
    09:30 for a pre-market confirmation, the declared close for after-hours.
    ``priced_by`` records who set the price (#2229): ``"operator"`` for a
    confirmed limit, ``"clerk"`` for one the automatic re-drive computed.
    """

    shape: LegShape
    valid_until_ms: int
    # The Clerk's live quote at send: the reference realized slippage is
    # measured from, at most ten seconds newer than the one the operator saw.
    reference_quote: TopOfBookQuote
    # The reduction this price was confirmed for. Cancellation resolves the
    # real quantity later; a different one is not what the operator reviewed.
    quantity: float
    priced_by: PricingProvenance = "operator"
    band_override: bool = False


def regular_session_open(now_ms: int) -> bool:
    """Is ``now_ms`` inside the canonical calendar's regular session (half-days included)?"""
    return session_state_at_ms(now_ms=now_ms).phase == "RTH"


def flatten_session(*, now_ms: int, policy: ProgramLegPolicy) -> SessionAuthorityState:
    """The session a reducing leg that reaches the broker at ``now_ms`` goes out in.

    The calendar's regular session widened by the window the broker declares
    -- the same window an extended-session program leg is shaped against --
    with after-hours ending at the calendar's scheduled close on an
    early-close day (``order_session_state_at_ms``). Callers pass the arrival
    instant (:func:`send_arrival_ms`), never the send instant: every
    reduction — an operator's flatten, the watchdog's re-drive, a re-priced
    EXIT — is judged where it may land (#2440 review).
    """
    return order_session_state_at_ms(now_ms=now_ms, extended_window=policy.window)


def price_recovery_reduction(
    *,
    side: OrderSide,
    now_ms: int,
    policy: ProgramLegPolicy,
    quote: TopOfBookQuote | None,
) -> RecoveryReductionPricing:
    """How a reduction of ``side`` would go out right now, for the operator to confirm.

    The session is the one a leg sent now reaches the broker in
    (:func:`send_arrival_ms`), so the ticket proposes what the send will
    accept a moment later. Raises ``ProgramLegRefused`` when none can: no open
    session, no extended-hours window to price in, no allowance
    to suggest a price from, no live quote, or a quote that prices to zero.
    """
    state = _tradeable_state(now_ms=now_ms, policy=policy)
    if state.phase == "RTH":
        return RegularSessionReduction()
    if policy.allowances is None or policy.allowances.exit_bps is None:
        raise ProgramLegRefused(policy.allowance_refusal or EXTENDED_HOURS_ALLOWANCE_UNSET)
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
        band_cap_bps=policy.allowances.exit_bps * policy.allowances.exit_band_multiple,
        band_limit_price=_band_limit_price(
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
    cannot vouch for freshness the server cannot see. The quote's age is
    measured at ``now_ms``; the session at the send instant
    (:func:`send_arrival_ms`).
    """
    state = _tradeable_state(now_ms=now_ms, policy=policy)
    if state.phase == "RTH":
        if confirmed is not None:
            raise ProgramLegRefused(RECOVERY_SESSION_CHANGED)
        return None
    if confirmed is None:
        raise ProgramLegRefused(RECOVERY_LIMIT_REQUIRED)
    if policy.allowances is None or policy.allowances.exit_bps is None:
        raise ProgramLegRefused(policy.allowance_refusal or EXTENDED_HOURS_ALLOWANCE_UNSET)
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
    band = _band_limit_price(
        side,
        current_quote,
        policy.allowances.exit_bps * policy.allowances.exit_band_multiple,
    )
    past_band = band is not None and (
        confirmed.limit_price < band if side is OrderSide.SELL else confirmed.limit_price > band
    )
    if past_band and not confirmed.band_override:
        raise ProgramLegRefused(RECOVERY_LIMIT_OUTSIDE_BAND)
    return ConfirmedRecoveryShape(
        shape=shape,
        valid_until_ms=state.next_transition_ms,
        reference_quote=current_quote,
        quantity=abs(quantity),
        band_override=past_band and confirmed.band_override,
    )


@dataclass(frozen=True)
class PricingSnapshot:
    """One read of a :class:`RecoveryPricing`: the policy in force and the live touch.

    Taken on the event loop — the production quote source registers IBKR
    demand in the market-liveness store, whose symbol map the IBKR status
    loop replaces there — and handed to synchronous code as a plain value, so
    pricing never touches the store from a worker thread (#2440 review).
    """

    policy: ProgramLegPolicy
    quote: TopOfBookQuote | None
    market_liveness: MarketLivenessFact | None = None

    def hold(self, now_ms: int) -> LegRefusal | None:
        verdict = reducing_send_verdict(
            now_ms=now_ms, extended_hours=False, valid_until_ms=None, liveness=self.market_liveness,
        )
        return verdict if isinstance(verdict, LegRefusal) else None

    @property
    def quote_spread_bps(self) -> float | None:
        """The touch's spread, logged beside every automatic price and refusal (#2229)."""
        return None if self.quote is None else quote_spread_bps(self.quote)

    def price(
        self, *, side: OrderSide, symbol: str, quantity: float, now_ms: int
    ) -> ConfirmedRecoveryShape | None:
        """:func:`price_automatic_recovery_reduction` against this read."""
        if hold := self.hold(now_ms):
            raise ProgramLegRefused(hold)
        return price_automatic_recovery_reduction(
            side=side,
            symbol=symbol,
            quantity=quantity,
            now_ms=now_ms,
            policy=self.policy,
            quote=self.quote,
        )


@dataclass(frozen=True)
class RecoveryPricing:
    """One bot-scoped policy and fresh market evidence for an automatic reduction.

    The facade reads immutable terms from its cache. Pricing and scheduling
    require the same instance identity; neither falls back to account defaults.
    """

    policy_for: Callable[[str], ProgramLegPolicy]
    quote_source: QuoteSource
    liveness_source: LivenessSource | None = None

    def read(self, symbol: str, now_ms: int, *, strategy_instance_id: str) -> PricingSnapshot:
        """Read this bot's cached execution policy and the current live touch."""
        return PricingSnapshot(
            policy=self.policy_for(strategy_instance_id), quote=self.quote_source(symbol, now_ms),
            market_liveness=self.read_liveness(symbol, now_ms),
        )

    def read_liveness(self, symbol: str, now_ms: int) -> MarketLivenessFact | None:
        """Read on the event loop immediately before creating or sending a leg."""
        return None if self.liveness_source is None else self.liveness_source(symbol, now_ms)


def reduction_market_hold(*, now_ms: int, fact: MarketLivenessFact | None) -> LegRefusal | None:
    """A positive vendor halt or fresh emergency close pauses an Alpaca EXIT.

    Missing/stale liveness otherwise falls back to the calendar. A retained
    IBKR halt survives a stale clock or reconnect until explicitly cleared
    (ADR 0067); it is evidence of a halt, not absence of evidence.
    """
    if fact is None:
        return None
    status = fact.symbol_status
    if fact.state == "HALTED" or (
        status is not None and status.symbol == fact.symbol and status.state == "HALTED"
    ):
        return LegRefusal(
            reason_code="EXIT_SYMBOL_HALTED",
            explanation="IBKR reports this symbol halted; the Clerk is holding the Alpaca exit.",
            next_step="The Clerk will check again when the halt is explicitly cleared.",
        )
    # Compose the market-wide evidence with the canonical freshness rule.
    # Unknown per-symbol status does not veto a reducing order; the retained
    # positive halt above does. IBKR is evidence only; execution stays Alpaca.
    clock_fact = compose_market_liveness(
        fact.symbol, now_ms=now_ms, market_clock=fact.market_clock,
        connected=True, connection_changed_at_ms=fact.observed_at_ms, symbol_status=None,
    )
    if clock_fact.state == "CLOSED" and regular_session_open(now_ms):
        return LegRefusal(
            reason_code="EXIT_EMERGENCY_CLOSE",
            explanation="The live market clock reports closed during the scheduled regular session.",
            next_step="The Clerk will check again when the market reopens.",
        )
    return None


def _no_live_quote(symbol: str, now_ms: int) -> TopOfBookQuote | None:
    return None


UNPRICEABLE_RECOVERY = RecoveryPricing(
    policy_for=lambda _sid: ProgramLegPolicy.regular_only(),
    quote_source=_no_live_quote,
)
"""The degraded default: nothing can be priced, so an out-of-session re-drive
defers rather than guessing (#2229). Explicit at every signature that accepts
a :class:`RecoveryPricing`, instead of implied by two omitted arguments."""


def price_automatic_recovery_reduction(
    *,
    side: OrderSide,
    symbol: str,
    quantity: float,
    now_ms: int,
    policy: ProgramLegPolicy,
    quote: TopOfBookQuote | None,
) -> ConfirmedRecoveryShape | None:
    """Price an automatic recovery reduction from the current instant (#2229).

    Also the send-time re-pricing of any EXIT whose recorded leg can no longer
    go out when its reduction is created (#2440, ``exit_resolution``): an EXIT
    decided in the regular session but delayed past the close goes out as
    this limit.

    Owner decision 2026-09-19 (evening), superseding "automatic re-drives wait
    for the operator outside the regular session": in an extended session the
    stuck-EXIT watchdog's re-drive is a limit the Clerk prices itself — the
    operator's suggested price from :func:`price_recovery_reduction`, taken as
    confirmed — never a market order the vendor would queue to the next open.
    The shape is durable on the EXIT's acceptance exactly as an
    operator-confirmed one is, stamped ``priced_by="clerk"`` so later copy
    never tells a trader to confirm a price nobody confirmed.

    ``None`` is the regular session's answer: inside 09:30–16:00 the re-drive
    is the market DAY leg a recovery EXIT with no recorded shape already
    builds — the same fact ``confirmed_shape=None`` encodes downstream, and
    the two session notions agree (both judge :func:`send_arrival_ms`, and
    ``_tradeable_state`` refuses every phase :func:`market_leg_sendable` does
    not call RTH), so the arm needs no guard of its own.

    Raises ``ProgramLegRefused`` when no priceable reduction exists: no open
    session, no allowance, no live quote, or an unpriceable anchor (all from
    :func:`price_recovery_reduction`), a book whose spread is wider than the
    deploy-time cap, or a price that quantises below what Alpaca accepts. The
    caller defers — the episode stays raised and the entry stays free for the
    operator's own priced flatten.

    The spread cap is automatic-path-only by design: the operator's ticket
    already shows ``wide_spread`` and can override it, which is informed
    consent. Depth is reported (``thin_book``), never gated — thin depth
    costs fills, a resting DAY limit dies at session end, and ``EXIT_STUCK``
    catches the position that never reduced; a wide spread costs money.
    """
    state = _tradeable_state(now_ms=now_ms, policy=policy)
    if state.phase == "RTH":
        return None
    proposal = price_recovery_reduction(side=side, now_ms=now_ms, policy=policy, quote=quote)
    assert isinstance(proposal, ExtendedLimitProposal)
    if quote_spread_bps(proposal.quote) > float(policy.allowances.exit_spread_cap_bps):
        raise ProgramLegRefused(RECOVERY_SPREAD_TOO_WIDE)
    shape = LegShape(
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        limit_price=float(proposal.suggested_limit_price),
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
    # A tradeable extended phase always names its next transition; the
    # None-capable branches belong to phases ``_tradeable_state`` refused.
    assert state.next_transition_ms is not None
    return ConfirmedRecoveryShape(
        shape=shape,
        valid_until_ms=state.next_transition_ms,
        reference_quote=proposal.quote,
        quantity=abs(quantity),
        priced_by="clerk",
    )


def reducing_send_verdict(
    *, extended_hours: bool, valid_until_ms: int | None, now_ms: int,
    liveness: MarketLivenessFact | None,
) -> ReducingLegVerdict | LegRefusal:
    """One EXIT send verdict: calendar eligibility plus positive live holds.

    Unknown/stale evidence falls back to the canonical calendar; an explicit
    retained halt survives reconnects until cleared (ADR 0067).
    """
    hold = reduction_market_hold(now_ms=now_ms, fact=liveness)
    return hold if hold is not None else reducing_leg_verdict(
        extended_hours=extended_hours, valid_until_ms=valid_until_ms, now_ms=now_ms,
    )


def reducing_leg_verdict(
    *, extended_hours: bool, valid_until_ms: int | None, now_ms: int
) -> ReducingLegVerdict:
    """The one rule every EXIT's reducing leg passes before the broker sees it.

    Program and recovery EXITs alike (#2440): applied to the leg actually about
    to be created — after the side reconciliation that can turn a limit into a
    market leg (R11) — and on every resubmission, so no path reaches the broker
    around it. An extended-hours leg with no recorded bound was priced for no
    session this rule can name, so it is ``expired``. Judged at
    :func:`send_arrival_ms`, so a leg within :data:`EXIT_SEND_GUARD_BAND_MS` of
    its bound is already past it.
    """
    if extended_hours:
        if valid_until_ms is None or send_arrival_ms(now_ms) >= valid_until_ms:
            return "expired"
        return "send" if scheduled_exchange_phase_at_ms(now_ms) in ("RTH", *TRADEABLE_EXTENDED_PHASES) else "wait"
    return "send" if market_leg_sendable(now_ms) else "wait"


def market_leg_sendable(now_ms: int) -> bool:
    """May a market DAY leg sent at ``now_ms`` go out: does it reach the broker inside the regular session?

    Alpaca queues a market DAY order that arrives outside the regular session
    for the next open, so outside it every reducing leg is a priced limit.
    Judged at :func:`send_arrival_ms`. The one question the send-time rule,
    the watchdog's choice to price a re-drive, and the runtime's
    unpriced-exit warning all ask (#2440 review).
    """
    return regular_session_open(now_ms) and regular_session_open(send_arrival_ms(now_ms))


def next_redrive_at_ms(*, not_before_ms: int, policy: ProgramLegPolicy) -> int:
    """The first instant, not before ``not_before_ms``, a recovery send is session-eligible.

    Inside the regular session it sends the market leg; in the declared PRE or
    POST window it prices a limit itself, which needs the policy's exit
    allowance (#2229) — without one the next chance is the regular open. The
    live touch that pricing also reads cannot be foreseen, so this is when
    a retry is allowed; a missing quote or custody refusal can defer it. Owner
    decision 2026-09-25 (#2440): the ``EXIT_NOT_FLAT`` notice states this
    time — the 04:00 pre-market sell after an after-hours exit that could not
    go out.

    The close-side guard can defer a send to the next session, but eligibility
    names that session's actual open. A timestamp authorizes evaluation;
    it is never a promise that the Clerk has submitted an order.
    """
    sendable = policy if policy.allowances is not None and policy.allowances.exit_bps is not None else ProgramLegPolicy.regular_only()
    try:
        _tradeable_state(now_ms=not_before_ms, policy=sendable)
        return not_before_ms
    except ProgramLegRefused as exc:
        return exc.refusal.available_at_ms or not_before_ms


def reducing_leg_session_end_ms(
    *, extended_hours: bool, valid_until_ms: int | None, sent_at_ms: int
) -> int | None:
    """When the broker stops working a sent reducing leg, or ``None`` when no end can be named.

    Read by the end-of-session alarm (:data:`REDUCING_ORDER_PAST_SESSION_GRACE_MS`),
    so it is the broker's expiry, not the Clerk's send bound. Every
    extended-hours leg the Clerk sends is a DAY limit, and Alpaca keeps a DAY
    extended-hours limit working through the regular session until that day's
    after-hours close (#2440 review): a limit sent at 04:00 is still live at
    09:35, so its 09:30 send bound (``valid_until_ms``) would alarm on the
    owner's own pre-market re-drive. Its end is the canonical calendar's
    after-hours close of the ET day it was sent (17:00 on an early-close day);
    ``valid_until_ms`` only when that day has no scheduled session. A market
    leg's end is the regular close of the day it was sent (13:00 on an
    early-close day) — read from the day, not the send instant's phase: a
    market leg must be sent inside the regular session.
    """
    scheduled = scheduled_extended_session_bounds(et_date_at_ms(sent_at_ms))
    if scheduled is None:
        return valid_until_ms if extended_hours else None
    return scheduled.close_ms if extended_hours else scheduled.rth_close_ms


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
    touch = Decimal(str(reduction_touch(side, bid=proposal.quote.bid, ask=proposal.quote.ask)))
    if touch <= 0:
        raise ValueError(f"quote touch must be positive; got {touch}")
    through = touch - limit_price if side is OrderSide.SELL else limit_price - touch
    touch_size = reduction_touch(side, bid=proposal.quote.bid_size, ask=proposal.quote.ask_size)
    outside_band = proposal.band_limit_price is not None and (
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


def reduction_touch[T](side: OrderSide, *, bid: T, ask: T) -> T:
    """The side of the book a reduction of ``side`` is priced and measured against: the bid for a sell, the ask for a cover.

    One selection for every price, size and reference that follows the
    touch — the suggested limit's anchor, a proposed limit's reach, and the
    reference a priced leg's realized slippage is measured from.
    """
    return bid if side is OrderSide.SELL else ask


def _signed_against_reference(side: OrderSide, reference_price: float, fill_price: float) -> float:
    """Dollars per share worse than the reference: the bid for a sell, the ask for a cover."""
    if side is OrderSide.SELL:
        return reference_price - fill_price
    return fill_price - reference_price


def _band_limit_price(
    side: OrderSide, quote: TopOfBookQuote, band_bps: Decimal
) -> Decimal | None:
    """The band edge, or ``None`` when the configured band is unbounded.

    A sell band of 10 000+ bps floors the touch to zero or below, which is not
    a price — an otherwise accepted configuration must not make a flatten
    unpriceable, so the band is treated as having no lower bound and any
    positive limit passes (PR #2230 review). Only the sell side can floor;
    a cover band only grows upward.
    """
    try:
        return _through_the_book(side, quote, band_bps)
    except ProgramLegRefused as exc:
        if exc.reason_code != RECOVERY_QUOTE_UNPRICEABLE.reason_code:
            raise
        return None


def _through_the_book(side: OrderSide, quote: TopOfBookQuote, allowance_bps: Decimal) -> Decimal:
    """The price ``allowance_bps`` past the bid (sell) or ask (cover), marketably rounded."""
    anchor = reduction_touch(side, bid=quote.bid, ask=quote.ask)
    try:
        return marketable_limit_price(
            side=side, anchor=Decimal(str(anchor)), allowance_bps=allowance_bps
        )
    except ValueError as exc:
        raise ProgramLegRefused(RECOVERY_QUOTE_UNPRICEABLE) from exc


def _tradeable_state(*, now_ms: int, policy: ProgramLegPolicy) -> SessionAuthorityState:
    """The session a leg sent at ``now_ms`` reaches the broker in, or a refusal naming why none is tradeable.

    Judged at :func:`send_arrival_ms`, exactly as :func:`reducing_leg_verdict`
    judges a leg about to be sent, so every pricing entry point here — the
    operator's ticket, the watchdog's re-drive, the send-time re-price — and
    the send-time rule agree on one instant (#2440 review). Callers pass the
    real ``now_ms``; only the session is judged later, never a quote's age.

    The refusal is the truth about that instant: with no declared window the
    authority trades only the regular session, and while the calendar's
    pre-market or after-hours is open that is "no extended-hours pricing",
    not "no session open" (:func:`extended_hours_pricing_unavailable`).
    """
    arrival_ms = send_arrival_ms(now_ms)
    current = flatten_session(now_ms=now_ms, policy=policy)
    state = flatten_session(now_ms=arrival_ms, policy=policy)
    # The guard protects closes; it never authorizes an early session open.
    if current.phase not in ("RTH", *TRADEABLE_EXTENDED_PHASES):
        state = current
    elif current.phase == "PRE" and state.phase == "RTH":
        raise ProgramLegRefused(no_session_open(current.next_transition_ms))
    if state.phase == "RTH" or state.phase in TRADEABLE_EXTENDED_PHASES:
        return state
    if policy.window is None and scheduled_exchange_phase_at_ms(arrival_ms) in TRADEABLE_EXTENDED_PHASES:
        raise ProgramLegRefused(extended_hours_pricing_unavailable(state.next_transition_ms))
    raise ProgramLegRefused(no_session_open(state.next_transition_ms))


def flatten_send_verdict(*, now_ms: int, policy: ProgramLegPolicy) -> SessionAuthorityState | LegRefusal:
    """The session an operator's flatten sent at ``now_ms`` goes out in, or the refusal the Clerk gives it.

    :func:`_tradeable_state`'s own answer, returned rather than raised, for a
    surface that shows it before anything is sent (the bot page's flatten
    button), so the page says what the Clerk would, judged at the same send
    instant -- never a second session reading of its own (#2440 review).
    """
    try:
        return _tradeable_state(now_ms=now_ms, policy=policy)
    except ProgramLegRefused as exc:
        return exc.refusal


def _extended_phase(state: SessionAuthorityState) -> ExtendedPhase:
    return "PRE" if state.phase == "PRE" else "POST"


__all__ = [
    "EXIT_SEND_GUARD_BAND_MS",
    "RECOVERY_BAND_ALLOWANCE_MULTIPLE",
    "RECOVERY_LIMIT_OUTSIDE_BAND",
    "RECOVERY_LIMIT_SESSION_ENDED",
    "RECOVERY_MARKET_WAIT_ENDED",
    "RECOVERY_QUOTE_MAX_AGE_MS",
    "RECOVERY_SPREAD_TOO_WIDE",
    "RECOVERY_SPREAD_WARNING_BPS",
    "REDUCING_ORDER_PAST_SESSION_GRACE_MS",
    "UNPRICEABLE_RECOVERY",
    "ConfirmedRecoveryLimit",
    "ConfirmedRecoveryShape",
    "ExtendedLimitProposal",
    "ExtendedPhase",
    "PricingProvenance",
    "PricingSnapshot",
    "QuoteSource",
    "RecoveryPricing",
    "RecoveryReductionPricing",
    "ReducingLegVerdict",
    "RegularSessionReduction",
    "extended_hours_pricing_unavailable",
    "flatten_send_verdict",
    "flatten_session",
    "market_leg_sendable",
    "next_redrive_at_ms",
    "no_session_open",
    "price_automatic_recovery_reduction",
    "price_recovery_reduction",
    "realized_slippage_bps",
    "recovery_reduction_shape",
    "reducing_leg_session_end_ms",
    "reducing_leg_verdict",
    "reduction_touch",
    "regular_session_open",
    "send_arrival_ms",
]
