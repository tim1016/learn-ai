"""The figures a preview shows for a resolution: each picked leg, the structure's money, and what it leans.

A resolution says which contract each leg's rule came to, on which quote. A
preview turns that into what the owner reads before deploying. Its money and
its refusals are the entry's own: one function works both out
(``app.options.entry.entry_figures``), and an entry raises the first refusal
where a preview is shown them all. So a structure whose round-trip cost is
over the owner's limit is still shown with its figures, and said to be over;
a leg quoted wider than the owner's gap limit is named; and picks that are
not the named structure are still shown, with the rule they break.

Formula: round-trip cost percent = round-trip cost / entry commitment · 100.
  Every other figure is another module's: each leg's gap, the limit price and
  the refusals are ``app/options/entry.py``'s; the premium, requirement,
  commitment, loss estimate and round-trip cost are
  ``app/options/requirement.py``'s; the gain, ranges and break-evens are
  ``app/options/at_expiry.py``'s; the lean is ``app/options/lean.py``'s; the
  daily decay is ``app/options/decay.py``'s.
Reference: ADR 0076 decisions 2, 5, 7, 17 and 23.
Canonical implementation: this file for the cost percent; the modules named
  above for everything else.
Validated against: tests/options/test_preview.py, and
  tests/options/test_preview_and_entry_agree.py for the entry's own figures.

Both percents are exact fractions here. Whoever shows one rounds it for the
eye, and whoever holds one to a limit compares it exactly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

from app.options.at_expiry import AtExpiry, at_expiry
from app.options.contract import OptionContract
from app.options.decay import DecayNotEstimated, LegDecay, StructureDecay, decay_not_estimated, structure_daily_decay
from app.options.entry import (
    EntryFigures,
    EntryRefusal,
    FeeProvision,
    LegGapOverLimit,
    bid_ask_gap_percent_of_mid,
    entry_figures,
)
from app.options.lean import Lean, lean
from app.options.quotes import HeldQuote
from app.options.requirement import EntryMoney, EntrySize, StructureMoneyRefusal
from app.options.resolver import Priced, Resolution
from app.options.structure import LegPosition, ResolvedStructure
from app.schemas.action_plan import ActionPlan, OptionEntryLeg

# No fee rate is pinned for the day, so the entry's fee provision, and with it what is set aside, is not known.
FEE_RATE_NOT_PINNED = "OPTION_FEE_RATE_NOT_PINNED"

# The two refusals that say an entry would be skipped right now and not that the picks are another structure:
# a leg quoted wider than the owner's limit, and a round-trip cost over the owner's limit. Both pass by themselves.
_SKIPPED_FOR_NOW = frozenset({StructureMoneyRefusal.LEG_GAP_OVER_LIMIT, StructureMoneyRefusal.COST_OVER_LIMIT})


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
    # Whether its gap is wider than the owner's limit, so that an entry would be skipped. None: not judged,
    # because the picks are not the plan's legs and no entry could be made of them.
    over_gap_limit: bool | None = None


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
    # Every reason an entry on these picks and quotes would be refused, in the order an entry asks: the one
    # above when there is one, each leg over the gap limit, a round-trip cost over its limit. Empty: none.
    entry_refusals: tuple[NotAsBuilt, ...] = ()
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
    # What a day takes from the entry, or why that is not estimated. None when the picks are not a structure.
    decay: StructureDecay | None = None


def _picked_legs(
    plan: ActionPlan,
    resolution: Resolution,
    *,
    model_is_known: bool,
    over_gap_limit: frozenset[str] | None,
) -> tuple[PickedLeg, ...]:
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
                over_gap_limit=None if over_gap_limit is None else leg.leg_id in over_gap_limit,
            )
        )
    return tuple(legs)


def _refusal(refused: EntryRefusal) -> NotAsBuilt:
    leg_ids = (refused.leg_id,) if isinstance(refused, LegGapOverLimit) else getattr(refused, "leg_ids", ())
    return NotAsBuilt(str(refused.reason), tuple(leg_ids), str(refused))


def _refusals_in_an_entrys_order(worked: EntryFigures) -> tuple[NotAsBuilt, ...]:
    """The entry's refusals, with a day that has no fee rate pinned said where the money would have been asked:
    after the shape and each leg's gap. With no provision the money is never worked out, so nothing follows it."""
    told = [_refusal(refused) for refused in worked.refusals]
    if worked.structure is not None and worked.fee_provision_cents is None:
        told.append(
            NotAsBuilt(FEE_RATE_NOT_PINNED, (), "no fee rate is pinned for today, so what an entry sets aside is not known")
        )
    return tuple(told)


def structure_figures(
    plan: ActionPlan,
    resolution: Resolution,
    *,
    units: int,
    leg_gap_limit_percent: Decimal,
    cost_limit_percent: Decimal,
    fee_provision: FeeProvision,
    model_is_known: bool,
    daily_decay: Mapping[str, LegDecay] | None = None,
) -> StructureFigures:
    """Everything a preview shows of ``resolution``, for ``units`` structures of ``plan``.

    The two limits are the owner's: the widest bid-ask gap a leg may have,
    and the most opening and closing at once may cost. An entry is held to
    both, and both are judged here by the entry's own function.

    ``model_is_known`` is false when the pricing model was given inputs that
    are not known to be true, such as dividends that could not be looked up.
    The picks stand, where no rule picked by delta, but no delta of ours is
    shown, no lean is worked out from one, and no daily decay is estimated.

    ``daily_decay`` is each pick's decay as the search worked it out
    (``app.options.decay.picks_daily_decay``), by leg id. Nothing is priced
    here. Handed none on a model that is known, the figures carry no decay
    at all: nobody asked for one. Only the structure's decay is carried. A
    leg's own is its contract's figure, a share, unsigned by the leg's side,
    and is shown to nobody.
    """
    if not model_is_known:
        daily_decay = None
    worked = entry_figures(
        plan,
        picked=resolution.picked,
        multipliers=resolution.multipliers,
        quotes=resolution.quotes,
        units=units,
        fee_provision=fee_provision,
        leg_gap_limit_percent=leg_gap_limit_percent,
        cost_limit_percent=cost_limit_percent,
    )
    structure, size = worked.structure, worked.size
    legs = _picked_legs(
        plan,
        resolution,
        model_is_known=model_is_known,
        over_gap_limit=None if structure is None else worked.legs_over_gap_limit,
    )
    refusals = _refusals_in_an_entrys_order(worked)
    not_as_built = next((refusal for refusal in refusals if refusal.reason not in _SKIPPED_FOR_NOW), None)
    if structure is None or size is None:
        return StructureFigures(legs=legs, not_as_built=not_as_built, entry_refusals=refusals)
    assert worked.net_price is not None and worked.round_trip_cost_cents is not None
    deltas = {leg.leg_id: leg.priced.delta for leg in legs if leg.priced is not None and leg.priced.delta is not None}
    without_delta = next((leg.leg_id for leg in legs if leg.leg_id not in deltas), None)
    money = worked.money
    return StructureFigures(
        legs=legs,
        not_as_built=not_as_built,
        entry_refusals=refusals,
        size=size,
        net_price=worked.net_price,
        fee_provision_cents=worked.fee_provision_cents,
        money=money,
        round_trip_cost_cents=worked.round_trip_cost_cents,
        round_trip_cost_percent=(
            None if money is None else percent_of(worked.round_trip_cost_cents, money.entry_commitment_cents)
        ),
        over_cost_limit=worked.cost_is_over_limit,
        at_expiry=None if money is None else at_expiry(structure, size, net_price=worked.net_price),
        lean=lean(structure, size, deltas) if without_delta is None else None,
        lean_unknown_leg_id=without_delta,
        decay=_decay(
            structure, size, daily_decay, model_is_known=model_is_known, priced_against=resolution.evidence.model_spot
        ),
    )


def _decay(
    structure: ResolvedStructure,
    size: EntrySize,
    daily_decay: Mapping[str, LegDecay] | None,
    *,
    model_is_known: bool,
    priced_against: float | None,
) -> StructureDecay | None:
    if not model_is_known:
        return decay_not_estimated(structure, DecayNotEstimated.INPUTS_NOT_KNOWN)
    if daily_decay is None:
        return None
    if priced_against is None:
        raise ValueError("each pick's decay was priced against the underlying's mid, and this resolution has none")
    return structure_daily_decay(structure, size, daily_decay, underlying_price=priced_against)
