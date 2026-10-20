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
    ExpiryRule,
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


# A plain October week, then the weeks after it. Friday the 10th is the week before, already gone.
_PREV_FRI = _session(2025, 10, 10, "Fri")
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


# Friday the 10th ended its week and is listed, but it is before Monday the 13th: nothing is left to choose.
@pytest.mark.parametrize(
    ("rule", "listed"),
    [
        pytest.param(SameDayExpiryRule(kind="same_day"), (_PREV_FRI,), id="same day: an expiry that has gone"),
        pytest.param(ThisWeekExpiryRule(kind="this_week"), (_PREV_FRI,), id="this week: an expiry that has gone"),
        pytest.param(
            MinDaysExpiryRule(kind="min_days", days=0), (_PREV_FRI,), id="at least no days: an expiry that has gone"
        ),
        pytest.param(SameDayExpiryRule(kind="same_day"), (), id="same day: nothing listed"),
        pytest.param(ThisWeekExpiryRule(kind="this_week"), (), id="this week: nothing listed"),
        pytest.param(MinDaysExpiryRule(kind="min_days", days=1), (), id="at least a day: nothing listed"),
    ],
)
def test_an_expiry_that_has_already_gone_or_is_not_listed_is_never_chosen(
    rule: ExpiryRule, listed: tuple[date, ...]
) -> None:
    assert expiry_for(rule, listed=listed, trading_date=_MON) is None


_FIVE_LISTED = (_MON, _WED, _FRI, _NEXT_FRI, _LAST_FRI)


@pytest.mark.parametrize(
    ("days", "listed", "expected"),
    [
        pytest.param(0, _FIVE_LISTED, _MON, id="no wait: today counts"),
        pytest.param(1, _FIVE_LISTED, _WED, id="a day or more: Tuesday is not listed, so Wednesday"),
        pytest.param(2, _FIVE_LISTED, _WED, id="two days is exactly Wednesday"),
        pytest.param(3, _FIVE_LISTED, _FRI, id="three days: Thursday is not listed, so Friday"),
        pytest.param(7, _FIVE_LISTED, _NEXT_FRI, id="a week: Monday the 20th is not listed, so Friday the 24th"),
        pytest.param(11, _FIVE_LISTED, _NEXT_FRI, id="eleven days is exactly Friday the 24th"),
        pytest.param(12, _FIVE_LISTED, _LAST_FRI, id="twelve days is past the 24th"),
        # Friday the 24th is eight sessions after Monday the 13th, and eleven days.
        pytest.param(
            11, (_NEXT_FRI,), _NEXT_FRI, id="calendar days, not sessions: eleven days reaches Friday the 24th"
        ),
        pytest.param(12, (_NEXT_FRI,), None, id="calendar days, not sessions: twelve days is past Friday the 24th"),
        pytest.param(3, (_NEXT_FRI, _FRI, _FRI, _MON), _FRI, id="blind to the order and repeats in what is listed"),
    ],
)
def test_at_least_n_days_is_the_nearest_listed_expiry_that_far_out(
    days: int, listed: tuple[date, ...], expected: date | None
) -> None:
    assert expiry_for(MinDaysExpiryRule(kind="min_days", days=days), listed=listed, trading_date=_MON) == expected


@pytest.mark.parametrize(
    ("listed", "trading_date", "expected"),
    [
        pytest.param((_PREV_FRI, _FRI), _MON, _FRI, id="passes over the week that has gone to the one that is here"),
        pytest.param(
            (_MON, _TUE, _WED, _FRI, _NEXT_FRI), _MON, _FRI, id="the weeks last session, skipping a mid-week expiry"
        ),
        pytest.param(
            (_TUE, _WED, _NEXT_MON, _NEXT_FRI),
            _MON,
            _NEXT_FRI,
            id="no session of the week listed: the next weeks Friday",
        ),
        pytest.param((_FRI, _NEXT_FRI), _FRI, _FRI, id="on the weeks last session it is that session when listed"),
        pytest.param(
            (_NEXT_MON, _NEXT_FRI),
            _FRI,
            _NEXT_FRI,
            id="on the weeks last session and not listed: the next weeks Friday",
        ),
        pytest.param(
            (_HOLIDAY_WEEK_MON, _HOLIDAY_WEEK_WED, _HOLIDAY_WEEK_THU, _FRI_AFTER_GOOD_FRIDAY),
            _HOLIDAY_WEEK_MON,
            _HOLIDAY_WEEK_THU,
            id="a week whose friday is a holiday is the thursday: from the monday",
        ),
        pytest.param(
            (_HOLIDAY_WEEK_MON, _HOLIDAY_WEEK_WED, _HOLIDAY_WEEK_THU, _FRI_AFTER_GOOD_FRIDAY),
            _HOLIDAY_WEEK_THU,
            _HOLIDAY_WEEK_THU,
            id="a week whose friday is a holiday is the thursday: from the thursday",
        ),
        pytest.param(
            (_GOOD_FRIDAY, _FRI_AFTER_GOOD_FRIDAY),
            _HOLIDAY_WEEK_WED,
            _FRI_AFTER_GOOD_FRIDAY,
            id="does not take a listed expiry on the holiday itself",
        ),
        # Sunday the 19th and Saturday the 18th are in Monday the 13th's week; neither is a session.
        pytest.param(
            (_SATURDAY, _NEXT_MON), _MON, None, id="a weeks last session is judged in its own week: nothing in this one"
        ),
        pytest.param(
            (_SATURDAY, _NEXT_FRI),
            _MON,
            _NEXT_FRI,
            id="a weeks last session is judged in its own week: the next Friday",
        ),
    ],
)
def test_this_week_is_the_last_session_of_the_week_the_trading_date_is_in(
    listed: tuple[date, ...], trading_date: date, expected: date | None
) -> None:
    assert expiry_for(ThisWeekExpiryRule(kind="this_week"), listed=listed, trading_date=trading_date) == expected


# ---- The strike a rule comes to ---------------------------------------------

_LADDER = (580_000, 585_000, 590_000, 595_000, 600_000)
_ATM = AtTheMoney(kind="atm")


_SHUFFLED_LADDER = (595_000, 580_000, 600_000, 585_000, 590_000, 585_000, 580_000)


@pytest.mark.parametrize(
    ("ladder", "price", "expected"),
    [
        pytest.param(_LADDER, "586.00", 585_000, id="nearer the lower strike"),
        pytest.param(_LADDER, "588.00", 590_000, id="nearer the higher strike"),
        pytest.param(_LADDER, "585.00", 585_000, id="exactly on a strike"),
        pytest.param(_LADDER, "587.50", 585_000, id="exactly between two: the lower"),
        pytest.param(_LADDER, "587.5001", 590_000, id="a hair past the middle: the higher"),
        pytest.param(_LADDER, "587.4999", 585_000, id="a hair short of the middle: the lower"),
        pytest.param(_LADDER, "500.00", 580_000, id="below the whole ladder: its first strike"),
        pytest.param(_LADDER, "900.00", 600_000, id="above the whole ladder: its last strike"),
        pytest.param(
            (585_000, 585_500, 586_000),
            "585.25",
            585_000,
            id="a tie is judged in thousandths: a fine ladder ties too, low",
        ),
        pytest.param(
            (585_000, 585_500, 586_000),
            "585.75",
            585_500,
            id="a tie is judged in thousandths: a fine ladder ties too, high",
        ),
        # A float reads 128.0285 times a thousand as 128028.50000000001, which is nearer the higher strike.
        pytest.param((128_028, 128_029), "128.0285", 128_028, id="a tie is exact where a float would tip it"),
        pytest.param(
            _SHUFFLED_LADDER, "586", 585_000, id="a ladder is a set of strikes whatever order and repeats it arrives in"
        ),
        pytest.param((), "586", None, id="no strikes listed is no strike"),
    ],
)
def test_at_the_money_is_the_listed_strike_nearest_the_price_and_the_lower_of_two(
    ladder: tuple[int, ...], price: str, expected: int | None
) -> None:
    assert strike_for(_ATM, ladder_thousandths=ladder, underlying_price=Decimal(price)) == expected


# The money at 586 is 585, the ladder's second strike.
@pytest.mark.parametrize(
    ("ladder", "price", "steps", "expected"),
    [
        pytest.param(_LADDER, "586.00", 1, 590_000, id="one up"),
        pytest.param(_LADDER, "586.00", 2, 595_000, id="two up"),
        pytest.param(_LADDER, "586.00", 3, 600_000, id="three up reaches the top"),
        pytest.param(_LADDER, "586.00", 4, None, id="four up is off the top"),
        pytest.param(_LADDER, "586.00", -1, 580_000, id="one down reaches the bottom"),
        pytest.param(_LADDER, "586.00", -2, None, id="two down is off the bottom"),
        pytest.param(_LADDER, "586.00", 50, None, id="far off the top"),
        pytest.param(
            (580_000, 585_000, 600_000, 650_000), "585", 1, 600_000, id="steps are listed strikes and not dollars: up"
        ),
        pytest.param(
            (580_000, 585_000, 600_000, 650_000),
            "601",
            -1,
            585_000,
            id="steps are listed strikes and not dollars: down",
        ),
        pytest.param(
            _SHUFFLED_LADDER,
            "586",
            2,
            595_000,
            id="a ladder is a set of strikes whatever order and repeats it arrives in",
        ),
        pytest.param((), "586", 1, None, id="no strikes listed is no strike"),
    ],
)
def test_steps_from_the_money_count_listed_strikes_from_the_at_the_money_one(
    ladder: tuple[int, ...], price: str, steps: int, expected: int | None
) -> None:
    rule = StepsFromTheMoney(kind="atm_offset", steps=steps)

    assert strike_for(rule, ladder_thousandths=ladder, underlying_price=Decimal(price)) == expected


# What a caller must never hand over as the underlying's price. An int or a float is as much a mistake as
# a zero: a float's rounding would decide a pick, so neither is quietly turned into a Decimal.
_NOT_A_PRICE = [
    pytest.param(Decimal("0"), id="zero"),
    pytest.param(Decimal("-1"), id="negative"),
    pytest.param(Decimal("NaN"), id="a Decimal NaN"),
    pytest.param(Decimal("Infinity"), id="infinite"),
    pytest.param(586, id="an int"),
    pytest.param(586.0, id="a float"),
    pytest.param(float("nan"), id="a float NaN"),
]


@pytest.mark.parametrize("price", _NOT_A_PRICE)
def test_a_price_that_is_not_a_price_is_a_callers_mistake(price: Any) -> None:
    with pytest.raises(ValueError, match="price"):
        strike_for(_ATM, ladder_thousandths=_LADDER, underlying_price=price)


# ---- Picking a contract ------------------------------------------------------


@pytest.mark.parametrize(
    ("expiry", "strike", "right", "expiries", "price", "expected"),
    [
        pytest.param(
            _SAME_DAY, None, "call", (_MON, _WED, _FRI), _PRICE, (_MON, "call", 585), id="a same day call at the money"
        ),
        pytest.param(_SAME_DAY, None, "put", (_MON,), _PRICE, (_MON, "put", 585), id="a put from the puts"),
        pytest.param(
            _SAME_DAY,
            None,
            "call",
            (_MON,),
            Decimal("587.50"),
            (_MON, "call", 585),
            id="between two strikes: the lower",
        ),
        pytest.param(
            _THIS_WEEK,
            _steps(1),
            "call",
            (_MON, _FRI),
            _PRICE,
            (_FRI, "call", 590),
            id="a step above the money: the next strike up",
        ),
        pytest.param(
            _SAME_DAY,
            _steps(-1),
            "put",
            (_MON,),
            _PRICE,
            (_MON, "put", 580),
            id="a step below the money: the next strike down",
        ),
    ],
)
def test_a_leg_comes_to_the_contract_its_right_expiry_and_strike_rules_name(
    expiry: dict[str, Any],
    strike: dict[str, Any] | None,
    right: OptionRight,
    expiries: tuple[date, ...],
    price: Decimal,
    expected: tuple[date, OptionRight, int],
) -> None:
    picked = pick_contract(
        _leg(expiry, strike, right=right), listing=_listing(expiries), trading_date=_MON, underlying_price=price
    )

    assert _picked(picked) == _contract(*expected)


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


# ---- Each way a leg comes to no contract --------------------------------------


@pytest.mark.parametrize(
    ("expiry", "expiries", "complete", "refusal"),
    [
        pytest.param(
            _SAME_DAY, (_MON, _FRI), False, PickRefusal.CHAIN_INCOMPLETE, id="an incomplete listing never yields a pick"
        ),
        pytest.param(
            _SAME_DAY,
            (_WED, _FRI),
            True,
            PickRefusal.EXPIRY_NOT_LISTED,
            id="a rule that meets no listed expiry is refused as not listed",
        ),
        pytest.param(
            _min_days(60),
            (_MON, _FRI),
            True,
            PickRefusal.EXPIRY_NOT_LISTED,
            id="an at least rule past everything listed is refused as not listed",
        ),
        pytest.param(
            _THIS_WEEK, (), True, PickRefusal.EXPIRY_NOT_LISTED, id="an empty complete listing is refused as not listed"
        ),
    ],
)
def test_a_leg_whose_expiry_cannot_be_settled_is_refused_for_the_listing_or_for_the_rule(
    expiry: dict[str, Any], expiries: tuple[date, ...], complete: bool, refusal: PickRefusal
) -> None:
    listing = _listing(expiries, complete=complete)

    picked = pick_contract(_leg(expiry), listing=listing, trading_date=_MON, underlying_price=_PRICE)

    assert _refusal(picked) is refusal


def test_an_incomplete_listing_is_refused_for_that_before_anything_else_is_asked() -> None:
    """With nothing listed at all, an incomplete chain is still what the owner is told."""
    listing = Listing(underlying="SPY", as_of=_MON, contracts=(), complete=False)

    picked = pick_contract(_leg(_SAME_DAY), listing=listing, trading_date=_MON, underlying_price=_PRICE)

    assert _refusal(picked) is PickRefusal.CHAIN_INCOMPLETE


@pytest.mark.parametrize(
    ("leg", "expiries", "right", "says", "does_not_say"),
    [
        pytest.param(_SAME_DAY, (_WED, _FRI), "call", ("calls", str(_MON)), ("week", "days"), id="same day"),
        pytest.param(_THIS_WEEK, (_TUE, _WED), "put", ("puts", str(_MON), "week"), ("days",), id="this week"),
        pytest.param(
            _min_days(60), (_MON, _FRI), "call", ("calls", str(_MON), "60", "days"), ("week",), id="at least 60 days"
        ),
    ],
)
def test_a_refusal_for_no_listed_expiry_says_the_right_the_date_and_the_rule(
    leg: dict[str, Any], expiries: tuple[date, ...], right: str, says: tuple[str, ...], does_not_say: tuple[str, ...]
) -> None:
    picked = pick_contract(
        _leg(leg, right=right), listing=_listing(expiries), trading_date=_MON, underlying_price=_PRICE
    )

    assert _refusal(picked) is PickRefusal.EXPIRY_NOT_LISTED
    assert isinstance(picked, PickRefused)
    for word in says:
        assert word in picked.detail
    for word in does_not_say:
        assert word not in picked.detail


def test_an_expiry_on_a_day_the_calendar_has_no_session_is_refused() -> None:
    listing = _listing((_HOLIDAY_WEEK_WED, _GOOD_FRIDAY), as_of=_HOLIDAY_WEEK_WED)

    picked = pick_contract(_leg(_min_days(2)), listing=listing, trading_date=_HOLIDAY_WEEK_WED, underlying_price=_PRICE)

    assert _refusal(picked) is PickRefusal.EXPIRY_NOT_A_SESSION


def test_steps_off_the_end_of_the_ladder_are_refused_as_a_strike_not_listed() -> None:
    listing = _listing((_MON,))

    above = pick_contract(_leg(_SAME_DAY, _steps(4)), listing=listing, trading_date=_MON, underlying_price=_PRICE)
    below = pick_contract(_leg(_SAME_DAY, _steps(-2)), listing=listing, trading_date=_MON, underlying_price=_PRICE)

    assert (_refusal(above), _refusal(below)) == (PickRefusal.STRIKE_NOT_LISTED, PickRefusal.STRIKE_NOT_LISTED)


@pytest.mark.parametrize(
    ("standard", "multiplier", "later_expiries"),
    [
        pytest.param(False, 100, (_FRI,), id="a rule that reaches a date listed only as non-standard contracts"),
        pytest.param(True, 10, (), id="a contract that says it is standard but is not a hundred shares"),
    ],
)
def test_a_rule_that_reaches_only_non_standard_contracts_is_refused_as_not_standard(
    standard: bool, multiplier: int, later_expiries: tuple[date, ...]
) -> None:
    listing = _listing((_MON,), standard=standard, multiplier=multiplier, extra=_listed(later_expiries))

    picked = pick_contract(_leg(_SAME_DAY), listing=listing, trading_date=_MON, underlying_price=_PRICE)

    assert _refusal(picked) is PickRefusal.NOT_STANDARD


def test_a_date_with_only_non_standard_contracts_is_passed_over_when_a_later_standard_date_meets_the_rule() -> None:
    """As the live resolver does: its expiries come from standard contracts alone."""
    listing = Listing(
        underlying="SPY", as_of=_MON, contracts=_listed((_MON,), standard=False) + _listed((_FRI,)), complete=True
    )

    picked = pick_contract(_leg(_min_days(0)), listing=listing, trading_date=_MON, underlying_price=_PRICE)

    assert _picked(picked).expiry_date == _FRI


@pytest.mark.parametrize(
    ("root", "strike", "standard", "leg_strike", "expected"),
    [
        pytest.param("QQQ", 586, True, None, 585, id="contracts on another underlying are not candidates"),
        pytest.param(
            "SPY", 586, False, None, 585, id="a non-standard contract is never picked even when nearest the money"
        ),
        pytest.param("SPY", 587, False, _steps(1), 590, id="steps are counted along the standard strikes only"),
    ],
)
def test_a_listed_contract_that_is_not_a_candidate_changes_neither_the_pick_nor_the_steps(
    root: str, strike: int, standard: bool, leg_strike: dict[str, Any] | None, expected: int
) -> None:
    extra = ListedContract(_contract(_MON, "call", strike, root=root), multiplier=100, standard=standard)
    listing = _listing((_MON,), extra=(extra,))

    picked = pick_contract(_leg(_SAME_DAY, leg_strike), listing=listing, trading_date=_MON, underlying_price=_PRICE)

    assert _picked(picked) == _contract(_MON, "call", expected)


# ---- A caller's mistake raises ------------------------------------------------


def test_a_listing_for_another_date_than_the_one_asked_about_raises() -> None:
    listing = _listing((_MON,), as_of=_TUE)

    with pytest.raises(ValueError, match=str(_TUE)):
        pick_contract(_leg(_SAME_DAY), listing=listing, trading_date=_MON, underlying_price=_PRICE)


@pytest.mark.parametrize("price", _NOT_A_PRICE)
def test_picking_with_no_underlying_price_to_call_the_money_from_raises(price: Any) -> None:
    with pytest.raises(ValueError, match="price"):
        pick_contract(_leg(_SAME_DAY), listing=_listing((_MON,)), trading_date=_MON, underlying_price=price)
