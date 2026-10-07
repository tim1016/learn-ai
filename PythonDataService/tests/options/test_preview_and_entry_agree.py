"""A preview's money and its refusals are an entry's own, over many made-up markets (#2913).

One function works the figures out for both (``app.options.entry.entry_figures``).
An entry raises the first refusal; a preview is shown every one. Here the two
are asked the same question again and again, and must never disagree: where an
entry prepares, the preview shows its figures to the cent and names no refusal;
where an entry refuses, the preview's first refusal is that one, by its code
and its words.

Resolutions are the resolver's own, on its own scene: each named structure and
its unnamed twin, with every leg's strike named outright so that the picks are
known. The picks' quotes are then made wide or narrow by hand.
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
from functools import cache
from types import MappingProxyType

from app.options.entry import LegGapOverLimit, PreparedEntry, prepare_entry
from app.options.named_structures import NamedStructure
from app.options.preview import StructureFigures, structure_figures
from app.options.requirement import StructureMoneyRefusal, StructureMoneyRefused
from app.options.resolver import Resolution
from app.options.structure_catalogue import starting_plan
from app.options.structure_check import StructureRefusal, StructureRefused
from app.schemas.action_plan import ActionPlan, OptionEntryLeg

# The resolver's own scenes: SPY with its bar closed at 585 on a dollar-wide ladder.
from tests.options import test_resolver as scenes

# The first comparison of the two, made by hand before they were one function, ran 1,065 resolutions.
_STRUCTURES_RESOLVED = 240
_MARKETS_EACH = 8
_SEED = 2913


@cache
def _scene() -> scenes._Scene:
    return scenes._scene()


def _strikes_in_the_named_order(name: NamedStructure, rng: random.Random) -> dict[str, int]:
    """Strikes that give the starting plan's legs the order its name means, from the watched window."""
    low = rng.randrange(572, 586)
    step = rng.choice([1, 2, 5])
    by_leg: dict[NamedStructure, dict[str, int]] = {
        NamedStructure.LONG_CALL: {"long_call": low},
        NamedStructure.LONG_PUT: {"long_put": low},
        NamedStructure.BULL_CALL_SPREAD: {"long_call": low, "short_call": low + step},
        NamedStructure.BEAR_PUT_SPREAD: {"long_put": low + step, "short_put": low},
        NamedStructure.BULL_PUT_SPREAD: {"short_put": low + step, "long_put": low},
        NamedStructure.BEAR_CALL_SPREAD: {"short_call": low, "long_call": low + step},
        NamedStructure.LONG_STRADDLE: {"long_call": low, "long_put": low},
        NamedStructure.LONG_STRANGLE: {"long_put": low, "long_call": low + step},
        NamedStructure.IRON_CONDOR: {
            "long_put": low, "short_put": low + step, "short_call": low + 2 * step, "long_call": low + 3 * step,
        },
        NamedStructure.IRON_BUTTERFLY: {
            "long_put": low, "short_put": low + step, "short_call": low + step, "long_call": low + 2 * step,
        },
        NamedStructure.CALL_BUTTERFLY: {"lower_wing": low, "body": low + step, "upper_wing": low + 2 * step},
        NamedStructure.PUT_BUTTERFLY: {"lower_wing": low, "body": low + step, "upper_wing": low + 2 * step},
    }
    return by_leg[name]


def _a_structure(rng: random.Random) -> tuple[ActionPlan, Resolution]:
    """One of the named structures, or the same legs under no name, resolved to strikes named outright: mostly
    in the order the name means, sometimes anywhere in the window, sometimes on two expiries."""
    name = rng.choice(list(NamedStructure))
    starts = [leg for leg in starting_plan(name, "SPY").on_enter if isinstance(leg, OptionEntryLeg)]
    strikes = _strikes_in_the_named_order(name, rng)
    if rng.random() < 0.3:
        strikes = {leg.leg_id: rng.randrange(572, 599) for leg in starts}
    two_expiries = rng.random() < 0.08
    legs = [
        scenes._leg(
            leg.leg_id,
            leg.right,
            {"selector": "absolute", "strike": float(strikes[leg.leg_id])},
            position=leg.position,
            ratio=leg.qty_ratio,
            expiry={
                "selector": "absolute",
                "expiration_ms": scenes.option_expiry_anchor_ms(
                    scenes._NEXT_FRIDAY if two_expiries and rng.random() < 0.5 else scenes._FRIDAY
                ),
            },
        )
        for leg in starts
    ]
    plan = scenes._plan(*legs)
    if rng.random() < 0.7:
        plan = plan.model_copy(update={"structure": name})
    return plan, _scene().resolve(plan)


def _quoted_again(resolution: Resolution, rng: random.Random) -> Resolution:
    """The same picks with each quote made as wide as a real one can be: a cent or two, or most of its price."""
    quotes = {}
    for leg_id, held in resolution.quotes.items():
        mid = (held.bid.price + held.ask.price) / 2
        half = rng.choice([Decimal("0.01")] * 4 + [Decimal("0.02")] * 3 + [Decimal("0.05"), Decimal("0.15"), Decimal("0.60")])
        # A sold leg's bid is a price something is sold at, and stays above zero.
        bid = max(mid - half, Decimal("0.01") if leg_id in resolution.sold else Decimal(0))
        quotes[leg_id] = scenes._quote(str(bid), str(mid + half), held.bid.received_ms)
    return replace(resolution, quotes=MappingProxyType(quotes))


def _the_two_answers(
    plan: ActionPlan, resolution: Resolution, *, units: int, fees: int, gap_limit: Decimal, cost_limit: Decimal
) -> tuple[PreparedEntry | StructureRefused | StructureMoneyRefused, StructureFigures]:
    try:
        entry: PreparedEntry | StructureRefused | StructureMoneyRefused = prepare_entry(
            plan,
            picked=resolution.picked,
            multipliers=resolution.multipliers,
            quotes=resolution.quotes,
            units=units,
            fee_provision_cents=fees,
            leg_gap_limit_percent=gap_limit,
            cost_limit_percent=cost_limit,
        )
    except (StructureRefused, StructureMoneyRefused) as refused:
        entry = refused
    shown = structure_figures(
        plan,
        resolution,
        units=units,
        leg_gap_limit_percent=gap_limit,
        cost_limit_percent=cost_limit,
        fee_provision=lambda _structure, _markets, _size: fees,
        model_is_known=True,
    )
    return entry, shown


def test_a_preview_and_an_entry_never_disagree_about_the_same_picks_at_the_same_quotes() -> None:
    rng = random.Random(_SEED)
    seen: Counter[str] = Counter()

    for _ in range(_STRUCTURES_RESOLVED):
        plan, resolution = _a_structure(rng)
        for _ in range(_MARKETS_EACH):
            market = _quoted_again(resolution, rng)
            units, fees = rng.choice([1, 1, 2, 5]), rng.choice([0, 6, 11, 37])
            gap_limit = Decimal(rng.choice(["8", "8", "8", "2", "0.5", "25", "200"]))
            cost_limit = Decimal(rng.choice(["10", "10", "4", "2.5", "50", "100", "0"]))

            entry, shown = _the_two_answers(
                plan, market, units=units, fees=fees, gap_limit=gap_limit, cost_limit=cost_limit
            )

            if isinstance(entry, PreparedEntry):
                seen["prepared"] += 1
                assert shown.entry_refusals == () and shown.not_as_built is None
                assert (shown.size, shown.net_price, shown.money, shown.round_trip_cost_cents) == (
                    entry.size, entry.net_limit_price, entry.money, entry.round_trip_cost_cents,
                )
                assert shown.fee_provision_cents == fees
                assert shown.over_cost_limit is False
                assert [leg.over_gap_limit for leg in shown.legs] == [False] * len(shown.legs)
                continue
            seen[str(entry.reason)] += 1
            first = shown.entry_refusals[0]
            assert (first.reason, first.detail) == (str(entry.reason), str(entry))
            if isinstance(entry, LegGapOverLimit):
                assert first.leg_ids == (entry.leg_id,)
            # What stops an entry only for now is told apart from picks that are not the structure built. The
            # cost limit is an entry's last question, so an entry it refuses has nothing else wrong with it.
            if entry.reason is StructureMoneyRefusal.COST_OVER_LIMIT:
                assert shown.not_as_built is None and shown.over_cost_limit is True
            elif entry.reason is not StructureMoneyRefusal.LEG_GAP_OVER_LIMIT:
                assert shown.not_as_built == first

    # Every way the two can meet was met: an entry prepared, each limit's refusal, the shape's and the money's.
    assert sum(seen.values()) == _STRUCTURES_RESOLVED * _MARKETS_EACH >= 1_065
    for met in (
        "prepared",
        StructureMoneyRefusal.LEG_GAP_OVER_LIMIT,
        StructureMoneyRefusal.COST_OVER_LIMIT,
        StructureMoneyRefusal.MIXED_EXPIRY_WITH_SOLD_LEG,
        StructureRefusal.SAME_CONTRACT,
        StructureRefusal.STRIKES_NOT_AS_NAMED,
        StructureRefusal.MORE_THAN_ONE_EXPIRY,
    ):
        assert seen[str(met)] >= 10, (met, dict(seen))


def test_a_preview_judges_every_legs_gap_by_the_owners_limit_where_an_entry_names_only_the_first() -> None:
    """Each leg's verdict is worked out apart here, from its own bid and ask: wider than the limit, or not."""
    rng = random.Random(_SEED + 1)
    several_over = 0

    for _ in range(60):
        plan, resolution = _a_structure(rng)
        for _ in range(_MARKETS_EACH):
            market = _quoted_again(resolution, rng)
            gap_limit = Decimal(rng.choice(["8", "2", "0.5", "25"]))

            _entry, shown = _the_two_answers(
                plan, market, units=1, fees=0, gap_limit=gap_limit, cost_limit=Decimal(100)
            )

            if shown.size is None:
                # Picks that are not the plan's legs: no entry could be made of them, and no leg is judged.
                assert [leg.over_gap_limit for leg in shown.legs] == [None] * len(shown.legs)
                continue
            wider = [
                leg.leg_id
                for leg in shown.legs
                if (Fraction(leg.quote.ask.price) - Fraction(leg.quote.bid.price)) * 200
                > Fraction(gap_limit) * (Fraction(leg.quote.bid.price) + Fraction(leg.quote.ask.price))
            ]
            assert [leg.leg_id for leg in shown.legs if leg.over_gap_limit] == wider
            named = [
                refusal.leg_ids[0]
                for refusal in shown.entry_refusals
                if refusal.reason == StructureMoneyRefusal.LEG_GAP_OVER_LIMIT
            ]
            assert named == wider
            several_over += len(wider) > 1

    assert several_over >= 10
