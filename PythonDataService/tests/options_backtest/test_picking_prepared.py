"""A leg prepared once on a date's listing picks exactly as ``pick_contract`` does, at every price.

A run picks at every minute of a session, and ``pick_contract`` regroups the whole listing on each call. The
prepared form settles the expiry once and takes only the strike at each price. These tests hold the two to the same
answer, refusals and caller mistakes included, over a sweep of listings, expiry rules, strike rules, rights and
prices. The dates are checked against the canonical calendar when the module loads.
"""

from __future__ import annotations

import random
from datetime import date
from decimal import Decimal
from functools import cache
from typing import Any

import pytest

from app.lean_sidecar.trading_calendar import is_trading_day
from app.options.contract import OptionContract, OptionRight
from app.options_backtest.market import Picked, PickRefusal, PickRefused
from app.options_backtest.picking import ListedContract, Listing, PreparedLeg, pick_contract, prepare_leg
from app.options_backtest.setup import LegSetup

_MONDAY = date(2025, 10, 13)
_WEDNESDAY = date(2025, 10, 15)
_FRIDAY = date(2025, 10, 17)
_SATURDAY = date(2025, 10, 18)
_NEXT_FRIDAY = date(2025, 10, 24)
_LAST_FRIDAY = date(2025, 10, 31)

assert all(is_trading_day(day) for day in (_MONDAY, _WEDNESDAY, _FRIDAY, _NEXT_FRIDAY, _LAST_FRIDAY))
assert not is_trading_day(_SATURDAY)

_EXPIRIES = (_MONDAY, _WEDNESDAY, _FRIDAY, _NEXT_FRIDAY, _LAST_FRIDAY)
_STRIKES = (580, 585, 590, 595, 600)


def _contracts(
    expiries: tuple[date, ...], strikes: tuple[int, ...] = _STRIKES, *, standard: bool = True
) -> tuple[ListedContract, ...]:
    return tuple(
        ListedContract(
            OptionContract.of(root="SPY", expiry_date=expiry, right=right, strike=strike),
            multiplier=100,
            standard=standard,
        )
        for expiry in expiries
        for right in ("call", "put")
        for strike in strikes
    )


def _listing(*contracts: ListedContract, complete: bool = True, as_of: date = _MONDAY) -> Listing:
    return Listing(underlying="SPY", as_of=as_of, contracts=contracts, complete=complete)


# Each listing shape reaches one of the five refusals somewhere in the sweep, and a plain pick elsewhere.
_LISTINGS: dict[str, Listing] = {
    "a full chain": _listing(*_contracts(_EXPIRIES)),
    "an incomplete chain": _listing(*_contracts(_EXPIRIES), complete=False),
    "nothing listed": _listing(),
    "the nearest week listed only as non-standard": _listing(
        *_contracts((_MONDAY, _WEDNESDAY, _NEXT_FRIDAY, _LAST_FRIDAY)), *_contracts((_FRIDAY,), standard=False)
    ),
    "only non-standard contracts": _listing(*_contracts(_EXPIRIES, standard=False)),
    "a Saturday expiry": _listing(*_contracts((_SATURDAY,))),
    "two strikes": _listing(*_contracts(_EXPIRIES, strikes=(590, 595))),
}

_EXPIRY_RULES: dict[str, dict[str, Any]] = {
    "same day": {"kind": "same_day"},
    "this week": {"kind": "this_week"},
    "at least 0 days": {"kind": "min_days", "days": 0},
    "at least 3 days": {"kind": "min_days", "days": 3},
    "at least 10 days": {"kind": "min_days", "days": 10},
    "at least 60 days": {"kind": "min_days", "days": 60},
}
_STRIKE_RULES: tuple[dict[str, Any], ...] = (
    {"kind": "atm"},
    {"kind": "atm_offset", "steps": 1},
    {"kind": "atm_offset", "steps": -1},
    {"kind": "atm_offset", "steps": 3},
    {"kind": "atm_offset", "steps": -3},
    {"kind": "atm_offset", "steps": 50},
)
# Between strikes, on a strike, exactly halfway (the lower strike wins), and off both ends of the ladder.
_PRICES = tuple(
    Decimal(price) for price in ("592.5", "592.4999", "590", "579.99", "600.01", "0.01", "10000", "587.5", "588")
)
_RIGHTS: tuple[OptionRight, ...] = ("call", "put")


def _leg(expiry: dict[str, Any], strike: dict[str, Any], right: OptionRight) -> LegSetup:
    return LegSetup.model_validate(
        {"leg_id": "leg", "right": right, "contracts": 1, "expiry": expiry, "strike": strike}
    )


def _prepared_pick(leg: LegSetup, listing: Listing, price: Decimal) -> Picked | PickRefused:
    prepared = prepare_leg(leg, listing=listing, trading_date=_MONDAY)
    return prepared if isinstance(prepared, PickRefused) else prepared.pick(price)


@cache
def _sweep(listing_name: str, expiry_name: str) -> tuple[tuple[str, Picked | PickRefused, Picked | PickRefused], ...]:
    """Every strike rule, right and price on one listing and expiry rule: the case, the direct answer, the prepared one."""
    listing, expiry = _LISTINGS[listing_name], _EXPIRY_RULES[expiry_name]
    rows = []
    for strike in _STRIKE_RULES:
        for right in _RIGHTS:
            leg = _leg(expiry, strike, right)
            for price in _PRICES:
                direct = pick_contract(leg, listing=listing, trading_date=_MONDAY, underlying_price=price)
                rows.append((f"{right} {strike} at {price}", direct, _prepared_pick(leg, listing, price)))
    return tuple(rows)


@pytest.mark.parametrize("expiry_name", _EXPIRY_RULES)
@pytest.mark.parametrize("listing_name", _LISTINGS)
def test_a_prepared_leg_picks_what_pick_contract_picks_at_every_price(listing_name: str, expiry_name: str) -> None:
    differing = [case for case, direct, prepared in _sweep(listing_name, expiry_name) if direct != prepared]

    assert not differing, f"{listing_name}, {expiry_name}: the prepared pick differs for {differing[:5]}"


def test_the_sweep_reaches_a_pick_and_every_kind_of_refusal() -> None:
    answers = [
        direct
        for listing_name in _LISTINGS
        for expiry_name in _EXPIRY_RULES
        for _, direct, _ in _sweep(listing_name, expiry_name)
    ]

    refused = {answer.reason for answer in answers if isinstance(answer, PickRefused)}
    assert refused == set(PickRefusal)
    assert any(isinstance(answer, Picked) for answer in answers)


def test_the_refusals_that_do_not_need_a_price_come_from_prepare_leg_and_the_strike_refusal_from_the_pick() -> None:
    same_day_atm = _leg({"kind": "same_day"}, {"kind": "atm"}, "call")
    steps_off_the_end = _leg({"kind": "same_day"}, {"kind": "atm_offset", "steps": 50}, "call")

    incomplete = prepare_leg(same_day_atm, listing=_LISTINGS["an incomplete chain"], trading_date=_MONDAY)
    nothing = prepare_leg(same_day_atm, listing=_LISTINGS["nothing listed"], trading_date=_MONDAY)
    saturday = prepare_leg(
        _leg({"kind": "min_days", "days": 5}, {"kind": "atm"}, "call"),
        listing=_LISTINGS["a Saturday expiry"],
        trading_date=_MONDAY,
    )
    prepared = prepare_leg(steps_off_the_end, listing=_LISTINGS["a full chain"], trading_date=_MONDAY)

    assert isinstance(incomplete, PickRefused) and incomplete.reason is PickRefusal.CHAIN_INCOMPLETE
    assert isinstance(nothing, PickRefused) and nothing.reason is PickRefusal.EXPIRY_NOT_LISTED
    assert isinstance(saturday, PickRefused) and saturday.reason is PickRefusal.EXPIRY_NOT_A_SESSION
    assert isinstance(prepared, PreparedLeg)
    refused = prepared.pick(Decimal("590"))
    assert isinstance(refused, PickRefused) and refused.reason is PickRefusal.STRIKE_NOT_LISTED


@pytest.mark.parametrize("order", ["as listed", "reversed", "shuffled"])
def test_the_strikes_a_leg_steps_along_do_not_depend_on_the_order_the_listing_arrives_in(order: str) -> None:
    contracts = list(_contracts((_MONDAY,)))
    if order == "reversed":
        contracts.reverse()
    elif order == "shuffled":
        random.Random(7).shuffle(contracts)
    listing = _listing(*contracts)
    # Strikes 580, 585, 590, 595, 600. At 592.5 the money is the lower of 590 and 595; one step up is 595, one down 585.
    expected = {"atm": 590, "up": 595, "down": 585}
    rules = {
        "atm": {"kind": "atm"},
        "up": {"kind": "atm_offset", "steps": 1},
        "down": {"kind": "atm_offset", "steps": -1},
    }

    for name, strike_rule in rules.items():
        leg = _leg({"kind": "same_day"}, strike_rule, "call")
        prepared = prepare_leg(leg, listing=listing, trading_date=_MONDAY)
        assert isinstance(prepared, PreparedLeg)
        for picked in (
            prepared.pick(Decimal("592.5")),
            pick_contract(leg, listing=listing, trading_date=_MONDAY, underlying_price=Decimal("592.5")),
        ):
            assert isinstance(picked, Picked)
            assert picked.contract.strike == Decimal(expected[name]), f"{order}, {name}"


def test_a_prepared_leg_has_settled_its_expiry_and_picks_the_strike_at_each_price() -> None:
    this_week = _leg({"kind": "this_week"}, {"kind": "atm"}, "put")

    prepared = prepare_leg(this_week, listing=_LISTINGS["a full chain"], trading_date=_MONDAY)

    assert isinstance(prepared, PreparedLeg)
    assert prepared.expiry == _FRIDAY
    low, high = prepared.pick(Decimal("581")), prepared.pick(Decimal("599"))
    assert isinstance(low, Picked) and isinstance(high, Picked)
    # 581 is nearest the 580 strike, 599 the 600 strike; both are the Friday's puts.
    assert (low.contract.strike, high.contract.strike) == (Decimal("580"), Decimal("600"))
    assert low.contract.expiry_date == high.contract.expiry_date == _FRIDAY
    assert low.contract.right == high.contract.right == "put"


def test_a_listing_of_another_date_raises_the_same_error_prepared_or_not() -> None:
    leg = _leg({"kind": "same_day"}, {"kind": "atm"}, "call")
    listing = _listing(*_contracts(_EXPIRIES), as_of=_WEDNESDAY)

    with pytest.raises(ValueError) as direct:
        pick_contract(leg, listing=listing, trading_date=_MONDAY, underlying_price=Decimal("590"))
    with pytest.raises(ValueError) as prepared:
        prepare_leg(leg, listing=listing, trading_date=_MONDAY)

    assert str(direct.value) == str(prepared.value)
    assert str(_WEDNESDAY) in str(direct.value)


@pytest.mark.parametrize("price", [Decimal(0), Decimal(-5), Decimal("NaN"), Decimal("Infinity"), 590, 590.0])
def test_a_price_that_is_not_a_price_raises_the_same_error_prepared_or_not(price: Any) -> None:
    leg = _leg({"kind": "same_day"}, {"kind": "atm"}, "call")
    listing = _LISTINGS["a full chain"]
    prepared = prepare_leg(leg, listing=listing, trading_date=_MONDAY)
    assert isinstance(prepared, PreparedLeg)

    with pytest.raises(ValueError) as direct:
        pick_contract(leg, listing=listing, trading_date=_MONDAY, underlying_price=price)
    with pytest.raises(ValueError) as via_prepared:
        prepared.pick(price)

    assert str(direct.value) == str(via_prepared.value)


def test_a_listing_of_the_wrong_date_and_a_bad_price_is_reported_for_its_date_first() -> None:
    # An incomplete listing of another date would also be refused for that, which is a refusal and not an error.
    leg = _leg({"kind": "same_day"}, {"kind": "atm"}, "call")
    listing = _listing(*_contracts(_EXPIRIES), as_of=_WEDNESDAY, complete=False)

    with pytest.raises(ValueError, match="describes"):
        pick_contract(leg, listing=listing, trading_date=_MONDAY, underlying_price=Decimal(0))


def test_a_bad_price_is_an_error_even_where_the_listing_would_have_been_refused_first() -> None:
    leg = _leg({"kind": "same_day"}, {"kind": "atm"}, "call")

    with pytest.raises(ValueError, match="price"):
        pick_contract(leg, listing=_LISTINGS["an incomplete chain"], trading_date=_MONDAY, underlying_price=Decimal(0))
