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

from decimal import Decimal

from app.broker.alpaca.clerk.budgets import entry_requirement
from app.broker.alpaca.clerk.live_envelope import (
    LIVE_ENVELOPE_CASH_EXCEEDED,
    LIVE_ENVELOPE_DISAGREEMENT,
    LIVE_ENVELOPE_UNOBSERVED,
    EnvelopeReservation,
    LiveEnvelopeGate,
)
from app.broker.alpaca.clerk.money import MoneyInputError, cash_admits, money_context, normalize_money, notional
from app.broker.alpaca.clerk.sqlite.budget_projection import BudgetUnavailable
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.risk_admission import require_current_risk_admission
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    AdmissionBlockedError,
    Capability,
    CapabilityDecision,
)
from app.broker.alpaca.regulatory_fees import RateNotPinnedError
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
    reference_price: float | Decimal | None,
    now_ms: int,
    strategy_instance_id: str | None = None,
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
    observation = require_current_risk_admission(repo, envelope=envelope, now_ms=now_ms)
    price = leg.limit_price if leg.order_type is OrderType.LIMIT else reference_price
    if price is None:
        raise _refuse(
            LIVE_ENVELOPE_UNOBSERVED,
            "A market ENTER has no decision-bar price to bound it against cash.",
        )
    # Built before the bound is asked, so the notional the refusal names and
    # the notional the reservation will claim are the same one property.
    # Preserve the historical sibling-row shape before budget cutover. The
    # budget branch below additionally seals the exact price for replay.
    reservation = EnvelopeReservation(quantity=leg.quantity, reference_price=float(price))
    try:
        reserved = repo.reserved_cash_decimal(seen_before_ms=observation.fills_seen_before_ms)
        required = notional(leg.quantity, price)
        affordable = cash_admits(cash=observation.cash_available_usd, claims=reserved, required=required)
        if strategy_instance_id is not None and repo.deployment_budget(strategy_instance_id) is not None:
            projection = repo.account_budget(cash=observation.cash_available_usd, seen_before_ms=observation.fills_seen_before_ms, modelled_fees_seen_before_ms=observation.modelled_fees_seen_before_ms)
            own = next(budget for budget in projection.deployments if budget.strategy_instance_id == strategy_instance_id)
            required, fee_cents = entry_requirement(quantity=leg.quantity, price=price, at_ms=now_ms)
            with money_context():
                affordable = own.active and required <= own.free and cash_admits(
                    cash=observation.cash_available_usd,
                    claims=projection.active_free_claims - own.cash_claim + projection.order_claims + projection.fee_claims,
                    required=required,
                )
            reservation = EnvelopeReservation(
                quantity=leg.quantity, reference_price=float(price),
                exact_reference_price=str(normalize_money(price)), fee_provision_cents=fee_cents,
            )
    except (MoneyInputError, BudgetUnavailable, RateNotPinnedError) as exc:
        raise _refuse(LIVE_ENVELOPE_UNOBSERVED, str(exc)) from exc
    if not affordable:
        raise _refuse(
            LIVE_ENVELOPE_CASH_EXCEEDED,
            f"ENTER needs {required:.2f} USD; "
            f"{observation.cash_available_usd:.2f} USD cash "
            f"with {reserved:.2f} USD reserved by working entries.",
        )
    return reservation


__all__ = ["require_envelope_admission"]
