"""An option entry, prepared: the structure checked, each leg's gap, its limit price, its money, and the cost limit."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from fractions import Fraction

import pytest

from app.options.entry import (
    DEFAULT_COST_LIMIT_PERCENT,
    DEFAULT_LEG_GAP_LIMIT_PERCENT,
    EntryFigures,
    LegGapOverLimit,
    PreparedEntry,
    entry_figures,
    prepare_entry,
)
from app.options.named_structures import NamedStructure
from app.options.quotes import HeldQuote
from app.options.requirement import StructureMoneyRefusal, StructureMoneyRefused
from app.options.structure_catalogue import starting_plan
from app.options.structure_check import StructureRefusal, StructureRefused
from app.schemas.action_plan import ActionPlan

# The resolver's own scenes: SPY with its bar closed at 585 on a dollar-wide ladder.
from tests.options import test_resolver as scenes

_BOUGHT, _SOLD = scenes._contract("call", 585), scenes._contract("call", 590)


def _bought_spread() -> ActionPlan:
    """The 585 call bought and the 590 call sold."""
    return scenes._plan(
        scenes._leg("long_call", "call", {"selector": "atm"}),
        scenes._leg("short_call", "call", {"selector": "leg_distance", "leg_id": "long_call", "dollars": 5.0}, position="short"),
    )


def _sold_spread() -> ActionPlan:
    """The 585 call sold and the 590 call bought."""
    return scenes._plan(
        scenes._leg("short_call", "call", {"selector": "atm"}, position="short"),
        scenes._leg("long_call", "call", {"selector": "leg_distance", "leg_id": "short_call", "dollars": 5.0}),
    )


def _market(bid: str, ask: str) -> HeldQuote:
    return scenes._quote(bid, ask, received_ms=0)


def _prepared(plan: ActionPlan, *, picked: dict, quotes: dict[str, HeldQuote], **terms: object) -> PreparedEntry:
    return prepare_entry(
        plan,
        picked=picked,
        multipliers=terms.pop("multipliers", dict.fromkeys(picked, 100)),
        quotes=quotes,
        units=terms.pop("units", 1),
        fee_provision_cents=terms.pop("fee_provision_cents", 0),
        leg_gap_limit_percent=terms.pop("leg_gap_limit_percent", DEFAULT_LEG_GAP_LIMIT_PERCENT),
        cost_limit_percent=terms.pop("cost_limit_percent", DEFAULT_COST_LIMIT_PERCENT),
        **terms,
    )


def test_a_bought_spread_is_sent_at_what_it_pays_and_sets_that_aside() -> None:
    """Bought at the 585's ask of 4.12 and sold at the 590's bid of 2.18: $1.94 a share, the PRD's own case."""
    entry = _prepared(
        _bought_spread(),
        picked={"long_call": _BOUGHT, "short_call": _SOLD},
        quotes={"long_call": _market("4.08", "4.12"), "short_call": _market("2.18", "2.22")},
    )

    assert entry.net_limit_price == Decimal("1.94")
    assert (entry.money.net_entry_premium_cents, entry.money.broker_requirement_cents) == (19_400, 0)
    assert (entry.money.entry_commitment_cents, entry.money.loss_at_expiry_cents) == (19_400, 19_400)
    # Four cents a share on each leg, a hundred shares.
    assert entry.round_trip_cost_cents == 800
    assert [(leg.leg_id, leg.contract, leg.position) for leg in entry.structure.legs] == [
        ("long_call", _BOUGHT, "long"),
        ("short_call", _SOLD, "short"),
    ]


def test_a_sold_spread_is_sent_at_what_it_receives_and_sets_aside_the_requirement_less_that() -> None:
    """A $5-wide call spread sold for $3.07: the requirement is $500.00 and the entry sets aside $193.00."""
    entry = _prepared(
        _sold_spread(),
        picked={"short_call": _BOUGHT, "long_call": _SOLD},
        quotes={"short_call": _market("5.00", "5.04"), "long_call": _market("1.89", "1.93")},
    )

    assert entry.net_limit_price == Decimal("-3.07")
    assert (entry.money.net_entry_premium_cents, entry.money.broker_requirement_cents) == (-30_700, 50_000)
    assert (entry.money.entry_commitment_cents, entry.money.loss_at_expiry_cents) == (19_300, 19_300)


def test_units_and_the_fee_provision_reach_every_figure_they_belong_in() -> None:
    quotes = {"long_call": _market("4.08", "4.12"), "short_call": _market("2.18", "2.22")}
    picked = {"long_call": _BOUGHT, "short_call": _SOLD}

    entry = _prepared(_bought_spread(), picked=picked, quotes=quotes, units=3, fee_provision_cents=45)

    assert (entry.size.units, entry.size.multiplier, entry.net_limit_price) == (3, 100, Decimal("1.94"))
    assert (entry.money.net_entry_premium_cents, entry.money.entry_commitment_cents) == (58_200, 58_245)
    assert entry.round_trip_cost_cents == 2_400


def test_a_structure_that_costs_most_of_itself_to_trade_is_refused_by_the_cost_limit() -> None:
    """Two legs with mids of $5.00 and $4.90, each four cents wide. Each leg's own market looks tight. Buying
    one and selling the other costs $0.14 and closing at once returns $0.06: $8 of a $14 commitment."""
    quotes = {"long_call": _market("4.98", "5.02"), "short_call": _market("4.88", "4.92")}
    picked = {"long_call": _BOUGHT, "short_call": _SOLD}

    with pytest.raises(StructureMoneyRefused) as refused:
        _prepared(_bought_spread(), picked=picked, quotes=quotes)
    under_a_looser_limit = _prepared(_bought_spread(), picked=picked, quotes=quotes, cost_limit_percent=Decimal("57.2"))

    assert refused.value.reason is StructureMoneyRefusal.COST_OVER_LIMIT
    assert (under_a_looser_limit.round_trip_cost_cents, under_a_looser_limit.money.entry_commitment_cents) == (800, 1_400)
    # 800 of 1,400 is 57.14%: over a limit of 57.1, inside one of 57.2.
    with pytest.raises(StructureMoneyRefused):
        _prepared(_bought_spread(), picked=picked, quotes=quotes, cost_limit_percent=Decimal("57.1"))


def test_each_leg_of_that_structure_is_inside_the_gap_limit_so_it_is_the_cost_limit_that_refuses_it() -> None:
    """The same two legs: four cents on a mid of $5.00 is 0.8% and on $4.90 is 0.82%, each far inside 8%."""
    quotes = {"long_call": _market("4.98", "5.02"), "short_call": _market("4.88", "4.92")}
    picked = {"long_call": _BOUGHT, "short_call": _SOLD}

    with pytest.raises(StructureMoneyRefused) as refused:
        _prepared(_bought_spread(), picked=picked, quotes=quotes, leg_gap_limit_percent=Decimal(8))
    with_no_cost_limit_to_speak_of = _prepared(
        _bought_spread(), picked=picked, quotes=quotes, leg_gap_limit_percent=Decimal(8), cost_limit_percent=Decimal(100)
    )

    assert refused.value.reason is StructureMoneyRefusal.COST_OVER_LIMIT
    assert not isinstance(refused.value, LegGapOverLimit)
    assert with_no_cost_limit_to_speak_of.net_limit_price == Decimal("0.14")


def _one_bought_call() -> ActionPlan:
    return scenes._plan(scenes._leg("long_call", "call", {"selector": "atm"}))


def test_a_leg_whose_gap_is_exactly_the_limit_is_admitted_and_one_a_cent_wider_is_refused() -> None:
    """Forty cents on a mid of $5.00 is 8% of it. Forty-one cents on a mid of $5.005 is over."""
    at_the_limit = _prepared(_one_bought_call(), picked={"long_call": _BOUGHT}, quotes={"long_call": _market("4.80", "5.20")})

    with pytest.raises(LegGapOverLimit) as refused:
        _prepared(_one_bought_call(), picked={"long_call": _BOUGHT}, quotes={"long_call": _market("4.80", "5.21")})

    assert at_the_limit.net_limit_price == Decimal("5.20")
    assert refused.value.reason is StructureMoneyRefusal.LEG_GAP_OVER_LIMIT
    assert (refused.value.leg_id, refused.value.contract, refused.value.limit_percent) == ("long_call", _BOUGHT, Decimal(8))
    assert (refused.value.bid, refused.value.ask) == (Decimal("4.80"), Decimal("5.21"))
    # 0.41 on a mid of 5.005, exact: a screen rounds it to 8.2%.
    assert refused.value.gap_percent_of_mid == Fraction(41, 5005) * 1000


def test_the_gap_limit_is_the_owners_own_and_a_looser_one_admits_the_same_leg() -> None:
    quotes = {"long_call": _market("4.80", "5.21")}

    admitted = _prepared(
        _one_bought_call(), picked={"long_call": _BOUGHT}, quotes=quotes, leg_gap_limit_percent=Decimal("8.2")
    )

    assert admitted.round_trip_cost_cents == 4_100
    with pytest.raises(LegGapOverLimit):
        _prepared(_one_bought_call(), picked={"long_call": _BOUGHT}, quotes=quotes, leg_gap_limit_percent=Decimal("8.19"))


def test_a_wide_leg_is_refused_as_itself_before_the_structures_cost_is_asked() -> None:
    """A dollar-wide market on a $5 option fails both limits. The leg's own market is the first no."""
    with pytest.raises(StructureMoneyRefused) as refused:
        _prepared(_one_bought_call(), picked={"long_call": _BOUGHT}, quotes={"long_call": _market("4.50", "5.50")})

    assert refused.value.reason is StructureMoneyRefusal.LEG_GAP_OVER_LIMIT


def test_when_two_legs_are_over_the_limit_the_first_in_the_structure_is_the_one_named() -> None:
    quotes = {"long_call": _market("3.80", "4.40"), "short_call": _market("2.00", "2.40")}
    picked = {"long_call": _BOUGHT, "short_call": _SOLD}

    with pytest.raises(LegGapOverLimit) as refused:
        _prepared(_bought_spread(), picked=picked, quotes=quotes)
    with pytest.raises(LegGapOverLimit) as only_the_second:
        _prepared(_bought_spread(), picked=picked, quotes=quotes | {"long_call": _market("4.08", "4.12")})

    assert (refused.value.leg_id, refused.value.contract) == ("long_call", _BOUGHT)
    assert (only_the_second.value.leg_id, only_the_second.value.contract) == ("short_call", _SOLD)


def test_a_leg_with_nothing_bid_is_as_wide_as_a_market_gets() -> None:
    """Bid zero, ask five cents: the gap is the whole ask, 200% of the mid. No everyday limit admits it."""
    with pytest.raises(LegGapOverLimit) as refused:
        _prepared(_one_bought_call(), picked={"long_call": _BOUGHT}, quotes={"long_call": _market("0", "0.05")})

    assert refused.value.gap_percent_of_mid == 200


def test_contracts_that_are_not_the_shape_the_plans_name_means_are_refused_before_any_money() -> None:
    """A bull call spread buys the lower strike. With the picks the other way round it is another structure."""
    plan = starting_plan(NamedStructure.BULL_CALL_SPREAD, "SPY")
    bought, sold = (leg.leg_id for leg in plan.on_enter)
    quotes = {bought: _market("2.18", "2.22"), sold: _market("4.08", "4.12")}

    with pytest.raises(StructureRefused) as refused:
        _prepared(plan, picked={bought: _SOLD, sold: _BOUGHT}, quotes=quotes)

    assert refused.value.reason is not StructureRefusal.NOT_THE_PLANS_LEGS
    assert set(refused.value.leg_ids) == {bought, sold}


def test_prices_that_do_not_belong_together_are_refused() -> None:
    """A spread bought for nothing: its requirement plus its premium is not above zero."""
    quotes = {"long_call": _market("2.10", "2.18"), "short_call": _market("2.18", "2.22")}

    with pytest.raises(StructureMoneyRefused) as refused:
        _prepared(_bought_spread(), picked={"long_call": _BOUGHT, "short_call": _SOLD}, quotes=quotes)

    assert refused.value.reason is StructureMoneyRefusal.COMMITMENT_NOT_POSITIVE


def test_a_resolutions_picks_quotes_and_multipliers_are_what_an_entry_is_prepared_from() -> None:
    """End to end from the resolver's own scene: the limit is the picked quotes' natural price."""
    scene = scenes._scene()
    resolution = scene.resolve(_bought_spread())

    entry = prepare_entry(
        _bought_spread(),
        picked=resolution.picked,
        multipliers=resolution.multipliers,
        quotes=resolution.quotes,
        units=1,
        fee_provision_cents=0,
        leg_gap_limit_percent=DEFAULT_LEG_GAP_LIMIT_PERCENT,
        cost_limit_percent=DEFAULT_COST_LIMIT_PERCENT,
    )

    paid = scene.quote_of(_BOUGHT).ask.price - scene.quote_of(_SOLD).bid.price
    assert dict(resolution.multipliers) == {"long_call": 100, "short_call": 100}
    assert entry.net_limit_price == paid
    assert entry.money.net_entry_premium_cents == int(paid * 100 * 100)
    assert dict(entry.quotes) == dict(resolution.quotes)


def test_legs_with_different_multipliers_or_a_missing_side_are_the_callers_mistake() -> None:
    quotes = {"long_call": _market("4.08", "4.12"), "short_call": _market("2.18", "2.22")}
    picked = {"long_call": _BOUGHT, "short_call": _SOLD}

    with pytest.raises(ValueError, match="multiplier"):
        _prepared(_bought_spread(), picked=picked, quotes=quotes, multipliers={"long_call": 100, "short_call": 10})
    with pytest.raises(ValueError, match="whole quote"):
        _prepared(_bought_spread(), picked=picked, quotes=quotes | {"short_call": replace(quotes["short_call"], bid=None)})


# ---- Every refusal at once: what a preview is shown -------------------------------------


def _figures(plan: ActionPlan, *, picked: dict, quotes: dict[str, HeldQuote], fees: int | None = 0, **terms: object) -> EntryFigures:
    return entry_figures(
        plan,
        picked=picked,
        multipliers=dict.fromkeys(picked, 100),
        quotes=quotes,
        units=terms.pop("units", 1),
        fee_provision=lambda _structure, _markets, _size: fees,
        leg_gap_limit_percent=terms.pop("leg_gap_limit_percent", DEFAULT_LEG_GAP_LIMIT_PERCENT),
        cost_limit_percent=terms.pop("cost_limit_percent", DEFAULT_COST_LIMIT_PERCENT),
        **terms,
    )


def test_asked_for_every_refusal_each_wide_leg_and_the_cost_are_all_named_with_the_figures_beside_them() -> None:
    """The 585 call at 3.90 by 4.30 and the 590 at 2.00 by 2.40: forty cents on mids of 4.10 and 2.20, which is
    9.76% and 18.18%, both over 8%. Bought for $2.30, it costs $80.00 of the $230.00 set aside to trade."""
    worked = _figures(
        _bought_spread(),
        picked={"long_call": _BOUGHT, "short_call": _SOLD},
        quotes={"long_call": _market("3.90", "4.30"), "short_call": _market("2.00", "2.40")},
    )

    assert [str(refused.reason) for refused in worked.refusals] == [
        StructureMoneyRefusal.LEG_GAP_OVER_LIMIT, StructureMoneyRefusal.LEG_GAP_OVER_LIMIT, StructureMoneyRefusal.COST_OVER_LIMIT,
    ]
    assert [refused.leg_id for refused in worked.refusals[:2]] == ["long_call", "short_call"]
    assert worked.legs_over_gap_limit == {"long_call", "short_call"}
    assert (worked.net_price, worked.money.entry_commitment_cents, worked.round_trip_cost_cents) == (
        Decimal("2.30"), 23_000, 8_000,
    )
    assert worked.cost_is_over_limit is True


def test_an_entry_asks_nothing_after_its_first_refusal_where_a_preview_asks_on() -> None:
    """A spread named a bull call spread whose strikes came out reversed, with a gap limit that is no percent.
    An entry is refused by the shape and never reads the limit. A preview goes on to the limit, and that is
    the caller's mistake."""
    plan = _bought_spread().model_copy(update={"structure": NamedStructure.BULL_CALL_SPREAD})
    reversed_picks = {"long_call": _SOLD, "short_call": _BOUGHT}
    quotes = {"long_call": _market("2.18", "2.22"), "short_call": _market("4.08", "4.12")}
    no_percent = Decimal(-1)

    with pytest.raises(StructureRefused) as refused:
        _prepared(plan, picked=reversed_picks, quotes=quotes, leg_gap_limit_percent=no_percent)
    assert refused.value.reason is StructureRefusal.STRIKES_NOT_AS_NAMED

    with pytest.raises(ValueError, match="a leg gap limit is a percent") as mistake:
        _figures(plan, picked=reversed_picks, quotes=quotes, leg_gap_limit_percent=no_percent)
    assert not isinstance(mistake.value, StructureRefused | StructureMoneyRefused)


def test_an_entry_refused_for_a_wide_leg_never_reads_the_fee_provision_it_was_handed() -> None:
    """The gap is asked before the money. A provision that is no amount is the caller's mistake, and an entry
    already refused for its leg's market never gets as far as finding that out."""
    picked = {"long_call": _BOUGHT, "short_call": _SOLD}
    wide = {"long_call": _market("3.90", "4.30"), "short_call": _market("2.18", "2.22")}
    tight = {"long_call": _market("4.08", "4.12"), "short_call": _market("2.18", "2.22")}

    with pytest.raises(LegGapOverLimit):
        _prepared(_bought_spread(), picked=picked, quotes=wide, fee_provision_cents=-1)
    with pytest.raises(ValueError, match="a fee provision is a whole number of cents") as mistake:
        _prepared(_bought_spread(), picked=picked, quotes=tight, fee_provision_cents=-1)
    assert not isinstance(mistake.value, StructureMoneyRefused)
    # A provision handed over as nothing is the same mistake, found at the same point, and is told in the
    # money's own words: an entry keeps no second copy of them.
    with pytest.raises(LegGapOverLimit):
        _prepared(_bought_spread(), picked=picked, quotes=wide, fee_provision_cents=None)
    with pytest.raises(ValueError, match="a fee provision is a whole number of cents") as nothing:
        _prepared(_bought_spread(), picked=picked, quotes=tight, fee_provision_cents=None)
    assert not isinstance(nothing.value, StructureMoneyRefused)
    assert str(nothing.value) == str(mistake.value).replace("-1", "None")


def test_an_entry_whose_money_is_refused_is_refused_for_that_whatever_crossing_its_markets_would_cost() -> None:
    """Two expiries and a sold leg: the requirement is not known, and the entry is refused. Its quotes are also
    finer than any cent, so what crossing them costs is no whole number of cents. An entry never works that out:
    it was refused first. Asked for every refusal, the cost is worked out, and that is the caller's mistake."""
    plan = scenes._plan(
        scenes._leg("short_call", "call", {"selector": "atm"}, position="short"),
        scenes._leg("long_call", "call", {"selector": "leg_distance", "leg_id": "short_call", "dollars": 5.0}),
    )
    picked = {"short_call": _BOUGHT, "long_call": scenes._contract("call", 590, scenes._NEXT_FRIDAY)}
    finer_than_a_cent = {"short_call": _market("5.00", "5.04"), "long_call": _market("1.89", "1.89005")}

    with pytest.raises(StructureMoneyRefused) as refused:
        _prepared(plan, picked=picked, quotes=finer_than_a_cent, leg_gap_limit_percent=Decimal(200))
    assert refused.value.reason is StructureMoneyRefusal.MIXED_EXPIRY_WITH_SOLD_LEG

    with pytest.raises(ValueError, match="whole number of cents") as mistake:
        _figures(plan, picked=picked, quotes=finer_than_a_cent, leg_gap_limit_percent=Decimal(200))
    assert not isinstance(mistake.value, StructureMoneyRefused)


def test_a_gap_limit_of_nothing_is_a_limit_and_refuses_any_leg_with_a_gap_at_all() -> None:
    """An entry takes a limit of zero. Only a leg bid and offered at one price passes it."""
    picked = {"long_call": _BOUGHT, "short_call": _SOLD}
    locked = {"long_call": _market("4.10", "4.10"), "short_call": _market("2.20", "2.20")}
    a_cent_wide = {"long_call": _market("4.10", "4.11"), "short_call": _market("2.20", "2.20")}

    assert _prepared(_bought_spread(), picked=picked, quotes=locked, leg_gap_limit_percent=Decimal(0)).round_trip_cost_cents == 0
    with pytest.raises(LegGapOverLimit):
        _prepared(_bought_spread(), picked=picked, quotes=a_cent_wide, leg_gap_limit_percent=Decimal(0))


def test_picks_that_are_not_the_plans_legs_have_that_one_refusal_and_no_figure() -> None:
    worked = _figures(
        _bought_spread(),
        picked={"long_call": _BOUGHT, "short_call": _BOUGHT},
        quotes={"long_call": _market("4.08", "4.12"), "short_call": _market("4.08", "4.12")},
    )

    assert [refused.reason for refused in worked.refusals] == [StructureRefusal.SAME_CONTRACT]
    assert (worked.structure, worked.size, worked.net_price, worked.money, worked.round_trip_cost_cents) == (
        None, None, None, None, None,
    )
    assert worked.legs_over_gap_limit == frozenset() and worked.cost_is_over_limit is None


def test_money_that_is_refused_leaves_the_cost_of_trading_known_and_the_limit_unjudged() -> None:
    """A spread bought for nothing: the 585's ask is the 590's bid."""
    worked = _figures(
        _bought_spread(),
        picked={"long_call": _BOUGHT, "short_call": _SOLD},
        quotes={"long_call": _market("2.10", "2.18"), "short_call": _market("2.18", "2.22")},
    )

    assert [refused.reason for refused in worked.refusals] == [StructureMoneyRefusal.COMMITMENT_NOT_POSITIVE]
    assert (worked.net_price, worked.money, worked.round_trip_cost_cents) == (Decimal(0), None, 1_200)
    assert worked.cost_is_over_limit is None


def test_with_no_fee_provision_to_be_had_nothing_is_set_aside_and_no_refusal_is_made_up() -> None:
    worked = _figures(
        _bought_spread(),
        fees=None,
        picked={"long_call": _BOUGHT, "short_call": _SOLD},
        quotes={"long_call": _market("4.08", "4.12"), "short_call": _market("2.18", "2.22")},
    )

    assert worked.refusals == ()
    assert (worked.fee_provision_cents, worked.money, worked.cost_is_over_limit) == (None, None, None)
    assert (worked.net_price, worked.round_trip_cost_cents) == (Decimal("1.94"), 800)

