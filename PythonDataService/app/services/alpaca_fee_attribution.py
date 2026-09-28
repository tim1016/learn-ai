"""Canonical ownership of dated Alpaca fees; no independent fee ledger.

Formula: cents_i = floor(C * w_i / sum(w)); leftover cents go to the
largest exact fractional remainders, ties ordered by custody subject ID.
Reference: PRD #2540 fee attribution contract and Hamilton apportionment;
  docs/references/alpaca-fee-attribution.md. Weights come exclusively from
  app.broker.alpaca.regulatory_fees (the pinned broker model).
Canonical implementation: this file.
Validated against: tests/services/test_alpaca_fee_attribution.py and
  tests/fixtures/golden/alpaca-fee-attribution/ (exact integer-cent parity).

Amounts remain Decimal inside the authority. Fraction is used only to compare
remainders exactly, so global Decimal precision cannot decide who gets a cent.
Missing population, linkage or overlap evidence is an unresolved account charge,
never a guessed deployment debit. Simulations never consume broker activities.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, Inexact, localcontext
from fractions import Fraction
from typing import Literal

from app.broker.alpaca.clerk.money import money_context
from app.broker.alpaca.regulatory_fees import (
    FeeComponent,
    FillFees,
    RateNotPinnedError,
    fees_for_fill,
    settle_session,
)
from app.broker.contract.models import OrderSide

ZERO = Decimal("0")
CENT = Decimal("0.01")
FeeState = Literal["estimated", "modelled_settled", "observed"]


@dataclass(frozen=True)
class FeeFill:
    fill_id: str
    subject_id: str
    side: OrderSide
    quantity: Decimal
    price: Decimal
    native_order_id: str | None = None
    reported_fee: Decimal | None = None
    observed_at_ms: int = 0


@dataclass(frozen=True)
class FeeCharge:
    """Normalized evidence, not heuristics on the vendor's description text.

    ``covers_fill_ids`` is only populated by an explicitly linked source.
    Unlinked overlapping fill fees/activities cannot be added safely. A refund
    names its original activity; a missing original remains unresolved.
    """

    charge_id: str
    amount: Decimal | None
    observed_at_ms: int
    native_order_id: str | None = None
    component: FeeComponent | None = None
    refund_of: str | None = None
    covers_fill_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class FeeShare:
    charge_id: str
    subject_id: str
    amount: Decimal
    state: FeeState
    observed_at_ms: int
    # Cash reservation already includes these fees with the underlying fill.
    included_in_fill: bool = False


@dataclass(frozen=True)
class FeeAttribution:
    shares: tuple[FeeShare, ...]
    unattributed: Decimal
    unresolved: tuple[str, ...]
    observed_total: Decimal | None
    predicted_total: Decimal | None

    @property
    def known(self) -> bool:
        return not self.unresolved

    @money_context()
    def total_for(self, subject_id: str) -> Decimal:
        return sum((share.amount for share in self.shares if share.subject_id == subject_id), ZERO)

    @money_context()
    def unobserved_cash_claim(
        self, *, cash_seen_before_ms: int, modelled_fees_seen_before_ms: int | None = None
    ) -> Decimal:
        """Disjoint fee claim: fill-fees stay in the existing unseen-fill claim.

        An observed activity is only safely inside broker cash after a later
        cash observation. Its trade-date timestamp does not prove recognition.
        Caller MUST refuse admission when ``known`` is false.
        """
        return self.unattributed + sum(
            (
                max(ZERO, share.amount)
                for share in self.shares
                if not share.included_in_fill
                and (share.state != "observed" or share.observed_at_ms >= cash_seen_before_ms)
                and not (
                    share.state == "modelled_settled"
                    and modelled_fees_seen_before_ms is not None
                    and share.observed_at_ms < modelled_fees_seen_before_ms
                )
            ),
            ZERO,
        )


@money_context()
def apportion_cents(amount: Decimal, weights: Mapping[str, Decimal]) -> dict[str, Decimal]:
    """Exact Hamilton shares, including deterministic signed reversal shares."""
    if not amount.is_finite() or amount % CENT:
        raise ValueError("fee settlement must be finite whole cents")
    if any(not weight.is_finite() or weight < 0 for weight in weights.values()):
        raise ValueError("fee weights must be finite and nonnegative")
    result = dict.fromkeys(sorted(weights), ZERO)
    total = sum((Fraction(weight) for weight in weights.values()), Fraction())
    if not total:
        if amount:
            raise ValueError("positive fee has no predicted weight")
        return result
    cents = int(abs(amount) / CENT)
    quotas = {key: cents * Fraction(weight) / total for key, weight in weights.items()}
    floors = {key: quota.numerator // quota.denominator for key, quota in quotas.items()}
    ranked = sorted(quotas, key=lambda key: (-(quotas[key] - floors[key]), key))
    for key in ranked[: cents - sum(floors.values())]:
        floors[key] += 1
    sign = -1 if amount < 0 else 1
    return {key: Decimal(sign * floors[key]) * CENT for key in sorted(floors)}


def _weights(
    fills: Sequence[FeeFill], priced: Sequence[FillFees], component: FeeComponent | None
) -> dict[str, Decimal]:
    weights: dict[str, Decimal] = {}
    for fill, price in zip(fills, priced, strict=True):
        components = (component,) if component else ("sec", "taf", "cat")
        values = [getattr(price, name) for name in components]
        if any(value is None for value in values):
            raise ValueError("the fee component has no pinned rate")
        weights[fill.subject_id] = weights.get(fill.subject_id, ZERO) + sum(values, ZERO)
    return weights


@money_context()
def attribute_session_fees(
    *,
    trade_date: date,
    fills: Sequence[FeeFill],
    charges: Sequence[FeeCharge],
    population_complete: bool,
    activities_complete: bool,
    simulated: bool = False,
    session_ended: bool = True,
    settlement_at_ms: int = 0,
) -> FeeAttribution:
    """Project one complete effective fill population and its fee evidence.

    The input is a canonical effective fill window: corrections replace their
    superseded fills upstream. Duplicate activity identities collapse; conflicting
    copies fail closed. Real settlements replace provisions for the entire day.
    """
    shares: list[FeeShare] = []
    unresolved: list[str] = []
    unattributed = ZERO
    priced = [
        fees_for_fill(trade_date=trade_date, side=fill.side, quantity=fill.quantity, fill_price=fill.price)
        for fill in fills
    ]
    try:
        with localcontext() as rounding:
            rounding.traps[Inexact] = False
            modelled = settle_session(priced)
        predicted: Decimal | None = modelled.total
    except RateNotPinnedError:
        predicted = None
    if not population_complete:
        unresolved.append("The complete fee-bearing fill population is unavailable. Reconcile account executions.")
    unique: dict[str, FeeCharge] = {}
    if not simulated:
        for charge in charges:
            prior = unique.get(charge.charge_id)
            # Observation time is delivery metadata, not economic identity.
            if prior is not None and (
                prior.amount, prior.native_order_id, prior.component, prior.refund_of, prior.covers_fill_ids
            ) != (
                charge.amount, charge.native_order_id, charge.component, charge.refund_of, charge.covers_fill_ids
            ):
                unresolved.append(f"Conflicting fee evidence for {charge.charge_id}. Reconcile broker activities.")
            if prior is None or charge.observed_at_ms < prior.observed_at_ms:
                unique[charge.charge_id] = charge
        if not activities_complete:
            unresolved.append("The broker activity read does not cover this fee day. Refresh account evidence.")
    reported = {fill.fill_id: fill for fill in fills if fill.reported_fee is not None and not simulated}
    covered_ids = {key for charge in unique.values() for key in charge.covers_fill_ids}
    for key, fill in sorted(reported.items()):
        if key not in covered_ids:
            shares.append(
                FeeShare(f"fill:{key}", fill.subject_id, fill.reported_fee, "observed", fill.observed_at_ms, True)
            )
    if not unique:
        if reported:
            if len(reported) != len(fills):
                unresolved.append(
                    "Some fill fees are reported but the remaining fee coverage is unknown. Reconcile fees."
                )
        elif predicted is None:
            unresolved.append("Fee rates are not pinned for this day. Review the fee model before deploying.")
        elif population_complete:
            state: FeeState = "modelled_settled" if simulated and session_ended else "estimated"
            for component in ("sec", "taf", "cat"):
                amount = getattr(modelled, component)
                portions = apportion_cents(amount, _weights(fills, priced, component))
                shares.extend(
                    FeeShare(f"model:{trade_date}:{component}", key, value, state, settlement_at_ms)
                    for key, value in portions.items()
                    if value
                )
        return FeeAttribution(tuple(shares), unattributed, tuple(dict.fromkeys(unresolved)), None, predicted)
    observed = sum((charge.amount for charge in unique.values() if charge.amount is not None), ZERO)
    if any(charge.amount is None for charge in unique.values()):
        observed = None
    original: dict[str, dict[str, Decimal]] = {}
    refunded: dict[str, Decimal] = {}
    reversed_shares: dict[str, dict[str, Decimal]] = {}
    for charge in sorted(unique.values(), key=lambda row: (row.refund_of is not None, row.charge_id)):
        amount = charge.amount
        reason: str | None = None
        portions: dict[str, Decimal] = {}
        if amount is None or not amount.is_finite():
            reason = "its amount is unknown"
        elif charge.refund_of:
            source = original.get(charge.refund_of)
            cumulative = refunded.get(charge.refund_of, ZERO) + abs(amount)
            if amount > 0 or source is None or cumulative > sum(source.values(), ZERO):
                reason = "its original charge/refund linkage is unavailable or inconsistent"
            else:
                target = apportion_cents(-cumulative, source)
                previous = reversed_shares.get(charge.refund_of, {})
                portions = {key: value - previous.get(key, ZERO) for key, value in target.items()}
                refunded[charge.refund_of] = cumulative
                reversed_shares[charge.refund_of] = target
        elif amount % CENT:
            reason = "the settlement is not in whole cents"
        elif amount < 0:
            reason = "the refund has no original-charge linkage"
        elif any(fill.reported_fee for fill in reported.values()) and not charge.covers_fill_ids:
            reason = "overlap with reported fill fees is unproven"
        elif not session_ended:
            reason = "the fee day is still open and settlement completeness is not proven"
        elif not population_complete or not activities_complete:
            reason = "the fee population or activity window is incomplete"
        elif charge.native_order_id:
            subjects = {fill.subject_id for fill in fills if fill.native_order_id == charge.native_order_id}
            if len(subjects) == 1:
                portions = {subjects.pop(): amount}
            else:
                reason = "the order does not have one proven custody owner"
        else:
            try:
                portions = apportion_cents(amount, _weights(fills, priced, charge.component))
            except ValueError as exc:
                reason = str(exc)
        if reason is not None:
            unresolved.append(
                f"Fee {charge.charge_id} is account-unattributed: {reason}. Reconcile account fee evidence."
            )
            if amount is not None and amount.is_finite():
                unattributed += amount
            continue
        if charge.refund_of is None:
            original[charge.charge_id] = portions
        shares.extend(
            FeeShare(charge.charge_id, key, value, "observed", charge.observed_at_ms)
            for key, value in sorted(portions.items())
            if value
        )
    return FeeAttribution(tuple(shares), unattributed, tuple(dict.fromkeys(unresolved)), observed, predicted)
