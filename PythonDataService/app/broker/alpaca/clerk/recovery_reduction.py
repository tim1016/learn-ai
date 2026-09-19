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

Formula (the suggested limit):
    sell: floor_tick( bid × (1 − exit_bps / 10⁴) )
    buy:  ceil_tick(  ask × (1 + exit_bps / 10⁴) )
Reference: docs/references/alpaca-extended-hours.md § "Operator flatten outside
    the regular session"; owner decisions 2026-09-19 on #2007.
Canonical implementation: this file for the session rule;
    ``app/broker/alpaca/marketable_limit.py::marketable_limit_price`` for the
    price.
Validated against: tests/broker/alpaca/clerk/test_recovery_reduction.py
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

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
    TradingSessionPhase,
    session_state_at_ms,
)

RECOVERY_QUOTE_MAX_AGE_MS = 10_000
"""How old the quote an operator confirmed against may be when they send.

Owner decision 2026-09-19: send the confirmed price, but ask for a refresh if
the bid/ask the operator looked at is more than about ten seconds old.
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


@dataclass(frozen=True)
class RegularSessionReduction:
    """Inside the regular session: the market DAY leg; no quote is needed."""


@dataclass(frozen=True)
class ExtendedLimitProposal:
    """What the operator is shown before confirming an extended-hours flatten."""

    phase: TradingSessionPhase
    side: OrderSide
    quote: TopOfBookQuote
    exit_allowance_bps: Decimal
    suggested_limit_price: Decimal


type RecoveryReductionPricing = RegularSessionReduction | ExtendedLimitProposal


@dataclass(frozen=True)
class ConfirmedRecoveryLimit:
    """The limit price the operator confirmed, and the quote they confirmed it against."""

    limit_price: Decimal
    quote_observed_at_ms: int


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
    anchor = quote.bid if side is OrderSide.SELL else quote.ask
    try:
        suggested = marketable_limit_price(
            side=side,
            anchor=Decimal(str(anchor)),
            allowance_bps=policy.allowances.exit_bps,
        )
    except ValueError as exc:
        raise ProgramLegRefused(RECOVERY_QUOTE_UNPRICEABLE) from exc
    return ExtendedLimitProposal(
        phase=state.phase,
        side=side,
        quote=quote,
        exit_allowance_bps=policy.allowances.exit_bps,
        suggested_limit_price=suggested,
    )


def recovery_reduction_shape(
    *,
    side: OrderSide,
    symbol: str,
    quantity: float,
    now_ms: int,
    policy: ProgramLegPolicy,
    confirmed: ConfirmedRecoveryLimit | None,
) -> LegShape | None:
    """The shape the operator's confirmed flatten reduces with.

    ``None`` is the regular-session market DAY leg — the shape a recovery EXIT
    with no recorded shape already builds. An extended-session shape is the
    operator's own price, checked against the clock and Alpaca's precision rule
    here, before any EXIT is accepted, so nothing downstream can reject it.
    """
    state = _tradeable_state(now_ms=now_ms, policy=policy)
    if state.phase == "RTH":
        if confirmed is not None:
            raise ProgramLegRefused(RECOVERY_SESSION_CHANGED)
        return None
    if confirmed is None:
        raise ProgramLegRefused(RECOVERY_LIMIT_REQUIRED)
    if not 0 <= now_ms - confirmed.quote_observed_at_ms <= RECOVERY_QUOTE_MAX_AGE_MS:
        raise ProgramLegRefused(RECOVERY_QUOTE_STALE)
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
    return shape


def _tradeable_state(*, now_ms: int, policy: ProgramLegPolicy) -> SessionAuthorityState:
    """The session now, or a refusal naming the next open when none is tradeable."""
    state = session_state_at_ms(now_ms=now_ms, extended_window=policy.window)
    if state.phase != "RTH" and state.phase not in TRADEABLE_EXTENDED_PHASES:
        raise ProgramLegRefused(no_session_open(state.next_transition_ms))
    return state


__all__ = [
    "RECOVERY_QUOTE_MAX_AGE_MS",
    "ConfirmedRecoveryLimit",
    "ExtendedLimitProposal",
    "RecoveryReductionPricing",
    "RegularSessionReduction",
    "no_session_open",
    "price_recovery_reduction",
    "recovery_reduction_shape",
]
