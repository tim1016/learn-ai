"""The envelope's ENTER-time check — the sibling of ``require_admission`` (ADR 0059 D4).

Order of refusals, each fail-closed: a sealed envelope that disagrees with
the environment; no fresh observation; a market leg with no decision-bar
price; then the cash rule (plan R1). The loss hold is not checked here: it
is an account hold ``require_admission`` already refuses on.

Nothing here contacts the broker. The cash fact arrives as an
``AccountObservation`` the sync already published onto the gate, and the
freshness question is asked at the caller's ``now_ms`` — the repository
clock, like every other stamp on this path.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.live_envelope import (
    LIVE_ENVELOPE_CASH_EXCEEDED,
    LIVE_ENVELOPE_DISAGREEMENT,
    LIVE_ENVELOPE_UNOBSERVED,
    EnvelopeReservation,
    LiveEnvelopeGate,
    cash_bound_admits,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    AdmissionBlockedError,
    Capability,
    CapabilityDecision,
)
from app.broker.contract.models import BrokerOrderLeg, OrderSide, OrderType


def _refuse(reason_code: str, why: str) -> AdmissionBlockedError:
    return AdmissionBlockedError(
        CapabilityDecision(
            allowed=False,
            capability=Capability.NEW_EXPOSURE,
            reason_code=reason_code,
            why=why,
        )
    )


def require_envelope_admission(
    repo: ClerkSqliteRepository,
    *,
    envelope: LiveEnvelopeGate,
    leg: BrokerOrderLeg,
    reference_price: float | None,
    now_ms: int,
) -> EnvelopeReservation:
    """Admit one ENTER against the envelope, or raise; returns what it reserves."""
    if leg.side is not OrderSide.BUY:
        # Long-only engine invariant, not a refusal: a short program would need
        # a named LIVE_ENVELOPE_* code of its own, which is a fifth code beyond
        # the plan's four and an owner decision (follow-up).
        raise ValueError("the envelope admits BUY legs only; every program ENTER is a BUY")
    if envelope.agreement == "disagreed":
        raise _refuse(
            LIVE_ENVELOPE_DISAGREEMENT,
            "The ALPACA_LIVE_* environment values differ from the envelope sealed "
            "at arming; re-arm to change them.",
        )
    observation = envelope.fresh_observation(now_ms)
    if observation is None:
        raise _refuse(
            LIVE_ENVELOPE_UNOBSERVED,
            "No fresh broker cash observation exists; the envelope cannot bound this ENTER yet.",
        )
    price = leg.limit_price if leg.order_type is OrderType.LIMIT else reference_price
    if price is None:
        raise _refuse(
            LIVE_ENVELOPE_UNOBSERVED,
            "A market ENTER has no decision-bar price to bound it against cash.",
        )
    reserved = repo.reserved_cash_usd(observed_at_ms=observation.observed_at_ms)
    # Built before the bound is asked, so the notional the refusal names and
    # the notional the reservation will claim are the same one property.
    reservation = EnvelopeReservation(quantity=leg.quantity, reference_price=price)
    if not cash_bound_admits(
        cash_available_usd=observation.cash_available_usd,
        reserved_usd=reserved,
        notional_usd=reservation.notional_usd,
    ):
        raise _refuse(
            LIVE_ENVELOPE_CASH_EXCEEDED,
            f"ENTER needs {reservation.notional_usd:.2f} USD; "
            f"{observation.cash_available_usd:.2f} USD cash "
            f"with {reserved:.2f} USD reserved by working entries.",
        )
    return reservation


__all__ = ["require_envelope_admission"]
