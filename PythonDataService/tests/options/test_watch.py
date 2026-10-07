"""The watched window: which contracts are watched for a plan, and which of them hold a quote line."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from functools import cache

import pytest

from app.options.contract import OptionContract
from app.options.expiry import option_expiry_anchor_ms
from app.options.named_structures import NamedStructure
from app.options.resolver import ContractsNotResolved, Listing, ResolverRefusal
from app.options.structure_catalogue import starting_plan
from app.options.watch import (
    LineClass,
    LineWant,
    NoLine,
    WindowContract,
    expiries_wanted,
    lines_a_plan_can_need,
    plan_lines,
    watched_window,
)
from app.schemas.action_plan import ActionPlan

# The resolver's own scenes: SPY with its bar closed at 585, a dollar-wide ladder from 560 to 610, on a Tuesday.
from tests.options import test_resolver as scenes

_AT_THE_MONEY = {"selector": "atm"}


def _window(plan: ActionPlan, *, money: str = "585.00", reach: int = 2, scene: scenes._Scene | None = None) -> tuple[WindowContract, ...]:
    return watched_window(
        plan,
        chain=(scene or scenes._scene()).chain,
        money=Decimal(money),
        today=scenes._TUESDAY,
        strikes_each_side=reach,
    )


def _strikes(window: tuple[WindowContract, ...], right: str = "call") -> list[int]:
    return sorted(int(seen.contract.strike) for seen in window if seen.contract.right == right)


# ---- The window ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("strike", "watched"),
    [
        pytest.param(_AT_THE_MONEY, [583, 584, 585, 586, 587], id="at the money"),
        pytest.param({"selector": "atm_offset", "offset": 3}, [586, 587, 588, 589, 590], id="three strikes up"),
        pytest.param({"selector": "atm_offset", "offset": -3}, [580, 581, 582, 583, 584], id="three strikes down"),
        pytest.param({"selector": "percent", "percent": 2.0}, [595, 596, 597, 598, 599], id="2% up is 596.70: around 597"),
        pytest.param({"selector": "delta", "target": 0.30}, [583, 584, 585, 586, 587], id="a delta aims at the money"),
        pytest.param({"selector": "absolute", "strike": 590.0}, [590], id="a named strike is watched alone"),
    ],
)
def test_a_legs_window_is_the_listed_strikes_around_where_its_rule_aims(strike: dict, watched: list[int]) -> None:
    window = _window(scenes._one("call", strike))

    assert _strikes(window) == watched
    assert {seen.contract.right for seen in window} == {"call"}
    assert {seen.contract.expiry_date for seen in window} == {scenes._FRIDAY}


def test_a_leg_measured_from_another_is_watched_around_that_legs_aim_moved_by_the_distance() -> None:
    spread = scenes._plan(
        scenes._leg("short_call", "call", {"selector": "leg_distance", "leg_id": "long_call", "dollars": 5.0}, position="short"),
        scenes._leg("long_call", "call", _AT_THE_MONEY),
    )

    window = _window(spread)

    assert _strikes(window) == [583, 584, 585, 586, 587, 588, 589, 590, 591, 592]
    distance = {int(seen.contract.strike): seen.distance for seen in window}
    assert (distance[585], distance[590], distance[583], distance[592]) == (0, 0, 2, 2)


def test_a_leg_measured_downward_from_another_is_watched_below_it() -> None:
    """A put spread: the bought put five dollars under the sold one."""
    spread = scenes._plan(
        scenes._leg("short_put", "put", _AT_THE_MONEY, position="short"),
        scenes._leg("long_put", "put", {"selector": "leg_distance", "leg_id": "short_put", "dollars": -5.0}),
    )

    assert _strikes(_window(spread, reach=1), "put") == [579, 580, 581, 584, 585, 586]


def test_a_contract_two_legs_want_is_as_near_as_the_nearer_wants_it() -> None:
    """One leg aims at 585 and the other at 587: the 586 is one strike from each, the 587 two from the first."""
    plan = scenes._plan(
        scenes._leg("near", "call", _AT_THE_MONEY),
        scenes._leg("far", "call", {"selector": "atm_offset", "offset": 2}, position="short"),
    )

    distance = {int(seen.contract.strike): seen.distance for seen in _window(plan)}

    assert distance == {583: 2, 584: 1, 585: 0, 586: 1, 587: 0, 588: 1, 589: 2}


def test_the_money_between_two_strikes_centres_the_window_on_the_lower() -> None:
    assert _strikes(_window(scenes._one("call", _AT_THE_MONEY), money="585.50")) == [583, 584, 585, 586, 587]
    assert _strikes(_window(scenes._one("call", _AT_THE_MONEY), money="585.51")) == [584, 585, 586, 587, 588]


def test_a_window_stops_at_the_end_of_the_ladder() -> None:
    assert _strikes(_window(scenes._one("call", _AT_THE_MONEY), money="609.20")) == [607, 608, 609, 610]
    # Counted past the last listed strike, a rule aims at nothing listed, and only what is in reach is watched.
    assert _strikes(_window(scenes._one("call", {"selector": "atm_offset", "offset": 3}), money="609.20")) == [610]


@pytest.mark.parametrize(
    ("expiry", "comes_to"),
    [
        pytest.param({"selector": "same_day"}, scenes._TUESDAY, id="same day"),
        pytest.param({"selector": "nearest_weekly"}, scenes._FRIDAY, id="nearest weekly"),
        pytest.param({"selector": "min_dte", "days": 7}, scenes._NEXT_FRIDAY, id="a week or more"),
        pytest.param(
            {"selector": "absolute", "expiration_ms": option_expiry_anchor_ms(scenes._NOVEMBER)}, scenes._NOVEMBER, id="a named date"
        ),
    ],
)
def test_a_leg_is_watched_in_the_expiry_its_rule_comes_to_today(expiry: dict, comes_to: date) -> None:
    plan = scenes._one("put", _AT_THE_MONEY, expiry=expiry)

    window = _window(plan)

    assert {seen.contract.expiry_date for seen in window} == {comes_to}
    assert _strikes(window, "put") == [583, 584, 585, 586, 587]
    assert expiries_wanted(plan, scenes._EXPIRIES, scenes._TUESDAY) == {comes_to}


def test_an_expiry_whose_contracts_are_not_known_yet_is_wanted_and_nothing_of_it_is_watched() -> None:
    """A provider names every expiry at once and the contracts of each one at a time. Until the Friday's
    contracts are read there is nothing to watch for a leg that comes to it, and the leg is refused as a
    search not yet complete."""
    scene = scenes._scene()
    not_read_yet = replace(
        scene,
        chain=replace(
            scene.chain,
            listings=tuple(listing for listing in scene.chain.listings if listing.contract.expiry_date != scenes._FRIDAY),
            expiries=scenes._EXPIRIES,
        ),
    )
    plan = scenes._one("call", _AT_THE_MONEY)

    assert _window(plan, scene=not_read_yet) == ()
    assert expiries_wanted(plan, scenes._EXPIRIES, scenes._TUESDAY) == {scenes._FRIDAY}
    refused = not_read_yet.refusal(plan)
    assert (refused.reason, refused.leg_ids) == (ResolverRefusal.SEARCH_INCOMPLETE, ("only",))
    # The expiry rule still chose among every date the chain names, not only the ones whose contracts are read.
    assert refused.evidence.legs[0].listed_expiries_ms == tuple(option_expiry_anchor_ms(day) for day in scenes._EXPIRIES)


def test_only_the_plans_own_standard_contracts_are_watched() -> None:
    """Beside the 585 call the chain lists a ten-share contract at the same strike's neighbour, and a QQQ call
    at the money. Neither is on the plan's ladder, so neither is watched or counted along it."""
    scene = scenes._scene()
    not_standard = Listing(scenes._contract("call", "585.5"), multiplier=10, shares_delivered=10, exercise="american")
    another_root = Listing(
        OptionContract.of(root="QQQ", expiry_date=scenes._FRIDAY, right="call", strike="585.25"),
        multiplier=100, shares_delivered=100, exercise="american",
    )
    crowded = replace(scene, chain=replace(scene.chain, listings=(*scene.chain.listings, not_standard, another_root)))

    window = _window(scenes._one("call", _AT_THE_MONEY), scene=crowded)

    assert [str(seen.contract.strike) for seen in window] == ["583", "584", "585", "586", "587"]
    assert {seen.contract.root for seen in window} == {"SPY"}


def test_a_leg_whose_expiry_rule_comes_to_nothing_adds_nothing_and_neither_does_a_leg_measured_from_it() -> None:
    not_listed = {"selector": "absolute", "expiration_ms": option_expiry_anchor_ms(date(2026, 10, 8))}
    plan = scenes._plan(
        scenes._leg("long_call", "call", _AT_THE_MONEY, expiry=not_listed),
        scenes._leg("short_call", "call", {"selector": "leg_distance", "leg_id": "long_call", "dollars": 5.0}, position="short"),
        scenes._leg("long_put", "put", _AT_THE_MONEY),
    )

    window = _window(plan)

    assert _strikes(window, "call") == []
    assert _strikes(window, "put") == [583, 584, 585, 586, 587]


@cache
def _resolved_with_every_quote(name: NamedStructure) -> tuple[scenes._Scene, dict[str, OptionContract]]:
    """The scene with a quote held for every listed strike, and what a named structure's starting plan picks."""
    every_quote = scenes._scene(watched=range(560, 611))
    return every_quote, dict(every_quote.resolve(starting_plan(name, "SPY")).picked)


@pytest.mark.parametrize("name", list(NamedStructure))
@pytest.mark.parametrize("reach", [3, 8, 25])
def test_resolving_from_the_window_alone_gives_the_same_contracts_or_an_incomplete_search(
    name: NamedStructure, reach: int
) -> None:
    """The window is where quotes are held. Whatever its size, the rules come to the contracts they would with
    every quote held, or the search is incomplete. A window too small never changes the pick."""
    plan = starting_plan(name, "SPY")
    every_quote, wanted = _resolved_with_every_quote(name)
    window = {seen.contract.occ_symbol for seen in _window(plan, reach=reach, scene=every_quote)}
    window_only = replace(
        every_quote,
        chain=replace(
            every_quote.chain,
            watched={symbol: held for symbol, held in every_quote.chain.watched.items() if symbol in window},
        ),
    )

    try:
        picked = window_only.resolve(plan).picked
    except ContractsNotResolved as refused:
        assert refused.reason is ResolverRefusal.SEARCH_INCOMPLETE
        assert reach < 25, "a window this wide holds every starting plan's contracts"
    else:
        assert dict(picked) == dict(wanted)
    assert len(window) <= lines_a_plan_can_need(plan, strikes_each_side=reach)


def test_the_lines_a_plan_can_need_count_each_legs_whole_window_and_a_named_strike_once() -> None:
    plan = scenes._plan(
        scenes._leg("by_delta", "call", {"selector": "delta", "target": 0.3}),
        scenes._leg("named", "call", {"selector": "absolute", "strike": 600.0}, position="short"),
    )

    assert lines_a_plan_can_need(plan, strikes_each_side=10) == 21 + 1


# ---- Who holds a line ---------------------------------------------------------


def _call(strike: int) -> OptionContract:
    return scenes._contract("call", strike)


def _want(strike: int, line_class: LineClass = LineClass.CANDIDATE, distance: int = 0) -> LineWant:
    return LineWant(_call(strike), line_class, distance)


def _symbols(*strikes: int) -> set[str]:
    return {_call(strike).occ_symbol for strike in strikes}


def test_one_contract_is_one_line_however_many_want_it_and_stands_as_its_highest_class() -> None:
    wants = [_want(585, LineClass.PREVIEW, 5), _want(585, LineClass.CANDIDATE, 2), _want(585, LineClass.HELD)]

    plan = plan_lines(wants, budget=0, ceiling=None, open_lines=())

    # Held: it gets its line though the budget has none.
    assert plan.lines == (_call(585),)
    assert plan.opens == (_call(585),)


def test_a_held_contract_keeps_its_line_while_a_candidate_gives_way() -> None:
    wants = [_want(585, distance=0), _want(586, distance=1), _want(570, LineClass.HELD)]

    plan = plan_lines(wants, budget=2, ceiling=None, open_lines=_symbols(585, 586))

    assert plan.lines == (_call(570), _call(585))
    assert plan.closes == (_call(586).occ_symbol,)
    assert dict(plan.without) == {_call(586).occ_symbol: NoLine.OVER_BUDGET}


def test_a_preview_gets_only_what_is_spare_and_is_refused_when_nothing_is() -> None:
    bot = [_want(584, distance=1), _want(585, distance=0), _want(586, distance=1)]
    page = [_want(600, LineClass.PREVIEW)]

    nothing_spare = plan_lines([*page, *bot], budget=3, ceiling=None, open_lines=())
    one_spare = plan_lines([*page, *bot], budget=4, ceiling=None, open_lines=())

    assert _call(600) not in nothing_spare.lines
    assert dict(nothing_spare.without) == {_call(600).occ_symbol: NoLine.OVER_BUDGET}
    assert one_spare.lines[-1] == _call(600)
    assert dict(one_spare.without) == {}


def test_the_candidates_farthest_from_their_windows_centre_give_way_first() -> None:
    wants = [_want(585 + step, distance=abs(step)) for step in (-2, -1, 0, 1, 2)]

    plan = plan_lines(wants, budget=3, ceiling=None, open_lines=())

    assert plan.lines == (_call(585), _call(584), _call(586))
    assert set(plan.without) == _symbols(583, 587)


def test_a_window_that_moves_keeps_the_strikes_it_still_wants_and_never_gives_up_a_held_contract() -> None:
    """The window moves up a strike. The 583 goes, the 588 comes, the four between are left as they are, and
    the held 570, far outside any window, stays."""
    held = _want(570, LineClass.HELD)
    moved = [held, *(_want(586 + step, distance=abs(step)) for step in (-2, -1, 0, 1, 2))]
    open_before = _symbols(570, 583, 584, 585, 586, 587)

    with_room = plan_lines(moved, budget=7, ceiling=None, open_lines=open_before)
    without_room = plan_lines(moved, budget=6, ceiling=None, open_lines=open_before)

    for plan in (with_room, without_room):
        assert plan.opens == (_call(588),)
        assert plan.closes == (_call(583).occ_symbol,)
        assert _call(570) in plan.lines


def test_after_the_provider_refuses_a_line_only_what_it_showed_it_grants_is_asked_for() -> None:
    wants = [_want(585 + step, distance=step) for step in range(6)]

    plan = plan_lines(wants, budget=5, ceiling=3, open_lines=())

    assert plan.lines == (_call(585), _call(586), _call(587))
    assert dict(plan.without) == {
        # Inside the Clerk's own budget: what stops these is the account's allowance.
        _call(588).occ_symbol: NoLine.NO_CAPACITY,
        _call(589).occ_symbol: NoLine.NO_CAPACITY,
        _call(590).occ_symbol: NoLine.OVER_BUDGET,
    }


def test_held_contracts_are_asked_for_past_the_budget_and_past_what_the_provider_granted() -> None:
    wants = [_want(570, LineClass.HELD), _want(571, LineClass.HELD), _want(585)]

    plan = plan_lines(wants, budget=1, ceiling=0, open_lines=())

    assert plan.lines == (_call(570), _call(571))
    assert dict(plan.without) == {_call(585).occ_symbol: NoLine.OVER_BUDGET}


def test_a_budget_below_zero_is_the_callers_mistake() -> None:
    with pytest.raises(ValueError, match="budget"):
        plan_lines([], budget=-1, ceiling=None, open_lines=())
    with pytest.raises(ValueError, match="window"):
        _window(scenes._one("call", _AT_THE_MONEY), reach=-1)
