"""A leg prepared once on a date's listing picks the contract a plain search for the money comes to.

A run picks at every minute of a session, and ``pick_contract`` regroups the whole listing on each call. The
prepared form settles the expiry once and takes only the strike at each price, by bisecting the sorted strikes. A
test that compared it with ``pick_contract`` would compare a function with itself (``pick_contract`` is the two steps
in a row), so the strike is held to an oracle this file owns: a linear search for the listed strike nearest the
price, the lower of two as near, and then so many places along the sorted ladder. The remaining tests pin what the
prepared form promises of its own: where each refusal comes from, that the order a listing arrives in changes
nothing, and the caller's mistakes. The dates are checked against the canonical calendar when the module loads.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from fractions import Fraction
from itertools import pairwise
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


# The listings the refusals that do not depend on the price are read from.
_LISTINGS: dict[str, Listing] = {
    "a full chain": _listing(*_contracts(_EXPIRIES)),
    "an incomplete chain": _listing(*_contracts(_EXPIRIES), complete=False),
    "nothing listed": _listing(),
    "a Saturday expiry": _listing(*_contracts((_SATURDAY,))),
}


def _leg(expiry: dict[str, Any], strike: dict[str, Any], right: OptionRight) -> LegSetup:
    return LegSetup.model_validate(
        {"leg_id": "leg", "right": right, "contracts": 1, "expiry": expiry, "strike": strike}
    )


def _strike_by_plain_search(listed: Sequence[Decimal], price: Decimal, steps: int) -> Decimal | None:
    """The strike a rule means, found the slow way: the nearest listed strike to the price, the lower of two as near,
    then ``steps`` places along the sorted strikes; None off either end."""
    ladder = sorted(set(listed))
    nearest = min(range(len(ladder)), key=lambda at: (abs(Fraction(ladder[at]) - Fraction(price)), ladder[at]))
    place = nearest + steps
    return ladder[place] if 0 <= place < len(ladder) else None


def _a_listed_ladder(rng: random.Random) -> list[Decimal]:
    """Some strikes in no particular order, gaps of one to three units, with repeats on a third of them."""
    unit = Decimal(rng.choice(["0.5", "1", "2.5", "5"]))
    strike = Decimal(rng.randint(2, 400)) * unit
    ladder = [strike]
    for _ in range(rng.choice([0, 0, 1, 2, 4, 7, 12])):
        strike += unit * rng.randint(1, 3)
        ladder.append(strike)
    if rng.random() < 0.35:
        ladder += [rng.choice(ladder) for _ in range(rng.randint(1, 3))]
    rng.shuffle(ladder)
    return ladder


def _prices_around(rng: random.Random, ladder: Sequence[Decimal]) -> list[tuple[str, Decimal]]:
    """Prices on each strike, at each exact middle between two (a tie) and a thousandth either side of it, off both
    ends, and at random between the ends."""
    strikes, milli = sorted(set(ladder)), Decimal("0.001")
    cases = [("on a strike", strike) for strike in strikes]
    for low, high in pairwise(strikes):
        middle = (low + high) / 2
        cases += [("a tie", middle), ("just under a tie", middle - milli), ("just over a tie", middle + milli)]
    cases += [
        ("under the ladder", strikes[0] - milli),
        ("under the ladder", strikes[0] / 2),
        ("over the ladder", strikes[-1] + milli),
        ("over the ladder", strikes[-1] * 2),
    ]
    spread = strikes[-1] - strikes[0]
    cases += [("between", (strikes[0] + spread * rng.randint(0, 1000) / 1000).quantize(milli)) for _ in range(4)]
    return [(name, price) for name, price in cases if price > 0]


def test_the_strike_a_prepared_leg_picks_is_the_one_a_plain_search_for_the_money_comes_to() -> None:
    rng, seen = random.Random(20261012), Counter[str]()
    same_day = {"kind": "same_day"}
    for _ in range(300):
        listed = _a_listed_ladder(rng)
        listing = _listing(
            *(
                ListedContract(
                    OptionContract.of(root="SPY", expiry_date=_MONDAY, right="call", strike=strike),
                    multiplier=100,
                    standard=True,
                )
                for strike in listed
            )
        )
        prices = _prices_around(rng, listed)
        seen["ladders"] += 1
        seen["one strike"] += len(set(listed)) == 1
        seen["repeats"] += len(set(listed)) < len(listed)
        for steps in (0, 1, -1, 2, -2, 50, -50):
            prepared = prepare_leg(
                _leg(same_day, {"kind": "atm"} if steps == 0 else {"kind": "atm_offset", "steps": steps}, "call"),
                listing=listing,
                trading_date=_MONDAY,
            )
            assert isinstance(prepared, PreparedLeg)
            for name, price in prices:
                expected = _strike_by_plain_search(listed, price, steps)
                picked = prepared.pick(price)
                where = f"{sorted(set(listed))}, {steps} steps, {name} at {price}"
                if expected is None:
                    assert isinstance(picked, PickRefused), where
                    assert picked.reason is PickRefusal.STRIKE_NOT_LISTED, where
                    seen["off the ladder by steps"] += 1
                else:
                    assert isinstance(picked, Picked), where
                    assert picked.contract.strike == expected, where
                seen[name] += 1
        # The one-call form is the two steps in a row: it answers what the prepared leg does, at one price a ladder.
        name, price = prices[len(prices) // 2]
        direct = pick_contract(
            _leg(same_day, {"kind": "atm"}, "call"), listing=listing, trading_date=_MONDAY, underlying_price=price
        )
        assert isinstance(direct, Picked) and direct.contract.strike == _strike_by_plain_search(listed, price, 0)
    # The sweep is held to its purpose: it reaches every case the bisection could get wrong.
    assert seen["a tie"] >= 5000
    assert seen["on a strike"] >= 6000
    assert seen["just under a tie"] >= 5000 and seen["just over a tie"] >= 5000
    assert seen["under the ladder"] >= 2500 and seen["over the ladder"] >= 2500
    assert seen["one strike"] >= 50 and seen["repeats"] >= 60
    assert seen["off the ladder by steps"] >= 15000


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
