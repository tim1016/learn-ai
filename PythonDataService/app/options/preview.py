"""The figures a preview shows for a resolution: each picked leg, the structure's money, and what it leans.

A resolution says which contract each leg's rule came to, on which quote. A
preview turns that into what the owner reads before deploying. It asks the
questions an entry asks (``app.options.entry``), in the same order, of the
same functions, with one difference: nothing stops at the first "no". A
structure whose round-trip cost is over the owner's limit is still shown
with its figures, and said to be over; picks that are not the named
structure are still shown, with the rule they break.

Formula: gap percent of mid = (ask - bid) / ((bid + ask) / 2) · 100, for one leg's quote;
  round-trip cost percent = round-trip cost / entry commitment · 100.
  Every other figure is another module's: the premium, requirement,
  commitment, loss estimate and round-trip cost are
  ``app/options/requirement.py``'s; the gain, ranges and break-evens are
  ``app/options/at_expiry.py``'s; the lean is ``app/options/lean.py``'s.
Reference: ADR 0076 decisions 2, 5 and 7. The gap as a percent of the mid
  quote is this repo's own definition (#2913).
Canonical implementation: this file for the two percents; the modules named
  above for everything else.
Validated against: tests/options/test_preview.py.

Both percents are exact fractions here. Whoever shows one rounds it for the
eye, and whoever holds one to a limit compares it exactly.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from fractions import Fraction

from app.options.at_expiry import AtExpiry, at_expiry
from app.options.contract import OptionContract
from app.options.lean import Lean, lean
from app.options.quotes import HeldQuote
from app.options.requirement import (
    EntryMoney,
    EntrySize,
    LegQuote,
    StructureMoneyRefused,
    entry_money,
    natural_net_price,
    round_trip_cost,
    round_trip_cost_is_over_limit,
)
from app.options.resolver import Priced, Resolution
from app.options.structure import LegPosition, ResolvedStructure
from app.options.structure_check import StructureRefused, require_named_shape, resolved_structure
from app.schemas.action_plan import ActionPlan, OptionEntryLeg

# No fee rate is pinned for the day, so the entry's fee provision, and with it what is set aside, is not known.
FEE_RATE_NOT_PINNED = "OPTION_FEE_RATE_NOT_PINNED"

# What an entry sets aside for its own fees, in whole cents, or None when no rate is pinned for it.
FeeProvision = Callable[[ResolvedStructure, Mapping[str, LegQuote], EntrySize], int | None]


def bid_ask_gap_percent_of_mid(bid: Decimal, ask: Decimal) -> Fraction:
    """One quote's bid-ask gap as a percent of its mid, exact."""
    if not (bid.is_finite() and ask.is_finite()) or bid < 0 or ask <= 0 or bid > ask:
        raise ValueError(f"a gap is read from a market that can be crossed; got bid {bid}, ask {ask}")
    return (Fraction(ask) - Fraction(bid)) * 200 / (Fraction(bid) + Fraction(ask))


def percent_of(part_cents: int, whole_cents: int) -> Fraction:
    """``part_cents`` as a percent of ``whole_cents``, exact."""
    if whole_cents <= 0:
        raise ValueError(f"a percent is taken of an amount above zero; got {whole_cents}")
    return Fraction(part_cents * 100, whole_cents)


@dataclass(frozen=True, slots=True)
class PickedLeg:
    """One leg as it was picked: its contract, the quote it was picked on, and what the model made of it."""

    leg_id: str
    position: LegPosition
    ratio: int
    contract: OptionContract
    multiplier: int
    quote: HeldQuote
    gap_percent_of_mid: Fraction
    # What the model was asked about the pick and what it answered. Its delta is None when it had no answer.
    priced: Priced | None
    # The provider's own delta, when it sent one. Shown beside ours, never used.
    vendor_delta: float | None


@dataclass(frozen=True, slots=True)
class NotAsBuilt:
    """Why the picked contracts cannot be entered as the structure the owner built: a typed refusal."""

    reason: str
    leg_ids: tuple[str, ...]
    detail: str


@dataclass(frozen=True, slots=True)
class StructureFigures:
    legs: tuple[PickedLeg, ...]
    # The first reason the picks are not the structure built, or cannot be set aside for. None: they are, and can.
    not_as_built: NotAsBuilt | None = None
    # Each figure below is None when it could not be worked out.
    size: EntrySize | None = None
    # What one structure is entered at, per share, at the quotes: positive is paid, negative received.
    net_price: Decimal | None = None
    fee_provision_cents: int | None = None
    money: EntryMoney | None = None
    round_trip_cost_cents: int | None = None
    round_trip_cost_percent: Fraction | None = None
    over_cost_limit: bool | None = None
    # None beside money: the legs do not share an expiry, so nothing at expiry is estimated.
    at_expiry: AtExpiry | None = None
    # None when a leg has no delta of ours; ``lean_unknown_leg_id`` then names the first such leg.
    lean: Lean | None = None
    lean_unknown_leg_id: str | None = None


def _picked_legs(plan: ActionPlan, resolution: Resolution, *, model_is_known: bool) -> tuple[PickedLeg, ...]:
    evidence = {leg.leg_id: leg for leg in resolution.evidence.legs}
    legs = []
    for leg in plan.on_enter:
        assert isinstance(leg, OptionEntryLeg)
        (pick,) = [candidate for candidate in evidence[leg.leg_id].candidates if candidate.passed_over is None]
        quote = resolution.quotes[leg.leg_id]
        assert quote.bid is not None and quote.ask is not None
        legs.append(
            PickedLeg(
                leg_id=leg.leg_id,
                position=leg.position,
                ratio=leg.qty_ratio,
                contract=resolution.picked[leg.leg_id],
                multiplier=resolution.multipliers[leg.leg_id],
                quote=quote,
                gap_percent_of_mid=bid_ask_gap_percent_of_mid(quote.bid.price, quote.ask.price),
                priced=pick.priced if model_is_known else None,
                vendor_delta=pick.vendor_delta,
            )
        )
    return tuple(legs)


def _refusal(refused: StructureRefused) -> NotAsBuilt:
    return NotAsBuilt(str(refused.reason), refused.leg_ids, str(refused))


def structure_figures(
    plan: ActionPlan,
    resolution: Resolution,
    *,
    units: int,
    cost_limit_percent: Decimal,
    fee_provision: FeeProvision,
    model_is_known: bool,
) -> StructureFigures:
    """Everything a preview shows of ``resolution``, for ``units`` structures of ``plan``.

    ``model_is_known`` is false when the pricing model was given inputs that
    are not known to be true, such as dividends that could not be looked up.
    The picks stand, where no rule picked by delta, but no delta of ours is
    shown and no lean is worked out from one.
    """
    shown = StructureFigures(legs=_picked_legs(plan, resolution, model_is_known=model_is_known))
    try:
        structure = resolved_structure(plan, resolution.picked)
    except StructureRefused as refused:
        return replace(shown, not_as_built=_refusal(refused))
    listed = {leg.multiplier for leg in shown.legs}
    if len(listed) != 1:
        # One order's legs are priced against one underlying in one unit.
        raise ValueError(f"every leg of a structure has one multiplier; these have {sorted(listed)}")
    size = EntrySize(multiplier=listed.pop(), units=units)
    markets = {}
    deltas: dict[str, float] = {}
    for leg in shown.legs:
        assert leg.quote.bid is not None and leg.quote.ask is not None
        markets[leg.leg_id] = LegQuote(bid=leg.quote.bid.price, ask=leg.quote.ask.price)
        if leg.priced is not None and leg.priced.delta is not None:
            deltas[leg.leg_id] = leg.priced.delta
    net_price = natural_net_price(structure, markets)
    cost = round_trip_cost(structure, markets, size)
    without_delta = next((leg.leg_id for leg in shown.legs if leg.leg_id not in deltas), None)
    shown = replace(
        shown,
        size=size,
        net_price=net_price,
        round_trip_cost_cents=cost,
        lean=lean(structure, size, deltas) if without_delta is None else None,
        lean_unknown_leg_id=without_delta,
    )
    try:
        require_named_shape(plan, structure, net_price=net_price)
    except StructureRefused as refused:
        shown = replace(shown, not_as_built=_refusal(refused))

    fees = fee_provision(structure, markets, size)
    if fees is None:
        unpinned = NotAsBuilt(
            FEE_RATE_NOT_PINNED, (), "no fee rate is pinned for today, so what an entry sets aside is not known"
        )
        return replace(shown, not_as_built=shown.not_as_built or unpinned)
    shown = replace(shown, fee_provision_cents=fees)
    try:
        money = entry_money(structure, size, net_price=net_price, fee_provision_cents=fees)
    except StructureMoneyRefused as refused:
        return replace(shown, not_as_built=shown.not_as_built or NotAsBuilt(str(refused.reason), (), str(refused)))
    return replace(
        shown,
        money=money,
        round_trip_cost_percent=percent_of(cost, money.entry_commitment_cents),
        over_cost_limit=round_trip_cost_is_over_limit(
            cost_cents=cost, commitment_cents=money.entry_commitment_cents, limit_percent=cost_limit_percent
        ),
        at_expiry=at_expiry(structure, size, net_price=net_price),
    )
