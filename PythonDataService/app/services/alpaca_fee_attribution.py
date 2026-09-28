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

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, Inexact, localcontext
from fractions import Fraction
from typing import Any, Literal

from app.broker.alpaca.clerk.money import money_context
from app.broker.alpaca.regulatory_fees import (
    FeeComponent,
    FillFees,
    RateNotPinnedError,
    SessionFees,
    fees_for_fill,
    settle_session,
)
from app.broker.contract.models import BrokerActivity, OrderSide

ZERO = Decimal("0")
CENT = Decimal("0.01")
FeeState = Literal["estimated", "modelled_settled", "observed"]
COMPONENTS: tuple[FeeComponent, ...] = ("sec", "taf", "cat")


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
class UnattributedCharge:
    """One account-level obligation kept out of per-bot attribution.

    Recognition follows the same observation-overlap rule as observed
    shares: a cash observation taken after ``observed_at_ms`` already
    includes the posted charge, so it must stop claiming availability
    ("count it once relative to the cash observation", PRD #2540).
    """

    charge_id: str
    amount: Decimal
    observed_at_ms: int


@dataclass(frozen=True)
class FeeAttribution:
    shares: tuple[FeeShare, ...]
    unattributed: Decimal
    unresolved: tuple[str, ...]
    observed_total: Decimal | None
    predicted_total: Decimal | None
    # Normalized account-unattributed execution evidence, shared with the
    # cash projection. Its notional is never charged to a bot's fee or P&L.
    external_fills: tuple[FeeFill, ...] = ()
    # Per-charge detail behind ``unattributed`` so the cash claim can respect
    # each charge's own observation time instead of double-counting forever.
    unattributed_charges: tuple[UnattributedCharge, ...] = ()

    @property
    def known(self) -> bool:
        return not self.unresolved

    @money_context()
    def total_for(self, subject_id: str) -> Decimal:
        return sum((share.amount for share in self.shares if share.subject_id == subject_id), ZERO)

    @money_context()
    def unobserved_cash_claim(
        self, *, cash_seen_before_ms: int, modelled_fees_seen_before_ms: int | None = None,
        subject_id: str | None = None,
    ) -> Decimal:
        """Disjoint fee claim: fill-fees stay in the existing unseen-fill claim.

        An observed activity is only safely inside broker cash after a later
        cash observation. Its trade-date timestamp does not prove recognition.
        The same rule gates each account-unattributed charge: it claims
        availability only until the first cash observation taken after its
        evidence was observed, and a negative (refund-like) charge never
        manufactures availability. Caller MUST refuse admission when
        ``known`` is false.
        """
        return sum(
            (
                max(ZERO, charge.amount)
                for charge in self.unattributed_charges
                if subject_id is None and charge.observed_at_ms >= cash_seen_before_ms
            ),
            ZERO,
        ) + sum(
            (
                max(ZERO, share.amount)
                for share in self.shares
                if (subject_id is None or share.subject_id == subject_id)
                and not share.included_in_fill
                and (share.state != "observed" or share.observed_at_ms >= cash_seen_before_ms)
                and not (
                    share.state == "modelled_settled"
                    and modelled_fees_seen_before_ms is not None
                    and share.observed_at_ms < modelled_fees_seen_before_ms
                )
            ),
            ZERO,
        )


@dataclass(frozen=True)
class CollapsedDeliveries[T]:
    """One evidence stream after the duplicate-delivery rule."""

    unique: dict[str, T]
    # Every disagreeing copy of an identity, first-seen copy first.
    conflicts: dict[str, tuple[T, ...]]


def collapse_deliveries[T](
    deliveries: Iterable[T], *, identity: Callable[[T], str], economics: Callable[[T], object],
) -> CollapsedDeliveries[T]:
    """The one duplicate-delivery rule for broker fee evidence.

    A repeated identity is one fact. Observation time is delivery metadata;
    every other field is economic identity. Deliveries arrive in recorded
    order and the first copy is retained, so repolling never moves a
    recognized charge's observation boundary (a later copy may carry an
    earlier clock stamp). Copies whose economics disagree are conflicts;
    every caller fails closed on them.
    """
    unique: dict[str, T] = {}
    conflicts: dict[str, list[T]] = {}
    for delivery in deliveries:
        key = identity(delivery)
        first = unique.setdefault(key, delivery)
        if first is not delivery and economics(first) != economics(delivery):
            conflicts.setdefault(key, [first]).append(delivery)
    return CollapsedDeliveries(unique, {key: tuple(copies) for key, copies in conflicts.items()})


def activity_economics(activity: BrokerActivity) -> dict[str, Any]:
    """A broker activity's economic identity: everything but its delivery time."""
    return activity.model_dump(exclude={"observed_at_ms"})


def collapse_activity_deliveries(activities: Iterable[BrokerActivity]) -> CollapsedDeliveries[BrokerActivity]:
    return collapse_deliveries(activities, identity=lambda row: row.activity_id, economics=activity_economics)


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
        components = (component,) if component else COMPONENTS
        values = [getattr(price, name) for name in components]
        if any(value is None for value in values):
            raise ValueError("the fee component has no pinned rate")
        weights[fill.subject_id] = weights.get(fill.subject_id, ZERO) + sum(values, ZERO)
    return weights


def _provisions_by_fill(
    fills: Sequence[FeeFill], priced: Sequence[FillFees], modelled: SessionFees,
) -> dict[tuple[str, FeeComponent], Decimal]:
    """Partition existing subject cents for replacement, never re-round a scope.

    First preserve the canonical full-population subject allocation. Within
    each subject only, the same Hamilton rule partitions those cents by fill
    identity so settling one order cannot erase another order's provision.
    """
    provisions: dict[tuple[str, FeeComponent], Decimal] = {}
    grouped: dict[str, list[tuple[FeeFill, FillFees]]] = {}
    for fill, price in zip(fills, priced, strict=True):
        grouped.setdefault(fill.subject_id, []).append((fill, price))
    for component in COMPONENTS:
        subjects = apportion_cents(getattr(modelled, component), _weights(fills, priced, component))
        for subject, amount in subjects.items():
            weights = {fill.fill_id: getattr(price, component)
                       for fill, price in grouped[subject]}
            for fill_id, value in apportion_cents(amount, weights).items():
                provisions[fill_id, component] = value
    return provisions


def _charge_scope(charge: FeeCharge, fills: Sequence[FeeFill]) -> list[FeeFill]:
    """Only explicit fill/order identity narrows an otherwise daily charge."""
    selected = list(fills)
    if charge.covers_fill_ids:
        wanted = set(charge.covers_fill_ids)
        selected = [fill for fill in selected if fill.fill_id in wanted]
        if {fill.fill_id for fill in selected} != wanted:
            raise ValueError("its explicit fill coverage is unavailable")
    if charge.native_order_id:
        if charge.covers_fill_ids and any(fill.native_order_id != charge.native_order_id for fill in selected):
            raise ValueError("its fill and order linkage disagree")
        selected = [fill for fill in selected if fill.native_order_id == charge.native_order_id]
        if len({fill.subject_id for fill in selected}) != 1:
            raise ValueError("the order does not have one proven custody owner")
    return selected


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
    copies fail closed. Real settlements replace only the corresponding
    fill/order and component provisions; unmatched obligations remain estimated.
    """
    shares: list[FeeShare] = []
    unresolved: list[str] = []
    unattributed = ZERO
    unattributed_charges: list[UnattributedCharge] = []
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
        collapsed = collapse_deliveries(
            charges, identity=lambda charge: charge.charge_id,
            economics=lambda charge: replace(charge, observed_at_ms=0),
        )
        unique = collapsed.unique
        unresolved.extend(
            f"Conflicting fee evidence for {charge_id}. Reconcile broker activities." for charge_id in collapsed.conflicts
        )
        if not activities_complete:
            unresolved.append("The broker activity read does not cover this fee day.")
    reported = {fill.fill_id: fill for fill in fills if fill.reported_fee is not None and not simulated}
    settled = {(key, component) for key in reported for component in COMPONENTS}
    replaced_reported: set[str] = set()
    price_by_id = {fill.fill_id: price for fill, price in zip(fills, priced, strict=True)}
    observed = sum((charge.amount for charge in unique.values() if charge.amount is not None), ZERO)
    if not unique or any(charge.amount is None for charge in unique.values()):
        observed = None
    original: dict[str, dict[str, Decimal]] = {}
    refunded: dict[str, Decimal] = {}
    remaining_shares: dict[str, dict[str, Decimal]] = {}
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
                remaining = remaining_shares.get(charge.refund_of, source)
                portions = apportion_cents(amount, remaining)
                refunded[charge.refund_of] = cumulative
                remaining_shares[charge.refund_of] = {
                    key: value + portions.get(key, ZERO) for key, value in remaining.items()
                }
        elif amount % CENT:
            reason = "the settlement is not in whole cents"
        elif amount < 0:
            reason = "the refund has no original-charge linkage"
        elif not session_ended:
            reason = "the fee day is still open and settlement completeness is not proven"
        elif not population_complete or not activities_complete:
            reason = "the fee population or activity window is incomplete"
        else:
            try:
                selected = _charge_scope(charge, fills)
                reported_overlap = {fill.fill_id for fill in selected} & reported.keys()
                if reported_overlap and (not reported_overlap <= set(charge.covers_fill_ids) or charge.component is not None):
                    raise ValueError("overlap with reported fill fees is unproven")
                if charge.native_order_id:
                    portions = {selected[0].subject_id: amount}
                else:
                    portions = apportion_cents(amount, _weights(selected, [price_by_id[fill.fill_id] for fill in selected], charge.component))
                settled.update((fill.fill_id, component) for fill in selected
                               for component in ((charge.component,) if charge.component else COMPONENTS))
                replaced_reported.update(reported_overlap)
            except ValueError as exc:
                reason = str(exc)
        if reason is not None:
            unresolved.append(
                f"Fee {charge.charge_id} is account-unattributed: {reason}. Reconcile account fee evidence."
            )
            if amount is not None and amount.is_finite():
                unattributed += amount
                unattributed_charges.append(UnattributedCharge(charge.charge_id, amount, charge.observed_at_ms))
            continue
        if charge.refund_of is None:
            original[charge.charge_id] = portions
        shares.extend(
            FeeShare(charge.charge_id, key, value, "observed", charge.observed_at_ms)
            for key, value in sorted(portions.items())
            if value
        )
    for key, fill in sorted(reported.items()):
        if key not in replaced_reported:
            shares.append(FeeShare(f"fill:{key}", fill.subject_id, fill.reported_fee, "observed", fill.observed_at_ms, True))
    pending = {(fill.fill_id, component) for fill in fills for component in COMPONENTS} - settled
    if pending and predicted is None:
        unresolved.append("Fee rates are not pinned for this day. Review the fee model before deploying.")
    elif pending and population_complete:
        provisions = _provisions_by_fill(fills, priced, modelled)
        state: FeeState = "modelled_settled" if simulated and session_ended else "estimated"
        for component in COMPONENTS:
            portions: dict[str, Decimal] = {}
            for fill in fills:
                if (fill.fill_id, component) in pending:
                    portions[fill.subject_id] = portions.get(fill.subject_id, ZERO) + provisions[fill.fill_id, component]
            shares.extend(FeeShare(f"model:{trade_date}:{component}", subject, amount, state, settlement_at_ms)
                          for subject, amount in sorted(portions.items()) if amount)
    return FeeAttribution(
        tuple(shares), unattributed, tuple(dict.fromkeys(unresolved)), observed, predicted,
        unattributed_charges=tuple(unattributed_charges),
    )
