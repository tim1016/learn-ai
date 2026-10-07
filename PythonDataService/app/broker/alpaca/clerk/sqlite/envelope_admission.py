"""The envelope's ENTER-time check — the sibling of ``require_admission`` (ADR 0059 D4).

Order of refusals, each fail-closed: an account not yet switched to
budgets; no fresh observation; a market leg with no decision-bar price; then
the budget rule.
The loss hold is not checked here: it is an account hold
``require_admission`` already refuses on.

Only a budgeted account admits an entry (#2553, owner decision 2026-09-29):
one still on authority version 1 refuses under ``BUDGETS_NOT_SWITCHED_ON``
and is never switched automatically. ``budget_entry_decision`` prices the
ENTER through the one entry requirement (``budgets.entry_requirement``: its
notional plus the canonical BUY fee provision) against the deployment's
budget and the account's other claims, and the reservation -- exact price
and fee provision -- rides the ``ENTER_ACCEPTED`` facts, so no claim is ever
re-quoted or priced without its fee.

An entry in option contracts (ADR 0076) passes the same refusals in the same
order through ``require_option_envelope_admission``, and is then held to the
budget rule on its entry commitment in exact cents: what the bot sets aside
for it, which is not a quantity times a price.

Nothing here contacts the broker. The cash fact arrives as an
``AccountObservation`` the sync already published onto the gate, and the
freshness question is asked at the caller's ``now_ms`` — the repository
clock, like every other stamp on this path.
"""

from __future__ import annotations

from decimal import Decimal

from app.broker.alpaca.clerk.budgets import BudgetUnavailable, budget_entry_decision, option_entry_decision
from app.broker.alpaca.clerk.live_envelope import (
    LIVE_ENVELOPE_CASH_EXCEEDED,
    LIVE_ENVELOPE_UNOBSERVED,
    EnvelopeReservation,
    LiveEnvelopeGate,
)
from app.broker.alpaca.clerk.money import MoneyInputError, normalize_money
from app.broker.alpaca.clerk.sqlite.budget_authority import (
    BUDGET_AUTHORIZATION,
    BUDGETS_NOT_SWITCHED_ON,
    BUDGETS_NOT_SWITCHED_ON_WHY,
)
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
    strategy_instance_id: str,
) -> EnvelopeReservation:
    """Admit one ENTER against the envelope, or raise; returns what it reserves."""
    if leg.side is not OrderSide.BUY:
        # Long-only engine invariant, not a refusal: a short program would need
        # a named LIVE_ENVELOPE_* code of its own, which is an owner decision
        # (follow-up).
        raise ValueError("the envelope admits BUY legs only; every program ENTER is a BUY")
    if repo.budget_authority_version() != BUDGET_AUTHORIZATION:
        raise _refuse(BUDGETS_NOT_SWITCHED_ON, BUDGETS_NOT_SWITCHED_ON_WHY)
    observation = require_current_risk_admission(repo, envelope=envelope, now_ms=now_ms)
    price = leg.limit_price if leg.order_type is OrderType.LIMIT else reference_price
    if price is None:
        raise _refuse(
            LIVE_ENVELOPE_UNOBSERVED,
            "A market ENTER has no decision-bar price to bound it against cash.",
        )
    try:
        projection = repo.account_budget(cash=observation.cash_available_usd, seen_before_ms=observation.fills_seen_before_ms, modelled_fees_seen_before_ms=observation.modelled_fees_seen_before_ms)
        decision = budget_entry_decision(projection, strategy_instance_id=strategy_instance_id,
            quantity=leg.quantity, price=price, at_ms=now_ms)
        exact_price = str(normalize_money(price))
    except BudgetUnavailable as exc:
        raise _refuse(exc.reason_code or LIVE_ENVELOPE_UNOBSERVED, str(exc)) from exc
    except (MoneyInputError, RateNotPinnedError) as exc:
        raise _refuse(LIVE_ENVELOPE_UNOBSERVED, str(exc)) from exc
    if not decision.allowed:
        raise _refuse(LIVE_ENVELOPE_CASH_EXCEEDED, decision.detail)
    return EnvelopeReservation(
        quantity=leg.quantity, reference_price=float(price),
        exact_reference_price=exact_price, fee_provision_cents=decision.fee_cents,
    )


def require_option_envelope_admission(
    repo: ClerkSqliteRepository,
    *,
    envelope: LiveEnvelopeGate,
    contracts: int,
    limit_price: Decimal,
    commitment_cents: int,
    fee_provision_cents: int,
    now_ms: int,
    strategy_instance_id: str,
) -> EnvelopeReservation:
    """Admit one ENTER in option contracts against the envelope, or raise; returns what it reserves.

    ``commitment_cents`` is the entry commitment the Clerk worked out itself
    (``app.options.entry.prepare_entry``): the requirement, the premium at
    the limit price, and ``fee_provision_cents``. The reservation keeps
    ``contracts`` and the premium a share, ``limit_price``; its claim is
    counted with the contract's own multiplier, read through the order
    (``envelope_reservations.entry_cash_claims``). For a bought option, whose
    requirement is zero, that claim plus the provision is the commitment.
    """
    if repo.budget_authority_version() != BUDGET_AUTHORIZATION:
        raise _refuse(BUDGETS_NOT_SWITCHED_ON, BUDGETS_NOT_SWITCHED_ON_WHY)
    observation = require_current_risk_admission(repo, envelope=envelope, now_ms=now_ms)
    try:
        projection = repo.account_budget(
            cash=observation.cash_available_usd, seen_before_ms=observation.fills_seen_before_ms,
            modelled_fees_seen_before_ms=observation.modelled_fees_seen_before_ms,
        )
        decision = option_entry_decision(
            projection, strategy_instance_id=strategy_instance_id,
            commitment_cents=commitment_cents, fee_provision_cents=fee_provision_cents,
        )
        exact_price = str(normalize_money(limit_price))
    except BudgetUnavailable as exc:
        raise _refuse(exc.reason_code or LIVE_ENVELOPE_UNOBSERVED, str(exc)) from exc
    except (MoneyInputError, RateNotPinnedError) as exc:
        raise _refuse(LIVE_ENVELOPE_UNOBSERVED, str(exc)) from exc
    if not decision.allowed:
        raise _refuse(LIVE_ENVELOPE_CASH_EXCEEDED, decision.detail)
    return EnvelopeReservation(
        quantity=float(contracts), reference_price=float(limit_price),
        exact_reference_price=exact_price, fee_provision_cents=fee_provision_cents,
    )


__all__ = ["require_envelope_admission", "require_option_envelope_admission"]
