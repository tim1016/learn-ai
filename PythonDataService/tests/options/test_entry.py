"""An option entry, prepared: the structure checked, its limit price, its money, and the cost limit."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from app.options.entry import DEFAULT_COST_LIMIT_PERCENT, PreparedEntry, prepare_entry
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
