"""Canonical ownership of dated Alpaca fees; no independent fee ledger.

Formula: cents_i = floor(C * w_i / sum(w)); leftover cents go to the
largest exact fractional remainders, ties ordered by custody subject ID.
Reference: PRD #2540 fee attribution contract and Hamilton apportionment;
  ADR 0059 fee attribution amendment; ADR 0076 for option fills. A fill's
  weight is what its own pinned broker model predicts, and nothing else:
  app.broker.alpaca.regulatory_fees for a fill in shares,
  app.broker.alpaca.option_fees for a fill of an option contract.
Canonical implementation: this file.
Validated against: tests/services/test_alpaca_fee_attribution.py,
  tests/services/test_option_day_fee_rows.py and
  tests/fixtures/golden/alpaca-fee-attribution/ (exact integer-cent parity).

Amounts remain Decimal inside the authority. The largest-remainder split is
``money.apportion_units``, which compares remainders as exact fractions, so
global Decimal precision cannot decide who gets a cent.
Missing population, linkage or overlap evidence is an unresolved account charge,
never a guessed deployment debit. Simulations never consume broker activities.

A fill of an option contract is one more fee-bearing fill of its day (ADR
0076). The option fee model prices it and the stock model never does. Until
a fee row covers it, it is charged the option model's own figure: SEC, TAF,
CAT and ORF each summed over the day's option fills and rounded up to the
cent, OCC rounded up on each fill, and shared among the bots by this file's one rule. On a simulated
account that is the whole of it. On a real account it is an estimate until
a fee row of its day is seen.

A posted fee row is shared among every fill in its scope, stock and option
alike, each weighted by what its own model predicts: for the row's fee type
when the row names one, else for all of that model's types together. The
stock model predicts no ORF and no OCC fee, so a row of either type falls on
option fills alone. The row's total is kept to the cent, and each share is
``observed`` as a stock fill's is: the total is Alpaca's, the split is the
models'. An option fill is never by itself a reason to leave a row unowned.

Alpaca Paper option fee activities have now been seen (#3225). Six attribution
gaps remain open. ADR 0076 decision 19 originally named seven; #3308 closed
(b), the outside option multiplier gap. The remaining letters are:

- (a) A row is shared among the fills of the day it is dated. If Alpaca
  dates an option fee by the day it posts, and that day has only stock
  fills, those stock bots are charged it as ``observed`` and nothing reads
  unresolved. The option fill it was for keeps its estimate, which no row
  then replaces and which goes on being held back from cash: the fee is
  counted twice, once on each bot. If that day has no fill at all the row
  has no weight to follow and is unowned, as any such row always was.
- (c) An estimate whose rows never come goes on being held back from cash,
  as a stock fill's does. Nothing bounds it.
- (d) No real row's fee type is read: the Clerk's activity record does not
  carry it, so every real row is shared by all types together.
- (e) Any untyped row, of any size, ends every estimate of its day. A row
  of one cent is shared out and leaves no estimate beside it. A row of
  nothing charges nothing and leaves none either.
- (f) A row dated to a New York day that has not ended is nobody's until
  midnight, and the account's fees read unresolved until then. Harmless for
  stock fees, which post after the close. If Alpaca posts an option fee with
  the fill, the first option fill of a day holds every bot's entries until
  midnight.
- (g) A negative row can never be linked to the charge it refunds: the
  Clerk's fee evidence carries no such link. So a negative row on an option
  day is nobody's and leaves the account's fees unresolved.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal, Inexact, localcontext
from typing import Any, Literal

from app.broker.alpaca.clerk.money import apportion_units, money_context
from app.broker.alpaca.option_fees import (
    OptionFeeComponent,
    OptionFillFees,
    OptionSessionFees,
    fees_for_option_fill,
    settle_option_session,
)
from app.broker.alpaca.regulatory_fees import (
    FeeComponent,
    FillFees,
    RateNotPinnedError,
    SessionFees,
    ceil_cents,
    fees_for_fill,
    settle_session,
)
from app.broker.contract.models import BrokerActivity, OrderSide
from app.utils.session_anchors import et_date_at_ms

ZERO = Decimal("0")
CENT = Decimal("0.01")
FeeState = Literal["estimated", "modelled_settled", "observed"]
COMPONENTS: tuple[FeeComponent, ...] = ("sec", "taf", "cat")
OPTION_COMPONENTS: tuple[OptionFeeComponent, ...] = ("sec", "taf", "cat", "orf", "occ")


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
    # When the broker executed it (int64 ms UTC); external fills carry it so
    # the account-money read can lot them in execution order.
    occurred_at_ms: int | None = None
    # Set for a fill of an option contract: the shares one contract's price
    # stands for. Its quantity is then contracts and its price dollars a share.
    multiplier: int | None = None


@dataclass(frozen=True)
class FeeCharge:
    """Normalized evidence, not heuristics on the vendor's description text.

    ``covers_fill_ids`` is only populated by an explicitly linked source.
    Unlinked overlapping fill fees/activities cannot be added safely. A refund
    names its original activity; a missing original remains unresolved.
    ``component`` is the fee type the charge names, one of the five the stock
    and the option fee models know between them; ``None`` when it names none.
    """

    charge_id: str
    amount: Decimal | None
    observed_at_ms: int
    native_order_id: str | None = None
    component: OptionFeeComponent | None = None
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
    # Normalized account-unattributed execution evidence from custody's era,
    # shared with the cash projection. Its notional is never charged to a
    # bot's fee or P&L.
    external_fills: tuple[FeeFill, ...] = ()
    # Tracked outside orders' executions from before custody began (H35), as
    # filled quantity per broker order id and nothing else: their shares and
    # cash are inside the account custody started from, so they only complete
    # the order's execution population and are never priced or lotted.
    pre_custody_quantities: Mapping[str, Decimal] = field(default_factory=dict)
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
                if charge.observed_at_ms >= cash_seen_before_ms
            ),
            ZERO,
        ) + sum(
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


def attribute_simulated_option_fees(
    *,
    trade_date_ms: int,
    fills: Sequence[FeeFill],
    population_complete: bool,
    session_ended: bool,
    settlement_at_ms: int,
) -> FeeAttribution:
    """One day's option fills of a simulated account, charged by the option fee model.

    Formula: for each fee type, the day's charge is the sum of the fills'
      accruals rounded up to the cent, OCC's rounded up fill by fill
      (``settle_option_session``); it is
      shared among the custody subjects in proportion to their own accruals
      of that type (``apportion_cents``).
    Reference: ADR 0076; the rates and the settlement rule are
      ``app/broker/alpaca/option_fees.py``'s, which cites Alpaca's fee schedule.
    Canonical implementation: ``option_fees`` for the fees, and
      :func:`attribute_session_fees` for the split, with no fee row to share.
    Validated against: tests/services/test_simulated_option_fee_attribution.py.

    ``fills`` are the day's fills of option contracts: each carries its
    contract's multiplier. A fill with none is refused: the day is
    unresolved and nothing is charged. The fills settle as a population of
    their own. A real day that also traded stock rounds SEC, TAF and CAT
    once across both; how Alpaca does that has not been seen, so a simulated
    day rounds the two apart, which is never less than rounding them
    together. Every amount is the model's: nothing is observed in a
    simulation.
    """
    if not fills:
        return FeeAttribution((), ZERO, (), None, ZERO)
    # An option fill is never priced as shares (ADR 0076). The shared rule below reads a fill with no
    # multiplier as a fill in shares, so one is refused here: unresolved, and nothing is charged.
    if any(fill.multiplier is None for fill in fills):
        unresolved = [] if population_complete else [
            "The complete fee-bearing fill population is unavailable. Reconcile account executions."
        ]
        unresolved.append(
            "An option fill cannot be priced by the option fee model: an option fill needs its contract's multiplier."
        )
        return FeeAttribution((), ZERO, tuple(unresolved), None, None)
    return attribute_session_fees(
        trade_date_ms=trade_date_ms, fills=fills, charges=(), population_complete=population_complete,
        activities_complete=True, simulated=True, session_ended=session_ended, settlement_at_ms=settlement_at_ms,
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
    """Exact Hamilton shares, including deterministic signed reversal shares.

    The unit split itself is ``money.apportion_units``, the one canonical
    largest-remainder rule; this adds only the whole-cent sign convention.
    """
    if not amount.is_finite() or amount % CENT:
        raise ValueError("fee settlement must be finite whole cents")
    if amount and not any(weights.values()):
        raise ValueError("positive fee has no predicted weight")
    units = apportion_units(int(abs(amount) / CENT), weights)
    sign = -1 if amount < 0 else 1
    return {key: Decimal(sign * cents) * CENT for key, cents in units.items()}


@dataclass(frozen=True)
class _ModelledFills:
    """One day's fills of one kind, each priced by that kind's own fee model.

    ``charge`` is what the model charges the day for them, each fee type
    rounded up to the cent once (an option fill's OCC, on each fill);
    ``None`` when a rate is not pinned.
    """

    fills: Sequence[FeeFill]
    priced: Sequence[FillFees | OptionFillFees]
    components: tuple[OptionFeeComponent, ...]
    charge: SessionFees | OptionSessionFees | None
    # An estimate's share is filed under ``<estimate_id>:<fee type>``.
    estimate_id: str
    # What is said when no estimate can be made.
    not_pinned: str


def _day_charge[S](priced: Sequence[Any], settle: Callable[[Sequence[Any]], S]) -> S | None:
    """What a model charges one day for ``priced``; ``None`` when a rate is not pinned."""
    try:
        with localcontext() as rounding:
            rounding.traps[Inexact] = False
            return settle(priced)
    except RateNotPinnedError:
        return None


def _modelled_stock_fills(trade_date_ms: int, fills: Sequence[FeeFill]) -> _ModelledFills:
    priced = [
        fees_for_fill(trade_date_ms=trade_date_ms, side=fill.side, quantity=fill.quantity, fill_price=fill.price)
        for fill in fills
    ]
    return _ModelledFills(
        fills, priced, COMPONENTS, _day_charge(priced, settle_session), f"model:{et_date_at_ms(trade_date_ms)}",
        "Fee rates are not pinned for this day. Review the fee model before deploying.",
    )


def _modelled_option_fills(trade_date_ms: int, fills: Sequence[FeeFill]) -> _ModelledFills:
    """Raises ``ValueError`` for a fill the option fee model refuses to price."""
    priced = [
        fees_for_option_fill(
            trade_date_ms=trade_date_ms, side=fill.side, contracts=fill.quantity, premium=fill.price,
            multiplier=fill.multiplier,
        )
        for fill in fills
    ]
    return _ModelledFills(
        fills, priced, OPTION_COMPONENTS, _day_charge(priced, settle_option_session),
        f"model:{et_date_at_ms(trade_date_ms)}:option",
        "Option fee rates are not pinned for this day. Review the option fee model before deploying.",
    )


def _components(price: FillFees | OptionFillFees) -> tuple[OptionFeeComponent, ...]:
    """The fee types the model that priced a fill predicts."""
    return OPTION_COMPONENTS if isinstance(price, OptionFillFees) else COMPONENTS


def _weight(price: FillFees | OptionFillFees, name: OptionFeeComponent) -> Decimal | None:
    """What one fill carries in one fee type's charge: what its model predicts, in whole cents for OCC.

    OCC is posted, and charged by the option model, per execution, so what a
    fill carries of it is its own rounded cents. Weighing by the unrounded
    accrual would tie bots whose executions round differently and hand the
    odd cent to the first name.
    """
    value = getattr(price, name)
    if name != "occ" or value is None:
        return value
    with localcontext() as rounding:
        rounding.traps[Inexact] = False
        return ceil_cents(value)


def _weights(
    fills: Sequence[FeeFill], priced: Sequence[FillFees | OptionFillFees], component: OptionFeeComponent | None
) -> dict[str, Decimal]:
    """Each subject's weight in one charge: what its fills' own models predict.

    For the one fee type ``component`` names, else for all the types of each
    fill's model together. A model with no fee of the named type predicts
    nothing for it, so its fills carry no weight in that charge.
    """
    weights: dict[str, Decimal] = {}
    for fill, price in zip(fills, priced, strict=True):
        known = _components(price)
        if component is None:
            components = known
        elif component in known:
            components = (component,)
        else:
            components = ()
        values = [_weight(price, name) for name in components]
        if any(value is None for value in values):
            raise ValueError("the fee component has no pinned rate")
        weights[fill.subject_id] = weights.get(fill.subject_id, ZERO) + sum(values, ZERO)
    return weights


def _provisions_by_fill(modelled: _ModelledFills) -> dict[tuple[str, OptionFeeComponent], Decimal]:
    """Partition existing subject cents for replacement, never re-round a scope.

    First preserve the canonical full-population subject allocation. Within
    each subject only, the same Hamilton rule partitions those cents by fill
    identity so settling one order cannot erase another order's provision.
    """
    provisions: dict[tuple[str, OptionFeeComponent], Decimal] = {}
    grouped: dict[str, list[tuple[FeeFill, FillFees | OptionFillFees]]] = {}
    for fill, price in zip(modelled.fills, modelled.priced, strict=True):
        grouped.setdefault(fill.subject_id, []).append((fill, price))
    for component in modelled.components:
        subjects = apportion_cents(
            getattr(modelled.charge, component), _weights(modelled.fills, modelled.priced, component)
        )
        for subject, amount in subjects.items():
            weights = {fill.fill_id: _weight(price, component)
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
    trade_date_ms: int,
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

    A fill carrying a multiplier is a fill of an option contract and is priced
    by the option fee model; every other fill by the stock model. A charge is
    shared among the fills in its scope by those predictions, stock and
    option fills together (``_weights``). What no charge has settled stays
    each model's own estimate, the stock fills and the option fills each
    rounded as a day of their own.

    Formula: the module's, with w_i the fill's own model's prediction.
    Reference: the module's; ADR 0076 for the option fills.
    Canonical implementation: this function.
    Validated against: tests/services/test_alpaca_fee_attribution.py and
      tests/services/test_option_day_fee_rows.py.
    """
    shares: list[FeeShare] = []
    unresolved: list[str] = []
    unattributed = ZERO
    unattributed_charges: list[UnattributedCharge] = []
    stock_fills = [fill for fill in fills if fill.multiplier is None]
    option_fills = [fill for fill in fills if fill.multiplier is not None]
    modelled = [_modelled_stock_fills(trade_date_ms, stock_fills)]
    if option_fills:
        try:
            modelled.append(_modelled_option_fills(trade_date_ms, option_fills))
        except ValueError as exc:
            # No row can be weighed against a fill its model cannot price: the
            # day is read as one whose fills are not all known, and says why.
            unshared = attribute_session_fees(
                trade_date_ms=trade_date_ms, fills=stock_fills, charges=charges, population_complete=False,
                activities_complete=activities_complete, simulated=simulated, session_ended=session_ended,
                settlement_at_ms=settlement_at_ms,
            )
            return replace(
                unshared, predicted_total=None,
                unresolved=(f"An option fill cannot be priced by the option fee model: {exc}.", *unshared.unresolved),
            )
    predicted: Decimal | None = None
    if all(kind.charge is not None for kind in modelled):
        predicted = sum((kind.charge.total for kind in modelled), ZERO)
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
    price_by_id = {
        fill.fill_id: price for kind in modelled for fill, price in zip(kind.fills, kind.priced, strict=True)
    }
    settled = {(key, component) for key in reported for component in _components(price_by_id[key])}
    replaced_reported: set[str] = set()
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
                settled.update(
                    (fill.fill_id, component) for fill in selected
                    for component in ((charge.component,) if charge.component else _components(price_by_id[fill.fill_id]))
                )
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
    state: FeeState = "modelled_settled" if simulated and session_ended else "estimated"
    for kind in modelled:
        pending = {(fill.fill_id, component) for fill in kind.fills for component in kind.components} - settled
        if pending and kind.charge is None:
            unresolved.append(kind.not_pinned)
        elif pending and population_complete:
            provisions = _provisions_by_fill(kind)
            for component in kind.components:
                portions: dict[str, Decimal] = {}
                for fill in kind.fills:
                    if (fill.fill_id, component) in pending:
                        portions[fill.subject_id] = portions.get(fill.subject_id, ZERO) + provisions[fill.fill_id, component]
                shares.extend(FeeShare(f"{kind.estimate_id}:{component}", subject, amount, state, settlement_at_ms)
                              for subject, amount in sorted(portions.items()) if amount)
    return FeeAttribution(
        tuple(shares), unattributed, tuple(dict.fromkeys(unresolved)), observed, predicted,
        unattributed_charges=tuple(unattributed_charges),
    )
