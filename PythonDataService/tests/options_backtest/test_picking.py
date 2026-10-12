"""Which contract a leg's rules come to on a past date, and each way they come to none.

Every date below is checked against the canonical calendar when the module loads
(``_session`` and ``_closed``), so a wrong date fails here, loudly, and does not
leave a test that proves nothing.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.lean_sidecar.trading_calendar import is_trading_day
from app.options.contract import OptionContract, OptionRight
from app.options_backtest.market import Picked, PickRefusal, PickRefused
from app.options_backtest.picking import (
    ListedContract,
    Listing,
    expiry_for,
    pick_contract,
    strike_for,
)
from app.options_backtest.setup import (
    AtTheMoney,
    LegSetup,
    MinDaysExpiryRule,
    SameDayExpiryRule,
    StepsFromTheMoney,
    ThisWeekExpiryRule,
)

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _session(year: int, month: int, day: int, weekday: str) -> date:
    """A date that is the weekday named and a session on the calendar."""
    found = date(year, month, day)
    assert _WEEKDAYS[found.weekday()] == weekday, f"{found} is a {_WEEKDAYS[found.weekday()]}"
    assert is_trading_day(found), f"{found} is not a session on the calendar"
    return found


def _closed(year: int, month: int, day: int, weekday: str) -> date:
    """A weekday the calendar has no session on."""
    found = date(year, month, day)
    assert _WEEKDAYS[found.weekday()] == weekday, f"{found} is a {_WEEKDAYS[found.weekday()]}"
    assert not is_trading_day(found), f"{found} is a session on the calendar"
    return found


# A plain October week, then the weeks after it.
_MON = _session(2025, 10, 13, "Mon")
_TUE = _session(2025, 10, 14, "Tue")
_WED = _session(2025, 10, 15, "Wed")
_FRI = _session(2025, 10, 17, "Fri")
_NEXT_MON = _session(2025, 10, 20, "Mon")
_NEXT_FRI = _session(2025, 10, 24, "Fri")
_LAST_FRI = _session(2025, 10, 31, "Fri")
# The week of Good Friday 2025: Monday to Thursday are sessions, Friday is not.
_HOLIDAY_WEEK_MON = _session(2025, 4, 14, "Mon")
_HOLIDAY_WEEK_WED = _session(2025, 4, 16, "Wed")
_HOLIDAY_WEEK_THU = _session(2025, 4, 17, "Thu")
_GOOD_FRIDAY = _closed(2025, 4, 18, "Fri")
_FRI_AFTER_GOOD_FRIDAY = _session(2025, 4, 25, "Fri")
_SATURDAY = _closed(2025, 10, 18, "Sat")

_STRIKES = (580, 585, 590, 595, 600)


def _contract(expiry: date, right: OptionRight, strike: int | str, root: str = "SPY") -> OptionContract:
    return OptionContract.of(root=root, expiry_date=expiry, right=right, strike=strike)


def _listed(
    expiries: tuple[date, ...],
    *,
    rights: tuple[OptionRight, ...] = ("call", "put"),
    strikes: tuple[int, ...] = _STRIKES,
    standard: bool = True,
    multiplier: int = 100,
) -> tuple[ListedContract, ...]:
    return tuple(
        ListedContract(_contract(expiry, right, strike), multiplier=multiplier, standard=standard)
        for expiry in expiries
        for right in rights
        for strike in strikes
    )


def _listing(
    expiries: tuple[date, ...],
    *,
    as_of: date = _MON,
    complete: bool = True,
    extra: tuple[ListedContract, ...] = (),
    **kinds: Any,
) -> Listing:
    return Listing(underlying="SPY", as_of=as_of, contracts=_listed(expiries, **kinds) + extra, complete=complete)


def _leg(expiry: dict[str, Any], strike: dict[str, Any] | None = None, *, right: str = "call") -> LegSetup:
    return LegSetup.model_validate(
        {
            "leg_id": "leg",
            "right": right,
            "contracts": 1,
            "expiry": expiry,
            "strike": strike or {"kind": "atm"},
        }
    )


_SAME_DAY = {"kind": "same_day"}
_THIS_WEEK = {"kind": "this_week"}


def _min_days(days: int) -> dict[str, Any]:
    return {"kind": "min_days", "days": days}


def _steps(steps: int) -> dict[str, Any]:
    return {"kind": "atm_offset", "steps": steps}


def _picked(result: Picked | PickRefused) -> OptionContract:
    assert isinstance(result, Picked), f"expected a pick, got {result}"
    assert result.multiplier == 100
    return result.contract


def _refusal(result: Picked | PickRefused) -> PickRefusal:
    assert isinstance(result, PickRefused), f"expected a refusal, got {result}"
    assert result.detail.strip(), "a refusal tells the owner why"
    return result.reason


_PRICE = Decimal("586.00")


# ---- The expiry a rule comes to ---------------------------------------------


def test_same_day_is_todays_expiry_when_it_is_listed_and_none_otherwise() -> None:
    rule = SameDayExpiryRule(kind="same_day")

    assert expiry_for(rule, listed=[_MON, _WED, _FRI], trading_date=_MON) == _MON
    assert expiry_for(rule, listed=[_TUE, _WED, _FRI], trading_date=_MON) is None


def test_an_expiry_that_has_already_gone_is_never_chosen() -> None:
    rule = MinDaysExpiryRule(kind="min_days", days=0)

    assert expiry_for(rule, listed=[_MON, _TUE], trading_date=_WED) is None


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        pytest.param(0, _MON, id="no wait: today counts"),
        pytest.param(1, _WED, id="a day or more: Tuesday is not listed, so Wednesday"),
        pytest.param(2, _WED, id="two days is exactly Wednesday"),
        pytest.param(3, _FRI, id="three days: Thursday is not listed, so Friday"),
        pytest.param(7, _NEXT_FRI, id="a week: Monday the 20th is not listed, so Friday the 24th"),
        pytest.param(11, _NEXT_FRI, id="eleven days is exactly Friday the 24th"),
        pytest.param(12, _LAST_FRI, id="twelve days is past the 24th"),
    ],
)
def test_at_least_n_days_is_the_nearest_listed_expiry_that_far_out(days: int, expected: date) -> None:
    listed = [_MON, _WED, _FRI, _NEXT_FRI, _LAST_FRI]

    assert expiry_for(MinDaysExpiryRule(kind="min_days", days=days), listed=listed, trading_date=_MON) == expected


def test_at_least_n_days_counts_calendar_days_and_not_sessions() -> None:
    """Friday the 24th is eight sessions after Monday the 13th, and eleven days."""
    listed = [_NEXT_FRI]

    assert expiry_for(MinDaysExpiryRule(kind="min_days", days=11), listed=listed, trading_date=_MON) == _NEXT_FRI
    assert expiry_for(MinDaysExpiryRule(kind="min_days", days=12), listed=listed, trading_date=_MON) is None


def test_this_week_is_the_weeks_last_session_and_skips_a_mid_week_expiry() -> None:
    rule = ThisWeekExpiryRule(kind="this_week")

    assert expiry_for(rule, listed=[_MON, _TUE, _WED, _FRI, _NEXT_FRI], trading_date=_MON) == _FRI
    assert expiry_for(rule, listed=[_TUE, _WED, _NEXT_MON, _NEXT_FRI], trading_date=_MON) == _NEXT_FRI


def test_this_week_on_the_weeks_last_session_is_that_session_when_listed() -> None:
    rule = ThisWeekExpiryRule(kind="this_week")

    assert expiry_for(rule, listed=[_FRI, _NEXT_FRI], trading_date=_FRI) == _FRI
    assert expiry_for(rule, listed=[_NEXT_MON, _NEXT_FRI], trading_date=_FRI) == _NEXT_FRI


def test_this_week_in_a_week_whose_friday_is_a_holiday_is_the_thursday() -> None:
    rule = ThisWeekExpiryRule(kind="this_week")
    listed = [_HOLIDAY_WEEK_MON, _HOLIDAY_WEEK_WED, _HOLIDAY_WEEK_THU, _FRI_AFTER_GOOD_FRIDAY]

    assert expiry_for(rule, listed=listed, trading_date=_HOLIDAY_WEEK_MON) == _HOLIDAY_WEEK_THU
    assert expiry_for(rule, listed=listed, trading_date=_HOLIDAY_WEEK_THU) == _HOLIDAY_WEEK_THU


def test_this_week_does_not_take_a_listed_expiry_on_the_holiday_itself() -> None:
    rule = ThisWeekExpiryRule(kind="this_week")

    assert expiry_for(rule, listed=[_GOOD_FRIDAY, _FRI_AFTER_GOOD_FRIDAY], trading_date=_HOLIDAY_WEEK_WED) == (
        _FRI_AFTER_GOOD_FRIDAY
    )


def test_a_weeks_last_session_is_judged_in_the_week_it_belongs_to_and_not_the_next() -> None:
    """Sunday the 19th and Saturday the 18th are in Monday the 13th's week; neither is a session."""
    rule = ThisWeekExpiryRule(kind="this_week")

    assert expiry_for(rule, listed=[_SATURDAY, _NEXT_MON], trading_date=_MON) is None
    assert expiry_for(rule, listed=[_SATURDAY, _NEXT_FRI], trading_date=_MON) == _NEXT_FRI


def test_an_expiry_rule_is_blind_to_order_and_repeats_in_what_is_listed() -> None:
    rule = MinDaysExpiryRule(kind="min_days", days=3)

    assert expiry_for(rule, listed=(_NEXT_FRI, _FRI, _FRI, _MON), trading_date=_MON) == _FRI


def test_an_expiry_rule_with_nothing_listed_comes_to_none() -> None:
    for rule in (
        SameDayExpiryRule(kind="same_day"),
        ThisWeekExpiryRule(kind="this_week"),
        MinDaysExpiryRule(kind="min_days", days=1),
    ):
        assert expiry_for(rule, listed=[], trading_date=_MON) is None


# ---- The strike a rule comes to ---------------------------------------------

_LADDER = (580_000, 585_000, 590_000, 595_000, 600_000)
_ATM = AtTheMoney(kind="atm")


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        pytest.param("586.00", 585_000, id="nearer the lower strike"),
        pytest.param("588.00", 590_000, id="nearer the higher strike"),
        pytest.param("585.00", 585_000, id="exactly on a strike"),
        pytest.param("587.50", 585_000, id="exactly between two: the lower"),
        pytest.param("587.5001", 590_000, id="a hair past the middle: the higher"),
        pytest.param("587.4999", 585_000, id="a hair short of the middle: the lower"),
        pytest.param("500.00", 580_000, id="below the whole ladder: its first strike"),
        pytest.param("900.00", 600_000, id="above the whole ladder: its last strike"),
    ],
)
def test_at_the_money_is_the_listed_strike_nearest_the_price_and_the_lower_of_two(price: str, expected: int) -> None:
    assert strike_for(_ATM, ladder_thousandths=_LADDER, underlying_price=Decimal(price)) == expected


def test_a_tie_is_judged_in_thousandths_so_a_fine_ladder_ties_too() -> None:
    ladder = (585_000, 585_500, 586_000)

    assert strike_for(_ATM, ladder_thousandths=ladder, underlying_price=Decimal("585.25")) == 585_000
    assert strike_for(_ATM, ladder_thousandths=ladder, underlying_price=Decimal("585.75")) == 585_500


def test_a_tie_is_exact_where_a_float_would_tip_it() -> None:
    """A float reads 128.0285 times a thousand as 128028.50000000001, which is nearer the higher strike."""
    ladder = (128_028, 128_029)

    assert strike_for(_ATM, ladder_thousandths=ladder, underlying_price=Decimal("128.0285")) == 128_028


@pytest.mark.parametrize(
    ("steps", "expected"),
    [
        pytest.param(1, 590_000, id="one up"),
        pytest.param(2, 595_000, id="two up"),
        pytest.param(3, 600_000, id="three up reaches the top"),
        pytest.param(4, None, id="four up is off the top"),
        pytest.param(-1, 580_000, id="one down reaches the bottom"),
        pytest.param(-2, None, id="two down is off the bottom"),
        pytest.param(50, None, id="far off the top"),
    ],
)
def test_steps_from_the_money_count_listed_strikes_from_the_at_the_money_one(steps: int, expected: int | None) -> None:
    """The money at 586 is 585, the ladder's second strike."""
    rule = StepsFromTheMoney(kind="atm_offset", steps=steps)

    assert strike_for(rule, ladder_thousandths=_LADDER, underlying_price=Decimal("586.00")) == expected


def test_steps_are_listed_strikes_and_not_dollars() -> None:
    ladder = (580_000, 585_000, 600_000, 650_000)

    assert strike_for(
        StepsFromTheMoney(kind="atm_offset", steps=1), ladder_thousandths=ladder, underlying_price=Decimal("585")
    ) == (600_000)
    assert strike_for(
        StepsFromTheMoney(kind="atm_offset", steps=-1), ladder_thousandths=ladder, underlying_price=Decimal("601")
    ) == (585_000)


def test_a_ladder_is_a_set_of_strikes_whatever_order_and_repeats_it_arrives_in() -> None:
    shuffled = (595_000, 580_000, 600_000, 585_000, 590_000, 585_000, 580_000)

    assert strike_for(_ATM, ladder_thousandths=shuffled, underlying_price=Decimal("586")) == 585_000
    assert (
        strike_for(
            StepsFromTheMoney(kind="atm_offset", steps=2), ladder_thousandths=shuffled, underlying_price=Decimal("586")
        )
        == 595_000
    )


def test_no_strikes_listed_is_no_strike() -> None:
    assert strike_for(_ATM, ladder_thousandths=(), underlying_price=Decimal("586")) is None
    assert (
        strike_for(
            StepsFromTheMoney(kind="atm_offset", steps=1), ladder_thousandths=[], underlying_price=Decimal("586")
        )
        is None
    )


@pytest.mark.parametrize("price", ["0", "-1", "NaN", "Infinity"])
def test_a_price_that_is_not_a_price_is_a_callers_mistake(price: str) -> None:
    with pytest.raises(ValueError, match="price"):
        strike_for(_ATM, ladder_thousandths=_LADDER, underlying_price=Decimal(price))


# ---- Picking a contract ------------------------------------------------------


def test_a_same_day_call_at_the_money_is_todays_nearest_strike() -> None:
    listing = _listing((_MON, _WED, _FRI))

    picked = pick_contract(_leg(_SAME_DAY), listing=listing, trading_date=_MON, underlying_price=_PRICE)

    assert _picked(picked) == _contract(_MON, "call", 585)


def test_a_put_is_picked_from_the_puts_and_a_call_from_the_calls() -> None:
    listing = _listing((_MON,))

    put = pick_contract(_leg(_SAME_DAY, right="put"), listing=listing, trading_date=_MON, underlying_price=_PRICE)

    assert _picked(put) == _contract(_MON, "put", 585)


def test_at_the_money_between_two_strikes_picks_the_lower_one() -> None:
    picked = pick_contract(
        _leg(_SAME_DAY), listing=_listing((_MON,)), trading_date=_MON, underlying_price=Decimal("587.50")
    )

    assert _picked(picked) == _contract(_MON, "call", 585)


def test_a_leg_a_step_above_the_money_is_the_next_listed_strike_up() -> None:
    picked = pick_contract(
        _leg(_THIS_WEEK, _steps(1)), listing=_listing((_MON, _FRI)), trading_date=_MON, underlying_price=_PRICE
    )

    assert _picked(picked) == _contract(_FRI, "call", 590)


def test_a_leg_a_step_below_the_money_is_the_next_listed_strike_down() -> None:
    picked = pick_contract(
        _leg(_SAME_DAY, _steps(-1), right="put"), listing=_listing((_MON,)), trading_date=_MON, underlying_price=_PRICE
    )

    assert _picked(picked) == _contract(_MON, "put", 580)


def test_a_friday_leg_for_this_week_is_that_fridays_contract() -> None:
    listing = _listing((_FRI, _NEXT_FRI), as_of=_FRI)

    picked = pick_contract(_leg(_THIS_WEEK), listing=listing, trading_date=_FRI, underlying_price=_PRICE)

    assert _picked(picked) == _contract(_FRI, "call", 585)


def test_a_week_with_a_holiday_friday_comes_to_the_thursday() -> None:
    listing = _listing((_HOLIDAY_WEEK_MON, _HOLIDAY_WEEK_THU, _FRI_AFTER_GOOD_FRIDAY), as_of=_HOLIDAY_WEEK_MON)

    picked = pick_contract(_leg(_THIS_WEEK), listing=listing, trading_date=_HOLIDAY_WEEK_MON, underlying_price=_PRICE)

    assert _picked(picked).expiry_date == _HOLIDAY_WEEK_THU


def test_at_least_seven_days_comes_to_the_nearest_listed_expiry_that_far_out() -> None:
    listing = _listing((_MON, _WED, _FRI, _NEXT_FRI, _LAST_FRI))

    picked = pick_contract(_leg(_min_days(7)), listing=listing, trading_date=_MON, underlying_price=_PRICE)

    assert _picked(picked).expiry_date == _NEXT_FRI


def test_an_expiry_listed_for_puts_only_is_not_a_call_expiry() -> None:
    listing = Listing(
        underlying="SPY",
        as_of=_MON,
        contracts=_listed((_MON,), rights=("put",)) + _listed((_FRI,), rights=("call",)),
        complete=True,
    )

    picked = pick_contract(_leg(_SAME_DAY), listing=listing, trading_date=_MON, underlying_price=_PRICE)

    assert _refusal(picked) is PickRefusal.EXPIRY_NOT_LISTED


def test_contracts_on_another_underlying_in_the_listing_are_not_candidates() -> None:
    nearer = ListedContract(_contract(_MON, "call", 586, root="QQQ"), multiplier=100, standard=True)
    listing = _listing((_MON,), extra=(nearer,))

    picked = pick_contract(_leg(_SAME_DAY), listing=listing, trading_date=_MON, underlying_price=_PRICE)

    assert _picked(picked) == _contract(_MON, "call", 585)


# ---- Each way a leg comes to no contract --------------------------------------


def test_an_incomplete_listing_never_yields_a_pick() -> None:
    listing = _listing((_MON, _FRI), complete=False)

    picked = pick_contract(_leg(_SAME_DAY), listing=listing, trading_date=_MON, underlying_price=_PRICE)

    assert _refusal(picked) is PickRefusal.CHAIN_INCOMPLETE


def test_an_incomplete_listing_is_refused_for_that_before_anything_else_is_asked() -> None:
    """With nothing listed at all, an incomplete chain is still what the owner is told."""
    listing = Listing(underlying="SPY", as_of=_MON, contracts=(), complete=False)

    picked = pick_contract(_leg(_SAME_DAY), listing=listing, trading_date=_MON, underlying_price=_PRICE)

    assert _refusal(picked) is PickRefusal.CHAIN_INCOMPLETE


def test_a_rule_that_meets_no_listed_expiry_is_refused_as_not_listed() -> None:
    listing = _listing((_WED, _FRI))

    picked = pick_contract(_leg(_SAME_DAY), listing=listing, trading_date=_MON, underlying_price=_PRICE)

    assert _refusal(picked) is PickRefusal.EXPIRY_NOT_LISTED


def test_an_at_least_rule_past_everything_listed_is_refused_as_not_listed() -> None:
    picked = pick_contract(
        _leg(_min_days(60)), listing=_listing((_MON, _FRI)), trading_date=_MON, underlying_price=_PRICE
    )

    assert _refusal(picked) is PickRefusal.EXPIRY_NOT_LISTED


def test_an_empty_complete_listing_is_refused_as_not_listed() -> None:
    listing = Listing(underlying="SPY", as_of=_MON, contracts=(), complete=True)

    picked = pick_contract(_leg(_THIS_WEEK), listing=listing, trading_date=_MON, underlying_price=_PRICE)

    assert _refusal(picked) is PickRefusal.EXPIRY_NOT_LISTED


def test_an_expiry_on_a_day_the_calendar_has_no_session_is_refused() -> None:
    listing = _listing((_HOLIDAY_WEEK_WED, _GOOD_FRIDAY), as_of=_HOLIDAY_WEEK_WED)

    picked = pick_contract(_leg(_min_days(2)), listing=listing, trading_date=_HOLIDAY_WEEK_WED, underlying_price=_PRICE)

    assert _refusal(picked) is PickRefusal.EXPIRY_NOT_A_SESSION


def test_steps_off_the_end_of_the_ladder_are_refused_as_a_strike_not_listed() -> None:
    listing = _listing((_MON,))

    above = pick_contract(_leg(_SAME_DAY, _steps(4)), listing=listing, trading_date=_MON, underlying_price=_PRICE)
    below = pick_contract(_leg(_SAME_DAY, _steps(-2)), listing=listing, trading_date=_MON, underlying_price=_PRICE)

    assert (_refusal(above), _refusal(below)) == (PickRefusal.STRIKE_NOT_LISTED, PickRefusal.STRIKE_NOT_LISTED)


def test_a_rule_that_reaches_a_date_listed_only_as_non_standard_contracts_is_refused_as_not_standard() -> None:
    listing = Listing(
        underlying="SPY", as_of=_MON, contracts=_listed((_MON,), standard=False) + _listed((_FRI,)), complete=True
    )

    picked = pick_contract(_leg(_SAME_DAY), listing=listing, trading_date=_MON, underlying_price=_PRICE)

    assert _refusal(picked) is PickRefusal.NOT_STANDARD


def test_a_contract_that_says_it_is_standard_but_is_not_a_hundred_shares_is_not_standard() -> None:
    listing = _listing((_MON,), multiplier=10)

    picked = pick_contract(_leg(_SAME_DAY), listing=listing, trading_date=_MON, underlying_price=_PRICE)

    assert _refusal(picked) is PickRefusal.NOT_STANDARD


def test_a_date_with_only_non_standard_contracts_is_passed_over_when_a_later_standard_date_meets_the_rule() -> None:
    """As the live resolver does: its expiries come from standard contracts alone."""
    listing = Listing(
        underlying="SPY", as_of=_MON, contracts=_listed((_MON,), standard=False) + _listed((_FRI,)), complete=True
    )

    picked = pick_contract(_leg(_min_days(0)), listing=listing, trading_date=_MON, underlying_price=_PRICE)

    assert _picked(picked).expiry_date == _FRI


def test_a_non_standard_contract_is_never_picked_even_when_it_is_nearest_the_money() -> None:
    nearest = ListedContract(_contract(_MON, "call", 586), multiplier=100, standard=False)
    listing = _listing((_MON,), extra=(nearest,))

    picked = pick_contract(_leg(_SAME_DAY), listing=listing, trading_date=_MON, underlying_price=_PRICE)

    assert _picked(picked) == _contract(_MON, "call", 585)


def test_steps_are_counted_along_the_standard_strikes_only() -> None:
    between = ListedContract(_contract(_MON, "call", 587), multiplier=100, standard=False)
    listing = _listing((_MON,), extra=(between,))

    picked = pick_contract(_leg(_SAME_DAY, _steps(1)), listing=listing, trading_date=_MON, underlying_price=_PRICE)

    assert _picked(picked) == _contract(_MON, "call", 590)


# ---- A caller's mistake raises ------------------------------------------------


def test_a_listing_for_another_date_than_the_one_asked_about_raises() -> None:
    listing = _listing((_MON,), as_of=_TUE)

    with pytest.raises(ValueError, match=str(_TUE)):
        pick_contract(_leg(_SAME_DAY), listing=listing, trading_date=_MON, underlying_price=_PRICE)


@pytest.mark.parametrize("price", ["0", "-586", "NaN"])
def test_picking_with_no_underlying_price_to_call_the_money_from_raises(price: str) -> None:
    with pytest.raises(ValueError, match="price"):
        pick_contract(_leg(_SAME_DAY), listing=_listing((_MON,)), trading_date=_MON, underlying_price=Decimal(price))
